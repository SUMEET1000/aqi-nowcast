"""Telegram Bot API client. Stdlib only, and it mirrors cpcb_api.py on purpose.

Stdlib rather than python-telegram-bot because this file makes exactly one API
call — sendMessage — and the dependency closure stays at psycopg[binary]
(build plan §0.6). The bot's conversation side is not here at all; it is a
Cloudflare Worker in bot/, which is what makes an instant reply possible
without a Python process that never sleeps.

The retry clause is broad for the reason cpcb_api.fetch's is: a dropped TLS
handshake raises ssl.SSLError, ConnectionResetError or RemoteDisconnected, and
none of those is a urllib.error.URLError. That miss cost a whole run on
2026-08-10 and left no trace in the database.
"""

import http.client
import json
import sys
import time
import urllib.error
import urllib.request

import env  # noqa: F401  (imported for the UTF-8 console side effect)
from env import require_env

BASE = "https://api.telegram.org"

# GitHub's runners reach Telegram fine, but the same transient-network argument
# as data.gov.in applies, and a message not sent today cannot be sent tomorrow —
# it would be yesterday's air. Fewer attempts than cpcb_api's 5 because
# Telegram's API is not measurably flaky the way data.gov.in is.
ATTEMPTS = 3

TIMEOUT_S = 30


class TelegramError(Exception):
    """Carries the HTTP status and Telegram's own description.

    Both matter and they say different things: 401 means the token is wrong,
    403 means this particular user blocked the bot (a normal event that must
    not fail the whole run), 400 with "chat not found" means they deleted the
    chat. send_alerts.py branches on exactly those.
    """

    def __init__(self, message: str, http_status: int | None,
                 description: str | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.description = description


def load_token() -> str:
    return require_env(
        "TELEGRAM_BOT_TOKEN",
        "Create a bot with @BotFather on Telegram; it hands you the token. "
        "The same value goes into the GitHub Actions secrets and into the bot "
        "Worker as an encrypted Cloudflare secret.",
    )


def redact(text: str, token: str) -> str:
    """Remove the bot token from anything about to be printed or stored.

    The token is in the URL path, not in a header, so every urllib error
    message that quotes the URL quotes the token — and this output goes to a
    public Actions log and into fetch_log.error_detail. db.redact does not
    cover this shape because no other credential in this project travels in a
    path segment.

    A token grants full control of the bot: reading every subscriber's messages
    and sending as us. It is the one credential here that is health-adjacent.
    """
    return text.replace(token, "***") if token else text


def call(token: str, method: str, payload: dict) -> dict:
    """One Bot API call. Raises on every failure — never a partial result (§0.5).

    Returns Telegram's `result` object.
    """
    url = f"{BASE}/bot{token}/{method}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "aqi-nowcast/0.1 (portfolio project; contact via repo)"},
    )

    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                parsed = json.load(resp)
            break

        # HTTPError is an OSError subclass, so it has to be caught before the
        # broad clause below or every 401 burns three attempts and reports
        # "unreachable" instead of "token rejected" — the same defect cpcb_api
        # carried until 2026-08-10.
        except urllib.error.HTTPError as e:
            # Telegram answers 4xx with a JSON body that says what is actually
            # wrong. Losing that and reporting a bare status is what makes
            # "403" indistinguishable between a dead token and one user having
            # blocked the bot.
            description = None
            retry_after = None
            try:
                detail = json.loads(e.read().decode("utf-8", "replace"))
                description = detail.get("description")
                retry_after = (detail.get("parameters") or {}).get("retry_after")
            except Exception:
                # A non-JSON error body is Telegram's gateway, not Telegram.
                # The status still carries the diagnosis, so this is not a
                # swallowed failure — the raise below is unconditional.
                pass

            if e.code == 429 and retry_after and attempt < ATTEMPTS:
                # Telegram says exactly how long to wait. Honouring it is the
                # difference between one delayed message and a run that trips
                # the limit repeatedly and loses the rest of the subscribers.
                print(f"  telegram rate limit, waiting {retry_after}s "
                      f"(attempt {attempt}/{ATTEMPTS})", file=sys.stderr)
                time.sleep(min(retry_after, 60))
                continue

            if e.code < 500 and e.code != 429:
                raise TelegramError(
                    f"HTTP {e.code} from Telegram {method}: {description or e.reason}",
                    e.code, description)

            print(f"  telegram HTTP {e.code}, attempt {attempt}/{ATTEMPTS}",
                  file=sys.stderr)
            if attempt == ATTEMPTS:
                raise TelegramError(
                    f"Telegram returned HTTP {e.code} on all {ATTEMPTS} attempts: "
                    f"{description or e.reason}", e.code, description)
            time.sleep(2 ** attempt)

        # ValueError covers json.load on a CDN maintenance page served under a
        # 200 — a transient, so retried rather than fatal.
        except (OSError, http.client.HTTPException, ValueError) as e:
            print(f"  telegram transient failure ({type(e).__name__}), "
                  f"attempt {attempt}/{ATTEMPTS}", file=sys.stderr)
            if attempt == ATTEMPTS:
                raise TelegramError(
                    f"Telegram unreachable after {ATTEMPTS} attempts: "
                    f"{type(e).__name__}: {redact(str(e), token)}", None)
            time.sleep(2 ** attempt)

    if not isinstance(parsed, dict) or not parsed.get("ok"):
        raise TelegramError(
            f"Telegram {method} answered 200 but not ok: {parsed!r}", 200,
            parsed.get("description") if isinstance(parsed, dict) else None)

    return parsed.get("result", {})


def escape(text: str) -> str:
    """HTML-escape a value going into a formatted message.

    Station names are third-party strings from CPCB and two of them already
    carry whitespace damage; an unescaped '<' would make Telegram reject the
    whole message with "can't parse entities", which is a 400 that looks like a
    code bug and fires only for the one station whose name changed.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_message(token: str, chat_id: int, text: str,
                 reply_markup: dict | None = None) -> int:
    """sendMessage, returning the message_id.

    parse_mode=HTML rather than Markdown: Telegram's legacy Markdown breaks on
    an unpaired '*' or '_' anywhere in the text, and these messages interpolate
    station names we do not control.

    disable_web_page_preview because the message cites CPCB documents; without
    it Telegram renders a preview card that pushes the number off the screen.
    """
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    result = call(token, "sendMessage", payload)
    return result.get("message_id", 0)

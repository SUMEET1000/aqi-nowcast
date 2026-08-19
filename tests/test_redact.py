"""Every credential in this project, against the redactor that guards the logs.

    python tests/test_redact.py

Imports db but never connects — redact() is pure, so this costs no Neon wake.

Three secrets can reach a public place: the Neon password, the data.gov.in
api-key, and the Telegram bot token. They get there through the same door.
scripts/ingest.py and scripts/send_alerts.py both end in a handler that catches
BaseException and writes the exception text to a GitHub Actions annotation, the
job summary, and fetch_log.error_detail. Actions logs on a public repo are
public.

Both handlers redact with db.redact rather than telegram_api.redact, because a
last-resort handler must not call load_token() and risk failing itself. So
db.redact is the only thing standing between an unanticipated exception and a
published credential, and it has to cover all three shapes — including the bot
token, which travels in a url path segment where the password and api-key
patterns cannot see it.

The negative cases matter as much as the positive ones. A redactor that eats
the host, the method name or the chat id leaves a log nobody can debug from,
and the next person deletes it.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from db import redact  # noqa: E402

# Shaped like the real thing, valued like nothing. A bot token is
# '<numeric id>:<~35 chars>'; a Neon url carries the password before the '@'.
NEON_PW = "npg_S3cretPasswordValue"
NEON_URL = (f"postgresql://neondb_owner:{NEON_PW}@ep-cool-lab-12345678."
            f"us-east-2.aws.neon.tech/neondb?sslmode=verify-full")
API_KEY = "579b464db66ec23bdd000001cdd3946e44ce4aad7209ff7b23ac571b"
# Assembled from parts on purpose. A whole token-shaped literal here trips
# GitHub's secret scanner and opens a "Telegram Bot Token / public leak" alert
# on a value that was never a token — which is a real cost: the next genuine
# alert arrives in a channel already known to cry wolf. Alert #1 on this repo,
# 2026-08-19, was exactly this. The runtime value is unchanged, so the regexes
# are still tested against the full shape.
BOT_TOKEN = "8123456789" + ":" + "AAHdqTcvCH1vGWJx" + "fSeofSAs0K5PALDsaw"

failures = 0


def gone(label: str, text: str, secret: str) -> None:
    """The secret must not survive redaction, in any form."""
    global failures
    out = redact(text)
    if secret in out:
        failures += 1
        print(f"  FAIL  {label}\n        secret survived in {out!r}")
    else:
        print(f"  ok    {label}")


def kept(label: str, text: str, fragment: str) -> None:
    """The diagnosis must survive it."""
    global failures
    out = redact(text)
    if fragment in out:
        print(f"  ok    {label}")
    else:
        failures += 1
        print(f"  FAIL  {label}\n        lost {fragment!r} from {out!r}")


print("Secrets must not survive:")
gone("neon password in a connection url", f"OperationalError: {NEON_URL}", NEON_PW)
gone("neon password as password=", f"could not connect password={NEON_PW} host=x", NEON_PW)
gone("data.gov.in key in a query string",
     f"URLError for https://api.data.gov.in/resource/3b01?api-key={API_KEY}&format=json",
     API_KEY)
# The live one. send_alerts.run() builds exactly this shape for anything the
# per-subscriber TelegramError handler did not catch.
gone("bot token in a url path",
     f"ValueError: unknown url type: 'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'",
     BOT_TOKEN)
gone("bot token on its own", f"TelegramError: token={BOT_TOKEN} rejected", BOT_TOKEN)

print("\nThe diagnosis must survive:")
kept("neon host", f"OperationalError: {NEON_URL}", "ep-cool-lab-12345678")
kept("data.gov.in resource id",
     f"URLError for https://api.data.gov.in/resource/3b01?api-key={API_KEY}", "resource/3b01")
kept("telegram method name",
     f"failed calling https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", "sendMessage")
# A chat id is 6+ digits and sits next to a colon in ordinary log lines. If the
# bare-token pattern is written loosely enough to eat one, every send failure
# becomes unattributable.
kept("chat id beside a colon", "send failed for chat 1234567890: HTTP 403", "1234567890")
kept("an ordinary message untouched", "OperationalError: connection timed out",
     "connection timed out")

print(f"\n{'FAILED' if failures else 'PASSED'} — {failures} failure(s)")
raise SystemExit(1 if failures else 0)

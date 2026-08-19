"""Postgres connection handling. The only module that touches DATABASE_URL.

Two production hazards live here, both found by design rather than by outage,
and both invisible if you only read the happy path.

The first is a secret leak: psycopg puts the connection string into the text of
some connection errors, and Actions logs on a public repo are public, so an
unhandled connect failure would publish the Neon password to the internet. Every
connect is wrapped so the raised message names the host only.

The second is Neon's cold start. The free tier suspends compute after ~5 minutes
idle and the ingester runs every 30, so the database is always asleep when we
knock and the wake occasionally exceeds the connect timeout. Without a retry
that shows up as random red CI runs that look like bugs.

The retry is printed, never silent (§0.5).
"""

import re
import sys
import time

import psycopg

# env, not cpcb_api: this module has no business importing the HTTP client just
# to read a credential, and init_db.py would inherit that dependency for free.
from env import require_env

CONNECT_ATTEMPTS = 3
CONNECT_TIMEOUT_S = 15


def redact(text: str) -> str:
    """Strip anything password-shaped out of a message before it is printed.

    Defence in depth: the caller already avoids echoing the URL, but psycopg
    composes its own error text and we do not control what it puts there.

    Public because scripts/ingest.py's crash handler stores exception text in
    fetch_log.error_detail, and an arbitrary exception may carry the connection
    string. One implementation, not two copies of the same regexes.
    """
    text = re.sub(r"://[^:/@\s]+:[^@\s]+@", "://***:***@", text)
    text = re.sub(r"password=\S+", "password=***", text, flags=re.IGNORECASE)
    # The data.gov.in key travels in the query string (cpcb_api.fetch builds
    # BASE?api-key=...), and this output goes two places that outlive the run:
    # fetch_log.error_detail, and stderr on a public Actions log. Latent rather
    # than live — no stdlib exception embeds the URL today — but it goes live
    # the moment anyone swaps urllib for requests, whose ConnectionError
    # messages carry the full URL with its query string.
    text = re.sub(r"(api[-_]?key=)[^&\s\"']+", r"\1***", text, flags=re.IGNORECASE)
    # The Telegram bot token sits in the url path (/bot<id>:<secret>/method),
    # not in a query string, so neither pattern above reaches it. Live rather
    # than latent: send_alerts.run() redacts with this function, because a
    # last-resort handler must not call load_token() and risk failing itself,
    # and its output goes to a public Actions annotation, the job summary and
    # fetch_log.error_detail. telegram_api.redact still does the exact-value
    # strip wherever the token is already in hand — this is the backstop for
    # everything that escapes to run(). A leaked token reads every subscriber's
    # messages and sends as us.
    text = re.sub(r"/bot\d+:[A-Za-z0-9_-]+", "/bot***", text)
    # The same secret outside a url, from anything that formats the token into
    # its own message. Latent, but the shape is specific enough to strip on
    # sight, and a false positive costs a confusing log line while a miss costs
    # the bot.
    text = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b", "***", text)
    return text


def _host_of(url: str) -> str:
    """The host, for error messages. Never the credentials."""
    m = re.search(r"@([^/:?\s]+)", url)
    return m.group(1) if m else "the configured host"


def database_url() -> str:
    return require_env(
        "DATABASE_URL",
        "This is the Neon connection string (Dashboard -> Connect).\n"
        "It must end in ?sslmode=require — Neon rejects plaintext connections.",
    )


# sslmode values that give no server authentication, in increasing order of how
# much they pretend to. disable/allow/prefer may not even encrypt, and have no
# legitimate use here. `require` encrypts and verifies nothing: anyone who can
# answer for the Neon hostname — DNS hijack, BGP hijack, a compromised resolver
# on the runner's path — can present a self-signed certificate, and libpq hands
# them the username, the password and every row with no error and no log line.
#
# Worth naming explicitly because the repo documented `require` in three places
# as if it were the secure setting, and the guard here only tested that the
# substring "sslmode=" was present, so `?sslmode=disable` passed without a word.
UNVERIFIED_SSLMODES = ("disable", "allow", "prefer")
VERIFYING_SSLMODES = ("verify-ca", "verify-full")


def _sslmode_of(url: str) -> str | None:
    m = re.search(r"[?&]sslmode=([^&\s]+)", url)
    return m.group(1).lower() if m else None


def check_sslmode(url: str) -> None:
    """Refuse a connection that cannot be encrypted; warn about one that is
    encrypted but unauthenticated.

    Not yet a hard failure on `require`, deliberately. Flipping that switch
    before DATABASE_URL is updated in both .env and the GitHub Actions secret
    fails the very next scheduled run, and every bulletin missed before someone
    notices is unrecoverable — the one cost this project treats as unacceptable.
    See README: migrate, verify one run, then raise this to a hard failure.
    """
    mode = _sslmode_of(url)
    if mode is None:
        print("  warning: DATABASE_URL has no sslmode — expected "
              "?sslmode=verify-full&sslrootcert=<CA bundle>", file=sys.stderr)
    elif mode in UNVERIFIED_SSLMODES:
        sys.exit(f"DATABASE_URL has sslmode={mode}, which permits an "
                 f"unencrypted connection. Neon requires TLS and so do we. "
                 f"Use ?sslmode=verify-full&sslrootcert=<CA bundle>")
    elif mode not in VERIFYING_SSLMODES:
        print(f"  warning: sslmode={mode} ENCRYPTS BUT DOES NOT VERIFY the "
              f"server certificate — an attacker who can answer for this "
              f"hostname sees the password and every row. Migrate to "
              f"?sslmode=verify-full&sslrootcert=<CA bundle>", file=sys.stderr)


def connect(url: str | None = None) -> psycopg.Connection:
    """Connect with retry. Raises ConnectionError with a redacted message."""
    url = url or database_url()
    host = _host_of(url)
    check_sslmode(url)

    last = None
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            return psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT_S)
        except Exception as e:
            # Bare Exception on purpose: psycopg raises several unrelated types
            # here (OperationalError, socket errors, DNS errors) and any of
            # them may carry the connection string in its text.
            last = redact(f"{type(e).__name__}: {e}")
            print(f"  db connect failed ({last}), attempt {attempt}/{CONNECT_ATTEMPTS} "
                  f"— Neon may be waking from suspend", file=sys.stderr)
            if attempt < CONNECT_ATTEMPTS:
                time.sleep(2 ** attempt)

    raise ConnectionError(
        f"Could not reach Postgres at {host} after {CONNECT_ATTEMPTS} attempts. "
        f"Last error: {last}"
    )

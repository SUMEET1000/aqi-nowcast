"""Postgres connection handling. The only module that touches DATABASE_URL.

Two production hazards live here, both discovered by design rather than by
outage, and both invisible if you only read the happy path:

1. SECRET LEAK. psycopg puts the connection string into the text of some
   connection errors. GitHub Actions logs on a public repo are public. An
   unhandled connect failure would therefore publish the Neon password to the
   internet. Every connect is wrapped so the raised message names the host
   only.

2. NEON COLD START. Neon's free tier suspends the compute after ~5 minutes
   idle. The ingester runs every 30 minutes, so the database is ALWAYS asleep
   when we knock, and the wake occasionally exceeds the connect timeout.
   Without a retry that shows up as random red CI runs that look like bugs.

The retry is printed, never silent (§0.5).
"""

import re
import sys
import time

import psycopg

from cpcb_api import require_env

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


def connect(url: str | None = None) -> psycopg.Connection:
    """Connect with retry. Raises ConnectionError with a redacted message."""
    url = url or database_url()
    host = _host_of(url)

    if "sslmode=" not in url:
        # Not fatal — Neon negotiates TLS anyway — but a URL without it usually
        # means it was copied from the wrong box in the dashboard.
        print(f"  warning: DATABASE_URL has no sslmode; expected ?sslmode=require",
              file=sys.stderr)

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

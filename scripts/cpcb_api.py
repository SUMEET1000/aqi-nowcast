"""Shared CPCB / data.gov.in access. Stdlib only.

Extracted from the Phase 0 probes so the Phase 1 ingester does not import from
a module whose docstring says "throwaway". Both probes and scripts/ingest.py
now import from here.

Everything in this file was learned empirically in Phase 0 and is load-bearing.
Read the comments before changing anything — several of these lines look
optional and are not.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

RESOURCE = "3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69"
BASE = f"https://api.data.gov.in/resource/{RESOURCE}"

# data.gov.in silently HANGS on requests with urllib's default User-Agent —
# no response, no error, just a read timeout after 45s+. The identical request
# with any ordinary UA returns in ~0.4s. Measured 2026-08-09. Do not remove:
# in the hourly cron this presents as a random ingestion failure.
HEADERS = {"User-Agent": "aqi-nowcast/0.1 (portfolio project; contact via repo)"}

# The API stamps every row with the same bulletin time in IST and carries no
# timezone marker. See docs/stations.md "Correction 2".
IST = timezone(timedelta(hours=5, minutes=30))

NULL_SENTINEL = "NA"  # see docs/stations.md "Correction 3"

ATTEMPTS = 5  # see the comment in fetch() — this number is load-bearing


def load_env(name: str) -> str | None:
    """Read a value from the environment, falling back to .env. Never printed.

    Splits on the FIRST '=' only, so values containing '=' survive intact —
    a Postgres URL ends in '?sslmode=require' and would otherwise be truncated.
    """
    value = os.environ.get(name)
    if value:
        return value

    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.exists(env_path):
        return None

    prefix = f"{name}="
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith(prefix):
                return line.split("=", 1)[1].strip().strip("\"'") or None
    return None


def require_env(name: str, hint: str) -> str:
    """load_env, but a missing value is a hard stop rather than a default (§0.5)."""
    value = load_env(name)
    if not value:
        sys.exit(f"{name} is not set. Put it in .env (see .env.example).\n{hint}")
    return value


def load_key() -> str:
    return require_env(
        "DATA_GOV_IN_API_KEY",
        "Get a personal key from data.gov.in -> My Account -> API Key. "
        "The demo key from blog tutorials is rate-limited across everyone who copied it.",
    )


def fetch(state: str, key: str, limit: int = 1000) -> list[dict]:
    """One call for a whole state. Raises or exits on every failure — never
    returns a partial or defaulted result (§0.5)."""
    params = {
        "api-key": key,
        "format": "json",  # without this the API returns XML
        "limit": str(limit),  # defaults to 10; 500+ rows exist nationally
        "filters[state]": state,
    }
    url = f"{BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=HEADERS)

    # data.gov.in intermittently drops the TLS handshake. Retried, not
    # swallowed (§0.5): each attempt is logged, and exhausting them is a hard
    # failure. In Phase 1 this is what fetch_log.outcome='http_error' counts.
    #
    # ATTEMPTS IS 5, NOT 3, AND THAT NUMBER IS LOAD-BEARING. Measured failure
    # rate is roughly 1 call in 3 (Phase 0 saw ~1 in 4; re-measured 2026-08-09
    # during a partial outage at 1 in 3). At 3 attempts a whole run fails
    # 0.33^3 = 3.6% of the time, which puts the run success rate near 96% —
    # against a Gate 1 threshold of 95%. The gate would then be decided by the
    # weather at data.gov.in rather than by whether our pipeline works.
    # 5 attempts puts it at 0.33^5 = 0.4%, i.e. ~99.6%.
    payload = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status != 200:
                    raise FetchError(f"HTTP {resp.status} from data.gov.in", resp.status)
                payload = json.load(resp)
            break
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"  data.gov.in transient failure ({type(e).__name__}), "
                  f"attempt {attempt}/{ATTEMPTS}", file=sys.stderr)
            if attempt == ATTEMPTS:
                raise FetchError(
                    f"data.gov.in unreachable after {ATTEMPTS} attempts: {e}", None
                )
            time.sleep(2 ** attempt)  # 2,4,8,16s — 30s worst case

    if payload.get("status") != "ok":
        raise FetchError(
            f"API returned status={payload.get('status')!r}: {payload.get('message')!r}",
            None,
        )

    records = payload.get("records", [])
    # No silent fallbacks (§0.5): an empty result is a hard stop, not a warning.
    if not records:
        raise FetchError(f"Zero records for state={state!r}.", None)
    if len(records) >= limit:
        raise FetchError(f"Hit the limit ({limit}) — results are being truncated.", None)
    return records


class FetchError(Exception):
    """Raised instead of sys.exit so the ingester can write a fetch_log row
    before it dies. The probes catch it and exit; ingest.py logs it first."""

    def __init__(self, message: str, http_status: int | None):
        super().__init__(message)
        self.http_status = http_status


def parse_value(raw: str | None) -> float | None:
    """Map the 'NA' sentinel to None. Never coerce it to 0.

    float() raises on 'NA' and pandas reads it as the string "NA", so this has
    to happen at parse time, before the value reaches SQL or a DataFrame.
    """
    if raw is None:
        return None
    raw = raw.strip()
    if raw == NULL_SENTINEL or raw == "":
        return None
    return float(raw)


def parse_bulletin_ts(raw: str) -> datetime:
    """last_update is 'DD-MM-YYYY HH:MM:SS' in IST, with no timezone marker."""
    return datetime.strptime(raw, "%d-%m-%Y %H:%M:%S").replace(tzinfo=IST)

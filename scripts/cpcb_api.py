"""Shared CPCB / data.gov.in access. Stdlib only.

Extracted from the Phase 0 probes so the Phase 1 ingester does not import from
a module whose docstring says "throwaway". Both the probes and scripts/ingest.py
import from here.

The constants below were measured in Phase 0, not chosen. Several of them look
optional and are not.
"""

import http.client
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# The bare module import is not redundant with the `from env import` below:
# importing env is what applies the UTF-8 console fix, and probe_cpcb.py reaches
# that fix only through this module. Drop this line and '§' prints as '?' in
# that one probe's output — a nasty thing to bisect.
import env  # noqa: F401  (imported for the UTF-8 console side effect)
from env import require_env

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

ATTEMPTS = 5  # not 3; see the arithmetic in fetch()


def load_key() -> str:
    return require_env(
        "DATA_GOV_IN_API_KEY",
        "Get a personal key from data.gov.in -> My Account -> API Key. "
        "The demo key from blog tutorials is rate-limited across everyone who copied it.",
    )


def fetch(state: str, key: str, limit: int = 1000) -> tuple[list[dict], int]:
    """One call for a whole state. Raises on every failure — never returns a
    partial or defaulted result (§0.5).

    Returns (records, http_status). The status is returned rather than assumed:
    ingest.py used to write a literal 200 into fetch_log for every successful
    run, and a stored constant is not evidence of anything.
    """
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
    # 5 attempts, not 3, and the number is load-bearing. Measured failure rate
    # is ~1 call in 3 (Phase 0 saw 1 in 4; re-measured 2026-08-09 during a
    # partial outage at 1 in 3). At 3 attempts a whole run fails 0.33^3 = 3.6%
    # of the time — a ~96% run success rate against a Gate 1 threshold of 95%,
    # which leaves the gate decided by data.gov.in's weather rather than by
    # whether our pipeline works. 5 attempts is 0.33^5 = 0.4%, i.e. ~99.6%.
    #
    # The except clause has to stay broad. It was
    # `(urllib.error.URLError, TimeoutError)` until 2026-08-10, which does not
    # cover a dropped handshake: ssl.SSLError, ConnectionResetError and
    # http.client.RemoteDisconnected are none of them URLError subclasses. One
    # TLS drop escaped this loop, sailed past ingest.py's `except FetchError`
    # and killed the run with no fetch_log row at all — 5m34s of step time and
    # no trace in the database (2026-08-10 04:15 UTC). OSError covers URLError,
    # ssl.SSLError, ConnectionResetError and socket errors; HTTPException
    # covers RemoteDisconnected and IncompleteRead. Between them that is every
    # way this call fails for network reasons.
    payload = None
    http_status = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                http_status = resp.status
                payload = json.load(resp)
            break

        # HTTPError first: it is an OSError subclass, so the broad clause below
        # would otherwise swallow every HTTP failure. It did until 2026-08-10,
        # and that cost two things. urlopen never *returns* a non-2xx response,
        # it raises — so the old `if resp.status != 200: raise FetchError(...)`
        # was dead code and fetch_log.http_status came out NULL on every
        # http_error row, which is the one column that tells "credential dead"
        # from "network dead". And a permanent 403 burned all five attempts and
        # 30s of backoff to report "unreachable" instead of "key rejected".
        #
        # 4xx is a fact about our request; retrying cannot change it. 429 and
        # 5xx are about their server right now, so they stay in the retry path
        # and carry their code into the final error.
        except urllib.error.HTTPError as e:
            http_status = e.code
            if e.code < 500 and e.code != 429:
                raise FetchError(
                    f"HTTP {e.code} from data.gov.in: {e.reason}. "
                    f"A 401/403 means DATA_GOV_IN_API_KEY is rejected or rate-limited; "
                    f"retrying will not fix it.", e.code)
            print(f"  data.gov.in HTTP {e.code}, attempt {attempt}/{ATTEMPTS}",
                  file=sys.stderr)
            if attempt == ATTEMPTS:
                raise FetchError(
                    f"data.gov.in returned HTTP {e.code} on all {ATTEMPTS} "
                    f"attempts: {e.reason}", e.code)
            time.sleep(2 ** attempt)

        # ValueError is in the tuple for json.load. JSONDecodeError is a
        # ValueError and is neither an OSError nor an HTTPException, so until
        # 2026-08-11 a non-JSON body escaped this loop with no retry at all —
        # the same shape of miss as the TLS one above, and the same trigger:
        # data.gov.in serving a CDN maintenance page or an HTML error under a
        # 200. That is the transient class the five attempts exist to absorb,
        # so it is retried rather than fatal.
        except (OSError, http.client.HTTPException, ValueError) as e:
            print(f"  data.gov.in transient failure ({type(e).__name__}), "
                  f"attempt {attempt}/{ATTEMPTS}", file=sys.stderr)
            if attempt == ATTEMPTS:
                raise FetchError(
                    f"data.gov.in unreachable after {ATTEMPTS} attempts: "
                    f"{type(e).__name__}: {e}", None
                )
            time.sleep(2 ** attempt)  # 2,4,8,16s — 30s worst case

    # Valid JSON is not necessarily the object we expect. A bare list or string
    # would make every .get() below raise AttributeError, which is not a
    # FetchError and would therefore be recorded as 'crash' rather than as the
    # upstream-format problem it is.
    if not isinstance(payload, dict):
        raise FetchError(
            f"expected a JSON object, got {type(payload).__name__}", None)

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
    return records, http_status


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

    Non-finite values are rejected here rather than stored. float() accepts
    'NaN', 'Infinity', '-inf' and '1e400' (overflows to inf) without complaint
    and Postgres DOUBLE PRECISION stores them happily, so one would sit in
    observations forever and poison every downstream mean — a NaN propagates
    through Phase 3's baselines and Phase 4's rolling features, and the symptom
    is a model that is inexplicably broken months later, not a traceable crash.

    Raising beats returning None because the two cases are different: 'NA' is
    CPCB saying "no reading", NaN is CPCB saying something malformed. The first
    is data; the second is an anomaly, and ingest.build_rows turns it into a
    logged parse_error and a non-zero exit (§0.5).

    Not enforced as a schema CHECK constraint, for the same reason
    db/schema.sql leaves pollutant_id unconstrained: a value-range CHECK would
    abort a whole bulletin over a genuine 1500 ug/m3 November spike or a future
    pollutant in different units. Rejecting here costs one record.
    """
    if raw is None:
        return None
    raw = raw.strip()
    if raw == NULL_SENTINEL or raw == "":
        return None
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"non-finite value {raw!r}")
    return value


def parse_bulletin_ts(raw: str) -> datetime:
    """last_update is 'DD-MM-YYYY HH:MM:SS' in IST, with no timezone marker."""
    return datetime.strptime(raw, "%d-%m-%Y %H:%M:%S").replace(tzinfo=IST)

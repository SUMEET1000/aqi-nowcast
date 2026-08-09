"""Proves the upsert never overwrites a real reading with a null (§4).

    python tests/test_null_guard.py

This rule is completely silent when broken. Nothing errors; readings just
quietly disappear and the gaps look like missing data. That is exactly why it
gets a test instead of a code read.

Imports the UPSERT statement from scripts/ingest.py rather than copying it, so
this tests the real statement and cannot drift away from it.

Everything runs inside a transaction that is ALWAYS rolled back, so the test
writes nothing to observations even when it fails.
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from db import connect          # noqa: E402
from ingest import UPSERT       # noqa: E402

# A pollutant id CPCB does not use, so a leaked row could never be mistaken for
# real data. The timestamp is far in the past for the same reason.
POLLUTANT = "TEST_NULL_GUARD"
TS = datetime(1990, 1, 1, tzinfo=timezone.utc)

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def main() -> int:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT station_id FROM stations ORDER BY station_id LIMIT 1")
            row = cur.fetchone()
            if not row:
                sys.exit("stations table is empty — run scripts/seed_stations.py first")
            station_id = row[0]

            def upsert(vmin, vmax, vavg):
                cur.execute(UPSERT, (station_id, POLLUTANT, TS, vmin, vmax, vavg, TS))

            def read():
                cur.execute(
                    """SELECT value_min, value_max, value_avg, ingested_at
                       FROM observations
                       WHERE station_id=%s AND pollutant_id=%s AND observation_ts=%s""",
                    (station_id, POLLUTANT, TS),
                )
                return cur.fetchone()

            print("1. first insert lands")
            upsert(10.0, 90.0, 62.0)
            vmin, vmax, vavg, first_ingest = read()
            check("value_avg after insert", vavg, 62.0)

            print("2. 'NA' (-> NULL) must NOT erase a real reading")
            upsert(None, None, None)
            vmin, vmax, vavg, _ = read()
            check("value_avg survives a NULL upsert", vavg, 62.0)
            check("value_min survives a NULL upsert", vmin, 10.0)
            check("value_max survives a NULL upsert", vmax, 90.0)

            print("3. a genuine correction must still apply")
            upsert(12.0, 88.0, 58.0)
            _, _, vavg, _ = read()
            check("value_avg accepts a real correction", vavg, 58.0)

            print("4. NULL -> value backfill must apply")
            cur.execute(
                """UPDATE observations SET value_avg=NULL
                   WHERE station_id=%s AND pollutant_id=%s AND observation_ts=%s""",
                (station_id, POLLUTANT, TS),
            )
            upsert(12.0, 88.0, 71.0)
            _, _, vavg, _ = read()
            check("NULL is backfilled by a later real value", vavg, 71.0)

            print("5. ingested_at means FIRST seen and never moves")
            _, _, _, last_ingest = read()
            check("ingested_at unchanged across 4 upserts", last_ingest, first_ingest)

    finally:
        # Never commit. The test leaves observations byte-identical.
        conn.rollback()
        conn.close()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) — {failures}")
        return 1
    print("null-guard OK — a null can never erase a reading, and ingested_at is immutable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

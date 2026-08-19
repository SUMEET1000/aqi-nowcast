"""Phase 3 — pull OpenAQ's hourly PM2.5 archive into pm25_history.

    python scripts/backfill_openaq.py                      # every mapped station
    python scripts/backfill_openaq.py --station 1          # just one
    python scripts/backfill_openaq.py --station 1 --since 2026-08-09

Run the one-station form first, over the window where CPCB and OpenAQ overlap,
then scripts/compare_sources.py. Until that reaches a verdict we do not know
whether OpenAQ's hourly measurement and CPCB's value_avg are the same quantity,
and backfilling 30 stations before finding out only makes a wrong answer bigger.

Rebuildable by design (db/schema.sql): this reads an archive that is still there
tomorrow, unlike scripts/ingest.py, whose bulletins are gone if missed. So it is
safe to re-run, and it re-runs cheaply — the upsert makes an unchanged hour a
no-op.

Stdlib plus psycopg. The OpenAQ client is imported from probe_history rather
than copied: one throttle, one 429 backoff, one place to fix them. That does
couple a Phase 3 script to a Phase 0 probe, which is deliberate — the probe is
committed, is in CLAUDE.md's command list, and is what produced the station
mapping this script reads.
"""

import argparse
import math
import sys
from datetime import datetime, timezone

from db import connect
from probe_history import (
    _dt,
    load_openaq_key,
    openaq_get,
    pm25_blocks,
    recent_block,
)

# OpenAQ's hourly endpoint caps a page at 1000. A year is ~8760 hours, so a full
# station is ~9 requests, and the 1.1s throttle inside openaq_get makes 30
# stations a few minutes of waiting rather than a rate-limit ban.
PAGE_LIMIT = 1000

# Sanity bounds on a single reading, applied at the trust boundary because this
# is a third-party feed we do not control. OpenAQ has historically served -999
# as a sentinel, and a negative concentration stored as a real value poisons
# every baseline that averages over it — silently, since a mean does not raise.
# The upper bound is deliberately generous: Delhi-NCR genuinely records above
# 900 in November, and anything tighter would discard exactly the severe-band
# hours Phase 4 has to be measured on.
MIN_VALUE = 0.0
MAX_VALUE = 2000.0

STATIONS = """
SELECT station_id, station_name, openaq_location_id
FROM stations
WHERE openaq_location_id IS NOT NULL
ORDER BY station_id
"""

UPSERT = """
INSERT INTO pm25_history (station_id, observation_ts, value)
VALUES (%s, %s, %s)
ON CONFLICT (station_id, observation_ts) DO UPDATE SET value = EXCLUDED.value
WHERE pm25_history.value IS DISTINCT FROM EXCLUDED.value
"""
# The WHERE is the same trick as ingest.py's upsert: it makes cur.rowcount mean
# "rows that actually changed", so a re-run reports 0 and proves it was a no-op
# instead of leaving us to assume it. A re-run reporting a non-zero count over a
# window already pulled means OpenAQ restated its archive, and any baseline
# already computed was computed on numbers that no longer exist.


def pm25_sensor(location_id: int, key: str) -> tuple[int, datetime, datetime] | None:
    """The newest contiguous PM2.5 sensor block for one OpenAQ location.

    Not the location's own datetimeFirst/Last. Gate 0.2 measured that those span
    every sensor a site ever had plus the gaps between them, overstating
    Ambala's usable history by roughly 7x. Only the newest block is safe to
    train on: older ones sit behind multi-year holes, and a model handed 2022
    and 2026 as adjacent hours learns a jump that never happened.
    """
    detail = openaq_get(f"locations/{location_id}", key)["results"][0]
    sensor_ids = [s["id"] for s in detail.get("sensors", [])
                  if s["parameter"]["name"] == "pm25"]
    if not sensor_ids:
        return None
    block = recent_block(pm25_blocks(sensor_ids, key))
    if not block:
        return None
    return block["sensor_id"], block["first"], block["last"]


def measurements(sensor_id: int, start: datetime, end: datetime, key: str):
    """Yield (hour_start_utc, value) pairs, paging until OpenAQ runs out.

    Stops on a short page rather than trusting meta.found, which OpenAQ reports
    as a string like '>1000' once a result set is large enough to matter.
    """
    page = 1
    while True:
        payload = openaq_get(
            f"sensors/{sensor_id}/measurements/hourly", key,
            datetime_from=start.isoformat(),
            datetime_to=end.isoformat(),
            limit=PAGE_LIMIT,
            page=page,
        )
        results = payload.get("results", [])
        for row in results:
            period = (row.get("period") or {}).get("datetimeFrom") or {}
            if period.get("utc"):
                yield _dt(period["utc"]), row.get("value")
        if len(results) < PAGE_LIMIT:
            return
        page += 1


def usable(value) -> bool:
    return (isinstance(value, (int, float))
            and math.isfinite(value)
            and MIN_VALUE <= value <= MAX_VALUE)


def backfill_station(cur, station_id: int, name: str, location_id: int,
                     key: str, since: datetime | None,
                     until: datetime | None) -> int:
    """Pull one station and upsert it. Returns rows changed; raises on failure."""
    found = pm25_sensor(location_id, key)
    if found is None:
        raise RuntimeError(f"OpenAQ location {location_id} has no usable PM2.5 sensor")
    sensor_id, first, last = found

    start = max(first, since) if since else first
    end = min(last, until) if until else last
    if start >= end:
        raise RuntimeError(
            f"requested window is outside sensor {sensor_id} coverage "
            f"({first.date()} to {last.date()})")

    rows, rejected = [], 0
    for ts, value in measurements(sensor_id, start, end, key):
        if usable(value):
            rows.append((station_id, ts, float(value)))
        else:
            rejected += 1

    if rejected:
        # Printed, never swallowed. A feed that starts serving sentinels is a
        # thing to hear about on the run it begins, not by wondering later why a
        # station mean drifted.
        print(f"    {rejected} reading(s) rejected as non-numeric or outside "
              f"[{MIN_VALUE}, {MAX_VALUE}]", file=sys.stderr)

    cur.executemany(UPSERT, rows)
    changed = cur.rowcount
    print(f"  station {station_id:>2}  sensor {sensor_id}  "
          f"{start.date()} to {end.date()}  "
          f"{len(rows)} hour(s) fetched, {changed} changed  {name}")
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill OpenAQ hourly PM2.5.")
    ap.add_argument("--station", type=int, help="one station_id, else all mapped")
    ap.add_argument("--since", help="ISO date, clamps the start of the window")
    ap.add_argument("--until", help="ISO date, clamps the end of the window")
    args = ap.parse_args()

    def parse(flag, value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        except ValueError:
            sys.exit(f"--{flag} is not an ISO date or datetime: {value!r}")

    since, until = parse("since", args.since), parse("until", args.until)
    key = load_openaq_key()

    with connect() as conn, conn.cursor() as cur:
        cur.execute(STATIONS)
        targets = [r for r in cur.fetchall()
                   if args.station is None or r[0] == args.station]
        if not targets:
            sys.exit(f"No station matches --station {args.station}, or none is "
                     f"mapped to OpenAQ. Run scripts/seed_stations.py first.")

        print(f"Backfilling {len(targets)} station(s) from OpenAQ hourly.")
        total, failures = 0, []
        for station_id, name, location_id in targets:
            try:
                total += backfill_station(cur, station_id, name, location_id,
                                          key, since, until)
                # Commit per station, so a failure at station 20 keeps the first
                # 19 rather than discarding twenty minutes of throttled requests.
                conn.commit()
            except Exception as exc:
                conn.rollback()
                failures.append((station_id, name, exc))
                print(f"  station {station_id:>2}  FAILED  {name}: {exc}",
                      file=sys.stderr)

    print(f"\n{total} row(s) written or changed across "
          f"{len(targets) - len(failures)} station(s).")
    if failures:
        # Degrade, then exit non-zero — the shape ingest.py uses. Committing the
        # stations that worked and still failing loudly beats both alternatives:
        # losing good data, and reporting success with stations missing.
        print(f"{len(failures)} station(s) failed:", file=sys.stderr)
        for station_id, name, exc in failures:
            print(f"  {station_id}  {name}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

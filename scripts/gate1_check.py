"""GATE 1 — a command with an exit code, not a judgement call (§0.4).

    python scripts/gate1_check.py

Run this 72 hours after the ingester goes live. Exit 0 IS the gate. Build plan
§4 defines two of the four checks; the other two come from things Phase 0
discovered and are the ones most likely to catch a real problem.
"""

import sys

from db import connect

HOURS_REQUIRED = 60      # 72 hourly bulletins, minus 12h slack for GitHub delays
MIN_STATIONS = 3         # build plan §4
MIN_SUCCESS_RATE = 0.95  # build plan §4

# Pinned in docs/stations.md. Two seed stations rather than one exists purely
# so that identical readings can be recognised as a pipeline bug, not weather.
SEED_STATIONS = ("Patti Mehar, Ambala - HSPCB", "Sector-7, Kurukshetra - HSPCB")

# Below this many readings the distinctness checks in check_seed_stations are
# not evidence of anything and must not be reported as failures.
#
# Measured on the very first bulletin: Ambala and Kurukshetra both reported
# value_avg = 48.0 (their min/max were 26/67 and 40/56, so plainly different
# sensors). PM2.5 averages are small integers, so at n=1 a collision is likely
# rather than surprising — but across 72 bulletins an identical series is
# impossible by chance. A false "pipeline bug, not weather" alarm would send
# someone hunting a bug that does not exist, so the check waits for a sample.
MIN_READINGS_FOR_DISTINCTNESS = 12

failures: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)
    print(f"  FAIL  {msg}")


def ok(msg: str) -> None:
    print(f"  PASS  {msg}")


def check_coverage(cur) -> None:
    print("\n1. Observation coverage (build plan §4 query 1)")
    cur.execute(
        """
        SELECT s.station_name, count(*) AS rows,
               count(DISTINCT o.observation_ts) AS bulletins,
               min(o.observation_ts), max(o.observation_ts)
        FROM observations o JOIN stations s USING (station_id)
        GROUP BY s.station_name ORDER BY bulletins DESC, s.station_name
        """
    )
    rows = cur.fetchall()
    if not rows:
        fail("observations is empty — the ingester has never written a row")
        return

    print(f"    {'station':<48} {'rows':>7} {'bulletins':>10}  window")
    for name, n, bulletins, lo, hi in rows:
        print(f"    {name:<48} {n:>7} {bulletins:>10}  {lo:%m-%d %H:%M} -> {hi:%m-%d %H:%M}")

    good = [r for r in rows if r[2] >= HOURS_REQUIRED]
    if len(good) >= MIN_STATIONS:
        ok(f"{len(good)} station(s) have >={HOURS_REQUIRED} distinct bulletins "
           f"(need {MIN_STATIONS})")
    else:
        fail(f"only {len(good)} station(s) have >={HOURS_REQUIRED} bulletins, "
             f"need {MIN_STATIONS}. Has it really been running 72h?")


def check_fetch_log(cur) -> None:
    print("\n2. Fetch outcomes (build plan §4 query 2)")
    cur.execute(
        """
        SELECT outcome, count(*) FROM fetch_log
        WHERE station_id IS NULL GROUP BY outcome ORDER BY count(*) DESC
        """
    )
    rows = cur.fetchall()
    if not rows:
        fail("fetch_log has no run-level rows — the ingester has never completed")
        return

    total = sum(n for _, n in rows)
    for outcome, n in rows:
        print(f"    {outcome:<16} {n:>6}  ({n / total:.1%})")

    # 'stale' is a healthy outcome: it means we ran and the national feed had
    # not moved yet. It is not a failure and must not be counted as one.
    healthy = sum(n for o, n in rows if o in ("success", "stale"))
    rate = healthy / total
    if rate >= MIN_SUCCESS_RATE:
        ok(f"run success rate {rate:.1%} over {total} runs (need {MIN_SUCCESS_RATE:.0%})")
    else:
        fail(f"run success rate {rate:.1%} over {total} runs, below {MIN_SUCCESS_RATE:.0%}. "
             "The scheduler or the API is the problem — fix that before anything else.")

    cur.execute(
        """
        SELECT outcome, count(*) FROM fetch_log
        WHERE station_id IS NOT NULL GROUP BY outcome
        """
    )
    anomalies = cur.fetchall()
    if anomalies:
        print("    station-level anomalies:")
        for outcome, n in anomalies:
            print(f"      {outcome:<18} {n}")


def check_gaps(cur) -> None:
    """The only check that detects a SILENTLY SKIPPED schedule.

    GitHub emails on a failed run and says nothing about a skipped one, so a
    missing hour leaves no trace anywhere except a hole in this table.
    """
    print("\n3. Missing bulletin hours (detects skipped GitHub schedules)")
    cur.execute(
        """
        WITH span AS (
            SELECT date_trunc('hour', min(observation_ts)) AS lo,
                   date_trunc('hour', max(observation_ts)) AS hi
            FROM observations
        ),
        expected AS (
            SELECT generate_series(lo, hi, interval '1 hour') AS hour FROM span
        )
        SELECT e.hour FROM expected e
        WHERE NOT EXISTS (
            SELECT 1 FROM observations o
            WHERE date_trunc('hour', o.observation_ts) = e.hour
        )
        ORDER BY e.hour
        """
    )
    gaps = [r[0] for r in cur.fetchall()]
    cur.execute(
        """
        SELECT count(*) FROM (
            SELECT DISTINCT date_trunc('hour', observation_ts) FROM observations
        ) t
        """
    )
    covered = cur.fetchone()[0]

    if not gaps:
        ok(f"no gaps — {covered} consecutive bulletin hours covered")
        return

    print(f"    {len(gaps)} missing hour(s) out of {len(gaps) + covered}:")
    for g in gaps[:12]:
        print(f"      {g:%Y-%m-%d %H:00}")
    if len(gaps) > 12:
        print(f"      ... and {len(gaps) - 12} more")

    # A gap is not automatically a gate failure: CPCB itself sometimes skips a
    # bulletin. It is a gate failure when it is bad enough to mean the
    # scheduler is unreliable, which is the thing Gate 1 is really testing.
    ratio = len(gaps) / (len(gaps) + covered)
    if ratio > 1 - MIN_SUCCESS_RATE:
        fail(f"{ratio:.1%} of hours missing — above the {1 - MIN_SUCCESS_RATE:.0%} budget")
    else:
        ok(f"{ratio:.1%} of hours missing, within the {1 - MIN_SUCCESS_RATE:.0%} budget")


def check_seed_stations(cur) -> None:
    print("\n4. Seed stations are distinct (pipeline-bug detector)")
    cur.execute(
        """
        SELECT s.station_name, count(*), count(DISTINCT o.value_avg)
        FROM observations o JOIN stations s USING (station_id)
        WHERE s.station_name = ANY(%s) AND o.pollutant_id = 'PM2.5'
        GROUP BY s.station_name
        """,
        (list(SEED_STATIONS),),
    )
    got = {name: (n, distinct) for name, n, distinct in cur.fetchall()}

    for name in SEED_STATIONS:
        if name not in got:
            # This one IS a failure at any sample size: no rows at all means
            # the testers pinned to this station would receive nothing.
            fail(f"seed station {name!r} has no PM2.5 rows — testers get nothing")
            continue
        n, distinct = got[name]
        if n < MIN_READINGS_FOR_DISTINCTNESS:
            print(f"  SKIP  {name!r}: only {n} reading(s), need "
                  f"{MIN_READINGS_FOR_DISTINCTNESS} before 'unchanging' means anything")
        elif distinct <= 1:
            fail(f"{name!r}: {n} readings but only {distinct} distinct value(s) — "
                 "a station that never changes is a dead sensor or a stuck pipeline")
        else:
            ok(f"{name!r}: {n} readings, {distinct} distinct values")

    enough = all(got.get(s, (0, 0))[0] >= MIN_READINGS_FOR_DISTINCTNESS
                 for s in SEED_STATIONS)
    if len(got) == 2 and not enough:
        print(f"  SKIP  identical-series check: needs "
              f"{MIN_READINGS_FOR_DISTINCTNESS}+ readings per station. Two stations "
              "sharing one integer average is a coincidence, not evidence.")
    elif len(got) == 2:
        cur.execute(
            """
            SELECT count(*) FROM (
                SELECT o.observation_ts, o.value_avg FROM observations o
                JOIN stations s USING (station_id)
                WHERE s.station_name = %s AND o.pollutant_id = 'PM2.5'
                INTERSECT ALL
                SELECT o.observation_ts, o.value_avg FROM observations o
                JOIN stations s USING (station_id)
                WHERE s.station_name = %s AND o.pollutant_id = 'PM2.5'
            ) t
            """,
            SEED_STATIONS,
        )
        identical = cur.fetchone()[0]
        total = min(got[SEED_STATIONS[0]][0], got[SEED_STATIONS[1]][0])
        if total and identical == total:
            fail("both seed stations report an IDENTICAL PM2.5 series. That is a "
                 "pipeline bug, not weather — they are ~120km apart.")
        else:
            ok(f"seed series differ ({identical}/{total} timestamps coincide)")


def main() -> int:
    conn = connect()
    try:
        with conn.cursor() as cur:
            check_coverage(cur)
            check_fetch_log(cur)
            check_gaps(cur)
            check_seed_stations(cur)
    finally:
        conn.close()

    print()
    if failures:
        print(f"GATE 1: FAIL ({len(failures)} check(s) failed)")
        for f in failures:
            print(f"  - {f}")
        print("\nDo not proceed to Phase 2. Build plan §4: if the gate fails, the "
              "scheduler is lying. Fix that before anything else.")
        return 1

    print("GATE 1: PASS — 72h of data logged, scheduler proven, seed stations live.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

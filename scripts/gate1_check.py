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

# Must match the cron in .github/workflows/ingest.yml ('13,43 * * * *'). Only
# used to report what fraction of ticks GitHub actually delivered — never to
# fail the gate. See check_delivery.
TICKS_PER_HOUR = 2

# A run whose bulletin is older than this proves the national feed was frozen
# over the span between them: CPCB normally publishes bulletin H:30 about an
# hour later, and every healthy run observed so far saw an age of 1.0-1.2h.
# Two hours leaves ~1h of slack before we accuse CPCB of a stall.
#
# This is what lets check_gaps tell "CPCB published nothing" apart from "our
# scheduler was not looking". Without it the gate blames our scheduler for
# CPCB's outages: on 2026-08-10 the feed froze 23:30->06:30 UTC and six
# bulletins were never published, which read as 50% missing hours and a broken
# scheduler.
FEED_STALL_AGE_H = 2

# Below this many capturable hours, a missed hour is still a failure but is not
# attributed to the scheduler by name. 24h is one full daily cycle, so CPCB's
# overnight behaviour is represented at least once. See check_gaps.
MIN_HOURS_TO_BLAME = 24

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
    # "RECORDED runs", not "runs". This rate can only ever see runs that lived
    # long enough to write a row, so it is a statement about our code, not
    # about the scheduler. On 2026-08-10 a run died mid-fetch without writing
    # anything and this check reported 100% over 10 runs for a day that
    # contained a failure. ingest.py's run() wrapper now records that case as
    # outcome='crash'; the delivery ratio below covers runs that never started.
    if rate >= MIN_SUCCESS_RATE:
        ok(f"run success rate {rate:.1%} over {total} RECORDED runs "
           f"(need {MIN_SUCCESS_RATE:.0%})")
    else:
        fail(f"run success rate {rate:.1%} over {total} RECORDED runs, below "
             f"{MIN_SUCCESS_RATE:.0%}. The API or our code is the problem — fix that "
             "before anything else.")

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

    check_delivery(cur, total)


def check_delivery(cur, recorded: int) -> None:
    """How many of the ticks the cron asked for did GitHub actually deliver?

    REPORTED, NEVER FAILED ON. GitHub dropping a scheduled tick is not our code
    failing, and check_gaps already owns the pass/fail on the consequence that
    matters (a bulletin we never captured). Failing here as well would fail the
    gate twice for one event, and for something we do not control.

    Measured 2026-08-09/10: 8 of ~22 due ticks arrived, 4-23 min late, with
    holes up to 3h32m — GitHub's schedule trigger is best-effort and says
    nothing when it skips.

    APPROXIMATE. fetch_log cannot tell a scheduled run from a manual or local
    one, so this over-counts deliveries by however many of those there were.
    It is a smoke signal, not a measurement; the Actions API is the authority.
    """
    cur.execute(
        """
        SELECT min(run_ts), max(run_ts) FROM fetch_log WHERE station_id IS NULL
        """
    )
    lo, hi = cur.fetchone()
    hours = (hi - lo).total_seconds() / 3600
    if hours < 1:
        return

    expected = hours * TICKS_PER_HOUR
    ratio = recorded / expected
    print(f"    schedule delivery: {recorded} runs over {hours:.1f}h, "
          f"~{expected:.0f} ticks due ({ratio:.0%})")
    if ratio < 0.8:
        print(f"    NOTE  GitHub delivered {ratio:.0%} of the cron's ticks. Not a "
              "failure of ours, but it means the 30-minute cadence's margin is "
              "gone and every delivered run is load-bearing.")


def check_gaps(cur) -> None:
    """The only check that detects a SILENTLY SKIPPED schedule.

    GitHub emails on a failed run and says nothing about a skipped one, so a
    missing hour leaves no trace anywhere except a hole in this table.

    But a hole has two possible authors and they need different fixes, so this
    check attributes each one before applying the budget:

      feed_stalled — CPCB published nothing. Proven, not assumed: a run whose
                     bulletin was already FEED_STALL_AGE_H hours old shows the
                     feed was frozen across that whole span, so nothing existed
                     for us to fetch. Excluded from the budget — polling harder
                     cannot fetch a bulletin that was never published.
      not_polled   — the feed moved and we were not looking. Ours. This is the
                     number the budget is actually about.

    Counting the two together is what made this check report "50% of hours
    missing, the scheduler is unreliable" for CPCB's 2026-08-10 overnight
    outage, which would have sent someone to fix a scheduler that was working.
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
        ),
        missing AS (
            SELECT e.hour FROM expected e
            WHERE NOT EXISTS (
                SELECT 1 FROM observations o
                WHERE date_trunc('hour', o.observation_ts) = e.hour
            )
        ),
        -- Each row is a span over which the national feed is PROVEN frozen:
        -- at run_ts the newest bulletin on offer was still bulletin_ts.
        stalls AS (
            SELECT date_trunc('hour', bulletin_ts) AS lo, run_ts AS hi
            FROM fetch_log
            WHERE station_id IS NULL
              AND bulletin_ts IS NOT NULL
              AND run_ts - bulletin_ts > %s * interval '1 hour'
        )
        SELECT m.hour,
               EXISTS (SELECT 1 FROM stalls s
                       WHERE m.hour >= s.lo AND m.hour <= s.hi) AS feed_stalled
        FROM missing m ORDER BY m.hour
        """,
        (FEED_STALL_AGE_H,),
    )
    gaps = cur.fetchall()
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

    stalled = [h for h, is_stalled in gaps if is_stalled]
    not_polled = [h for h, is_stalled in gaps if not is_stalled]

    print(f"    {len(gaps)} missing hour(s) out of {len(gaps) + covered}: "
          f"{len(stalled)} feed_stalled (CPCB), {len(not_polled)} not_polled (ours)")
    for hour, is_stalled in gaps[:12]:
        label = "feed_stalled" if is_stalled else "not_polled"
        print(f"      {hour:%Y-%m-%d %H:00}  {label}")
    if len(gaps) > 12:
        print(f"      ... and {len(gaps) - 12} more")

    if stalled:
        print(f"    {len(stalled)} hour(s) excluded: CPCB's feed was demonstrably "
              f"frozen (a run saw a bulletin >{FEED_STALL_AGE_H}h old). Not "
              "recoverable by any polling cadence.")

    # The budget applies to hours we could have captured and did not. Stalled
    # hours are excluded from BOTH sides of the ratio — they were never on
    # offer, so they are not evidence either way about our scheduler.
    denominator = covered + len(not_polled)
    ratio = len(not_polled) / denominator if denominator else 0.0
    if ratio > 1 - MIN_SUCCESS_RATE:
        # Same discipline MIN_READINGS_FOR_DISTINCTNESS applies in
        # check_seed_stations: still a
        # failure, but do not NAME a culprit on a sample too small to identify
        # one. At the 72h the gate is written for the denominator is ~72, where
        # 5% is ~4 hours and the accusation is earned. At 8 hours a single hole
        # is 12.5% and could be almost any true rate. Stating "the scheduler is
        # the problem" there is how someone gets sent to fix a scheduler that
        # works — which this repo has already done once.
        if denominator < MIN_HOURS_TO_BLAME:
            fail(f"{ratio:.1%} of capturable hours missed "
                 f"({len(not_polled)}/{denominator}) — above the "
                 f"{1 - MIN_SUCCESS_RATE:.0%} budget, but only {denominator} hour(s) "
                 f"of sample. Too few to say whether the scheduler or a one-off "
                 f"is at fault; re-run at {MIN_HOURS_TO_BLAME}+ hours.")
        else:
            fail(f"{ratio:.1%} of capturable hours missed "
                 f"({len(not_polled)}/{denominator}) — above the "
                 f"{1 - MIN_SUCCESS_RATE:.0%} budget. The scheduler is the problem, "
                 "not CPCB.")
    else:
        ok(f"{ratio:.1%} of capturable hours missed ({len(not_polled)}/{denominator}), "
           f"within the {1 - MIN_SUCCESS_RATE:.0%} budget")


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

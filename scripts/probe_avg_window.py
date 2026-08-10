"""Probe — what window does the CPCB API's `avg_value` cover?

    python scripts/probe_avg_window.py

READ-ONLY. It opens no write transaction and touches nothing but SELECTs.

One question, an exit code, no new dependencies (§0.6). Answers the item
docs/cpcb_aqi_breakpoints.md has carried as unresolved since Phase 0:

    is `avg_value` a SHORT-window value (one hour, or sub-hourly samples
    aggregated into an hour), or is it ALREADY the 24-hour running average CPCB
    computes its AQI from?

Why it matters, in two places:

  Phase 2 — CPCB's health advisory is written against the OVERALL AQI (worst
    sub-index across >=3 pollutants), not against the PM2.5 band we report. To
    key the advisory off the real overall band we need 24h averages. If CPCB
    already ships them, one bulletin is enough. If not, we build the window
    ourselves out of `observations` and CPCB's >=16h-of-24 rule applies to us.

  Phase 4 — lags of a 24h running mean are NOT lags of an hourly reading. A
    feature set built on the wrong assumption is a silent modelling error, not
    a crash. Settle it before it is load-bearing in two more phases.

THE HYPOTHESIS, stated so it can be broken: `avg_value` is already the average
over CPCB's own sub-index averaging period for that pollutant — 24 hours for
PM2.5, PM10, NO2, SO2 and NH3; **8 hours for O3 and CO**. That per-pollutant
split is CPCB's, not ours, and it is in the breakpoint table in
docs/cpcb_aqi_breakpoints.md. Testing every pollutant against 24h is wrong and
manufactures violations for the two 8-hourly ones.

THE LOGIC. Tests A and B both assume that hypothesis and look for something that
DISPROVES it. Neither can prove a long window outright; they can only fail to
break it, which is the honest shape for this question.

  A  A W-hour running mean moves from one hour to the next by exactly
     (v_new - v_old) / W, and both of those values live inside the window's own
     [min, max]. So |avg(t) - avg(t-1h)| can never exceed (max - min) / W. A
     single pair that exceeds it disproves the hypothesis for that series.

     WITH ONE ALLOWANCE. min/max/avg are published as integers, so two rounded
     averages can overstate the true step by up to 1.0. Measured 2026-08-10:
     without this allowance the test flags 14 PM2.5 pairs, and ALL of them are
     a step of exactly 1.0 against a ceiling below 1.0 — rounding, not physics.
     The allowance is what stops this probe from accusing the data of the
     probe's own arithmetic.

  B  If min/max/avg describe the SAME window, an avg published RECENTLY must
     fall inside this row's [min, max], because the two windows overlap almost
     completely.

     ONLY RECENTLY, and this is a correction to the obvious version of the test.
     avg(t-k) is the mean of [t-k-W, t-k], of which the first k hours sit
     OUTSIDE this row's window — so for large k it may legitimately escape the
     envelope and prove nothing. The naive form (compare the whole preceding W
     hours) therefore manufactures violations at the far end of the lookback,
     which is exactly what it did on 2026-08-10: it called PM10 and O3 short
     while Test A called them long. Comparisons are restricted to
     k <= W / OVERLAP_DIVISOR, where the windows share at least 75% of their
     span and the claim actually holds.

  C  Only matters if A and B say "short". CPCB will not compute a sub-index on
     fewer than 16 hours of data, so if WE have to build the 24h window, how
     often would we actually clear that bar? GitHub delivers ~0.6 bulletins an
     hour and CPCB's feed froze for 7h overnight on 2026-08-09/10. If we rarely
     hold 16 of the previous 24 hours, the overall AQI is unavailable most of
     the time and the PM2.5-only fallback is the COMMON path, not the rare one.
     That changes the Phase 2 design, so it is measured rather than assumed.

THE ANSWER IS PER POLLUTANT, and deliberately not collapsed into one word. The
Phase 2 question is "can we compute CPCB's overall AQI from a single bulletin?",
which needs EVERY contributing pollutant to be pre-averaged. One dissenting
pollutant is a real constraint on that, not noise to be averaged away.

REFUSES ON A SMALL SAMPLE, on purpose — the same discipline as
gate1_check.py's MIN_READINGS_FOR_DISTINCTNESS and MIN_HOURS_TO_BLAME. Below
MIN_BULLETINS it prints every number it has and labels the direction
PROVISIONAL, but it does not record a verdict and it exits non-zero. A probe
that concludes from 9 bulletins is how a wrong fact gets written into three
documents.
"""

from db import connect

# CPCB's own sub-index averaging periods, from the breakpoint table in
# docs/cpcb_aqi_breakpoints.md. O3 and CO are EIGHT-hourly; everything else is
# 24-hourly. A pollutant absent from this map has no known averaging period and
# is REPORTED AND SKIPPED rather than assumed to be 24h (§0.5) — if CPCB starts
# publishing an eighth pollutant we want to be told, not to silently test it
# against a window we invented.
CPCB_AVG_HOURS = {
    "PM2.5": 24,
    "PM10": 24,
    "NO2": 24,
    "SO2": 24,
    "NH3": 24,
    "OZONE": 8,
    "CO": 8,
}

# Tests A and B both need a window's worth of history before they mean anything.
# 24 bulletins over >=24h is one full daily cycle, so CPCB's overnight behaviour
# is represented at least once.
MIN_BULLETINS = 24
MIN_SPAN_H = 24

# A consecutive pair counts as "one bulletin hour apart" inside this tolerance.
# CPCB publishes on the half hour but delivery drifts, and a pair 3 hours apart
# would compare across a gap the 1-hour bound does not describe.
PAIR_GAP_MIN_H = 0.5
PAIR_GAP_MAX_H = 1.5

# min/max/avg are published as integers, so a value can sit a whole unit outside
# the envelope on rounding alone, and a step between two rounded averages can
# overstate the true step by up to 1.0 (each is off by at most 0.5).
#
# Both allowances are generous TO the hypothesis this probe is trying to break.
# That is the right direction to be wrong in: it means a "short window" verdict
# is earned rather than an artifact, and a "long window" verdict is the weaker
# claim of the two. Without ROUNDING_SLACK the test flagged 14 PM2.5 pairs on
# 2026-08-10, every one of them a step of 1.0 against a sub-1.0 ceiling.
ENVELOPE_TOL = 1.0
ROUNDING_SLACK = 1.0

# Test B only compares averages published within W / OVERLAP_DIVISOR hours, so
# the two windows share at least 75% of their span and the containment claim
# holds. 6 hours for a 24h pollutant, 2 hours for an 8-hourly one.
OVERLAP_DIVISOR = 4

# CPCB's own rules, quoted in docs/cpcb_aqi_breakpoints.md: a sub-index needs
# >=16 hours of data, and an overall AQI needs >=3 pollutants, one of which must
# be PM2.5 or PM10.
CPCB_MIN_HOURS = 16
CPCB_WINDOW_H = 24
CPCB_MIN_POLLUTANTS = 3

# Above this share of impossible pairs / escaped averages, the 24h hypothesis is
# rejected. Not zero: one bad row from a CPCB correction should not decide this.
VIOLATION_BUDGET = 0.02

VERDICT_SHORT = "SHORT window (hourly or sub-hourly) — we must build the averages ourselves"
VERDICT_LONG = "LONG window (consistent with CPCB's own averaging period) — CPCB ships it"


def window_values() -> str:
    """CPCB_AVG_HOURS as a SQL VALUES list, so the join happens in Postgres.

    Returned as literal SQL rather than parameters because it is a compile-time
    constant of this file — no user input reaches it.
    """
    return ", ".join(f"('{p}', {h})" for p, h in CPCB_AVG_HOURS.items())


def report_unmapped(cur) -> None:
    """Name any pollutant we have no averaging period for. Never assume one."""
    cur.execute(
        "SELECT DISTINCT pollutant_id FROM observations WHERE NOT (pollutant_id = ANY(%s))",
        (list(CPCB_AVG_HOURS),),
    )
    unmapped = [p for (p,) in cur.fetchall()]
    if unmapped:
        print(f"    WARNING  no CPCB averaging period known for {unmapped} — excluded "
              "from both tests. Add it to CPCB_AVG_HOURS from the breakpoint table.")


def sample(cur) -> tuple[int, float]:
    """How many distinct bulletins, over how many hours."""
    cur.execute(
        """
        SELECT count(DISTINCT observation_ts),
               EXTRACT(EPOCH FROM (max(observation_ts) - min(observation_ts))) / 3600,
               min(observation_ts), max(observation_ts)
        FROM observations
        """
    )
    bulletins, span_h, lo, hi = cur.fetchone()
    span_h = float(span_h or 0)
    print("\n0. Sample")
    if not bulletins:
        print("    observations is empty — nothing to measure")
        return 0, 0.0
    print(f"    {bulletins} distinct bulletins over {span_h:.1f}h "
          f"({lo:%Y-%m-%d %H:%M} -> {hi:%Y-%m-%d %H:%M} UTC)")
    print(f"    need {MIN_BULLETINS} bulletins over {MIN_SPAN_H}h before a verdict is recorded")
    report_unmapped(cur)
    return bulletins, span_h


def test_a(cur) -> dict[str, bool]:
    """Hour-to-hour movement against the (max - min) / W ceiling.

    Returns {pollutant: says_short}. A pollutant "says short" when more than
    VIOLATION_BUDGET of its consecutive-hour pairs move further than a running
    mean over CPCB's own averaging period could manage — each such pair is a
    counterexample to the hypothesis.
    """
    print("\n1. Test A — can a running mean over CPCB's own window move this fast?")
    cur.execute(
        f"""
        WITH win (pollutant_id, hours) AS (VALUES {window_values()}),
        stepped AS (
            SELECT o.pollutant_id, w.hours, o.observation_ts, o.value_avg,
                   o.value_min, o.value_max,
                   lag(o.value_avg)       OVER p AS prev_avg,
                   lag(o.value_min)       OVER p AS prev_min,
                   lag(o.value_max)       OVER p AS prev_max,
                   lag(o.observation_ts)  OVER p AS prev_ts
            FROM observations o
            JOIN win w USING (pollutant_id)
            WHERE o.value_avg IS NOT NULL
              AND o.value_min IS NOT NULL
              AND o.value_max IS NOT NULL
            WINDOW p AS (PARTITION BY o.station_id, o.pollutant_id
                         ORDER BY o.observation_ts)
        ),
        pairs AS (
            SELECT pollutant_id, hours,
                   abs(value_avg - prev_avg) AS step,
                   -- The union of both rows' envelopes: v_new and v_old both
                   -- lie inside it, so the mean cannot move further than this.
                   -- Plus the integer-rounding allowance.
                   (greatest(value_max, prev_max) - least(value_min, prev_min))
                       / hours::float + %s AS ceiling
            FROM stepped
            WHERE prev_ts IS NOT NULL
              AND EXTRACT(EPOCH FROM (observation_ts - prev_ts)) / 3600
                  BETWEEN %s AND %s
        )
        SELECT pollutant_id, min(hours) AS hours,
               count(*)                              AS pairs,
               count(*) FILTER (WHERE step > ceiling) AS impossible,
               max(step)                              AS max_step,
               max(step - ceiling)                    AS worst_excess
        FROM pairs
        GROUP BY pollutant_id
        ORDER BY pollutant_id
        """,
        (ROUNDING_SLACK, PAIR_GAP_MIN_H, PAIR_GAP_MAX_H),
    )
    rows = cur.fetchall()
    if not rows:
        print("    no consecutive hourly pairs yet — nothing to test")
        return {}

    print(f"    {'pollutant':<10} {'window':>7} {'pairs':>7} {'impossible':>11} "
          f"{'max step':>9} {'worst excess':>13}  reads as")
    says_short = {}
    for pollutant, hours, n, bad, max_step, excess in rows:
        share = bad / n if n else 0.0
        says_short[pollutant] = share > VIOLATION_BUDGET
        print(f"    {pollutant:<10} {hours:>6}h {n:>7} {bad:>7} ({share:4.0%}) "
              f"{max_step:>9.1f} {excess:>13.2f}  "
              f"{'SHORT' if says_short[pollutant] else 'long'}")
    return says_short


def test_b(cur) -> dict[str, bool]:
    """Do RECENT averages fit inside this row's min/max envelope?

    Returns {pollutant: says_short}. See the module docstring for why the
    lookback is W / OVERLAP_DIVISOR rather than the full window.
    """
    print("\n2. Test B — does the min/max envelope contain recent averages?")
    cur.execute(
        f"""
        WITH win (pollutant_id, hours) AS (VALUES {window_values()})
        SELECT o.pollutant_id,
               min(w.hours) AS hours,
               max(w.hours::float / %s) AS lookback_h,
               count(*) AS checked,
               count(*) FILTER (
                   WHERE p.value_avg < o.value_min - %s
                      OR p.value_avg > o.value_max + %s
               ) AS escaped
        FROM observations o
        JOIN win w USING (pollutant_id)
        JOIN observations p
          ON  p.station_id     = o.station_id
          AND p.pollutant_id   = o.pollutant_id
          AND p.observation_ts <= o.observation_ts
          AND p.observation_ts >  o.observation_ts
                                  - make_interval(mins => (w.hours * 60 / %s)::int)
        WHERE o.value_min IS NOT NULL
          AND o.value_max IS NOT NULL
          AND p.value_avg IS NOT NULL
        GROUP BY o.pollutant_id
        ORDER BY o.pollutant_id
        """,
        (OVERLAP_DIVISOR, ENVELOPE_TOL, ENVELOPE_TOL, OVERLAP_DIVISOR),
    )
    rows = cur.fetchall()
    if not rows:
        print("    nothing to compare yet")
        return {}

    print(f"    {'pollutant':<10} {'window':>7} {'lookback':>9} {'checked':>9} "
          f"{'escaped':>13}  reads as")
    says_short = {}
    for pollutant, hours, lookback, n, bad in rows:
        share = bad / n if n else 0.0
        says_short[pollutant] = share > VIOLATION_BUDGET
        print(f"    {pollutant:<10} {hours:>6}h {lookback:>8.1f}h {n:>9} {bad:>7} "
              f"({share:4.0%})  {'SHORT' if says_short[pollutant] else 'long'}")
    return says_short


def test_c(cur) -> None:
    """If WE have to build the 24h window, how often would we clear CPCB's rules?

    Reported, never a pass/fail. It does not answer the window question — it
    prices the answer if the window turns out to be short.
    """
    print(f"\n3. Test C — how often would we hold {CPCB_MIN_HOURS} of the previous "
          f"{CPCB_WINDOW_H}h?")
    cur.execute(
        """
        WITH span AS (
            SELECT date_trunc('hour', min(observation_ts)) AS lo,
                   date_trunc('hour', max(observation_ts)) AS hi
            FROM observations
        ),
        -- Only hours whose whole 24h lookback sits inside our logged span. An
        -- hour near the start fails for lack of history, not for lack of
        -- coverage, and counting it would understate what we can do.
        grid AS (
            SELECT generate_series(lo + make_interval(hours => %s), hi,
                                   interval '1 hour') AS at_h
            FROM span
        ),
        obs AS (
            SELECT DISTINCT station_id, pollutant_id,
                   date_trunc('hour', observation_ts) AS h
            FROM observations
            WHERE value_avg IS NOT NULL
        ),
        cov AS (
            SELECT g.at_h, o.station_id, o.pollutant_id, count(*) AS hours
            FROM grid g
            JOIN obs o
              ON  o.h <= g.at_h
              AND o.h >  g.at_h - make_interval(hours => %s)
            GROUP BY g.at_h, o.station_id, o.pollutant_id
        ),
        qual AS (
            SELECT at_h, station_id,
                   count(*) FILTER (WHERE hours >= %s) AS n_pollutants,
                   bool_or(pollutant_id IN ('PM2.5', 'PM10') AND hours >= %s) AS has_pm,
                   bool_or(pollutant_id = 'PM2.5'          AND hours >= %s) AS has_pm25
            FROM cov
            GROUP BY at_h, station_id
        )
        SELECT (SELECT count(*) FROM grid)
               * (SELECT count(*) FROM stations WHERE is_active)        AS station_hours,
               count(*) FILTER (WHERE has_pm25)                          AS pm25_ok,
               count(*) FILTER (WHERE n_pollutants >= %s AND has_pm)     AS aqi_ok
        FROM qual
        """,
        (CPCB_WINDOW_H, CPCB_WINDOW_H, CPCB_MIN_HOURS, CPCB_MIN_HOURS,
         CPCB_MIN_HOURS, CPCB_MIN_POLLUTANTS),
    )
    station_hours, pm25_ok, aqi_ok = cur.fetchone()

    if not station_hours:
        print(f"    fewer than {CPCB_WINDOW_H}h of history — no hour has a full lookback yet")
        return

    print(f"    {station_hours} station-hours with a full {CPCB_WINDOW_H}h lookback")
    print(f"    PM2.5 sub-index computable:  {pm25_ok:>6} ({pm25_ok / station_hours:.1%})")
    print(f"    overall AQI computable:      {aqi_ok:>6} ({aqi_ok / station_hours:.1%}) "
          f"(>={CPCB_MIN_POLLUTANTS} pollutants, one of PM2.5/PM10)")
    if aqi_ok / station_hours < 0.5:
        print("    NOTE  the overall AQI would be UNAVAILABLE most of the time, so the "
              "PM2.5-only fallback would be the common path rather than the rare one. "
              "That is a Phase 2 design input, not a bug.")


def main() -> int:
    conn = connect()
    try:
        with conn.cursor() as cur:
            bulletins, span_h = sample(cur)
            if not bulletins:
                print("\nVERDICT: none — observations is empty.")
                return 1

            a = test_a(cur)
            b = test_b(cur)
            test_c(cur)
    finally:
        conn.close()

    print("\n4. Verdict, per pollutant")
    if not a and not b:
        print("    neither test had any rows to look at")
        print("\nVERDICT: none.")
        return 1

    # A pollutant is only settled when BOTH tests looked at it and agreed. Two
    # tests of one hypothesis disagreeing means one is measuring something other
    # than what it claims, and that is not resolved by taking a majority.
    settled: dict[str, bool] = {}
    disputed: list[str] = []
    for pollutant in sorted(set(a) | set(b)):
        if pollutant in a and pollutant in b and a[pollutant] == b[pollutant]:
            settled[pollutant] = a[pollutant]
        else:
            disputed.append(pollutant)

    for pollutant, is_short in sorted(settled.items()):
        print(f"    {pollutant:<10} {'SHORT window' if is_short else 'long window':<13} "
              f"(A and B agree)")
    for pollutant in disputed:
        print(f"    {pollutant:<10} {'DISPUTED':<13} (A says "
              f"{'short' if a.get(pollutant) else 'long'}, B says "
              f"{'short' if b.get(pollutant) else 'long'})")

    short = sorted(p for p, s in settled.items() if s)
    long_ = sorted(p for p, s in settled.items() if not s)

    print()
    if bulletins < MIN_BULLETINS or span_h < MIN_SPAN_H:
        print(f"VERDICT: WITHHELD — {bulletins} bulletins over {span_h:.1f}h, need "
              f"{MIN_BULLETINS} over {MIN_SPAN_H}h.")
        print(f"  PROVISIONAL: {len(long_)} pollutant(s) read as pre-averaged "
              f"({', '.join(long_) or 'none'}); {len(short)} read as short-window "
              f"({', '.join(short) or 'none'}).")
        print("  Not recorded anywhere. Re-run once the sample is there — a fact written "
              "into three documents off a partial day is worse than an open question.")
        return 1

    if disputed:
        print(f"VERDICT: INCONCLUSIVE — Tests A and B disagree on {', '.join(disputed)}.")
        print("  They test one hypothesis from two directions, so a disagreement means "
              "one of them is wrong. Fix the test before recording anything.")
        return 1

    if not short:
        print(f"VERDICT: {VERDICT_LONG}")
        print(f"  All {len(long_)} pollutants: {', '.join(long_)}.")
        print("  CPCB's overall AQI is therefore computable from a SINGLE bulletin — no "
              "24h window of our own, and no >=16h coverage problem.")
    elif not long_:
        print(f"VERDICT: {VERDICT_SHORT}")
        print(f"  All {len(short)} pollutants: {', '.join(short)}.")
        print("  The overall AQI must be built from our own logged history, and Test C's "
              "coverage number is what says how often that is possible.")
    else:
        print("VERDICT: SPLIT — the answer differs by pollutant.")
        print(f"  pre-averaged by CPCB: {', '.join(long_)}")
        print(f"  short window, ours to build: {', '.join(short)}")
        print("  An overall AQI needs every contributing pollutant, so the short-window "
              "ones set the constraint. This is a real Phase 2 limit, not noise.")

    print(f"\n  Settled over {bulletins} bulletins / {span_h:.1f}h. Record it in "
          "docs/cpcb_aqi_breakpoints.md with today's date and this command.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

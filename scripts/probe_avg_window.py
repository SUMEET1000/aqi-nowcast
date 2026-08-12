"""Probe — what window does the CPCB API's `avg_value` cover?

    python scripts/probe_avg_window.py

Read-only: no write transaction, nothing but SELECTs.

One question, an exit code, no new dependencies (§0.6). It answers the item
docs/cpcb_aqi_breakpoints.md has carried as unresolved since Phase 0 — is
`avg_value` a short-window value (one hour, or sub-hourly samples aggregated
into an hour), or is it already the 24-hour running average CPCB computes its
AQI from?

That matters in two places. In Phase 2, CPCB's health advisory is written
against the overall AQI (worst sub-index across >=3 pollutants) rather than the
PM2.5 band we report, so keying the advisory off the real overall band needs 24h
averages: if CPCB already ships them one bulletin is enough, and if not we build
the window ourselves out of `observations` under CPCB's >=16h-of-24 rule. In
Phase 4, lags of a 24h running mean are not lags of an hourly reading, and a
feature set built on the wrong assumption is a silent modelling error rather
than a crash. Settle it before it is load-bearing in two more phases.

The hypothesis, stated so it can be broken: `avg_value` is already the average
over CPCB's own sub-index averaging period for that pollutant — 24 hours for
PM2.5, PM10, NO2, SO2 and NH3, but 8 hours for O3 and CO. That per-pollutant
split is CPCB's, not ours, and it is in the breakpoint table in
docs/cpcb_aqi_breakpoints.md; testing everything against 24h manufactures
violations for the two 8-hourly pollutants.

Tests A and B both assume the hypothesis and look for something that disproves
it. Neither can prove a long window outright — they can only fail to break it,
which is the honest shape for this question.

  A  A W-hour running mean moves from one hour to the next by exactly
     (v_new - v_old) / W, and both values live inside the window's own
     [min, max], so |avg(t) - avg(t-1h)| can never exceed (max - min) / W. One
     pair that exceeds it disproves the hypothesis for that series.

     With one allowance: min/max/avg are published as integers, so two rounded
     averages can overstate the true step by up to 1.0. Measured 2026-08-10 —
     without the allowance the test flags 14 PM2.5 pairs, every one a step of
     exactly 1.0 against a ceiling below 1.0, which is rounding rather than
     physics. It stops the probe accusing the data of its own arithmetic.

  B  If min/max/avg describe the same window, an avg published recently must
     fall inside this row's [min, max], because the two windows overlap almost
     completely.

     Recently only — a correction to the obvious form of the test. avg(t-k) is
     the mean of [t-k-W, t-k], whose first k hours sit outside this row's
     window, so for large k it may legitimately escape the envelope and prove
     nothing. Comparing the whole preceding W hours manufactures violations at
     the far end of the lookback, which is what it did on 2026-08-10: it called
     PM10 and O3 short while Test A called them long. Comparisons are restricted
     to k <= W / OVERLAP_DIVISOR, where the windows share at least 75% of their
     span and the claim actually holds.

  C  Only matters if A and B say "short". CPCB will not compute a sub-index on
     fewer than 16 hours of data, so if we have to build the 24h window, how
     often would we clear that bar? GitHub delivers ~0.6 bulletins an hour and
     CPCB's feed froze for 7h overnight on 2026-08-09/10. If we rarely hold 16
     of the previous 24 hours, the overall AQI is unavailable most of the time
     and the PM2.5-only fallback is the common path rather than the rare one —
     which changes the Phase 2 design, so it is measured rather than assumed.

The answer is per pollutant and deliberately not collapsed into one word. The
Phase 2 question is "can we compute CPCB's overall AQI from a single bulletin?",
which needs every contributing pollutant to be pre-averaged, so one dissenting
pollutant is a real constraint rather than noise to average away.

It refuses on a small sample on purpose, the same discipline as gate1_check.py's
MIN_READINGS_FOR_DISTINCTNESS and MIN_HOURS_TO_BLAME. Below MIN_BULLETINS it
prints every number it has and labels the direction provisional, but records no
verdict and exits non-zero. A probe that concludes from 9 bulletins is how a
wrong fact gets written into three documents.
"""

from db import connect

# CPCB's own sub-index averaging periods, from the breakpoint table in
# docs/cpcb_aqi_breakpoints.md. O3 and CO are eight-hourly, everything else is
# 24-hourly. A pollutant absent from this map has no known averaging period and
# is reported and skipped rather than assumed to be 24h (§0.5): if CPCB starts
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

# Published as integers, so a value can sit a whole unit outside the envelope on
# rounding alone, and a step between two rounded averages can overstate the true
# step by up to 1.0 (each is off by at most 0.5). See Test A in the docstring.
#
# Both allowances are generous to the hypothesis the probe is trying to break,
# which is the right direction to be wrong in: a "short window" verdict is then
# earned rather than an artifact, and "long window" is the weaker of the two
# claims.
ENVELOPE_TOL = 1.0
ROUNDING_SLACK = 1.0

# 6 hours for a 24h pollutant, 2 for an 8-hourly one — see Test B.
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

# The share of a pollutant's pairs on which Test A could have registered a
# violation at all. Below this the pollutant is untestable, not "long".
#
# Published values are integers, so `step` is an integer and ROUNDING_SLACK = 1.0
# keeps `ceiling` at >= 1.0, which means a step of exactly 1 can never exceed it
# — the smallest step that can flag is 2. For a pollutant whose envelope is wide
# relative to its window (CO at hours=8 with single-digit values gives a ceiling
# of ~1.125), even a purely hourly series would need a 2-unit jump to register.
# Those pollutants came back 'long' regardless of the truth, and the verdict
# logic read that silence as agreement with Test B and wrote it down as a result.
# A test that cannot fail is not evidence, same as for the gates.
POWER_FLOOR = 0.10

# Worded as a failure to reject rather than as a finding, matching what the
# docstring says the tests can do. The printed verdict said "CPCB ships it" and
# main() returned 0, which CLAUDE.md treats as licence to write the answer into
# docs/cpcb_aqi_breakpoints.md.
#
# "not W hours", not "hourly": Test A rejects a running mean over CPCB's own
# period W for that pollutant, but a mean over some other long window (12h, 6h)
# would also produce violations and nothing here excludes it. Calling the
# alternative "hourly or sub-hourly" claims more than the test supports.
VERDICT_SHORT = ("NOT a running mean over CPCB's own period — the window is "
                 "something else, possibly hourly, and we must build the "
                 "averages ourselves")
VERDICT_LONG = ("NOT DISPROVED — consistent with CPCB's own averaging period, "
                "on this sample, by these two tests")


def window_values() -> str:
    """CPCB_AVG_HOURS as a SQL VALUES list, so the join happens in Postgres.

    Returned as literal SQL rather than parameters because it is a compile-time
    constant of this file — no user input reaches it.
    """
    return ", ".join(f"('{p}', {h})" for p, h in CPCB_AVG_HOURS.items())


def report_unmapped(cur) -> list[str]:
    """Name any pollutant we have no averaging period for. Never assume one.

    RETURNS the list now, instead of only printing it. It printed a WARNING and
    returned None, and nothing propagated — so the verdict below could announce
    "All N pollutants ... the overall AQI is computable from a SINGLE bulletin"
    while a pollutant that contributes to that very AQI had been silently
    dropped from both tests. A claim about all seven cannot be made from six.
    """
    cur.execute(
        "SELECT DISTINCT pollutant_id FROM observations WHERE NOT (pollutant_id = ANY(%s))",
        (list(CPCB_AVG_HOURS),),
    )
    unmapped = sorted(p for (p,) in cur.fetchall())
    if unmapped:
        print(f"    WARNING  no CPCB averaging period known for {unmapped} — excluded "
              "from both tests. Add it to CPCB_AVG_HOURS from the breakpoint table.")
    return unmapped


def sample(cur) -> tuple[int, float, list[str]]:
    """How many distinct bulletins, over how many hours, and what we cannot test."""
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
        return 0, 0.0, []
    print(f"    {bulletins} distinct bulletins over {span_h:.1f}h "
          f"({lo:%Y-%m-%d %H:%M} -> {hi:%Y-%m-%d %H:%M} UTC)")
    print(f"    need {MIN_BULLETINS} bulletins over {MIN_SPAN_H}h before a verdict is recorded")
    unmapped = report_unmapped(cur)
    return bulletins, span_h, unmapped


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
               max(step - ceiling)                    AS worst_excess,
               -- POWER: pairs on which a violation was even POSSIBLE. Values
               -- are integers, so the smallest step that can exceed a ceiling
               -- of >=1.0 is 2. Where ceiling >= 2 the pair could only flag on
               -- a 3+ jump, and a pollutant whose pairs are nearly all like
               -- that cannot answer the question either way.
               count(*) FILTER (WHERE ceiling < 2.0)  AS discriminating
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
          f"{'max step':>9} {'worst excess':>13} {'power':>7}  reads as")
    says_short = {}
    for pollutant, hours, n, bad, max_step, excess, discriminating in rows:
        share = bad / n if n else 0.0
        power = discriminating / n if n else 0.0
        if power < POWER_FLOOR:
            # Not says_short=False. Absent from the dict entirely, so the
            # verdict logic cannot mistake this silence for agreement.
            verdict = "UNTESTABLE"
        else:
            says_short[pollutant] = share > VIOLATION_BUDGET
            verdict = "SHORT" if says_short[pollutant] else "not disproved"
        print(f"    {pollutant:<10} {hours:>6}h {n:>7} {bad:>7} ({share:4.0%}) "
              f"{max_step:>9.1f} {excess:>13.2f} {power:>6.0%}  {verdict}")

    untestable = [r[0] for r in rows if r[0] not in says_short]
    if untestable:
        print(f"    {len(untestable)} pollutant(s) UNTESTABLE by Test A "
              f"({', '.join(untestable)}): fewer than {POWER_FLOOR:.0%} of pairs "
              f"had a ceiling a 2-unit step could exceed, so this test could not "
              f"have rejected the hypothesis for them whatever the truth is.")
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
          -- Strictly less than, not <=. With <= every row was compared against
          -- itself, and o.value_min <= o.value_avg <= o.value_max holds by
          -- definition, so each group carried one guaranteed-clean comparison —
          -- 1 in 6 at a 6h lookback on hourly bulletins, a mechanical ~17%%
          -- dilution of `share` against a 2%% budget.
          AND p.observation_ts <  o.observation_ts
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
        -- Restricted to ACTIVE stations, because the denominator below counts
        -- active stations only. Without this a retired station with history
        -- contributed to the numerator but not the denominator, and the
        -- coverage ratio could exceed 100%%.
        obs AS (
            SELECT DISTINCT o.station_id, o.pollutant_id,
                   date_trunc('hour', o.observation_ts) AS h
            FROM observations o
            JOIN stations s USING (station_id)
            WHERE o.value_avg IS NOT NULL AND s.is_active
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
        # station_hours is grid_hours x active_stations, so zero has two causes
        # and they need different fixes. Naming only the first sends you looking
        # at history when the station table is empty.
        print(f"    no station-hours to measure: either fewer than {CPCB_WINDOW_H}h "
              f"of history (no hour has a full lookback yet), or there are no "
              f"ACTIVE stations at all — check "
              f"SELECT count(*) FROM stations WHERE is_active")
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
            # Same reason as gate1_check.py: date_trunc() truncates in the
            # session timezone and IST is a HALF-HOUR offset, so hour buckets
            # move depending on which machine ran this. In IST they land on :30
            # UTC — exactly where CPCB's bulletins sit — so two bulletins can
            # share a bucket and a third can fall out of one. This probe also
            # prints its sample window labelled 'UTC', which was only true by
            # luck.
            cur.execute("SET TIME ZONE 'UTC'")
            bulletins, span_h, unmapped = sample(cur)
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

    # A pollutant is only settled when both tests looked at it and agreed. Two
    # tests of one hypothesis disagreeing means one is measuring something other
    # than what it claims, and that is not resolved by taking a majority.
    settled: dict[str, bool] = {}
    disputed: list[str] = []
    for pollutant in sorted(set(a) | set(b)):
        if pollutant in a and pollutant in b and a[pollutant] == b[pollutant]:
            settled[pollutant] = a[pollutant]
        else:
            disputed.append(pollutant)

    # "A and B agree" is weaker than it reads. The two tests are not
    # independent: both read the same [min, max] envelope, so if CPCB's min/max
    # were hourly extremes while avg were a 24h mean, both would swing together
    # and agree on the wrong answer. Agreement here means two views of one
    # envelope did not contradict the hypothesis, not that two measurements
    # confirmed it.
    for pollutant, is_short in sorted(settled.items()):
        print(f"    {pollutant:<10} "
              f"{'SHORT window' if is_short else 'not disproved':<14} "
              f"(A and B agree — note: both read the same min/max envelope)")

    def opinion(d: dict, pollutant: str) -> str:
        # Not a bare d.get(): that printed "long" for a pollutant the test never
        # saw, which is a fabricated opinion attributed to a test that produced
        # no rows for it.
        if pollutant not in d:
            return "not tested"
        return "short" if d[pollutant] else "not disproved"

    for pollutant in disputed:
        print(f"    {pollutant:<10} {'DISPUTED':<14} (A says "
              f"{opinion(a, pollutant)}, B says {opinion(b, pollutant)})")

    short = sorted(p for p, s in settled.items() if s)
    long_ = sorted(p for p, s in settled.items() if not s)

    # Pollutants Test A could not discriminate on, and pollutants with no known
    # CPCB averaging period, are both absent from `settled`. Neither absence is
    # evidence of a long window, so the verdicts below must not speak for them.
    untested = sorted(set(unmapped) | (set(CPCB_AVG_HOURS) - set(settled) - set(disputed)))

    print()
    if bulletins < MIN_BULLETINS or span_h < MIN_SPAN_H:
        print(f"VERDICT: WITHHELD — {bulletins} bulletins over {span_h:.1f}h, need "
              f"{MIN_BULLETINS} over {MIN_SPAN_H}h.")
        print(f"  PROVISIONAL, not a result: {len(long_)} pollutant(s) read as "
              f"pre-averaged ({', '.join(long_) or 'none'}); {len(short)} read as "
              f"short-window ({', '.join(short) or 'none'}).")
        print("  Not recorded anywhere. Re-run once the sample is there — a fact written "
              "into three documents off a partial day is worse than an open question.")
        return 1

    if untested:
        # A claim about the overall AQI needs every contributing pollutant. The
        # verdict printed "All N pollutants ... computable from a SINGLE
        # bulletin" while a pollutant feeding that AQI had been dropped from
        # both tests with nothing propagating the omission.
        print(f"VERDICT: INCOMPLETE — {len(untested)} pollutant(s) were never "
              f"tested: {', '.join(untested)}.")
        print("  Either no CPCB averaging period is known for them, or Test A had "
              "no power to discriminate. The overall-AQI question needs every "
              "contributing pollutant, so it cannot be answered from the rest.")
        return 1

    if disputed:
        print(f"VERDICT: INCONCLUSIVE — Tests A and B disagree on {', '.join(disputed)}.")
        print("  They test one hypothesis from two directions, so a disagreement means "
              "one of them is wrong. Fix the test before recording anything.")
        return 1

    if not short:
        print(f"VERDICT: {VERDICT_LONG}")
        print(f"  All {len(long_)} tested pollutants: {', '.join(long_)}.")
        print("  This is a FAILURE TO REJECT, not a confirmation. Neither test can prove "
              "a long window; they can only break it, and neither broke.")
        print("  Proceed with Phase 2's overall AQI computed from a SINGLE bulletin, but "
              "record it in docs/cpcb_aqi_breakpoints.md as an ASSUMPTION with this date "
              "and sample size — not as a measured fact, and not in the README.")
        print("  If Phase 4's lag features behave strangely later, re-examine this first.")
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

    print(f"\n  Over {bulletins} bulletins / {span_h:.1f}h. Record it in "
          "docs/cpcb_aqi_breakpoints.md with today's date, this command, and the "
          "sample size — the sample is part of the claim.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

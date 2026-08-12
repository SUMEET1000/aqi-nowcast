"""Gate 1 — a command with an exit code, not a judgement call (§0.4).

    python scripts/gate1_check.py

Run this 72 hours after the ingester goes live. Exit 0 is the gate. Build plan
§4 defines two of the four checks; the other two come out of Phase 0 findings
and are the ones most likely to catch a real problem.
"""

import sys

from db import connect

HOURS_REQUIRED = 60      # 72 hourly bulletins, minus 12h slack for GitHub delays
MIN_STATIONS = 3         # build plan §4
MIN_SUCCESS_RATE = 0.95  # build plan §4

# Below this fraction of hours carrying at least one run, say so loudly.
# Not a gate — check_gaps owns the pass/fail. See check_polling.
MIN_HOURS_COVERED = 0.95

# A run whose bulletin is older than this proves the national feed was frozen
# across the span between them: CPCB publishes bulletin H:30 about an hour
# later, and every healthy run observed so far saw an age of 1.0-1.2h, so two
# hours leaves ~1h of slack before we accuse CPCB of a stall. This is what lets
# check_gaps tell "CPCB published nothing" from "our scheduler was not looking".
# Without it the gate blames us for their outages — the 2026-08-10 freeze
# (23:30->06:30 UTC, six bulletins never published) read as 50% missing hours
# and a broken scheduler.
FEED_STALL_AGE_H = 2

# The window, after a missing hour H, in which a run's observation counts as
# evidence about H. See check_gaps.
#
# This is the locality bound and nothing else. A run four hours after H saw a
# feed that had had four hours to catch up, so it says nothing about whether H
# was ever on offer; without the bound, one row whitewashed eight days. Whether
# the feed was actually frozen is decided separately, by FEED_STALL_AGE_H
# against the bulletin's age at run time. Folding the staleness requirement in
# here instead would stop the check from excusing a run made *during* a freeze,
# which is its best evidence.
FEED_STALL_WINDOW_H = 4

# Below this many capturable hours, a missed hour is still a failure but is not
# attributed to the scheduler by name. 24h is one full daily cycle, so CPCB's
# overnight behaviour is represented at least once. See check_gaps.
MIN_HOURS_TO_BLAME = 24

# Pinned in docs/stations.md. Two seed stations rather than one exists purely
# so that identical readings can be recognised as a pipeline bug, not weather.
SEED_STATIONS = ("Patti Mehar, Ambala - HSPCB", "Sector-7, Kurukshetra - HSPCB")

# Below this many readings the distinctness checks in check_seed_stations are
# not evidence of anything and must not be reported as failures. Measured on
# the very first bulletin: Ambala and Kurukshetra both reported value_avg = 48.0
# (min/max 26/67 and 40/56, so plainly different sensors). PM2.5 averages are
# small integers, so at n=1 a collision is likely rather than surprising, while
# across 72 bulletins an identical series is impossible by chance. The check
# waits for a sample rather than sending someone hunting a bug that isn't there.
MIN_READINGS_FOR_DISTINCTNESS = 12

# Above this share of exactly-coinciding (timestamp, value) pairs, the two seed
# stations are not measuring independently. Not 1.0 — exact equality made the
# detector unfalsifiable in practice, since a single 'NA' at one station or one
# bulletin where one was missing breaks a 100% match, so a fully duplicated feed
# passed on 59 of 60. PM2.5 averages are small integers and the two stations are
# ~120km apart, so genuine coincidence runs at a few percent, not ninety.
SEED_IDENTICAL_SHARE = 0.90

# How long the pipeline may be silent before the gate refuses to certify it.
# The two thresholds are far apart because they measure different systems.
#
# MAX_RUN_AGE_MIN is ours: two triggers fire at :05/:35 and :13/:43, so 90
# minutes is roughly three consecutive missed ticks, and if nothing has run in
# that long the ingester is down and nothing else on this page is worth reading.
#
# MAX_BULLETIN_AGE_H is CPCB's, and CPCB genuinely freezes — the 2026-08-10
# overnight stall ran 23:30->06:30 UTC with nothing published. Three hours here
# would fail the gate every night for an outage that is not ours, the exact
# false accusation check_gaps exists to prevent. Twelve is beyond any freeze
# seen so far and still catches a feed that is dead for days.
MAX_RUN_AGE_MIN = 90
MAX_BULLETIN_AGE_H = 12

# What counts as a run, and what counts as a per-station anomaly. Classify by
# outcome, never by `station_id IS NULL` — the two are not the same set, and the
# difference silently corrupted this gate. ingest.py writes unknown_station rows
# with no station_id (it has none; that is what makes the station unknown), so
# every one landed in the run-level bucket and inflated the success-rate
# denominator. One CPCB rename over 72 runs produced 72 extra rows and reported
# "run success rate 50.0% over 144 recorded runs" for 72 healthy runs, failing
# the gate with "the API or our code is the problem" — while the anomaly listing
# (station_id IS NOT NULL) never showed them, so the output did not name the
# cause either.
RUN_OUTCOMES = ("success", "stale", "http_error", "parse_error", "crash")
ANOMALY_OUTCOMES = ("station_missing", "unknown_station")

failures: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)
    print(f"  FAIL  {msg}")


def ok(msg: str) -> None:
    print(f"  PASS  {msg}")


def check_freshness(cur) -> None:
    """Is the pipeline ALIVE RIGHT NOW? Runs first, because nothing else asks.

    Every other check here is bounded by the data it finds. check_gaps and
    check_polling build their expected-hour series from min(ts)..max(ts) of the
    table itself, so an outage at the tail is not a gap inside the window — the
    window silently ends where the data ends, and until this check existed
    nothing compared anything to now().

    That certifies a corpse. Collect 60 bulletins over days 1-4, let the
    Cloudflare PAT expire (fine-grained PATs always do) or let GitHub disable
    the cron after 60 days of no commits — both documented failure modes — then
    run this on day 20. Coverage: 60 distinct bulletins, pass. Fetch outcomes:
    all success, pass. Gaps: the 16 dead days are outside the generated series
    entirely, pass. Exit 0 on a pipeline dead for a fortnight, and the same
    shape as the other failure this file carries a scar from — the measurement
    excluding the region the failure lived in.
    """
    print("\n0. Pipeline liveness")
    cur.execute(
        """
        SELECT max(run_ts),
               extract(epoch FROM now() - max(run_ts)) / 60
        FROM fetch_log
        """
    )
    last_run, run_age_min = cur.fetchone()
    if last_run is None:
        fail("fetch_log is empty — the ingester has never run")
    elif run_age_min > MAX_RUN_AGE_MIN:
        fail(f"last run was {run_age_min / 60:.1f}h ago (limit "
             f"{MAX_RUN_AGE_MIN}min). The ingester is not running NOW, so the "
             f"numbers below describe a pipeline that has stopped. Check the "
             f"Cloudflare Worker (expired PAT?) and the Actions tab (GitHub "
             f"disables schedules after 60 days of no commits).")
    else:
        ok(f"last run {run_age_min:.0f} min ago")

    cur.execute(
        """
        SELECT max(observation_ts),
               extract(epoch FROM now() - max(observation_ts)) / 3600
        FROM observations
        """
    )
    last_obs, obs_age_h = cur.fetchone()
    if last_obs is None:
        fail("observations is empty — nothing has ever been captured")
    elif obs_age_h > MAX_BULLETIN_AGE_H:
        fail(f"newest bulletin is {obs_age_h:.1f}h old (limit "
             f"{MAX_BULLETIN_AGE_H}h). Longer than any CPCB freeze observed so "
             f"far, so this is a dead feed rather than an overnight stall.")
    else:
        ok(f"newest bulletin {obs_age_h:.1f}h old")


def check_coverage(cur) -> None:
    """Bulletins carrying a real PM2.5 reading, per station.

    Both filters in the query are load-bearing, and neither was there
    originally. `pollutant_id = 'PM2.5'` because the product forecasts PM2.5 —
    counting all seven let a station satisfy the gate on NO2 and NH3 alone.
    `value_avg IS NOT NULL` because ingest.py writes a row for every pollutant
    every bulletin, NULL where CPCB sent 'NA', so a station whose PM2.5 sensor
    had been dark for all 60 bulletins still counted 60 and passed. Same defect
    as Gate 0.1's has_pm25: a row is not a reading.

    What MIN_STATIONS is worth, honestly: observation_ts is one national
    bulletin timestamp identical on every row, so every live station has the
    same bulletin count. This is close to a single boolean ("do we have 60
    bulletins") counted three times, and the per-station number only separates
    stations whose sensors differ in liveness — which is precisely what the
    value_avg filter lets it see.
    """
    print("\n1. PM2.5 coverage per station (build plan §4 query 1)")
    cur.execute(
        """
        SELECT s.station_name, count(*) AS rows,
               count(DISTINCT o.observation_ts) AS bulletins,
               min(o.observation_ts), max(o.observation_ts)
        FROM observations o JOIN stations s USING (station_id)
        WHERE o.pollutant_id = 'PM2.5' AND o.value_avg IS NOT NULL
        GROUP BY s.station_name ORDER BY bulletins DESC, s.station_name
        """
    )
    rows = cur.fetchall()
    if not rows:
        fail("no station has a single non-NULL PM2.5 reading — the ingester has "
             "never written one, or every PM2.5 sensor is reporting 'NA'")
        return

    print(f"    {'station':<48} {'rows':>7} {'bulletins':>10}  window")
    for name, n, bulletins, lo, hi in rows:
        print(f"    {name:<48} {n:>7} {bulletins:>10}  {lo:%m-%d %H:%M} -> {hi:%m-%d %H:%M}")

    good = [r for r in rows if r[2] >= HOURS_REQUIRED]
    if len(good) >= MIN_STATIONS:
        ok(f"{len(good)} station(s) have >={HOURS_REQUIRED} bulletins carrying a "
           f"real PM2.5 value (need {MIN_STATIONS})")
    else:
        fail(f"only {len(good)} station(s) have >={HOURS_REQUIRED} bulletins with "
             f"a non-NULL PM2.5 reading, need {MIN_STATIONS}.")


def check_fetch_log(cur) -> None:
    print("\n2. Fetch outcomes (build plan §4 query 2)")
    cur.execute(
        """
        SELECT outcome, count(*) FROM fetch_log
        WHERE outcome = ANY(%s) GROUP BY outcome ORDER BY count(*) DESC
        """,
        (list(RUN_OUTCOMES),),
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
    # "recorded runs", not "runs". This rate can only see runs that lived long
    # enough to write a row, so it describes our code and not the scheduler. On
    # 2026-08-10 a run died mid-fetch without writing anything and this check
    # reported 100% over 10 runs for a day that contained a failure. ingest.py's
    # run() wrapper records that case as outcome='crash' now; the coverage check
    # below is what covers runs that never started.
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
        WHERE outcome = ANY(%s) GROUP BY outcome
        """,
        (list(ANOMALY_OUTCOMES),),
    )
    anomalies = cur.fetchall()
    if anomalies:
        print("    station-level anomalies:")
        for outcome, n in anomalies:
            print(f"      {outcome:<18} {n}")

    check_polling(cur)


def check_polling(cur) -> None:
    """What fraction of hours had at least one run?

    Reported, never failed on. A trigger dropping a tick is not our code
    failing, and check_gaps already owns the pass/fail on the consequence that
    matters — a bulletin we never captured. Failing here too would fail the gate
    twice for one event, over something we do not control.

    This was a tick-delivery ratio until 2026-08-10: recorded runs divided by a
    TICKS_PER_HOUR constant pinned to the cron in ingest.yml. Do not reintroduce
    it. With two independent triggers (the Cloudflare Worker in trigger/ and the
    GitHub cron kept on as backup) no single number describes what was due, so
    that constant has no correct value and the ratio misreports whatever it is
    set to. "At least one run per hour" holds under any number of triggers and
    is the property that matters: CPCB publishes hourly and each bulletin sits
    on the feed for about an hour, so one run an hour is the real floor.

    Approximate in one direction — fetch_log cannot tell a scheduled run from a
    dispatched, manual, or local one, so a laptop run counts as coverage. The
    Actions API is the authority on which trigger fired.
    """
    cur.execute(
        """
        WITH span AS (
            SELECT date_trunc('hour', min(run_ts)) AS lo,
                   date_trunc('hour', max(run_ts)) AS hi
            FROM fetch_log WHERE outcome = ANY(%s)
        ),
        expected AS (
            SELECT generate_series(lo, hi, interval '1 hour') AS hour FROM span
        )
        SELECT count(*) FILTER (
                   WHERE EXISTS (SELECT 1 FROM fetch_log f
                                 WHERE f.outcome = ANY(%s)
                                   AND date_trunc('hour', f.run_ts) = e.hour)
               ) AS covered,
               count(*) AS total
        FROM expected e
        """,
        (list(RUN_OUTCOMES), list(RUN_OUTCOMES)),
    )
    covered, total = cur.fetchone()
    if not total or total < 2:
        # Say why rather than returning in silence: a missing polling line with
        # no reason beside it is indistinguishable from the check having been
        # deleted.
        print(f"    polling coverage: not computed ({total or 0} hour(s) of span, "
              f"needs 2)")
        return

    ratio = covered / total
    print(f"    polling coverage: {covered}/{total} hours carried a run ({ratio:.0%})")
    if ratio < MIN_HOURS_COVERED:
        print(f"    NOTE  {total - covered} hour(s) had NO run at all. Not a failure "
              "of ours, but each one is an hour in which a bulletin could only be "
              "captured by luck. Check the Actions API for which trigger dropped, "
              "and trigger/README.md for the known failure modes (an expired PAT "
              "leaves the throttled GitHub cron as the only trigger).")


def check_gaps(cur) -> None:
    """The only check that detects a silently skipped schedule.

    GitHub emails on a failed run and says nothing about a skipped one, so a
    missing hour leaves no trace anywhere except a hole in this table.

    A hole has two possible authors needing different fixes, so each one is
    attributed before the budget is applied. feed_stalled means CPCB published
    nothing, proven rather than assumed: a run whose bulletin was already
    FEED_STALL_AGE_H hours old shows the feed was frozen across that span, so
    there was nothing to fetch and polling harder would not have helped — those
    are excluded from the budget. not_polled means the feed moved and we were
    not looking, which is ours, and is what the budget is about.

    Counting the two together made this check report "50% of hours missing, the
    scheduler is unreliable" for CPCB's 2026-08-10 overnight outage, and would
    have sent someone to fix a scheduler that was working.
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
        )
        -- An hour is excused only by a run that was looking at the time. Three
        -- conditions, each doing a distinct job:
        --   run_ts - bulletin_ts > FEED_STALL_AGE_H — the feed was demonstrably
        --       stale when we looked, which is what separates a real freeze
        --       from CPCB's routine ~1h publication lag.
        --   bulletin_ts < m.hour — nothing for this hour was on offer even then.
        --   m.hour <= run_ts < m.hour + FEED_STALL_WINDOW_H — the evidence is
        --       local to the hour it excuses (see the constant).
        --
        -- The upper bound is the one that was missing. The excusing span ran
        -- from date_trunc(bulletin_ts) to run_ts with no limit, so one row with
        -- an old bulletin and a recent run_ts — a local replay, or the first run
        -- after a long outage, e.g. bulletin Aug 5 / run Aug 13 — marked all
        -- eight days 'feed_stalled'. Both sides of the budget dropped to zero
        -- and the check reported "0.0%% of capturable hours missed": a pass on an
        -- eight-day outage, from the check that owns the scheduler's pass/fail.
        SELECT m.hour,
               EXISTS (
                   SELECT 1 FROM fetch_log f
                   WHERE f.outcome = ANY(%s)
                     AND f.bulletin_ts IS NOT NULL
                     AND f.run_ts - f.bulletin_ts > %s * interval '1 hour'
                     AND f.bulletin_ts < m.hour
                     AND f.run_ts >= m.hour
                     AND f.run_ts <  m.hour + %s * interval '1 hour'
               ) AS feed_stalled,
               -- Were we looking anywhere near this hour at all? If not the
               -- hour is unattributable: with no run in the window there is no
               -- evidence either way about whether CPCB published, so blaming
               -- ourselves would be a guess reported as a verdict. Still counted
               -- against the budget — we cannot claim an hour we did not
               -- witness — but reported separately, because "we were down" and
               -- "the scheduler dropped a tick" need different fixes.
               EXISTS (
                   SELECT 1 FROM fetch_log f
                   WHERE f.outcome = ANY(%s)
                     AND f.run_ts >= m.hour
                     AND f.run_ts <  m.hour + %s * interval '1 hour'
               ) AS observed
        FROM missing m ORDER BY m.hour
        """,
        (list(RUN_OUTCOMES), FEED_STALL_AGE_H, FEED_STALL_WINDOW_H,
         list(RUN_OUTCOMES), FEED_STALL_WINDOW_H),
    )
    gaps = cur.fetchall()
    cur.execute(
        """
        SELECT count(*) FROM (
            SELECT DISTINCT date_trunc('hour', observation_ts) FROM observations
            WHERE value_avg IS NOT NULL
        ) t
        """
    )
    covered = cur.fetchone()[0]

    if not covered:
        # A zero-row result is not a pass. With observations empty the expected
        # series is empty too, so `gaps` comes back empty and this returned
        # ok("no gaps — 0 consecutive bulletin hours covered") — the check that
        # owns the scheduler's pass/fail reporting success on an empty table.
        # check_coverage happens to fail first, but that is an ordering accident
        # rather than a guarantee.
        fail("no hour has a non-NULL reading — nothing to check gaps against")
        return

    if not gaps:
        ok(f"no gaps — {covered} consecutive bulletin hours covered")
        return

    def label_of(is_stalled: bool, observed: bool) -> str:
        if is_stalled:
            return "feed_stalled"
        return "not_polled" if observed else "unattributable"

    stalled = [h for h, s, o in gaps if s]
    not_polled = [h for h, s, o in gaps if not s and o]
    unattributable = [h for h, s, o in gaps if not s and not o]

    print(f"    {len(gaps)} missing hour(s) out of {len(gaps) + covered}: "
          f"{len(stalled)} feed_stalled (CPCB), {len(not_polled)} not_polled (ours), "
          f"{len(unattributable)} unattributable (we were not running)")
    for hour, is_stalled, observed in gaps[:12]:
        print(f"      {hour:%Y-%m-%d %H:00}  {label_of(is_stalled, observed)}")
    if len(gaps) > 12:
        print(f"      ... and {len(gaps) - 12} more")

    if stalled:
        print(f"    {len(stalled)} hour(s) excluded: CPCB's feed was demonstrably "
              f"frozen (a run {FEED_STALL_AGE_H}-{FEED_STALL_WINDOW_H}h later was "
              f"still offered an older bulletin). Not recoverable by any polling "
              f"cadence.")

    if unattributable:
        print(f"    {len(unattributable)} hour(s) UNATTRIBUTABLE: no run happened "
              f"within {FEED_STALL_WINDOW_H}h, so there is no evidence about whether "
              f"CPCB published. Counted against us below — we cannot claim an hour "
              f"we did not witness — but the fix is 'why were we down', not 'why "
              f"did a tick drop'.")

    # The budget applies to hours we could have captured and did not. Stalled
    # hours drop out of both sides of the ratio — they were never on offer, so
    # they say nothing either way about our scheduler. Unattributable hours are
    # charged to us, conservatively: an hour nobody looked at is an hour we did
    # not capture, whatever CPCB was doing.
    missed = not_polled + unattributable
    denominator = covered + len(missed)
    if not denominator:
        # Not `x if denominator else 0.0`, which made 0/0 a pass. A ratio
        # computed from no data is not evidence of anything, least of all
        # success.
        fail("no capturable hours to judge — cannot say whether polling worked")
        return
    ratio = len(missed) / denominator
    if ratio > 1 - MIN_SUCCESS_RATE:
        # Same discipline as MIN_READINGS_FOR_DISTINCTNESS: still a failure, but
        # do not name a culprit on a sample too small to identify one. At the
        # 72h the gate is written for the denominator is ~72, where 5% is ~4
        # hours and the accusation is earned. At 8 hours a single hole is 12.5%
        # and could be almost any true rate — and "the scheduler is the problem"
        # on that sample is how someone gets sent to fix a scheduler that works,
        # which has already happened here once.
        if denominator < MIN_HOURS_TO_BLAME:
            fail(f"{ratio:.1%} of capturable hours missed "
                 f"({len(missed)}/{denominator}) — above the "
                 f"{1 - MIN_SUCCESS_RATE:.0%} budget, but only {denominator} hour(s) "
                 f"of sample. Too few to say whether the scheduler or a one-off "
                 f"is at fault; re-run at {MIN_HOURS_TO_BLAME}+ hours.")
        else:
            culprit = ("The scheduler is the problem, not CPCB."
                       if len(not_polled) >= len(unattributable)
                       else "Mostly hours with no run at all — the ingester was "
                            "DOWN, which is a different fault from a dropped tick.")
            fail(f"{ratio:.1%} of capturable hours missed "
                 f"({len(missed)}/{denominator}: {len(not_polled)} not_polled, "
                 f"{len(unattributable)} unattributable) — above the "
                 f"{1 - MIN_SUCCESS_RATE:.0%} budget. {culprit}")
    else:
        ok(f"{ratio:.1%} of capturable hours missed ({len(missed)}/{denominator}), "
           f"within the {1 - MIN_SUCCESS_RATE:.0%} budget")


def check_seed_stations(cur) -> None:
    print("\n4. Seed stations are distinct (pipeline-bug detector)")
    cur.execute(
        """
        SELECT s.station_name, count(o.value_avg), count(DISTINCT o.value_avg)
        FROM observations o JOIN stations s USING (station_id)
        WHERE s.station_name = ANY(%s) AND o.pollutant_id = 'PM2.5'
        GROUP BY s.station_name
        """,
        (list(SEED_STATIONS),),
    )
    got = {name: (n, distinct) for name, n, distinct in cur.fetchall()}

    for name in SEED_STATIONS:
        if name not in got:
            # A failure at any sample size: no rows at all means the testers
            # pinned to this station would receive nothing.
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
        # count(value_avg) above, not count(*) — `identical` counts
        # (observation_ts, value_avg) pairs, so the denominator has to count the
        # same population. With count(*) the two sides counted different things
        # and the ratio below was not a ratio of anything.
        total = min(got[SEED_STATIONS[0]][0], got[SEED_STATIONS[1]][0])
        share = identical / total if total else 0.0
        # Thresholded, not exact equality. Firing only on `identical == total`
        # meant one 'NA' at one station, or one bulletin where one was missing,
        # flipped a genuinely duplicated feed to a pass — and the pass line then
        # printed the evidence of the bug it had just cleared, since "seed series
        # differ (59/60 timestamps coincide)" reads as green. Two stations
        # ~120km apart sharing 90% of their exact integer averages is not
        # weather.
        if total and share > SEED_IDENTICAL_SHARE:
            fail(f"seed stations share {identical}/{total} ({share:.0%}) of their "
                 f"exact PM2.5 values. That is a pipeline bug, not weather — they "
                 f"are ~120km apart.")
        else:
            ok(f"seed series differ ({identical}/{total} = {share:.0%} of "
               f"timestamps coincide, limit {SEED_IDENTICAL_SHARE:.0%})")


def main() -> int:
    conn = connect()
    try:
        with conn.cursor() as cur:
            # A gate must not depend on which machine ran it. date_trunc('hour',
            # <timestamptz>) truncates in the session's timezone and IST is
            # +05:30, a half-hour offset, so hour buckets land 30 minutes apart
            # between this laptop and a UTC runner. Runs at 10:05 and 11:55 UTC
            # give 2/2 hours covered = 100% in UTC, but 2/3 = 67% plus a spurious
            # "1 hour had no run at all" in IST — same data, two verdicts, and
            # check_gaps owns a real pass/fail on it. IST is worse still because
            # its buckets land on :30 UTC, exactly where CPCB's bulletins sit, so
            # a bulletin seconds either side of a boundary falls in a different
            # bucket and two bulletins can share one.
            cur.execute("SET TIME ZONE 'UTC'")
            check_freshness(cur)
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

    # Says only what was actually measured. It claimed "72h of data logged",
    # which no check tests — HOURS_REQUIRED counts distinct bulletins, and at
    # the measured ~18/day, 60 bulletins is ~80 hours rather than 72. A pass
    # line asserting an unchecked number is the same failure as a gate that
    # cannot fail; see check_freshness.
    print(f"GATE 1: PASS — pipeline live, >={HOURS_REQUIRED} bulletins on "
          f">={MIN_STATIONS} stations, scheduler proven, seed stations distinct.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

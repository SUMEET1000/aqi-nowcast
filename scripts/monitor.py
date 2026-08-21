"""Phase 5 monitoring — distribution drift and station staleness.

    python scripts/monitor.py --dry-run    # print everything, write nothing
    python scripts/monitor.py --backfill   # fill drift_log from all of history
    python scripts/monitor.py --self-test  # no database, no network
    python scripts/monitor.py              # what monitor.yml runs, weekly

Two jobs, one Neon wake.

DRIFT is the one with a deadline. In October the stubble-burning season shifts
the PM2.5 distribution hard and a summer-trained model degrades. Build plan §8
wants that written up as a dated post-mortem, and a post-mortem needs a BEFORE
to point at. The before cannot be collected in November, which is why this
lands in August. `--backfill` recovers the whole history in one pass, so the
2025 season is already in the table on day one and this October has a real
prior to be compared against rather than a feeling.

STALENESS answers "has a sensor gone quiet". Cheap to add here, and it shares
the connection.

THE ALERT CHANNEL IS THE EXIT CODE, and that is a decision rather than a
shortcut. GitHub emails on a failed run, so exiting non-zero reaches Sumeet with
no new secret, no new dependency and no Telegram token in a third place. What it
costs: nothing is delivered while the workflow itself is broken. gate1_check.py
is the backstop there, as it already is for the Cloudflare PAT.

BOTH CHECKS FIRE ON CHANGE, NEVER ON STATE. A drift that persists for six weeks
alerts once, on the week it started; a station dark since August alerts once, on
the day it went dark. The alternative is a red workflow every week from October
to February, which teaches the reader to ignore it and then hides the next real
one — the same argument send_alerts.compose makes for the 3h staleness rule.
"""

import argparse
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone

import env  # noqa: F401  — import-time UTF-8 console fix; this file prints µg/m³
from db import connect, redact
from ingest import annotate, clean_detail, step_summary

# Weeks of history the newest week is compared against. The baseline is their
# MEDIAN, not their mean, so one already-drifted week inside the window cannot
# drag the bar up to meet the next one.
BASELINE_WEEKS = 8

# Drift fires when newest_week_mean / median(prior 8 weeks) leaves this band.
#
# 1.5 IS MEASURED, NOT CHOSEN. [measured 2026-08-21: the ratio series over all
# 46 stable-station weeks in pm25_history, 2025-09-22 -> 2026-08-17] Exactly six
# weeks cross 1.5 and every one of them is the stubble season: 2025-10-13 (2.19),
# 10-20 (3.03), 10-27 (2.91), 11-03 (2.33), 11-10 (1.93), plus 2025-09-29 (1.68)
# which the station-count guard below rejects. Ordinary weather never exceeded
# 1.38. So this threshold has been validated against a real drift event that
# already happened, which is the only way to know it is not decoration.
#
# THE LOW SIDE HAS NEVER FIRED and is weaker evidence. The lowest ratio in the
# record is 0.55 (2026-08-03, the monsoon clean-out), so 0.5 is set just past
# it: it means "cleaner than anything on record", not "monsoon arrived". Treat a
# low-side fire as unexplained until something explains it.
DRIFT_RATIO_HIGH = 1.5
DRIFT_RATIO_LOW = 0.5

# The ratio is computed over the stations the current week and the baseline
# weeks HAVE IN COMMON, never over whatever each week happened to contain.
#
# THIS IS NOT A REFINEMENT, IT IS THE DIFFERENCE BETWEEN A MONITOR THAT WORKS
# AND ONE THAT DOES NOT. [measured 2026-08-21: replaying the check over all 77
# backfilled weeks] pm25_history starts at 7 stations, falls to 1 through
# mid-2025, and ramps 1 -> 9 -> 22 -> 29 across September and October 2025 —
# i.e. the network was being built out during the exact weeks of the stubble
# season. A guard that merely rejected weeks whose station count had moved
# threw away 2025-09-22 through 2025-11-10 and so ALERTED ON NOTHING AT ALL for
# the one event this file exists to catch, while still firing three times on
# 1-station weeks in mid-2025. Both faults have the same cause: comparing a
# mean over one set of stations against a mean over a different set.
#
# Intersecting first removes the confound instead of detecting it, and the
# minimum below is then a real floor rather than a stability test. Replayed over
# the same 77 weeks it raises EXACTLY ONE alert — 2025-10-20, ratio 2.71 — and
# classifies 38 weeks normal and 27 as having no comparable set.
#
# One honest caveat: that alert lands a week after the rise actually began
# (2025-10-13 was already 2.19 on the all-station mean, but too few of its 28
# stations had reported in half the baseline weeks to be compared). That lag is
# an artefact of the network being built out in 2025, not of the design — the
# roster has held at 28-30 since 2025-09-29, so every station now clears
# BASELINE_PRESENCE. Expect no lag this October, but do not claim it was proven.
MIN_COMMON_STATIONS = 5

# A station counts toward the common set if it reported in at least this share
# of the baseline weeks. Not all of them: these sensors flicker, and requiring a
# perfect attendance record over 8 weeks would empty the set on a normal month.
BASELINE_PRESENCE = 0.5

# A station with no non-NULL PM2.5 for this long is dark.
#
# 12 and not 3: CPCB's feed freezes every morning, the last bulletin before it
# is 05:00 IST and the next is between 10:00 and 13:00, so a gap of up to 8h is
# routine and a 3h rule would report all 30 stations dark every single morning.
# [measured 2026-08-13: 66 bulletins, four consecutive days, no exception]
MAX_DARK_HOURS = 12

# pm25_history is a bulk archive, not a live feed, and OpenAQ publishes it
# behind real time — 19h on 2026-08-21. 36h is that plus a missed daily run.
# Past this, drift is being computed on a series that stopped advancing, which
# is the failure mode where every number stays reassuringly normal.
MAX_HISTORY_LAG_H = 36

# fetch_log outcomes owned by this script. Deliberately outside
# gate1_check.RUN_OUTCOMES and ANOMALY_OUTCOMES, which are explicit whitelists,
# for the same reason send_alerts.py's are: this run is observable in the same
# table without moving the ingester's success rate, which Gate 1 is computed
# from. Do not add them to either tuple.
OUTCOME_OK = "monitor_ok"
OUTCOME_ALERT = "monitor_alert"
OUTCOME_CRASH = "monitor_crash"

LOG = """
INSERT INTO fetch_log (station_id, outcome, rows_returned, bulletin_ts, error_detail)
VALUES (%s, %s, %s, %s, %s)
"""

# Exact 0.0 is a dead sensor, not clean air, and 2.27% of the table is exactly
# zero. Excluded here for the reason db/schema.sql gives above drift_log.
SNAPSHOT = """
INSERT INTO drift_log (week_start, station_id, n, mean, std, p50, p90, p99, max_value)
SELECT date_trunc('week', observation_ts)::date,
       station_id,
       count(*),
       avg(value),
       coalesce(stddev_samp(value), 0),
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY value),
       percentile_cont(0.9)  WITHIN GROUP (ORDER BY value),
       percentile_cont(0.99) WITHIN GROUP (ORDER BY value),
       max(value)
FROM pm25_history
WHERE value <> 0
  AND observation_ts >= %s
GROUP BY 1, 2
ON CONFLICT (week_start, station_id) DO UPDATE SET
    n         = EXCLUDED.n,
    mean      = EXCLUDED.mean,
    std       = EXCLUDED.std,
    p50       = EXCLUDED.p50,
    p90       = EXCLUDED.p90,
    p99       = EXCLUDED.p99,
    max_value = EXCLUDED.max_value
"""

# Per station, not pre-aggregated, because drift_verdict has to intersect the
# station sets before it averages anything.
#
# Complete weeks only. The current week is written by SNAPSHOT like any other —
# a partial week that is 40% of the way through reads as a 60% drop, so it is
# excluded here, at read time, rather than withheld on write.
SERIES = """
SELECT week_start, station_id, mean, p99
FROM drift_log
WHERE week_start < date_trunc('week', now())::date
ORDER BY week_start, station_id
"""

# `value_avg IS NOT NULL` is load-bearing and is the same trap gate1_check
# .check_coverage documents: ingest.py writes a row per pollutant per bulletin,
# NULL where CPCB sent 'NA', so a station whose sensor has been dark for a month
# still has rows. A row is not a reading.
DARK = """
SELECT s.station_id, s.station_name,
       max(o.observation_ts) FILTER (WHERE o.value_avg IS NOT NULL)
FROM stations s
LEFT JOIN observations o
       ON o.station_id = s.station_id AND o.pollutant_id = 'PM2.5'
WHERE s.is_active
GROUP BY s.station_id, s.station_name
ORDER BY s.station_id
"""

failures: list[str] = []
alerts: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)
    print(f"  FAIL  {msg}")


def alert(msg: str) -> None:
    alerts.append(msg)
    print(f"  ALERT {msg}")


def ok(msg: str) -> None:
    print(f"  PASS  {msg}")


def skip(msg: str) -> None:
    print(f"  SKIP  {msg}")


def drift_verdict(weeks: list[tuple], i: int) -> tuple[str, float, float]:
    """Classify week `weeks[i]` against the BASELINE_WEEKS before it.

    weeks is [(week_start, {station_id: mean}), ...] oldest first. Returns
    (status, ratio, baseline) where status is one of: short, roster, high, low,
    normal.

    Pure, and it is the entire decision — so self_test can drive it with a
    fabricated October and prove the check can fail, and so the same function
    can be replayed over the backfilled history to show which weeks it would
    have alerted on.

    Both sides of the ratio are means over `common`, the SAME set of stations.
    See MIN_COMMON_STATIONS.
    """
    if i < BASELINE_WEEKS:
        return "short", 0.0, 0.0

    prior = weeks[i - BASELINE_WEEKS:i]
    current = weeks[i][1]

    seen: dict[int, int] = {}
    for _, means in prior:
        for station_id in means:
            seen[station_id] = seen.get(station_id, 0) + 1
    need = len(prior) * BASELINE_PRESENCE
    common = {s for s in current if seen.get(s, 0) >= need}
    if len(common) < MIN_COMMON_STATIONS:
        return "roster", 0.0, 0.0

    def over_common(means: dict) -> float | None:
        vals = [means[s] for s in common if s in means]
        return statistics.mean(vals) if vals else None

    baseline_vals = [v for v in (over_common(m) for _, m in prior) if v is not None]
    if len(baseline_vals) < need:
        return "roster", 0.0, 0.0

    baseline = statistics.median(baseline_vals)
    if baseline <= 0:
        return "short", 0.0, 0.0

    ratio = over_common(current) / baseline
    if ratio >= DRIFT_RATIO_HIGH:
        return "high", ratio, baseline
    if ratio <= DRIFT_RATIO_LOW:
        return "low", ratio, baseline
    return "normal", ratio, baseline


def snapshot(cur, backfill: bool) -> int:
    """Upsert weekly per-station distributions. Returns rows written."""
    # Without --backfill, still rewrite the last 10 weeks rather than only the
    # current one. OpenAQ backfills late and backfill_openaq.py runs a two-day
    # window, so a week's numbers keep moving for days after it ends. Rewriting
    # is free — the upsert is keyed on (week, station).
    since = "1970-01-01" if backfill else \
        (datetime.now(timezone.utc) - timedelta(weeks=10)).date().isoformat()
    cur.execute(SNAPSHOT, (since,))
    return cur.rowcount


def load_weeks(cur) -> tuple[list[tuple], dict]:
    """(week_start, {station_id: mean}) oldest first, plus per-week mean p99."""
    cur.execute(SERIES)
    weeks: list[tuple] = []
    p99s: dict = {}
    for week_start, station_id, mean, p99 in cur.fetchall():
        if not weeks or weeks[-1][0] != week_start:
            weeks.append((week_start, {}))
            p99s[week_start] = []
        weeks[-1][1][station_id] = float(mean)
        p99s[week_start].append(float(p99))
    return weeks, {w: statistics.mean(v) for w, v in p99s.items()}


def check_drift(cur) -> None:
    """Did the newest complete week's distribution move, and is that new?"""
    print("\n1. Distribution drift (build plan §8)")
    weeks, p99s = load_weeks(cur)

    if len(weeks) <= BASELINE_WEEKS:
        skip(f"{len(weeks)} complete week(s) in drift_log, need more than "
             f"{BASELINE_WEEKS} before a baseline means anything. Run "
             f"--backfill: the history reaches 2025-02-18.")
        return

    latest = len(weeks) - 1
    status, ratio, baseline = drift_verdict(weeks, latest)
    week, means = weeks[latest]
    mean = statistics.mean(means.values())

    print(f"    week {week}  {len(means)} stations  mean {mean:.1f} µg/m³  "
          f"p99 {p99s[week]:.1f}  baseline {baseline:.1f}  ratio {ratio:.2f}")

    if status == "short":
        skip("not enough comparable history for a baseline")
        return
    if status == "roster":
        skip(f"fewer than {MIN_COMMON_STATIONS} stations reported in both this "
             f"week and at least {BASELINE_PRESENCE:.0%} of the baseline weeks. "
             f"There is no common set to compare, so any ratio would be "
             f"measuring the roster rather than the air.")
        return
    if status == "normal":
        ok(f"mean {mean:.1f} is {ratio:.2f}x the trailing {BASELINE_WEEKS}-week "
           f"median (band {DRIFT_RATIO_LOW}-{DRIFT_RATIO_HIGH})")
        return

    # Crossed. Alert only if last week had not already crossed the same way —
    # otherwise every week of the stubble season is a fresh red workflow.
    previous = drift_verdict(weeks, latest - 1)[0] if latest else "short"
    if previous == status:
        ok(f"drift {status.upper()} continues from {weeks[latest - 1][0]} "
           f"(ratio {ratio:.2f}) — already reported, not re-alerting")
        return

    direction = "ABOVE" if status == "high" else "BELOW"
    alert(f"DRIFT: week {week} mean {mean:.1f} µg/m³ is {ratio:.2f}x the "
          f"trailing {BASELINE_WEEKS}-week median of {baseline:.1f} — "
          f"{direction} the band. p99 {p99s[week]:.1f}. This is the incident. Write it "
          f"up in docs/incidents/ while it is fresh (build plan §8/§12), and "
          f"record the model's error before and after.")


def check_stale(cur, now: datetime, dry_run: bool) -> None:
    """Which stations just went quiet, and is the history feed still moving?"""
    print("\n2. Station staleness")

    cur.execute("SELECT station_id, was_dark FROM station_health")
    was_dark = {sid: dark for sid, dark in cur.fetchall()}
    first_run = not was_dark

    cur.execute(DARK)
    rows = cur.fetchall()
    now_dark, recovered, live = [], [], 0

    for station_id, name, last_seen in rows:
        age_h = None if last_seen is None else \
            (now - last_seen).total_seconds() / 3600
        dark = age_h is None or age_h > MAX_DARK_HOURS

        if dark:
            now_dark.append((station_id, name, age_h))
        else:
            live += 1
        # .get default False: a station added to the roster since the last run
        # is treated as previously live, so if it is dark now that is news.
        if not dark and was_dark.get(station_id, False):
            recovered.append((station_id, name))

        if not dry_run:
            cur.execute(
                """
                INSERT INTO station_health (station_id, last_seen, was_dark, checked_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (station_id) DO UPDATE SET
                    last_seen  = EXCLUDED.last_seen,
                    was_dark   = EXCLUDED.was_dark,
                    checked_at = EXCLUDED.checked_at
                """,
                (station_id, last_seen, dark),
            )

    for station_id, name in recovered:
        print(f"    recovered: {station_id} {name}")

    newly = [d for d in now_dark if not was_dark.get(d[0], False)]

    print(f"    {live} live, {len(now_dark)} dark (limit {MAX_DARK_HOURS}h), "
          f"{len(recovered)} recovered")
    for station_id, name, age_h in now_dark:
        age = "never reported" if age_h is None else f"{age_h:.1f}h ago"
        mark = "NEW" if any(d[0] == station_id for d in newly) else "known"
        print(f"    dark: {station_id:>3} {name:<48} {age} ({mark})")

    if first_run and now_dark:
        # The table was empty, so every dark station looks new. Saying "3
        # stations just went dark" on the first run would be a lie the very
        # first time this file speaks, and the reader has no way to tell.
        skip(f"first run — station_health was empty, so {len(now_dark)} dark "
             f"station(s) are recorded as the baseline rather than alerted on. "
             f"The next run compares against this.")
    elif newly:
        for station_id, name, age_h in newly:
            age = "has never reported" if age_h is None else f"last seen {age_h:.1f}h ago"
            alert(f"STATION DARK: {station_id} {name} — {age}. Subscribers to "
                  f"this station get nothing. Check whether it left the CPCB "
                  f"feed entirely (ingest.py logs station_missing) or is "
                  f"reporting 'NA'.")
    else:
        ok(f"no station changed state since the last run")

    # The drift half is computed from pm25_history, so a frozen archive makes
    # every number above look reassuringly normal. This is the check that says
    # the input stopped moving.
    cur.execute("SELECT max(observation_ts) FROM pm25_history")
    newest = cur.fetchone()[0]
    if newest is None:
        fail("pm25_history is empty — drift has nothing to measure. Run "
             "scripts/backfill_openaq.py.")
        return
    lag_h = (now - newest).total_seconds() / 3600
    if lag_h > MAX_HISTORY_LAG_H:
        fail(f"pm25_history is {lag_h:.1f}h behind (limit {MAX_HISTORY_LAG_H}h). "
             f"Its newest hour is {newest:%Y-%m-%d %H:%M} UTC. Every drift "
             f"number above is computed on a series that has stopped "
             f"advancing. Check the 'Pull OpenAQ hourly PM2.5' step in "
             f"ingest.yml — it fails every run when OPENAQ_API_KEY is missing "
             f"from the repository secrets, and the CPCB half still succeeds, "
             f"so the job stays green-ish while this quietly freezes.")
    else:
        ok(f"pm25_history is {lag_h:.1f}h behind real time")


def self_test() -> int:
    """Prove both decisions can actually fail. No database, no network.

    This repo has a scar: five separate checks could not fail and all five
    reported PASS, and every one of them had been written as evidence. The test
    for a new check is what input makes it print FAIL — if the answer needs a
    code change, it is not a check.
    """
    print("Self-test — can these checks fail?\n")
    bad = 0

    def case(name: str, got, want) -> None:
        nonlocal bad
        if got == want:
            print(f"  PASS  {name}: {got}")
        else:
            print(f"  FAIL  {name}: got {got}, want {want}")
            bad += 1

    def series(means: list[float], stations: int = 29) -> list[tuple]:
        return [(f"w{i}", {s: m for s in range(stations)})
                for i, m in enumerate(means)]

    flat = [60.0, 62.0, 58.0, 61.0, 59.0, 63.0, 60.0, 62.0]

    case("flat series is normal",
         drift_verdict(series(flat + [61.0]), 8)[0], "normal")

    # The real shape: 2025-10-06 mean 41.1 against a 32.0 baseline, then
    # 2025-10-13 at 80.9 — ratio 2.19.
    case("October spike fires",
         drift_verdict(series(flat + [130.0]), 8)[0], "high")

    # Week two of the same season must stay quiet. This is the property that
    # decides whether the reader still trusts a red workflow in November.
    sustained = series(flat + [130.0, 135.0])
    case("sustained spike still classifies high",
         drift_verdict(sustained, 9)[0], "high")
    case("...and week 1 also high, so check_drift suppresses week 2",
         drift_verdict(sustained, 8)[0], "high")

    case("collapse fires low",
         drift_verdict(series(flat + [25.0]), 8)[0], "low")

    # 1.38 was the highest ordinary week on record. It must not fire.
    case("highest ordinary week on record does not fire",
         drift_verdict(series(flat + [60.5 * 1.38]), 8)[0], "normal")

    # THE REGRESSION THAT MATTERS. Replayed over the real backfill, an earlier
    # version of this check rejected every week from 2025-09-22 to 2025-11-10 —
    # the whole stubble season — because the station roster was ramping 1 -> 29
    # at the same time. It alerted on nothing. Here the roster grows 6 -> 29
    # while the air also spikes, and the six long-standing stations are enough
    # to carry the comparison, so the spike must still be seen.
    growing = series(flat, stations=6) + [("w8", {s: 130.0 for s in range(29)})]
    case("spike is still seen while the roster is growing",
         drift_verdict(growing, 8)[0], "high")

    # ...but a genuinely disjoint roster has nothing to compare and must say so
    # rather than invent a ratio.
    disjoint = series(flat, stations=6) + [("w8", {s: 130.0 for s in range(50, 79)})]
    case("disjoint roster is rejected, not alerted",
         drift_verdict(disjoint, 8)[0], "roster")

    # The three false alarms the same replay found, all on weeks where only one
    # station reported at all: 2025-07-07 (0.42), 08-04 (1.56), 08-18 (1.57).
    case("a one-station week cannot raise an alarm",
         drift_verdict(series(flat, stations=1) + [("w8", {0: 130.0})], 8)[0],
         "roster")

    case("short history is skipped",
         drift_verdict(series([60.0, 61.0]), 1)[0], "short")

    # Staleness is a set difference, and the property that matters is that an
    # already-dark station is silent.
    was = {26: True, 21: False}
    dark_now = [(26, "Narnaul", 400.0), (21, "Panipat", 30.0)]
    newly = [d for d in dark_now if not was.get(d[0], False)]
    case("already-dark station does not re-alert", [d[0] for d in newly], [21])

    print()
    if bad:
        print(f"SELF-TEST: FAIL ({bad} case(s))")
        return 1
    print("SELF-TEST: PASS — drift discriminates October from ordinary weather, "
          "and a known-dark station stays quiet.")
    return 0


def log_row(conn, outcome, *, rows_returned=None, error_detail=None) -> None:
    with conn.cursor() as cur:
        cur.execute(LOG, (None, outcome, rows_returned, None,
                          clean_detail(error_detail)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backfill", action="store_true",
                        help="rebuild drift_log from all of pm25_history "
                             "(2025-02-18 onward), not just recent weeks")
    parser.add_argument("--dry-run", action="store_true",
                        help="print everything, write nothing, exit 0")
    parser.add_argument("--self-test", action="store_true",
                        help="prove the checks can fail; no database")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    conn = connect()
    try:
        with conn.cursor() as cur:
            # Same reason gate1_check.main() gives: date_trunc('week', ...)
            # truncates in the session timezone, IST is a half-hour offset, and
            # a monitor that reports different weeks on a laptop and on a runner
            # is not a monitor.
            cur.execute("SET TIME ZONE 'UTC'")
            now = datetime.now(timezone.utc)

            print(f"monitor — {now:%Y-%m-%d %H:%M} UTC"
                  f"{'  (dry run, nothing written)' if args.dry_run else ''}")

            print("\n0. Snapshot")
            if args.dry_run:
                skip("would upsert drift_log")
                written = 0
            else:
                written = snapshot(cur, args.backfill)
                ok(f"{written} station-week row(s) written to drift_log"
                   f"{' (full history)' if args.backfill else ''}")
                conn.commit()

            check_drift(cur)
            check_stale(cur, now, args.dry_run)

            if not args.dry_run:
                conn.commit()

        print()
        summary = (f"drift+staleness: {len(alerts)} alert(s), "
                   f"{len(failures)} failure(s), {written} row(s) snapshotted")
        print(summary)

        for a in alerts:
            annotate("warning", a)
        for f in failures:
            annotate("error", f)
        step_summary(summary + ("".join(f"\n- {m}" for m in alerts + failures)))

        if args.dry_run:
            return 0

        # One fetch_log row either way, so a run that found nothing is still
        # evidence it looked. outcome distinguishes them.
        outcome = OUTCOME_ALERT if (alerts or failures) else OUTCOME_OK
        log_row(conn, outcome, rows_returned=written,
                error_detail=" | ".join(alerts + failures) or
                f"run={os.environ.get('GITHUB_RUN_ID', 'local')}")
        conn.commit()

        # Non-zero is the alert channel — GitHub emails on a failed run. See
        # the module docstring.
        return 1 if (alerts or failures) else 0
    finally:
        conn.close()


def run() -> int:
    """main(), plus a last-resort record of anything it failed to catch.

    Same argument as ingest.run() and send_alerts.run(): a run that dies without
    a fetch_log row is invisible, and a monitor nobody can tell has stopped is
    worse than no monitor, because its silence reads as good news.
    """
    try:
        return main()
    except BaseException as e:
        detail = redact(f"{type(e).__name__}: {e}")
        annotate("error", f"CRASH: {detail}")
        step_summary(f"CRASH: {detail}")
        try:
            conn = connect()
            try:
                log_row(conn, OUTCOME_CRASH,
                        error_detail=f"run={os.environ.get('GITHUB_RUN_ID', 'local')} "
                                     f"{detail}")
                conn.commit()
                print(f"  recorded as fetch_log outcome='{OUTCOME_CRASH}'",
                      file=sys.stderr)
            finally:
                conn.close()
        except BaseException as log_error:
            print(f"  could not record the crash either: "
                  f"{redact(str(log_error))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())

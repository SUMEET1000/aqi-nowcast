"""Phase 5 — can the alert be served from CPCB instead of OpenAQ?

    python scripts/probe_cpcb_signal.py             # exit code is the verdict
    python scripts/probe_cpcb_signal.py --self-test # prove the checks can fail

Read-only. One Neon wake. Stdlib plus psycopg — deliberately NO pandas, numpy or
lightgbm, so this runs on `requirements.txt` alone like the other probes.

THE QUESTION
------------
`classify.py --tail --stale 7` measured what the 12h win becomes on the input a
07:00 IST send actually has: margin +0.114 -> +0.084 against a fold std of 0.116,
i.e. 1 of 4 horizons to 0 of 4 `[measured 2026-08-22]`. OpenAQ publishes ~6.5h
late, so the model is asked to forecast the evening from the previous evening.

CPCB's `observations` is fresh every 30 minutes. The obvious move is to serve
from it instead. The obvious move has a catch: `value_avg` is an AQI SUB-INDEX
OF A 24-HOUR MEAN, not a concentration `[measured 2026-08-19: compare_sources.py,
4 stations, ~180h each]`. A 24h mean dilutes an evening spike 24:1, and the
features that won stage 3 are spike-shaped — rolling max, trend, z-score,
hours-since-exceed. On a 24h mean those go near-flat.

So the real question is a TRADE, not a preference: is fresh-but-smoothed better
than sharp-but-7h-stale, at the thing the product does?

WHY THIS RUNS BEFORE ANY MODEL IS BUILT
---------------------------------------
`observations` starts 2026-08-09, which is Phase 1 go-live, and CPCB publishes no
archive. That is ~6,800 readings against pm25_history's 115,556 rows at h=12
`[measured 2026-08-22]`. Noise scales about as 1/sqrt(n), so a model trained on
it would be graded with a ruler wider than the margin being hunted, and could not
be believed either way until roughly November.

This probe needs 13 days, not three months, because it grades no model. It
compares two SERVE-TIME INPUTS head to head on rank, which is threshold-free and
therefore unit-free — the only way to put a sub-index and a concentration on one
axis without inventing a conversion.

CHECK 2 IS THE ONE THIS SCRIPT EXISTS FOR. Checks 1 and 3 can each kill the idea
cheaply, but only check 2 can say the trade is worth making.
"""

import argparse
import statistics
import sys
from datetime import timezone

import env  # noqa: F401  — import-time UTF-8 console fix
from db import connect
from forecast import MAX_ISSUE_AGE_H

# The horizon and label the stage-3 table is scored on. Restated rather than
# imported: classify.py pulls in features.py, which imports pandas at module
# level, and this probe is meant to run without requirements-model.txt.
HORIZON_H = 12
WINDOW_H = 3
EVENT_ABOVE = 121.0          # CPCB's Very Poor band

# The lag classify.py --stale 7 was run at, so the two answers are about the
# same rival. OpenAQ was ~6.5h behind for 27 of 30 stations on 2026-08-22.
STALE_H = 7

# 01:30 UTC is the send. The cron in send_alerts.yml is `30 1 * * *`, and every
# bulletin is stamped at :30 past the UTC hour.
SEND_HOUR_UTC = 1

# Below this the probe reaches no verdict. A rank comparison decided by forty
# events is decided by which two bad evenings fell inside 13 days — the same
# failure that made per-station routing lose 16 of 16 cells.
MIN_EVENTS = 100

# A candidate must clear this on its own before beating the rival counts.
# JUDGEMENT, not measurement, and it must keep saying so — replace it once real
# runs exist. Beating the rival alone is not enough: the self-test's flat input
# scores 0.500, carries no information whatever, and still "wins" whenever the
# stale concentration lands anti-correlated at 0.476. Without this floor the
# check would promote an input that says nothing.
MIN_AUC = 0.55

# Hour-to-hour movement, as a fraction of the same series' own 24h movement.
# A perfect 24h trailing mean still moves hour to hour, so the floor is not zero;
# what would kill the spike features is movement far below OpenAQ's.
MIN_JITTER_RATIO = 0.25


def auc(scores: list[tuple[float, bool]]) -> float:
    """Probability a random event outranks a random non-event. Ties count half.

    Rank-based, so it compares a sub-index against a concentration without a
    conversion between them — which is the whole reason this probe can answer
    anything at 13 days. 0.5 is a coin flip; below 0.5 the input is
    anti-correlated with the event.
    """
    ranked = sorted(scores, key=lambda pair: pair[0])
    ranks, i = [0.0] * len(ranked), 0
    while i < len(ranked):
        j = i
        while j + 1 < len(ranked) and ranked[j + 1][0] == ranked[i][0]:
            j += 1
        shared = (i + j) / 2 + 1          # average rank, 1-based
        for k in range(i, j + 1):
            ranks[k] = shared
        i = j + 1
    pos = [r for r, (_, is_event) in zip(ranks, ranked) if is_event]
    n_pos, n_neg = len(pos), len(ranked) - len(pos)
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    return (sum(pos) - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def load(cur) -> tuple[dict, dict]:
    """Both series as {station_id: {epoch_hour: value}}. One wake, two queries.

    Both sides go through the identical epoch-hour expression. CPCB stamps on the
    hour in IST and OpenAQ buckets these sensors the same way, so both land at :30
    past the UTC hour and join on the raw timestamp. Truncating ONE side moves it
    to :00 and produces zero shared hours out of hundreds — a defensive alignment
    causing the exact misalignment it was added to prevent.
    """
    def hour(ts):
        return int(ts.astimezone(timezone.utc).timestamp()) // 3600

    cur.execute("""
        SELECT station_id, observation_ts, value_avg
        FROM observations
        WHERE pollutant_id = 'PM2.5' AND value_avg IS NOT NULL
    """)
    cpcb = {}
    for station, ts, value in cur.fetchall():
        cpcb.setdefault(station, {})[hour(ts)] = float(value)

    cur.execute("""
        SELECT station_id, observation_ts, value
        FROM pm25_history
        WHERE observation_ts >= (SELECT min(observation_ts) FROM observations)
                                - interval '2 days'
    """)
    openaq = {}
    for station, ts, value in cur.fetchall():
        # An exact 0.0 is a dead sensor, not clean air: 58 percent of them sit in
        # runs longer than 24h and 81 percent start after a reading above 20.
        # features.DROP_EXACT_ZERO masks them; masking here keeps the two paths
        # agreeing about which hours exist.
        if float(value) != 0.0:
            openaq.setdefault(station, {})[hour(ts)] = float(value)
    return cpcb, openaq


def check_freshness(cpcb: dict) -> bool:
    """CHECK 1 — is CPCB actually fresh at 01:30 UTC, which is 07:00 IST?

    B's entire reason to exist. CPCB's feed freezes overnight — the last morning
    bulletin is 05:00 IST and the next is between 10:00 and 13:00
    `[measured 2026-08-13: 66 bulletins, four days, no exceptions]` — so 07:00 IST
    should sit about 2.0h behind. That was measured on BULLETIN timestamps, never
    at the send hour specifically, and the freshness number this whole problem
    came from was also a measurement read as answering a question it never asked.
    """
    print(f"\n=== CHECK 1 — CPCB age at {SEND_HOUR_UTC:02d}:30 UTC (07:00 IST) ===")
    hours = sorted({h for series in cpcb.values() for h in series})
    if not hours:
        print("  FAIL — no CPCB PM2.5 rows at all.")
        return False

    ages, sends = [], [h for h in range(hours[0], hours[-1] + 1)
                       if h % 24 == SEND_HOUR_UTC]
    for send in sends:
        newest = [max((h for h in series if h <= send), default=None)
                  for series in cpcb.values()]
        fresh = [send - h for h in newest if h is not None]
        if fresh:
            ages.append(statistics.median(fresh))

    if not ages:
        print("  UNTESTABLE — no send hour falls inside the collected range.")
        return False
    worst, median = max(ages), statistics.median(ages)
    print(f"  {len(ages)} send hours   median age {median:.1f}h   worst {worst:.1f}h"
          f"   limit {MAX_ISSUE_AGE_H}h")
    if median > MAX_ISSUE_AGE_H:
        print(f"  FAIL — CPCB is not fresh at the send hour either. B has no "
              f"reason to exist; the lag is not OpenAQ's alone.")
        return False
    print("  CPCB clears the staleness guard at the send hour.")
    return True


def check_head_to_head(cpcb: dict, openaq: dict) -> bool | None:
    """CHECK 2 — fresh-and-smoothed against sharp-and-stale, on rank.

    At issue hour t the two candidate serve-time inputs are CPCB's value_avg at t
    and OpenAQ's concentration at t - STALE_H. The truth is OpenAQ's own
    concentration over the WINDOW_H hours from t + HORIZON_H, which is the label
    the stage-3 table is scored on.

    Both inputs are graded on the SAME rows — a row is admitted only when both can
    answer. That is the fairness rule the whole benchmark rests on: scoring each
    candidate over whatever hours it happens to reach once gave persistence 49,993
    pairs against climatology's 7,534, which is two numbers off two different
    exams.

    Returns None when the sample is too small to decide, never a pass.
    """
    print(f"\n=== CHECK 2 — predicting a >{EVENT_ABOVE:.0f} evening {HORIZON_H}h out ===")
    fresh, stale, smoothed, events = [], [], [], 0
    for station, truth in openaq.items():
        if station not in cpcb:
            continue
        for issue in sorted(truth):
            window = [truth[h] for h in
                      range(issue + HORIZON_H, issue + HORIZON_H + WINDOW_H)
                      if h in truth]
            if len(window) < WINDOW_H:
                continue
            here, back = cpcb[station].get(issue), truth.get(issue - STALE_H)
            # THE CONFOUND CONTROL. value_avg is a 24h mean, and averaging alone
            # lifts a ranking by cancelling hour-to-hour noise. Without a stale
            # SMOOTHED rival on the same rows, a win here cannot be attributed to
            # freshness rather than to smoothing — and smoothing is free, since
            # OpenAQ can be averaged without changing source.
            trailing = [truth[h] for h in range(issue - STALE_H - 23,
                                                issue - STALE_H + 1) if h in truth]
            if here is None or back is None or len(trailing) < 12:
                continue
            is_event = max(window) > EVENT_ABOVE
            fresh.append((here, is_event))
            stale.append((back, is_event))
            smoothed.append((sum(trailing) / len(trailing), is_event))
            events += is_event

    print(f"  {len(fresh):,} rows on {len(set(openaq) & set(cpcb))} stations, "
          f"{events:,} events ({events / max(len(fresh), 1):.1%})")
    if events < MIN_EVENTS:
        print(f"  UNTESTABLE — under {MIN_EVENTS} events. A rank comparison "
              f"decided by this many is decided by which evenings landed in the "
              f"window. Re-run when the history is longer.")
        return None

    return decide(auc(fresh), auc(stale), auc(smoothed), events)


def decide(a_fresh: float, a_stale: float, a_smooth: float,
           events: int) -> bool | None:
    """The verdict of check 2, as arithmetic on four numbers and nothing else.

    Split out from the measurement so every branch is reachable from a test with
    three AUCs, rather than only through a synthetic time series contrived to
    produce them. Building that series was harder than the rule it was meant to
    exercise, and the version that "passed" was rejecting on the WRONG branch —
    a check that cannot fail on its own condition is the defect this repo has
    already found five times.
    """
    print(f"  CPCB value_avg, fresh at issue          AUC {a_fresh:.3f}")
    print(f"  OpenAQ ug/m3, {STALE_H}h stale                AUC {a_stale:.3f}")
    print(f"  OpenAQ 24h mean, {STALE_H}h stale (control)   AUC {a_smooth:.3f}")
    print(f"  margin over stale raw {a_fresh - a_stale:+.3f}   "
          f"over stale smoothed {a_fresh - a_smooth:+.3f}   floor {MIN_AUC:.2f}")
    if events < MIN_EVENTS:
        print(f"  UNTESTABLE — under {MIN_EVENTS} events.")
        return None
    if a_fresh < MIN_AUC:
        print(f"  FAIL — the fresh sub-index is under {MIN_AUC:.2f}, i.e. barely "
              f"better than a coin flip about the evening. It can still 'beat' a "
              f"stale reading that lands anti-correlated, and that is not a "
              f"reason to build on it.")
        return False
    if a_fresh <= a_stale:
        print("  FAIL — the fresh sub-index ranks evenings no better than the "
              "stale concentration. Freshness does not pay for the smoothing, so "
              "B buys nothing and costs a retrain on an input with no history.")
        return False
    # An AUC computed from n_pos events carries roughly this much sampling error.
    # Comparing the margin against it rather than against a constant keeps the
    # gate honest as the history grows: at 114 events it demands ~0.03, and it
    # will demand less in November without anyone editing a number. Conservative
    # and approximate — the positives dominate the variance here, and the two
    # AUCs share rows, so part of this error cancels and the true bar is lower.
    noise = (a_fresh * (1 - a_fresh) / events) ** 0.5
    print(f"  sampling error on {events} events is about {noise:.3f}")
    if a_fresh - a_smooth <= noise:
        print("  FAIL — the win is SMOOTHING, not freshness. A 24h mean of the "
              "STALE OpenAQ series ranks evenings within sampling error of the "
              "fresh sub-index. features.py already builds roll_mean_24, so the "
              "model has that information today and changing source adds "
              "nothing it does not already hold.")
        return False
    print("  The fresh sub-index ranks evenings better than both rivals, "
          "including the stale-but-smoothed control, so the gain is freshness "
          "rather than averaging. How much of it a model converts is a separate "
          "question needing more data.")
    return True


def check_jitter(cpcb: dict, openaq: dict) -> None:
    """CHECK 3 — does value_avg move hour to hour, or is it a flat line?

    Not pass/fail, and deliberately not part of the verdict. It measures each
    series against ITSELF — median one-hour movement over median 24-hour movement
    — so a sub-index and a concentration are comparable without a conversion.

    What it is for: the stage-3 win came from spike-shaped features, and a 24h
    mean is a low-pass filter. A low ratio says those columns would be near-flat
    on CPCB even if check 2 passes, so B would need a different feature set
    rather than the same one pointed at a new table.
    """
    print("\n=== CHECK 3 — hour-to-hour movement, each series against itself ===")

    def jitter(series: dict[int, float]) -> float | None:
        step = [abs(series[h] - series[h - 1]) for h in series if h - 1 in series]
        day = [abs(series[h] - series[h - 24]) for h in series if h - 24 in series]
        if len(step) < 24 or len(day) < 24 or statistics.median(day) == 0:
            return None
        return statistics.median(step) / statistics.median(day)

    ratios = {"CPCB value_avg": [], "OpenAQ ug/m3": []}
    for station in sorted(set(cpcb) & set(openaq)):
        for label, series in (("CPCB value_avg", cpcb[station]),
                              ("OpenAQ ug/m3", openaq[station])):
            value = jitter(series)
            if value is not None:
                ratios[label].append(value)

    for label, values in ratios.items():
        if values:
            print(f"  {label:<16} median {statistics.median(values):.3f} "
                  f"over {len(values)} stations")
        else:
            print(f"  {label:<16} no station has enough consecutive hours")

    both = [v for v in ratios.values() if v]
    if len(both) == 2:
        share = statistics.median(ratios["CPCB value_avg"]) / statistics.median(
            ratios["OpenAQ ug/m3"])
        print(f"  CPCB moves {share:.2f}x as much of its own daily range per hour")
        if share < MIN_JITTER_RATIO:
            print(f"  NOTE — under {MIN_JITTER_RATIO}. The spike-shaped features "
                  f"(rolling max, trend, z-score, hours-since-exceed) have little "
                  f"to read here. B would be a new feature set, not a new table.")


def self_test() -> int:
    """Prove each decision can go the other way, on inputs with known answers.

    Five checks in this repo once could not fail and all five reported PASS. The
    tell is a pass condition derivable from the code alone, so every case below
    names the input that must break it.
    """
    print("=== SELF-TEST ===")
    failures = []

    def check(label, got, want):
        ok = got == want or (isinstance(want, float) and abs(got - want) < 1e-9)
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got}, want {want}")
        if not ok:
            failures.append(label)

    check("a perfect ranker scores 1.0",
          auc([(1.0, False), (2.0, False), (3.0, True), (4.0, True)]), 1.0)
    check("a reversed ranker scores 0.0",
          auc([(1.0, True), (2.0, True), (3.0, False), (4.0, False)]), 0.0)
    check("a constant ranker scores 0.5 rather than winning on ties",
          auc([(7.0, True), (7.0, False), (7.0, True), (7.0, False)]), 0.5)

    # An input that is fresh and USELESS must lose. Without this the head-to-head
    # would reward freshness on its own, which is the assumption being tested.
    # Long enough to clear MIN_EVENTS: one spike a day, and a 3h window
    # admits three issue hours per spike.
    hours = range(1000, 2200)
    truth = {h: (300.0 if h % 24 == 20 else 30.0) for h in hours}
    useful = {h: truth.get(h + HORIZON_H, 30.0) for h in hours}
    flat = {h: 50.0 for h in hours}
    beats = check_head_to_head({1: flat}, {1: truth})
    check("a flat fresh input loses to the stale concentration", beats, False)
    helps = check_head_to_head({1: useful}, {1: truth})
    check("a fresh input that sees the future wins", helps, True)

    # Every branch of the verdict, driven directly. Each line names the ONE
    # condition it is there to trip; if a branch is ever shadowed by the one
    # above it, the case below stops matching its label and this goes red.
    print("\n  -- the verdict, branch by branch --")
    check("too few events reaches no verdict at all",
          decide(0.90, 0.50, 0.50, MIN_EVENTS - 1), None)
    check("an uninformative fresh input is refused even when it 'wins'",
          decide(0.52, 0.48, 0.48, 500), False)
    check("losing to the stale concentration is refused",
          decide(0.70, 0.75, 0.60, 500), False)
    check("beating the raw rival but TYING the smoothed control is refused",
          decide(0.80, 0.60, 0.799, 500), False)
    check("clearing all three is the only way through",
          decide(0.80, 0.60, 0.60, 500), True)

    # Freshness has to be able to fail, or check 1 is decoration.
    old = {1: {h: 10.0 for h in hours if h % 24 == 12}}
    check("a feed that only publishes at midday fails the send-hour check",
          check_freshness(old), False)

    print()
    if failures:
        print(f"SELF-TEST FAILED: {failures}")
        return 1
    print("SELF-TEST PASSED — every check above can reach both answers.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--self-test", action="store_true",
                    help="prove the checks can fail; no database, no network")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    with connect() as conn, conn.cursor() as cur:
        cpcb, openaq = load(cur)
    print(f"CPCB: {sum(len(v) for v in cpcb.values()):,} readings on "
          f"{len(cpcb)} stations   "
          f"OpenAQ: {sum(len(v) for v in openaq.values()):,} on {len(openaq)}")

    fresh_enough = check_freshness(cpcb)
    beats = check_head_to_head(cpcb, openaq)
    check_jitter(cpcb, openaq)

    print("\n=== VERDICT ===")
    if not fresh_enough:
        print("NO — CPCB is not fresh at the send hour, which was B's only "
              "advantage. Do not build it. The 07:00 lag is a property of the "
              "send time, not of OpenAQ.")
        return 1
    if beats is None:
        print("UNTESTABLE — CPCB is fresh enough, but there are too few events "
              "to say whether that pays for the smoothing. This is the expected "
              "answer today: observations starts 2026-08-09. Re-run it in "
              "November; nothing needs building in the meantime.")
        return 1
    if not beats:
        print("NO — CPCB is fresh at the send hour, and that freshness still "
              "buys nothing once the stale-but-smoothed control is on the same "
              "rows. Do not retrain on observations: the information is already "
              "reachable from the source in use, and switching would trade an "
              "18-month history for a 13-day one to get it.")
        return 1
    print("YES — CPCB is fresh at the send hour AND ranks bad evenings better "
          "than the stale concentration. B is worth building. It is still a "
          "RETRAIN, not a switch: value_avg is a sub-index of a 24h mean and the "
          "model has never seen that quantity. Check 3 above says whether the "
          "existing spike features would carry over.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

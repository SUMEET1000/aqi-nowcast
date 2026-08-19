"""GATE 3.0 — is OpenAQ's hourly PM2.5 the same quantity as CPCB's value_avg?

    python scripts/compare_sources.py --station 1

Read-only and re-runnable. The exit code is the verdict.

Why this exists. Phase 3 and 4 train on scripts/backfill_openaq.py's archive and
then serve against the CPCB feed the ingester logs. If those two carry different
quantities, nothing crashes — the model simply predicts one thing and is judged
against another, forever. That is the same silent-drop failure class as Gate
0.2's station-name join, and it has to be measured rather than assumed.

The specific doubt is documented in docs/cpcb_aqi_breakpoints.md: on 2026-08-11
probe_avg_window.py failed to disprove that CPCB's avg_value is ALREADY a
24-hour average. OpenAQ serves genuine hourly measurements. If the assumption
holds, CPCB's series should track a trailing 24h mean of OpenAQ far better than
it tracks OpenAQ's raw hourly value.

    SAME QUANTITY   raw fits at least as well as smoothed   exit 0
    CPCB IS SMOOTHED  smoothed fits materially better       exit 0
    UNRESOLVED      too few hours, or neither fits          exit 2

UNRESOLVED is not a failure of the script. It is the honest answer when the
sample cannot separate the two, and it is what stops Phase 3 proceeding on a
guess. Do not widen a threshold to turn it into a verdict.
"""

import argparse
import statistics
import sys
from datetime import timedelta

from db import connect

# Below this many jointly-covered hours, say UNRESOLVED rather than guess.
#
# Set to five complete diurnal cycles. PM2.5 has a strong daily shape, so a
# window that does not contain several whole ones can be decided by which hours
# happened to land in it. Five is the smallest number that is clearly more than
# a weekend.
#
# Chosen against what is available rather than against what would be nice, and
# stated plainly because the difference matters: on 2026-08-19 the ingester held
# 189 distinct CPCB PM2.5 hours, and the smoothed series gives up its first 24
# to the lookback, so the ceiling here is about 165. A threshold above that
# could only ever print UNRESOLVED, which is as useless as one that could only
# ever pass. This one can still fail — it does whenever OpenAQ's archive lags
# the live feed by more than a couple of days, which is the realistic risk.
MIN_MATCHED_HOURS = 120

# CPCB's own published rule for a valid 24-hour average, reused here rather than
# invented: at least 16 of the 24 hours present. probe_avg_window.py's Test C
# measured it as satisfiable on 89.2 percent of station-hours in our data.
LOOKBACK_HOURS = 24
MIN_LOOKBACK_PRESENT = 16

# How much better "materially better" is. A trailing mean is a low-pass filter,
# so if CPCB really is smoothed the gap is large, not marginal — anything near
# 1.0 would be reading noise as a result.
SMOOTHED_WINS_RATIO = 0.7

# Hourly PM2.5 is volatile enough that a raw-vs-smoothed comparison only means
# something if the raw series actually moves. A flat series fits both equally
# and would produce a confident verdict from no information.
MIN_RAW_STDEV = 5.0

OPENAQ_SERIES = """
SELECT observation_ts, value
FROM pm25_history
WHERE station_id = %s
ORDER BY observation_ts
"""

# Joined on the raw timestamp, because the two sources already agree on it.
#
# Measured 2026-08-19, and it is not what the hour arithmetic predicts. CPCB
# stamps a bulletin on the hour in IST, which is 30 minutes past the hour in
# UTC. OpenAQ buckets these sensors into IST-aligned hours too, so its periods
# also land at :30 UTC — both sides read 2026-08-19T15:30:00+00:00 for the same
# hour, and equality matches every row.
#
# This started as a date_trunc('hour', ...) on the CPCB side, written to absorb
# an offset that turns out not to exist. That truncation moved CPCB to :00 while
# OpenAQ stayed at :30 and produced zero shared hours out of 189 and 309 — the
# defensive alignment caused the exact misalignment it was added to prevent.
# Do not reintroduce it on one side. If a future sensor really is :00-aligned,
# truncate BOTH sides or the join silently empties again.
CPCB_SERIES = """
SELECT observation_ts, value_avg
FROM observations
WHERE station_id = %s
  AND pollutant_id = 'PM2.5'
  AND value_avg IS NOT NULL
ORDER BY observation_ts
"""


def trailing_means(series: dict) -> dict:
    """Trailing LOOKBACK_HOURS mean of an hourly series, keyed by its end hour.

    An hour is produced only when at least MIN_LOOKBACK_PRESENT of the window's
    hours actually exist. Absent hours are absent, never filled — a forward-fill
    here would manufacture the smoothness the test is trying to detect.
    """
    out = {}
    for hour in series:
        window = [series[hour - timedelta(hours=k)]
                  for k in range(LOOKBACK_HOURS)
                  if hour - timedelta(hours=k) in series]
        if len(window) >= MIN_LOOKBACK_PRESENT:
            out[hour] = statistics.fmean(window)
    return out


def agreement(left: dict, right: dict) -> dict | None:
    """MAE, mean signed difference and correlation over the shared hours."""
    hours = sorted(set(left) & set(right))
    if len(hours) < 2:
        return None
    diffs = [left[h] - right[h] for h in hours]
    stats = {
        "n": len(hours),
        "mae": statistics.fmean(abs(d) for d in diffs),
        "bias": statistics.fmean(diffs),
        "corr": None,
    }
    try:
        stats["corr"] = statistics.correlation([left[h] for h in hours],
                                               [right[h] for h in hours])
    except statistics.StatisticsError:
        # Raised when either series is constant over the shared hours. That is a
        # real property of the data, not an error to hide: a station stuck on
        # one value has no correlation to report, and printing "n/a" says so.
        pass
    return stats


def show(label: str, stats: dict | None) -> None:
    if stats is None:
        print(f"  {label:<28} no overlapping hours")
        return
    corr = f"{stats['corr']:+.3f}" if stats["corr"] is not None else "  n/a"
    print(f"  {label:<28} n={stats['n']:>5}  MAE={stats['mae']:>7.2f}  "
          f"bias={stats['bias']:>+7.2f}  r={corr}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Gate 3.0 — source agreement.")
    ap.add_argument("--station", type=int, required=True)
    args = ap.parse_args()

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT station_name FROM stations WHERE station_id = %s",
                    (args.station,))
        row = cur.fetchone()
        if not row:
            sys.exit(f"No station with station_id={args.station}.")
        name = row[0]

        cur.execute(OPENAQ_SERIES, (args.station,))
        openaq = {ts: v for ts, v in cur.fetchall()}
        cur.execute(CPCB_SERIES, (args.station,))
        cpcb = {ts: v for ts, v in cur.fetchall()}

    print(f"Station {args.station} — {name}")
    print(f"  OpenAQ hours: {len(openaq)}   CPCB hours: {len(cpcb)}")
    if not openaq or not cpcb:
        print("\nVERDICT: UNRESOLVED — one side has no rows in this window.")
        return 2

    # Coverage each way. This is the silent-drop check: a join that quietly
    # keeps a handful of hours looks identical in the statistics below to one
    # that matched everything, and only these two counts tell them apart.
    both = set(openaq) & set(cpcb)
    print(f"  shared hours: {len(both)}   "
          f"OpenAQ only: {len(set(openaq) - set(cpcb))}   "
          f"CPCB only: {len(set(cpcb) - set(openaq))}")

    smoothed = trailing_means(openaq)
    raw_stats = agreement(openaq, cpcb)
    smooth_stats = agreement(smoothed, cpcb)

    print("\nAgreement with CPCB value_avg:")
    show("OpenAQ raw hourly", raw_stats)
    show(f"OpenAQ trailing {LOOKBACK_HOURS}h mean", smooth_stats)

    if raw_stats is None or smooth_stats is None:
        print(f"\nVERDICT: UNRESOLVED — one comparison had no overlapping hours.")
        return 2
    if min(raw_stats["n"], smooth_stats["n"]) < MIN_MATCHED_HOURS:
        print(f"\nVERDICT: UNRESOLVED — fewer than {MIN_MATCHED_HOURS} matched "
              f"hours on at least one comparison. Backfill a longer window, or "
              f"wait for the ingester to log more.")
        return 2

    spread = statistics.stdev([openaq[h] for h in sorted(both)]) if len(both) > 1 else 0.0
    if spread < MIN_RAW_STDEV:
        print(f"\nVERDICT: UNRESOLVED — OpenAQ hourly barely varies over this "
              f"window (stdev {spread:.2f} < {MIN_RAW_STDEV}), so smoothing it "
              f"changes nothing and the two hypotheses are indistinguishable.")
        return 2

    ratio = smooth_stats["mae"] / raw_stats["mae"] if raw_stats["mae"] else float("inf")
    print(f"\n  smoothed MAE / raw MAE = {ratio:.3f}  "
          f"(smoothed wins below {SMOOTHED_WINS_RATIO})")

    if ratio <= SMOOTHED_WINS_RATIO:
        print("\nVERDICT: CPCB IS SMOOTHED — value_avg tracks a trailing "
              f"{LOOKBACK_HOURS}h mean of OpenAQ far better than it tracks the "
              "raw hourly value.\nPhase 3 and 4 must train on the trailing mean, "
              "not on raw hourly readings, and the README says so.")
        return 0
    if ratio >= 1.0:
        print("\nVERDICT: SAME QUANTITY — raw hourly fits at least as well as "
              "the smoothed series.\nTrain on OpenAQ hourly as it comes.")
        return 0

    print(f"\nVERDICT: UNRESOLVED — smoothing helps ({ratio:.3f}) but not "
          f"decisively ({SMOOTHED_WINS_RATIO}).\nNeither hypothesis is "
          f"supported. Do not pick one; report this and widen the window.")
    return 2


if __name__ == "__main__":
    sys.exit(main())

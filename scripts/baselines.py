"""Phase 3 — what a model has to beat.

    python scripts/baselines.py              # read the cache, or pull if absent
    python scripts/baselines.py --refresh    # re-pull from Neon first

Three baselines (persistence, seasonal persistence, climatology) scored at four
horizons, MAE and RMSE reported separately. Prints a dated markdown table for
README.md — Gate 3.

Neon bills per wake, not per second, so the history is pulled once into a
gitignored CSV and every later run computes from the file. Iterating on the
arithmetic against the database would be dozens of wakes for a query whose
answer never changes.

stdlib plus psycopg for the one pull (build plan §0.6). aqi is imported for the
severe-band cut so the number lives in one place.
"""

import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import env  # noqa: F401  — import-time UTF-8 console fix, needed for µg/m³
import aqi

# Held-out window. Printed in the output so the table cannot be read without it.
HELDOUT_DAYS = 90

HORIZONS = (6, 12, 24, 48)

# India has no DST, so a fixed offset is exact. Hour-of-day and month for the
# climatology cells are IST, because that is the daily cycle the pollution
# follows; grouping by UTC hour would smear the evening peak across two cells.
IST = timezone(timedelta(hours=5, minutes=30))

# Severe starts above the top PM2.5 concentration band edge. Read from aqi
# rather than written as 250 here — the build plan asks for error conditional
# on the severe band, and two copies of that edge would disagree eventually.
SEVERE_ABOVE = aqi.PM25_BANDS[-1][0]

CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "pm25_history.csv")

QUERY = """
SELECT station_id, observation_ts, value
FROM pm25_history
ORDER BY station_id, observation_ts
"""


def pull(path: str) -> None:
    """One Neon wake. Overwrites the cache."""
    from db import connect

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(QUERY)
        rows = cur.fetchall()
    if not rows:
        sys.exit("pm25_history is empty — run scripts/backfill_openaq.py first")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(("station_id", "observation_ts", "value"))
        for station_id, ts, value in rows:
            w.writerow((station_id, ts.astimezone(timezone.utc).isoformat(), value))
    print(f"cached {len(rows)} rows -> {path}")


def load(path: str) -> dict[int, dict[int, float]]:
    """CSV -> {station_id: {hour_index: value}}.

    The hour index is epoch seconds // 3600, so hour t+h is t + h with no date
    arithmetic and no gap-filling: a missing hour is simply an absent key.
    """
    series: dict[int, dict[int, float]] = defaultdict(dict)
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            ts = datetime.fromisoformat(row["observation_ts"])
            series[int(row["station_id"])][int(ts.timestamp()) // 3600] = float(row["value"])
    return series


def _errors(pairs: list[tuple[float, float]]) -> tuple[float, float, int]:
    """(MAE, RMSE, n). Reported separately because RMSE punishes the spikes
    this product exists for and MAE hides them."""
    n = len(pairs)
    if n == 0:
        return float("nan"), float("nan"), 0
    mae = sum(abs(t - p) for t, p in pairs) / n
    rmse = (sum((t - p) ** 2 for t, p in pairs) / n) ** 0.5
    return mae, rmse, n


def _seasonal_lag(h: int) -> int:
    """Whole days back to "the same hour yesterday", for a forecast issued h
    hours before the target.

    The naive lag is a flat 24h, and at h=48 that reads the hour 24h AFTER the
    forecast was issued — a future value, i.e. a leak, in the one baseline
    whose whole job is to be honest. Rounding up to whole days keeps the same
    hour-of-day and keeps the value at or before issue time.
    """
    return 24 * -(-h // 24)


def climatology(series: dict[int, dict[int, float]], cutoff: int
                ) -> dict[tuple[int, int, int], float]:
    """Mean per (station, IST month, IST hour), over TRAIN hours only.

    Fitting this on the held-out window would leak the answers it is being
    scored against — the one leak a baseline can still commit.
    """
    cells: dict[tuple[int, int, int], list] = defaultdict(lambda: [0.0, 0])
    for station, hours in series.items():
        for hour, value in hours.items():
            if hour >= cutoff:
                continue
            ist = datetime.fromtimestamp(hour * 3600, IST)
            cell = cells[(station, ist.month, ist.hour)]
            cell[0] += value
            cell[1] += 1
    return {key: total / n for key, (total, n) in cells.items()}


def score(series: dict[int, dict[int, float]], cutoff: int
          ) -> dict[tuple[str, int], dict[str, tuple[float, float, int]]]:
    """{(baseline, horizon): {"all": (mae, rmse, n), "severe": (...)}}

    A target hour is scored only when ALL THREE baselines can predict it, so
    the three rows of a horizon are one race over one set of hours. Scoring
    each baseline over everything it happens to reach instead gave persistence
    49993 pairs against climatology's 7534 — climatology cannot answer for a
    station whose training half contains no June, and most stations here start
    in September 2025. The two MAEs then came off different sets of hours and
    invited exactly the comparison the table exists to support.

    Gaps are never filled — beyond ~3h a forward-fill is fabrication (build
    plan §10). A missing hour simply removes the pair from every baseline.
    """
    clim = climatology(series, cutoff)
    out: dict[tuple[str, int], dict[str, list]] = defaultdict(
        lambda: {"all": [], "severe": []})

    for station, hours in series.items():
        for target, truth in hours.items():
            if target < cutoff:
                continue
            ist = datetime.fromtimestamp(target * 3600, IST)
            clim_cell = clim.get((station, ist.month, ist.hour))
            for h in HORIZONS:
                preds = {
                    "persistence": hours.get(target - h),
                    "seasonal persistence": hours.get(target - _seasonal_lag(h)),
                    "climatology": clim_cell,
                }
                if any(p is None for p in preds.values()):
                    continue
                for name, pred in preds.items():
                    out[(name, h)]["all"].append((truth, pred))
                    if truth > SEVERE_ABOVE:
                        out[(name, h)]["severe"].append((truth, pred))

    return {key: {cut: _errors(pairs) for cut, pairs in cuts.items()}
            for key, cuts in out.items()}


def render(results, cutoff: int, stations: int) -> str:
    cut_ist = datetime.fromtimestamp(cutoff * 3600, IST)
    empty = (float("nan"), float("nan"), 0)
    lines = [
        f"Baselines — measured {datetime.now(IST):%Y-%m-%d}, "
        f"{stations} stations, held-out = last {HELDOUT_DAYS} days "
        f"(from {cut_ist:%Y-%m-%d %H:%M} IST).",
        "",
        "| Baseline | Horizon | MAE | RMSE | n | MAE severe | RMSE severe | n severe |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name in ("persistence", "seasonal persistence", "climatology"):
        for h in HORIZONS:
            cuts = results.get((name, h), {})
            a = cuts.get("all", empty)
            s = cuts.get("severe", empty)
            lines.append(
                f"| {name} | {h}h | {a[0]:.2f} | {a[1]:.2f} | {a[2]} | "
                f"{s[0]:.2f} | {s[1]:.2f} | {s[2]} |")
    lines += [
        "",
        f"MAE and RMSE in µg/m³. Severe = above {SEVERE_ABOVE} µg/m³.",
        "",
        "Seasonal persistence reads the same hour a whole number of days back, "
        "rounded up to the horizon: 24h at 6/12/24h and 48h at 48h. A flat 24h "
        "lag would read a value from after the forecast was issued.",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh", action="store_true",
                    help="re-pull pm25_history from Neon (one wake)")
    args = ap.parse_args()

    if args.refresh or not os.path.exists(CACHE):
        pull(CACHE)

    series = load(CACHE)
    newest = max(h for hours in series.values() for h in hours)
    oldest = min(h for hours in series.values() for h in hours)
    cutoff = newest - HELDOUT_DAYS * 24

    if oldest >= cutoff:
        sys.exit(f"history spans {(newest - oldest) / 24:.0f} days, all inside the "
                 f"{HELDOUT_DAYS}-day held-out window — climatology has nothing to "
                 f"train on. Backfill more before trusting this table.")

    print(render(score(series, cutoff), cutoff, len(series)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

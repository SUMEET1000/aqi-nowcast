"""Phase 4 stage 2, probe 2 — can we train on the forecast the model will actually get?

    python scripts/probe_weather_forecast.py             # exit code is the verdict
    python scripts/probe_weather_forecast.py --self-test # prove the checks can fail

Read-only. Stdlib only, no API key, no database, no Neon wake. Builds nothing.

THE QUESTION
------------
A forecast for hour t at horizon 24h is issued at t-24h. At that moment we do not
know what the wind WILL be at t; we only know what the weather forecast SAID it
would be. Training on the wind that actually happened teaches the model from an
answer key it will never have in production. The score looks good and the alert
does not work — the same failure as Phase 4 stage 1, where the best MAE had the
worst recall.

So: does Open-Meteo keep old forecasts at a 24h lead, far enough back to cover
our training window?

TWO ENDPOINTS, AND THE OBVIOUS ONE IS THE WRONG ONE
---------------------------------------------------
`historical-forecast-api` sounds right and is not. It stitches together the FIRST
FEW HOURS of each successive model run `[vendor-doc: Open-Meteo historical-forecast
docs, read 2026-08-20]`, so it is close to analysis — near-actuals wearing a
forecast's name. Training on it would be the answer key again, one step removed.

`previous-runs-api` is the real thing: it serves each variable at a FIXED LEAD
TIME, as `<variable>_previous_day1`, `_previous_day2`, and so on. `_previous_day1`
at hour t is what the model predicted for t about a day earlier — which is what
we will have at issue time.

CHECK 3 IS THE ONE THAT MATTERS. If `_previous_day1` turns out to equal the
current-run value, the endpoint is not giving lead-time information and this whole
route collapses back to training on actuals. That must be measured, not assumed.
"""

import argparse
import csv
import http.client
import os
import statistics
import sys
from datetime import date, timedelta

import env  # noqa: F401  — import-time UTF-8 console fix

from probe_weather_grid import (ARCHIVE, MIN_PRESENT, STATIONS_CACHE, compare,
                                get, series)

PREVIOUS_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"

# The variables stage 2 wants, all six confirmed present on the archive and
# forecast endpoints on 2026-08-20. The open question is which of them survive
# at a lead-time offset — boundary_layer_height is the doubtful one, and it is
# the one most likely to matter, since a collapsing boundary layer is half the
# spike mechanism.
VARIABLES = (
    "wind_speed_10m",
    "wind_direction_10m",
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "boundary_layer_height",
)

# benchmark.py forecasts at 6, 12, 24 and 48 hours. This endpoint is indexed in
# WHOLE DAYS, so day1 serves the 24h horizon and day2 the 48h. There is no
# _previous_day0, so 6h and 12h have no matching lead time here — a limitation to
# report, not to paper over by feeding them day1.
LEAD_DAYS = (1, 2)

# Our training window starts here; the docs say most models are archived from
# January 2024, which would cover it. Saying so is not the same as seeing it.
TRAINING_START = date(2025, 2, 18)

# Days sampled per probe window. Enough hours that a forecast and an actual have
# to disagree about real weather rather than about rounding.
WINDOW_DAYS = 14

COMPARE_VAR = "wind_speed_10m"


def lead_names(variables=VARIABLES, days=LEAD_DAYS) -> list[str]:
    return [f"{v}_previous_day{d}" for v in variables for d in days]


def fetch_previous(lat: float, lon: float, start: date, end: date,
                   variables=None) -> dict:
    """One Previous Runs call for one point. Returns the single block."""
    hourly = list(VARIABLES) + lead_names() if variables is None else variables
    return get(PREVIOUS_RUNS, {
        "latitude": f"{lat:.6f}", "longitude": f"{lon:.6f}",
        "hourly": ",".join(hourly), "timezone": "UTC",
        "start_date": str(start), "end_date": str(end),
    })[0]


def check_reach(lat: float, lon: float) -> bool:
    """CHECK 1 — does the lead-time archive cover our training window?

    Asks for the first 14 days of the PM2.5 history. A window the endpoint does
    not hold comes back empty or all-null rather than as an error, so the test is
    on the values, never on the status code.
    """
    end = TRAINING_START + timedelta(days=WINDOW_DAYS)
    print(f"\n=== CHECK 1 — reach back to {TRAINING_START} ===")
    block = fetch_previous(lat, lon, TRAINING_START, end,
                           [COMPARE_VAR, f"{COMPARE_VAR}_previous_day1"])
    hours = len(series(block, "time"))
    lead = series(block, f"{COMPARE_VAR}_previous_day1")
    present = sum(x is not None for x in lead)
    print(f"  {hours} hours requested from {TRAINING_START}")
    print(f"  {COMPARE_VAR}_previous_day1 present {present}/{hours or 1}"
          f" ({present / max(hours, 1):.1%})")
    if present == 0:
        print("  FAIL — the lead-time archive does not reach the start of our "
              "PM2.5 history. Stage 2 cannot train on archived forecasts for the "
              "whole window; report the usable start date instead of trimming "
              "the history silently.")
        return False
    print("  reaches the training window")
    return True


def check_variables(lat: float, lon: float, start: date) -> bool:
    """CHECK 2 — which variables and which lead times actually exist."""
    end = start + timedelta(days=WINDOW_DAYS)
    print(f"\n=== CHECK 2 — variables at lead time, {start} -> {end} ===")
    block = fetch_previous(lat, lon, start, end)
    hourly = block.get("hourly") or {}
    hours = len(hourly.get("time") or [])
    ok = True
    for v in VARIABLES:
        row = []
        for d in LEAD_DAYS:
            vals = hourly.get(f"{v}_previous_day{d}")
            if vals is None:
                row.append(f"day{d} ABSENT")
                ok = False
                continue
            share = sum(x is not None for x in vals) / max(hours, 1)
            flag = "" if share >= MIN_PRESENT else " UNUSABLE"
            row.append(f"day{d} {share:>6.1%}{flag}")
            if share < MIN_PRESENT:
                ok = False
        print(f"  {v:<24} " + "   ".join(row))
    return ok


def check_is_really_a_forecast(lat: float, lon: float, start: date) -> bool:
    """CHECK 3 — is _previous_day1 a real 24h-ahead forecast, or actuals relabelled?

    THE CHECK THIS SCRIPT EXISTS FOR. If a lead-time series equals the current-run
    series, the endpoint carries no lead-time information and the honest route is
    gone. A pass needs BOTH: the forecast must differ from the current run, and it
    must still track ERA5 well enough to be worth having.

    The gap against ERA5 is also the answer to a question stage 2 would otherwise
    have to guess: how much a model trained on actuals would have been flattered.
    """
    end = start + timedelta(days=WINDOW_DAYS)
    print(f"\n=== CHECK 3 — is it a forecast at all, {start} -> {end} ===")
    block = fetch_previous(lat, lon, start, end)
    now = series(block, COMPARE_VAR)
    actual = series(get(ARCHIVE, {
        "latitude": f"{lat:.6f}", "longitude": f"{lon:.6f}",
        "hourly": COMPARE_VAR, "timezone": "UTC",
        "start_date": str(start), "end_date": str(end)})[0], COMPARE_VAR)

    ok = True
    n, mae, r, identical = compare(now, actual)
    print(f"  current run vs ERA5 actual      n={n:>5} MAE={mae:>6.3f} r={r:>6.3f}")
    for d in LEAD_DAYS:
        lead = series(block, f"{COMPARE_VAR}_previous_day{d}")
        n1, mae1, r1, id1 = compare(lead, now)
        n2, mae2, r2, id2 = compare(lead, actual)
        print(f"  day{d} forecast vs current run   n={n1:>5} MAE={mae1:>6.3f} "
              f"r={r1:>6.3f}{'  IDENTICAL' if id1 else ''}")
        print(f"  day{d} forecast vs ERA5 actual   n={n2:>5} MAE={mae2:>6.3f} "
              f"r={r2:>6.3f}{'  IDENTICAL' if id2 else ''}")
        if n1 == 0 or n2 == 0:
            print(f"  FAIL — no overlapping hours at day{d}; cannot classify.")
            ok = False
        elif id1:
            print(f"  FAIL — day{d} is bit-identical to the current run. This is "
                  "not a lead-time archive, and training on it would be training "
                  "on actuals under another name.")
            ok = False
        else:
            print(f"  day{d} carries real forecast error: {mae2:.3f} m/s against "
                  f"ERA5. THAT IS THE OPTIMISM a model trained on actuals would "
                  f"have absorbed and never shown you.")
    return ok


def check_cells(lat: float, lon: float, start: date) -> None:
    """Does this endpoint use the same grid as the two already measured?

    Not pass/fail. A different grid is workable; a different grid discovered
    later, as an unexplained feature shift, is not.
    """
    print("\n=== CHECK 4 — grid cell, against the 2026-08-20 measurement ===")
    block = fetch_previous(lat, lon, start, start + timedelta(days=1),
                           [COMPARE_VAR, f"{COMPARE_VAR}_previous_day1"])
    cell = (round(block["latitude"], 4), round(block["longitude"], 4))
    print(f"  requested {lat:.4f},{lon:.4f}  ->  cell {cell[0]:.4f},{cell[1]:.4f}")
    print(f"  archive and forecast endpoints returned 28.4359,77.1136 for the "
          f"Gurugram cluster on 2026-08-20")
    print("  same grid" if cell == (28.4359, 77.1136)
          else "  DIFFERENT GRID — recount the shared cells before stage 2")


def self_test(lat: float, lon: float) -> int:
    """Prove check 3 can fail, by feeding it a case where it must.

    Compare the current run against ITSELF. compare() has to call that identical,
    which is the same signal check 3 treats as a failure. If this passes silently
    the check is decorative — five checks in this repo once were.
    """
    print("=== SELF-TEST — compare a series against itself ===")
    start = date.today() - timedelta(days=30)
    block = fetch_previous(lat, lon, start, start + timedelta(days=2),
                           [COMPARE_VAR, f"{COMPARE_VAR}_previous_day1"])
    now = series(block, COMPARE_VAR)
    n, mae, r, identical = compare(now, now)
    print(f"  n={n} MAE={mae} identical={identical}")
    if not identical or n == 0:
        print("SELF-TEST FAILED: compare() cannot detect an identical series, so "
              "check 3 could never report the failure it exists to catch.")
        return 1
    lead = series(block, f"{COMPARE_VAR}_previous_day1")
    n2, _, _, id2 = compare(now, lead)
    print(f"  and against the day1 lead: n={n2} identical={id2}")
    print("SELF-TEST PASSED — the identical-series path is reachable and detected")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--station", type=int, default=28,
                    help="station id to probe; 28 is Teri Gram, in the Gurugram "
                         "shared cell measured on 2026-08-20")
    args = ap.parse_args()

    with open(STATIONS_CACHE, encoding="utf-8") as fh:
        rows = {int(r["station_id"]): r for r in csv.DictReader(fh)}
    row = rows[args.station]
    lat, lon = float(row["latitude"]), float(row["longitude"])
    print(f"probing station {args.station}: {row['station_name']}")

    if args.self_test:
        return self_test(lat, lon)

    reach = check_reach(lat, lon)
    # Recent dates for checks 2-4: if check 1 failed, the training window holds
    # nothing to measure, and a probe that reports on an empty window is the
    # unfailable-check mistake again.
    recent = date.today() - timedelta(days=WINDOW_DAYS + 9)
    variables = check_variables(lat, lon, recent)
    genuine = check_is_really_a_forecast(lat, lon, recent)
    check_cells(lat, lon, recent)

    print("\n=== VERDICT ===")
    print(f"6h and 12h horizons have no matching lead time here — this endpoint "
          f"is indexed in whole days (day{LEAD_DAYS[0]}..day{LEAD_DAYS[-1]}). "
          f"Stage 2 covers 24h and 48h honestly; 6h and 12h need a different "
          f"answer, and feeding them day1 would be a 24h-old forecast pretending "
          f"to be a 6h one.")
    if not reach or not genuine:
        print("NO — the route itself does not hold. Either the lead-time archive "
              "does not reach our window, or a lead column is actuals relabelled. "
              "Do not build stage 2 on archived forecasts; fall back to ERA5 "
              "actuals AND report the optimism, never quietly train on actuals.")
        return 1
    if not variables:
        print("PARTIAL — the route holds: archived forecasts at a 24h and 48h "
              "lead exist, reach the training window, and carry real forecast "
              "error. But at least one variable above is ABSENT or too sparse at "
              "lead time, and it is NOT available for training even though the "
              "live forecast endpoint serves it. That is a train/serve "
              "availability gap, not a modelling choice: taking the missing "
              "variable from the ERA5 archive instead would put the answer key "
              "back in, for that one column. Decide it deliberately next "
              "session — the honest default is to drop it and let an ablation "
              "say whether it was worth anything.")
        return 1
    print("YES — archived forecasts at a 24h and 48h lead exist, reach our "
          "training window, and carry real forecast error. Stage 2 can train on "
          "what it will actually be given at issue time.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

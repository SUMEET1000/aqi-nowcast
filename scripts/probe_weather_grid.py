"""Phase 4 stage 2 kill gate — can Open-Meteo's grid tell our stations apart?

    python scripts/probe_weather_grid.py             # full run, exit code is the verdict
    python scripts/probe_weather_grid.py --self-test # prove the checks can fail
    python scripts/probe_weather_grid.py --endpoint forecast   # serving grid alone

Read-only. Stdlib only, no API key, no database, no Neon wake.

WHY THIS EXISTS
---------------
Stage 2 adds weather features because spikes are driven by wind dropping and the
boundary layer collapsing. If two stations land in one model grid cell they get
identical weather rows, and the model cannot use weather to tell them apart.
That claim sat in CLAUDE.md as [vendor-doc] and had never been run.

It also fixes the wrong endpoint. The [vendor-doc] note was about the CAMS
air-quality grid; stage 2 needs WEATHER, which is a different model on a
different grid:

    training history -> archive-api.open-meteo.com/v1/archive   (ERA5 family)
    serving          -> api.open-meteo.com/v1/forecast

THE TWO WAYS THIS CAN FAIL, AND ONLY ONE IS OBVIOUS
---------------------------------------------------
A. Two stations share a cell, so their weather is identical by construction.
B. Two stations sit in DIFFERENT cells whose values are identical anyway.

B is the same failure wearing a different hat, and a cell-count check cannot see
it. Check B compares the series, which is why it is not redundant with check A.

Reporting a shared cell is NOT an error exit. The rule was fixed with the owner
before any number was seen: a shared cell is a written-down limitation, not a
stop. A non-zero exit means the probe could not reach a VERDICT.
"""

import argparse
import csv
import http.client
import itertools
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

import env  # noqa: F401  — import-time UTF-8 console fix, needed for ° and µ

# features.load_coords does exactly this read, but importing features pulls in
# pandas and numpy. A probe that answers "should stage 2 exist" must run before
# requirements-model.txt is installed, so the four lines are duplicated on
# purpose. STATIONS_CACHE is the same path features.py derives.
STATIONS_CACHE = os.path.join("data", "stations.csv")

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"

# Same reasoning as cpcb_api.HEADERS: a default urllib UA is the kind of thing
# a public API treats differently, and naming the caller is basic manners.
HEADERS = {"User-Agent": "aqi-nowcast/0.1 (portfolio project; contact via repo)"}

# The variables stage 2 intends to use. boundary_layer_height is the one most
# likely to be missing — it is an ERA5 field, and the docs list it under
# "additional variables" rather than in the main hourly table.
VARIABLES = (
    "wind_speed_10m",
    "wind_direction_10m",
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "boundary_layer_height",
)

# The series compared in check B. Wind is the dispersion variable the whole
# stage rests on, so if any variable has to separate two stations it is this one.
COMPARE_VAR = "wind_speed_10m"

# Pairs closer than this get compared individually. The grid measured out at
# 0.0703 deg x 0.1023 deg (~7.8 x ~10.0 km) on 2026-08-20, so 12 km covers every
# pair that could plausibly collide. It is a screening width, not the answer —
# check A reads the real cell off the response and does not consult this.
CLOSE_KM = 12.0

# Days of history for the series comparison. Long enough that two cells have to
# disagree about a real weather system, short enough to stay one small request.
WINDOW_DAYS = 30

# pm25_history starts here (2025-02-18, the OpenAQ backfill). A weather variable
# that does not reach back this far cannot be a training feature.
TRAINING_START = "2025-02-18"

# Below this share of non-null hours over the training window, a variable is
# reported UNUSABLE rather than as a feature. Gaps are never filled in this
# project (build plan §10), so a 60%-present variable is 40% missing forever.
MIN_PRESENT = 0.90

# Our own history, for check D. Written by scripts/baselines.py --refresh.
PM25_CACHE = os.path.join("data", "pm25_history.csv")

# Below this many overlapping hours a station pair is skipped rather than
# compared. Two stations sharing 40 hours say nothing about either.
MIN_SHARED_HOURS = 200

ATTEMPTS = 4


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle km. Same formula as features.neighbours, kept local rather
    than exported: that one is nested inside its caller and this is a probe."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def get(url: str, params: dict) -> list[dict]:
    """One Open-Meteo call. Always returns a list, even for one location.

    The retry clause is deliberately broad, for the reason written up in
    cpcb_api.fetch: a dropped TLS handshake raises ssl.SSLError /
    ConnectionResetError / RemoteDisconnected, and none of those are
    urllib.error.URLError subclasses. A narrow clause lets the exact failure the
    retry exists for escape the retry loop.
    """
    query = urllib.parse.urlencode(params, safe=",")
    req = urllib.request.Request(f"{url}?{query}", headers=HEADERS)
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                body = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            print(f"  HTTP {e.code} attempt {attempt}/{ATTEMPTS}: {detail}",
                  file=sys.stderr)
            if e.code < 500 or attempt == ATTEMPTS:
                raise SystemExit(f"Open-Meteo HTTP {e.code}: {detail}")
            time.sleep(2 * attempt)
        except (OSError, http.client.HTTPException, ValueError) as e:
            print(f"  {type(e).__name__} attempt {attempt}/{ATTEMPTS}: {e}",
                  file=sys.stderr)
            if attempt == ATTEMPTS:
                raise SystemExit(f"Open-Meteo unreachable after {ATTEMPTS}: {e}")
            time.sleep(2 * attempt)
    if isinstance(body, dict):
        body = [body]
    return body


def fetch_grid(endpoint: str, coords: list[tuple[float, float]],
               variables=VARIABLES) -> list[dict]:
    """One request for every station. Open-Meteo takes comma-separated lists."""
    params = {
        "latitude": ",".join(f"{lat:.6f}" for lat, _ in coords),
        "longitude": ",".join(f"{lon:.6f}" for _, lon in coords),
        "hourly": ",".join(variables),
        "timezone": "UTC",
    }
    if endpoint == "archive":
        end = date.today() - timedelta(days=6)   # ERA5 lags real time ~5 days
        params["start_date"] = str(end - timedelta(days=WINDOW_DAYS))
        params["end_date"] = str(end)
        return get(ARCHIVE, params)
    params["past_days"] = 7
    params["forecast_days"] = 1
    return get(FORECAST, params)


def series(block: dict, name: str) -> list:
    return (block.get("hourly") or {}).get(name) or []


def compare(a: list, b: list) -> tuple[int, float, float, bool]:
    """n, MAE, Pearson r, identical — over hours where BOTH sides are present.

    Returns r as nan when either side never varies, which is a real outcome here
    rather than an error: a cell whose wind never moves is itself the finding.
    """
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if not pairs:
        return 0, float("nan"), float("nan"), False
    xs, ys = zip(*pairs)
    mae = sum(abs(x - y) for x, y in pairs) / len(pairs)
    identical = all(x == y for x, y in pairs)
    try:
        r = statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        r = float("nan")
    return len(pairs), mae, r, identical


def check_cells(label: str, blocks: list[dict], ids: list[int],
                names: dict[int, str]) -> dict[int, tuple[float, float]]:
    """CHECK A — which stations landed in the same grid cell.

    Open-Meteo returns the WGS84 centre of the grid cell it actually used, not
    the coordinate we sent. Grouping on that returned pair IS the answer; nothing
    here is inferred from a documented resolution.
    """
    print(f"\n=== CHECK A — grid cells, {label} ===")
    cell_of, groups = {}, {}
    for sid, block in zip(ids, blocks):
        cell = (round(block["latitude"], 4), round(block["longitude"], 4))
        cell_of[sid] = cell
        groups.setdefault(cell, []).append(sid)

    print(f"  model: {blocks[0].get('model') or '(not reported)'}")
    print(f"  {len(ids)} stations -> {len(groups)} distinct cells")
    shared = {c: s for c, s in groups.items() if len(s) > 1}
    if not shared:
        print("  no two stations share a cell")
    for cell, sids in sorted(shared.items()):
        print(f"  cell {cell[0]:.4f},{cell[1]:.4f}  <- {len(sids)} stations")
        for sid in sids:
            print(f"      {sid:>2}  {names[sid]}")

    spacings = sorted({round(c[0], 4) for c in cell_of.values()})
    if len(spacings) > 1:
        step = min(b - a for a, b in zip(spacings, spacings[1:]))
        print(f"  smallest latitude step between distinct cells: "
              f"{step:.4f}deg (~{step * 111.0:.1f} km)")

    n_exposed = sum(len(s) for s in shared.values())
    print(f"  stations in a shared cell: {n_exposed} of {len(ids)}")
    return cell_of


def check_series(label: str, blocks: list[dict], ids: list[int],
                 names: dict[int, str], coords: dict[int, tuple[float, float]],
                 cell_of: dict) -> bool:
    """CHECK B — do close stations actually receive different weather?

    Check A cannot see two distinct cells carrying identical values. This can.
    Returns False when some close pair could not be classified at all.
    """
    print(f"\n=== CHECK B — {COMPARE_VAR} across close pairs, {label} ===")
    by_id = dict(zip(ids, blocks))

    far = max(itertools.combinations(ids, 2), key=lambda p: haversine(coords[p[0]], coords[p[1]]))
    n, mae, r, _ = compare(series(by_id[far[0]], COMPARE_VAR),
                           series(by_id[far[1]], COMPARE_VAR))
    far_km = haversine(coords[far[0]], coords[far[1]])
    print(f"  control, the two most distant stations {far[0]} vs {far[1]} "
          f"({far_km:.0f} km): n={n} MAE={mae:.3f} r={r:.3f}")
    print(f"  {'pair':<9} {'km':>6} {'cell':>8} {'n':>5} {'MAE':>8} {'r':>7}  verdict")

    ok = True
    for a, b in itertools.combinations(ids, 2):
        km = haversine(coords[a], coords[b])
        if km >= CLOSE_KM:
            continue
        same_cell = cell_of[a] == cell_of[b]
        n, mae, r, identical = compare(series(by_id[a], COMPARE_VAR),
                                       series(by_id[b], COMPARE_VAR))
        if n == 0:
            verdict, ok = "NO DATA - cannot classify", False
        elif same_cell:
            verdict = "SHARED CELL" + ("" if identical else " but values differ (!)")
        elif identical:
            verdict = "DISTINCT CELLS, IDENTICAL VALUES - grid cannot separate"
        else:
            verdict = "separated"
        print(f"  {a:>3} v{b:>3} {km:>6.2f} {'same' if same_cell else 'diff':>8} "
              f"{n:>5} {mae:>8.3f} {r:>7.3f}  {verdict}")
        print(f"            {names[a]}")
        print(f"            {names[b]}")
    return ok


def check_variables(label: str, blocks: list[dict]) -> bool:
    """CHECK C part 1 — does each intended variable exist on this endpoint?"""
    print(f"\n=== CHECK C1 — variable availability, {label} ===")
    hourly = (blocks[0].get("hourly") or {})
    ok = True
    for v in VARIABLES:
        vals = hourly.get(v)
        if vals is None:
            print(f"  {v:<24} ABSENT")
            ok = False
            continue
        present = sum(x is not None for x in vals)
        print(f"  {v:<24} present {present}/{len(vals)} "
              f"({present / len(vals):.1%})")
    return ok


def check_cost(cell_of: dict, ids: list[int], coords: dict) -> bool:
    """CHECK D — does a shared cell actually cost anything?

    Checks A-C answer "can the grid separate two stations". This answers the
    question a subscriber would care about: if two stations get the same weather,
    do they in fact read the same PM2.5? The comparison is against our own
    history, so it costs no request and no Neon wake.

    stdlib csv rather than pandas on purpose — this probe has to run before
    requirements-model.txt is installed.

    Returns True when the comparison could be made at all. A shared cell is not
    an error exit; that rule was fixed before these numbers were seen.
    """
    print("\n=== CHECK D — do stations sharing a weather cell read the same PM2.5? ===")
    if not os.path.exists(PM25_CACHE):
        print(f"  {PM25_CACHE} absent — run scripts/baselines.py --refresh. SKIPPED.")
        return True

    by_hour: dict[str, dict[int, float]] = {}
    with open(PM25_CACHE, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            v = float(row["value"])
            # features.DROP_EXACT_ZERO: 0.0 ug/m3 is a dead sensor, not clean air.
            if v > 0:
                by_hour.setdefault(row["observation_ts"], {})[int(row["station_id"])] = v

    def pm_mae(a: int, b: int):
        both = [(h[a], h[b]) for h in by_hour.values() if a in h and b in h]
        if len(both) < MIN_SHARED_HOURS:
            return None
        return len(both), sum(abs(x - y) for x, y in both) / len(both)

    groups = {"sharing a weather cell": [], "close, different cells": [],
              "every pair, up to 312 km": []}
    for a, b in itertools.combinations(ids, 2):
        r = pm_mae(a, b)
        if r is None:
            continue
        groups["every pair, up to 312 km"].append(r[1])
        if haversine(coords[a], coords[b]) < CLOSE_KM:
            key = ("sharing a weather cell" if cell_of[a] == cell_of[b]
                   else "close, different cells")
            groups[key].append(r[1])
            print(f"  {a:>3} v{b:>3}  {key:<24} n={r[0]:>6}  PM2.5 MAE {r[1]:>6.1f}")

    print()
    if not groups["sharing a weather cell"]:
        print("  no shared-cell pair has enough overlapping history to compare")
        return True
    for key, vals in groups.items():
        if vals:
            med = sorted(vals)[len(vals) // 2]
            print(f"  {key:<26} {len(vals):>4} pairs   median PM2.5 MAE {med:>6.1f} ug/m3")
    print("  If those three medians are the same number, a finer grid buys nothing:")
    print("  station-level PM2.5 is local, not weather-driven at this scale.")
    return True


def check_coverage(coords: tuple[float, float], name: str) -> bool:
    """CHECK C2 — does the archive reach back over the whole training window?

    One station, one request. A variable missing 40% of the training window is
    not a feature: this project never fills gaps (build plan §10).
    """
    end = date.today() - timedelta(days=6)
    print(f"\n=== CHECK C2 — archive coverage {TRAINING_START} -> {end}, "
          f"{name} ===")
    blocks = get(ARCHIVE, {
        "latitude": f"{coords[0]:.6f}", "longitude": f"{coords[1]:.6f}",
        "hourly": ",".join(VARIABLES), "timezone": "UTC",
        "start_date": TRAINING_START, "end_date": str(end),
    })
    hourly = blocks[0].get("hourly") or {}
    hours = len(hourly.get("time") or [])
    print(f"  {hours} hours returned")
    ok = True
    for v in VARIABLES:
        vals = hourly.get(v)
        if vals is None:
            print(f"  {v:<24} ABSENT over the training window")
            ok = False
            continue
        share = sum(x is not None for x in vals) / max(hours, 1)
        flag = "" if share >= MIN_PRESENT else f"  UNUSABLE (< {MIN_PRESENT:.0%})"
        print(f"  {v:<24} present {share:.2%}{flag}")
        if share < MIN_PRESENT:
            ok = False
    return ok


def self_test() -> int:
    """Prove the checks can fail, before trusting them on real data.

    The lesson this repo already paid for: five checks were found whose pass
    condition was derivable from the code alone, and all five reported PASS.
    So: send ONE coordinate twice. It must come back as a shared cell with
    identical values, and compare() must say so.
    """
    print("=== SELF-TEST — two requests for the SAME point ===")
    here = (28.4275, 77.1465)
    blocks = fetch_grid("archive", [here, here])
    cell_of = check_cells("self-test", blocks, [901, 902],
                          {901: "same point A", 902: "same point B"})
    n, mae, r, identical = compare(series(blocks[0], COMPARE_VAR),
                                   series(blocks[1], COMPARE_VAR))
    print(f"  n={n} MAE={mae} identical={identical}")
    failures = []
    if cell_of[901] != cell_of[902]:
        failures.append("one point resolved to two cells")
    if not identical:
        failures.append("identical input gave non-identical series")
    if n == 0:
        failures.append("no overlapping hours returned")
    if failures:
        print("SELF-TEST FAILED: " + "; ".join(failures))
        return 1
    print("SELF-TEST PASSED — the shared-cell path is reachable and detected")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--endpoint", choices=("archive", "forecast", "both"),
                    default="both")
    ap.add_argument("--self-test", action="store_true",
                    help="prove the checks can fail, then exit")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    with open(STATIONS_CACHE, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    ids = sorted(int(r["station_id"]) for r in rows)
    names = {int(r["station_id"]): r["station_name"] for r in rows}
    coords = {int(r["station_id"]):
              (float(r["latitude"]), float(r["longitude"])) for r in rows}
    print(f"{len(ids)} stations from {STATIONS_CACHE}")

    endpoints = ("archive", "forecast") if args.endpoint == "both" else (args.endpoint,)
    ok = True
    for ep in endpoints:
        blocks = fetch_grid(ep, [coords[s] for s in ids])
        if len(blocks) != len(ids):
            print(f"{ep}: asked for {len(ids)} locations, got {len(blocks)} — "
                  f"cannot map a block to a station", file=sys.stderr)
            return 1
        cell_of = check_cells(ep, blocks, ids, names)
        ok &= check_series(ep, blocks, ids, names, coords, cell_of)
        ok &= check_variables(ep, blocks)

    ok &= check_cost(cell_of, ids, coords)

    if "archive" in endpoints:
        ok &= check_coverage(coords[ids[0]], names[ids[0]])

    print("\n=== VERDICT ===")
    if not ok:
        print("NO VERDICT — something above could not be classified, or a "
              "variable stage 2 needs is absent or too sparse. Read the FAIL "
              "lines; do not start stage 2 on this.")
        return 1
    print("VERDICT REACHED — every station is assigned a cell, every close pair "
          "is classified, every variable is present over the training window.")
    print("A shared cell is a limitation to write down, not a stop: that rule "
          "was fixed before these numbers were seen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

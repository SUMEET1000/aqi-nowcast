"""Build and inspect the archived-forecast weather caches.

    python scripts/weather.py --pull-cells
    python scripts/weather.py --pull
    python scripts/weather.py --describe
    python scripts/weather.py --self-test
"""

import argparse
import csv
import os
import sys
from datetime import date, datetime, timedelta

import env  # noqa: F401 -- import-time UTF-8 console fix

from probe_weather_grid import get, series


PREVIOUS_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"

# Two properties of what this endpoint returns, neither of them measured here.
#
# The newest ~5 days of boundary_layer_height are live model output rather than
# reanalysis, because ERA5 arrives about five days behind real time
# [vendor-doc: Open-Meteo archive docs; the lag itself has never been timed here].
#
# _previous_day1 comes from the run one calendar day earlier, so its lead runs
# 24-47h across that day rather than a flat 24h. Longer than the horizon and
# never shorter, so it handicaps a 24h model rather than cheating for it
# [assumed: reading the run timestamp off successive live forecast fetches
# would settle it; the response carries no issue time].
STATIONS_CACHE = os.path.join("data", "stations.csv")
CELLS_CACHE = os.path.join("data", "weather_cells.csv")
WEATHER_CACHE = os.path.join("data", "weather.csv")
TRAINING_START = date(2025, 2, 18)
BATCH_SIZE = 10

FORECAST_VARIABLES = (
    "wind_speed_10m",
    "wind_direction_10m",
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
)
LEAD_COLUMNS = tuple(
    f"{variable}_previous_day{day}"
    for variable in FORECAST_VARIABLES for day in (1, 2)
)
WEATHER_COLUMNS = LEAD_COLUMNS + ("boundary_layer_height",)
CELL_FIELDS = ("station_id", "cell_lat", "cell_lon")
WEATHER_FIELDS = ("cell_lat", "cell_lon", "hour") + WEATHER_COLUMNS


def fail(message: str) -> None:
    print(f"FAIL -- {message}", file=sys.stderr)
    raise SystemExit(1)


def read_stations() -> list[dict]:
    with open(STATIONS_CACHE, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        fail(f"{STATIONS_CACHE} has no stations")
    required = {"station_id", "station_name", "latitude", "longitude"}
    if set(rows[0]) < required:
        fail(f"{STATIONS_CACHE} is missing required columns")
    ids = [int(row["station_id"]) for row in rows]
    if len(ids) != len(set(ids)):
        fail(f"{STATIONS_CACHE} has duplicate station IDs")
    return sorted(rows, key=lambda row: int(row["station_id"]))


def rounded_cell(block: dict) -> tuple[float, float]:
    try:
        return round(float(block["latitude"]), 4), round(float(block["longitude"]), 4)
    except (KeyError, TypeError, ValueError) as exc:
        fail(f"Open-Meteo response has no usable grid cell: {exc}")


def request_cells(stations: list[dict]) -> list[dict]:
    # [measured 2026-08-20: four direct requests to previous-runs-api] A
    # multi-coordinate request returns one response block per requested point.
    blocks = get(PREVIOUS_RUNS, {
        "latitude": ",".join(f"{float(row['latitude']):.6f}" for row in stations),
        "longitude": ",".join(f"{float(row['longitude']):.6f}" for row in stations),
        "hourly": "wind_speed_10m",
        "timezone": "UTC",
        "start_date": str(TRAINING_START),
        "end_date": str(TRAINING_START + timedelta(days=1)),
    })
    if len(blocks) != len(stations):
        fail(f"asked for {len(stations)} station cells, got {len(blocks)} response blocks")
    return blocks


def pull_cells() -> int:
    stations = read_stations()
    blocks = request_cells(stations)
    rows = []
    groups: dict[tuple[float, float], list[dict]] = {}
    for station, block in zip(stations, blocks):
        cell = rounded_cell(block)
        row = {"station_id": station["station_id"],
               "cell_lat": f"{cell[0]:.4f}", "cell_lon": f"{cell[1]:.4f}"}
        rows.append(row)
        groups.setdefault(cell, []).append(station)

    write_csv(CELLS_CACHE, CELL_FIELDS, rows)
    print(f"{len(stations)} stations -> {len(groups)} distinct cells")
    for cell, members in sorted(groups.items()):
        if len(members) > 1:
            ids = "/".join(member["station_id"] for member in members)
            names = ", ".join(member["station_name"] for member in members)
            print(f"  shared cell {cell[0]:.4f},{cell[1]:.4f}: {ids} ({names})")

    expected = ({12, 23, 28}, {13, 14, 18})
    observed = {frozenset(int(member["station_id"]) for member in members)
                for members in groups.values() if len(members) > 1}
    if {frozenset(group) for group in expected} != observed:
        fail(f"shared-cell clusters changed: observed {sorted(map(sorted, observed))}")
    if len(stations) != 30 or len(groups) != 26:
        fail(f"expected 30 stations -> 26 cells, got {len(stations)} -> {len(groups)}")
    print("PASS -- expected shared clusters 12/23/28 (Gurugram) and 13/14/18 (Faridabad)")
    return 0


def read_cells() -> list[tuple[float, float]]:
    if not os.path.exists(CELLS_CACHE):
        fail(f"{CELLS_CACHE} is absent; run --pull-cells first")
    with open(CELLS_CACHE, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows or tuple(rows[0]) != CELL_FIELDS:
        fail(f"{CELLS_CACHE} must have exactly {', '.join(CELL_FIELDS)}")
    try:
        cells = [(round(float(row["cell_lat"]), 4), round(float(row["cell_lon"]), 4))
                 for row in rows]
    except (KeyError, TypeError, ValueError) as exc:
        fail(f"{CELLS_CACHE} has an invalid cell: {exc}")
    distinct = sorted(set(cells))
    if len(rows) != 30 or len(distinct) != 26:
        fail(f"{CELLS_CACHE} expected 30 stations and 26 cells, got {len(rows)} and {len(distinct)}")
    return distinct


def validate_hourly(block: dict, expected_cell: tuple[float, float]) -> tuple[list[str], dict[str, list]]:
    actual_cell = rounded_cell(block)
    if actual_cell != expected_cell:
        fail(f"requested cell {expected_cell}, endpoint resolved {actual_cell}")
    hourly = block.get("hourly")
    if not isinstance(hourly, dict):
        fail(f"cell {expected_cell} has no hourly response")
    times = series(block, "time")
    if not times:
        fail(f"cell {expected_cell} has no hourly timestamps")
    for column in WEATHER_COLUMNS:
        values = hourly.get(column)
        if not isinstance(values, list) or len(values) != len(times):
            fail(f"cell {expected_cell} column {column} is absent or has {len(values) if isinstance(values, list) else 'no'} values for {len(times)} hours")
        present = sum(value is not None for value in values)
        if present != len(times):
            fail(f"cell {expected_cell} column {column} is only {present}/{len(times)} non-null")
    return times, hourly


def pull_weather() -> int:
    cells = read_cells()
    end = date.today()
    rows = []
    hourly_names = list(WEATHER_COLUMNS)
    # [measured 2026-08-20: four direct requests to previous-runs-api] This
    # complete window fits in one request per cell; it is not chunked by year.
    for offset in range(0, len(cells), BATCH_SIZE):
        batch = cells[offset:offset + BATCH_SIZE]
        print(f"fetching cells {offset + 1}-{offset + len(batch)} of {len(cells)}")
        blocks = get(PREVIOUS_RUNS, {
            "latitude": ",".join(f"{lat:.4f}" for lat, _ in batch),
            "longitude": ",".join(f"{lon:.4f}" for _, lon in batch),
            "hourly": ",".join(hourly_names),
            "timezone": "UTC",
            "start_date": str(TRAINING_START),
            "end_date": str(end),
        })
        if len(blocks) != len(batch):
            fail(f"batch {offset + 1}-{offset + len(batch)} returned {len(blocks)} blocks")
        for cell, block in zip(batch, blocks):
            times, hourly = validate_hourly(block, cell)
            for index, hour in enumerate(times):
                rows.append({"cell_lat": f"{cell[0]:.4f}", "cell_lon": f"{cell[1]:.4f}",
                             "hour": hour,
                             **{column: hourly[column][index] for column in WEATHER_COLUMNS}})

    validate_weather_rows(rows, require_start=True)
    write_csv(WEATHER_CACHE, WEATHER_FIELDS, rows)
    print_weather_summary(rows, "rows written")
    return 0


def write_csv(path: str, fields: tuple[str, ...], rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def parse_hour(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        fail(f"invalid hour {value!r}: {exc}")


def validate_weather_rows(rows: list[dict], require_start: bool, fail_on_gaps: bool = True) -> tuple[dict[tuple[float, float], list[datetime]], list[tuple[tuple[float, float], datetime, datetime]]]:
    if not rows:
        fail("weather cache has no rows")
    by_cell: dict[tuple[float, float], list[datetime]] = {}
    for row in rows:
        try:
            cell = round(float(row["cell_lat"]), 4), round(float(row["cell_lon"]), 4)
        except (KeyError, TypeError, ValueError) as exc:
            fail(f"weather row has invalid cell: {exc}")
        hour = parse_hour(row.get("hour", ""))
        for column in WEATHER_COLUMNS:
            if column not in row or row[column] in (None, ""):
                fail(f"weather row {cell} {hour.isoformat()} has null {column}")
        by_cell.setdefault(cell, []).append(hour)
    gaps = []
    for cell, hours in by_cell.items():
        hours.sort()
        if len(hours) != len(set(hours)):
            fail(f"weather cache has duplicate hours for cell {cell}")
        if require_start and hours[0] != datetime.combine(TRAINING_START, datetime.min.time()):
            fail(f"cell {cell} starts {hours[0].isoformat()}, not {TRAINING_START}T00:00")
        for before, after in zip(hours, hours[1:]):
            if after - before != timedelta(hours=1):
                gaps.append((cell, before, after))
    if gaps and fail_on_gaps:
        first = gaps[0]
        fail(f"weather cache has {len(gaps)} hour gaps; first {first[0]} {first[1].isoformat()} -> {first[2].isoformat()}")
    return by_cell, gaps


def read_weather() -> list[dict]:
    if not os.path.exists(WEATHER_CACHE):
        fail(f"{WEATHER_CACHE} is absent; run --pull first")
    with open(WEATHER_CACHE, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows or tuple(rows[0]) != WEATHER_FIELDS:
        fail(f"{WEATHER_CACHE} must have exactly {', '.join(WEATHER_FIELDS)}")
    return rows


def print_weather_summary(rows: list[dict], label: str) -> None:
    by_cell, _ = validate_weather_rows(rows, require_start=True)
    hours = [hour for values in by_cell.values() for hour in values]
    print(f"{label}: {len(rows)}; distinct cells: {len(by_cell)}; "
          f"first hour: {min(hours).isoformat(timespec='minutes')}; "
          f"last hour: {max(hours).isoformat(timespec='minutes')}")
    for column in WEATHER_COLUMNS:
        present = sum(row[column] not in (None, "") for row in rows)
        print(f"  {column}: non-null {present}/{len(rows)} ({present / len(rows):.1%})")


def describe() -> int:
    rows = read_weather()
    by_cell, gaps = validate_weather_rows(rows, require_start=True, fail_on_gaps=False)
    hours = [hour for values in by_cell.values() for hour in values]
    print(f"coverage starts {min(hours).date()} across {len(by_cell)} cells")
    print(f"hour gaps: {len(gaps)}")
    for cell, before, after in gaps[:5]:
        print(f"  {cell[0]:.4f},{cell[1]:.4f}  {before.isoformat()} -> {after.isoformat()}")
    print_weather_summary(rows, "cached rows")
    return 0


def self_test() -> int:
    """Show that a present-but-null hourly field is rejected on its values."""
    print("=== SELF-TEST -- all-null columns are rejected by value ===")
    cell = (28.4359, 77.1136)
    times = ["2025-02-18T00:00", "2025-02-18T01:00"]
    bad = {"latitude": cell[0], "longitude": cell[1], "hourly": {"time": times}}
    for column in WEATHER_COLUMNS:
        bad["hourly"][column] = [1.0, 1.0]
    bad["hourly"]["boundary_layer_height"] = [None, None]
    try:
        validate_hourly(bad, cell)
    except SystemExit as exc:
        if exc.code == 1:
            print("SELF-TEST PASSED -- all-null column reached the FAIL path")
            return 0
        raise
    print("SELF-TEST FAILED -- all-null column passed validation", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--pull-cells", action="store_true")
    actions.add_argument("--pull", action="store_true")
    actions.add_argument("--describe", action="store_true")
    actions.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.pull_cells:
        return pull_cells()
    if args.pull:
        return pull_weather()
    if args.describe:
        return describe()
    return self_test()


if __name__ == "__main__":
    sys.exit(main())

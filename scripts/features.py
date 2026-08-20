"""Phase 4 — the feature matrix. Everything a model is allowed to look at.

    python scripts/features.py --describe        # column list and row counts
    python scripts/features.py --pull-stations   # cache coordinates (one Neon wake)

No database at import time, no network, no ML library. tests/test_features.py
imports this, and the leak check has to be runnable without the machinery whose
correctness it is checking.

THE RULE THIS FILE EXISTS TO ENFORCE
------------------------------------
A forecast for target hour t at horizon h is ISSUED AT t - h. Every feature is
counted backwards from issue time, never from the target.

The mistake is easy and quiet. "Lag 24" counted back from the target is right.
"Lag 1" counted back from the target is 1 hour before the answer and 23 hours
after the forecast was supposed to exist — a leak that makes the score look
excellent and the model useless. So features here are built ONCE per station,
indexed by issue hour, using only backward shifts; the target is the single
negative shift in the file, and it is the only thing allowed to be one.

Gaps are never filled. Build plan §10 caps forward-fill at ~3h and these sensors
go dark in blocks of 10-19h, so a fill would be fabrication. Each station is
reindexed onto a complete hourly index and pandas leaves NaN at the gaps, which
is why the boosting libraries were chosen: they take NaN natively.
"""

import argparse
import csv
import math
import os
import sys

import numpy as np
import pandas as pd

import env  # noqa: F401  — import-time UTF-8 console fix, needed for µg/m³
from baselines import CACHE, HORIZONS, IST, SEVERE_ABOVE  # noqa: F401

# Own-history lags, in hours before issue time. Lag 0 is the value AT issue
# time, which is exactly the persistence prediction — that identity is pinned by
# a test, because it is what guarantees every candidate is scored on the same
# rows. Provisional until the notebook's PACF either confirms or revises it.
LAGS = (0, 1, 2, 3, 6, 12, 24, 48)

# Rolling windows, in hours, ENDING AT and INCLUDING issue time.
ROLL_WINDOWS = (6, 24)

# A rolling statistic over a window less than this fraction present is NaN
# rather than a mean of two survivors calling itself a 24-hour mean. One of the
# ablation questions.
MIN_PRESENT = 0.5

# Trend terms: value now minus value this many hours ago. 120 on the way up is a
# different situation from 120 on the way down, and no single lag says which.
TREND_SPANS = (3, 6)

# Neighbouring stations to average. Pollution arrives from somewhere, and
# neighbouring stations correlate — this is the flat-feature stand-in for the
# graph model the no-deep-learning rule excludes.
K_NEIGHBOURS = 3
NEIGHBOUR_LAG = 6

STATIONS_CACHE = os.path.join(os.path.dirname(CACHE), "stations.csv")
CELLS_CACHE = os.path.join(os.path.dirname(CACHE), "weather_cells.csv")
WEATHER_CACHE = os.path.join(os.path.dirname(CACHE), "weather.csv")

STATIONS_QUERY = """
SELECT station_id, station_name, latitude, longitude
FROM stations
WHERE latitude IS NOT NULL AND longitude IS NOT NULL
ORDER BY station_id
"""


def pull_stations(path: str = STATIONS_CACHE) -> None:
    """Coordinates, cached beside the history. One Neon wake.

    docs/stations.md carries the same numbers but keys them by the verbatim
    CPCB station name, and the history is keyed by station_id — joining the two
    by name would reintroduce exactly the whitespace-damaged string join this
    project already documents as fragile.
    """
    from db import connect

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(STATIONS_QUERY)
        rows = cur.fetchall()
    if not rows:
        sys.exit("no stations carry coordinates — run scripts/seed_stations.py")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(("station_id", "station_name", "latitude", "longitude"))
        w.writerows(rows)
    print(f"cached {len(rows)} stations -> {path}")


def load_coords(path: str = STATIONS_CACHE) -> dict[int, tuple[float, float]]:
    with open(path, encoding="utf-8") as fh:
        return {int(r["station_id"]): (float(r["latitude"]), float(r["longitude"]))
                for r in csv.DictReader(fh)}


def load_cells(path: str = CELLS_CACHE) -> dict[int, tuple[float, float]]:
    """Station IDs -> the Open-Meteo grid cell each station resolves to."""
    with open(path, encoding="utf-8") as fh:
        return {int(r["station_id"]): (float(r["cell_lat"]), float(r["cell_lon"]))
                for r in csv.DictReader(fh)}


# A reading of exactly 0.0 ug/m3 is a dead sensor, not clean air, and it is
# treated as MISSING rather than dropped from the table.
#
# Measured 2026-08-20 by notebooks/01_eda_cleaning.ipynb over 192,666 rows:
# 4,379 exact zeros (2.27%) across 29 of 30 stations. Two facts settle it —
# 58% of those hours sit in runs longer than 24h (29 such runs, longest 272h),
# and 81% of zero-runs BEGIN in the hour after a reading above 20 ug/m3, median
# 64.4 and max 1000.0. Air does not fall from 1000 to 0 in one hour.
#
# The range clamp at backfill_openaq.py:102 accepts these because 0.0 is inside
# [0, 2000]. Fixing it there would rewrite history for a rule that may change;
# masking here keeps the raw table raw and the rule in one visible place.
DROP_EXACT_ZERO = True

EPOCH = pd.Timestamp("1970-01-01", tz="utc")


def to_hour(timestamps: pd.Series) -> pd.Series:
    """Timestamps -> epoch hour, WITHOUT assuming a datetime resolution.

    The obvious `.astype("int64") // 10**9 // 3600` is wrong here and wrong
    silently: pandas 3.0 parses these to datetime64[us], not [ns], so that
    expression is off by 1000x and folds 13,000 hours of history into 14. Every
    fold then reports zero test rows, which reads as a bad fold boundary rather
    than a unit bug.

    Dividing two pandas objects asks pandas for the unit instead of asserting
    one, so this stays correct if the parsed resolution changes again.
    """
    return (timestamps - EPOCH) // pd.Timedelta("1h")


def load_weather(path: str = WEATHER_CACHE) -> dict[tuple[float, float], pd.DataFrame]:
    """Open-Meteo cache -> one epoch-hour-indexed frame per forecast cell."""
    raw = pd.read_csv(path)
    raw["epoch_hour"] = to_hour(pd.to_datetime(raw["hour"], utc=True))
    return {
        (float(cell_lat), float(cell_lon)): group.drop(
            columns=["cell_lat", "cell_lon", "hour"]
        ).set_index("epoch_hour").sort_index()
        for (cell_lat, cell_lon), group in raw.groupby(["cell_lat", "cell_lon"])
    }


def load_wide(path: str = CACHE) -> pd.DataFrame:
    """CSV -> one column per station, one row per hour, NO GAPS IN THE INDEX.

    The index is the epoch hour (seconds // 3600), the same unit baselines.py
    uses, so hour t+h is t+h with no date arithmetic. Reindexing onto the
    complete range is what makes .shift(n) mean exactly n hours — on the raw
    rows it would mean "n rows back", which across a 14-hour outage is a
    silently wrong lag.
    """
    raw = pd.read_csv(path, parse_dates=["observation_ts"])
    raw["hour"] = to_hour(raw["observation_ts"])
    wide = raw.pivot_table(index="hour", columns="station_id", values="value")
    full = pd.RangeIndex(int(wide.index.min()), int(wide.index.max()) + 1)
    wide = wide.reindex(full)
    if DROP_EXACT_ZERO:
        # Masked, not deleted: a dead sensor produces the same absence as an
        # offline one, and the gap policy already handles absence correctly.
        wide = wide.mask(wide == 0.0)
    return wide


def neighbours(coords: dict[int, tuple[float, float]], k: int = K_NEIGHBOURS
               ) -> dict[int, list[int]]:
    """station_id -> its k nearest other stations, by great-circle distance.

    Haversine rather than plain Euclidean on lat/lon: a degree of longitude is
    ~97 km at Haryana's latitude against 111 km for a degree of latitude, so a
    Euclidean nearest-neighbour would be stretched east-west by ~13%.
    """
    def haversine(a, b):
        lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
        h = (math.sin((lat2 - lat1) / 2) ** 2
             + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
        return 2 * 6371.0 * math.asin(math.sqrt(h))

    out = {}
    for s, here in coords.items():
        others = sorted((haversine(here, there), o)
                        for o, there in coords.items() if o != s)
        out[s] = [o for _, o in others[:k]]
    return out


def _cyclical(values: np.ndarray, period: int) -> tuple[np.ndarray, np.ndarray]:
    """sin/cos of a calendar value.

    Hour 23 and hour 0 are one hour apart; as integers they are 23 apart, so a
    linear model reads 11pm and midnight as maximally different. On a circle
    they are adjacent again, which is the truth.
    """
    radians = 2 * math.pi * values / period
    return np.sin(radians), np.cos(radians)


def _station_block(wide: pd.DataFrame, station: int, horizon: int,
                   nbrs: dict[int, list[int]] | None,
                   spatial: bool, cyclical: bool,
                   min_present: float, weather: bool = True,
                   blh: str = "none",
                   weather_frame: pd.DataFrame | None = None) -> pd.DataFrame:
    """One station's rows, indexed by ISSUE hour.

    Every column below is a backward shift or a trailing window, so no value in
    a row can carry a timestamp later than that row's index. Forecast values
    are also allowed to carry target-hour timestamps because they were issued
    before the issue hour; `_target` is future by definition.
    """
    own = wide[station]
    block = pd.DataFrame(index=wide.index)

    for lag in LAGS:
        block[f"lag_{lag}"] = own.shift(lag)

    for w in ROLL_WINDOWS:
        # center=False is the default and is passed anyway: a centred window
        # reads forward, and that is the one change that would reintroduce the
        # need for a purged split. tests/test_features.py greps for it.
        roll = own.rolling(w, min_periods=max(1, int(math.ceil(w * min_present))),
                           center=False)
        block[f"roll_mean_{w}"] = roll.mean()
        block[f"roll_std_{w}"] = roll.std()

    for span in TREND_SPANS:
        block[f"trend_{span}"] = own - own.shift(span)

    if spatial and nbrs is not None:
        near = wide[nbrs[station]].mean(axis=1)
        block["nbr_mean"] = near
        block[f"nbr_mean_lag_{NEIGHBOUR_LAG}"] = near.shift(NEIGHBOUR_LAG)
        block["regional_mean"] = wide.mean(axis=1)

    if cyclical:
        ist = pd.to_datetime(wide.index * 3600, unit="s", utc=True).tz_convert(IST)
        for name, values, period in (("hour", ist.hour, 24),
                                     ("dow", ist.dayofweek, 7),
                                     ("month", ist.month, 12)):
            s, c = _cyclical(np.asarray(values, dtype=float), period)
            block[f"{name}_sin"], block[f"{name}_cos"] = s, c

    if weather and horizon in (24, 48):
        if weather_frame is None:
            raise ValueError(f"no weather frame for station {station}")
        lead = "previous_day1" if horizon == 24 else "previous_day2"
        for name, column in (("wx_wind_speed", "wind_speed_10m"),
                             ("wx_temp", "temperature_2m"),
                             ("wx_rh", "relative_humidity_2m"),
                             ("wx_precip", "precipitation")):
            block[name] = weather_frame[f"{column}_{lead}"].shift(-horizon)
        direction = weather_frame[f"wind_direction_10m_{lead}"].shift(-horizon)
        sin, cos = _cyclical(direction.to_numpy(dtype=float), 360)
        block["wx_wind_dir_sin"], block["wx_wind_dir_cos"] = sin, cos
        if blh == "issue":
            block["wx_blh_issue"] = weather_frame["boundary_layer_height"]
            block["wx_blh_trend_24"] = (weather_frame["boundary_layer_height"]
                                        - weather_frame["boundary_layer_height"].shift(24))
        elif blh == "target":
            # Deliberately reads the answer key; this exists to measure a ceiling and must never become the default.
            block["wx_blh_target"] = weather_frame["boundary_layer_height"].shift(-horizon)

    block["station_id"] = station
    block["_target"] = own.shift(-horizon)
    return block


def build(wide: pd.DataFrame, horizon: int,
          coords: dict[int, tuple[float, float]] | None = None,
          spatial: bool = True, cyclical: bool = True,
          min_present: float = MIN_PRESENT, weather: bool = True,
          blh: str = "none",
          cells: dict[int, tuple[float, float]] | None = None,
          weather_frames: dict[tuple[float, float], pd.DataFrame] | None = None
          ) -> pd.DataFrame:
    """All stations stacked. One row = one forecast, indexed by issue hour.

    ROW ADMISSION: a row survives only when the target is present AND lag_0 is
    present. Everything else may be NaN.

    That second condition is not a data-quality preference, it is the fairness
    rule. lag_0 is the persistence prediction, so requiring it means persistence
    can answer for every admitted row, which means every candidate is scored on
    an identical set of hours. Phase 3 settled this after scoring each baseline
    over whatever it happened to reach gave persistence 49,993 pairs against
    climatology's 7,534 — two error figures off two different sets of hours.
    """
    if blh not in {"none", "issue", "target"}:
        raise ValueError(f"blh must be one of none, issue, target; got {blh!r}")

    nbrs = neighbours(coords) if (spatial and coords) else None
    if spatial and nbrs is not None:
        # A station with history but no coordinates would silently lose its
        # spatial columns to NaN and read as a station whose neighbours are all
        # dark. Refuse instead.
        missing = [s for s in wide.columns if s not in nbrs]
        if missing:
            raise ValueError(f"no coordinates for station(s) {missing}; "
                             "re-run --pull-stations or pass spatial=False")

    if not (weather and horizon in (24, 48)):
        cells = weather_frames = None
    else:
        # Read from the cache unless a caller supplied frames. tests/test_features.py
        # supplies its own: a leak gate that loads the real gitignored cache is not
        # runnable on a fresh clone, and it cannot control what the forecast column
        # holds, which is the one thing the weather cases have to assert.
        if cells is None:
            cells = load_cells()
        if weather_frames is None:
            weather_frames = load_weather()
        # The completed hourly history index makes every shift an hourly lead,
        # including across outages in either source.
        weather_frames = {cell: frame.reindex(wide.index)
                          for cell, frame in weather_frames.items()}
        missing = [s for s in wide.columns if s not in cells]
        if missing:
            raise ValueError(f"no weather cell for station(s) {missing}; "
                             "re-run scripts/weather.py --pull-cells")
        missing_frames = [s for s in wide.columns if cells[s] not in weather_frames]
        if missing_frames:
            raise ValueError(f"no weather data for station(s) {missing_frames}; "
                             "re-run scripts/weather.py --pull")

    blocks = [_station_block(
        wide, s, horizon, nbrs, spatial, cyclical, min_present, weather, blh,
        weather_frames[cells[s]] if weather_frames is not None else None)
              for s in wide.columns]
    out = pd.concat(blocks)
    out.index.name = "issue_hour"
    return out[out["_target"].notna() & out["lag_0"].notna()]


def split_xy(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Feature columns and target, kept in one place so no caller can forget to
    drop `_target` from X — which would hand the model the answer."""
    return frame.drop(columns=["_target"]), frame["_target"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pull-stations", action="store_true",
                    help="cache station coordinates from Neon (one wake)")
    ap.add_argument("--describe", action="store_true",
                    help="print the column list and per-horizon row counts")
    args = ap.parse_args()

    if args.pull_stations:
        pull_stations()
        if not args.describe:
            return 0

    wide = load_wide()
    coords = load_coords() if os.path.exists(STATIONS_CACHE) else None
    if coords is None:
        print("no station cache — spatial features OFF. "
              "Run --pull-stations to enable them.\n")

    print(f"hourly grid: {len(wide)} hours x {len(wide.columns)} stations, "
          f"{int(wide.notna().sum().sum())} readings "
          f"({wide.notna().mean().mean():.1%} of slots present)\n")

    for h in HORIZONS:
        frame = build(wide, h, coords, spatial=coords is not None)
        X, y = split_xy(frame)
        wx_count = sum(column.startswith("wx_") for column in X.columns)
        print(f"horizon {h:>2}h: {len(X.columns)} feature columns "
              f"({wx_count} wx_ columns):")
        print("  " + ", ".join(X.columns))
        severe = int((y > SEVERE_ABOVE).sum())
        print(f"horizon {h:>2}h: {len(X):>7,} rows, {severe:>5,} severe, "
              f"{X.isna().mean().mean():.1%} of feature cells NaN\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

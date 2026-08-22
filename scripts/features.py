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

# Hours the label spans, starting at the target hour. 1 reproduces the stage 1
# and stage 3 tables exactly; scripts/classify.py raises it, because the decision
# a subscriber makes is about a stretch of the evening rather than one clock hour.
TARGET_WINDOW = 1

# Trend terms: value now minus value this many hours ago. 120 on the way up is a
# different situation from 120 on the way down, and no single lag says which.
TREND_SPANS = (3, 6)

# Tail features, off by default. They are aimed at the spike, which is where
# every failure so far has been, and they are gated so that the stage 1 and
# stage 3 tables stay reproducible to the decimal without them.
#
# Extra own-history lags: the longest ordinary lag is 48h, so nothing sees
# "the same hour last week".
TAIL_LAGS = (72, 168)

# A reading above this counts as an exceedance for the recency features. It is
# CPCB's Very Poor band, the same number classify.py defaults to, because a
# station that crossed the line twice yesterday is in a different regime.
EXCEED_ABOVE = 121.0
EXCEED_COUNT_WINDOWS = (24, 72)

# Cap on hours-since-last-exceedance. Uncapped, a clean station reads as an
# arbitrarily large number that swamps the split points; a week is long enough
# to mean "not recently".
SINCE_CAP = 168

# Window for the per-station z-score. Station means differ a lot and the model
# otherwise has to learn each one from station_id alone. A week is long enough
# to carry a level and short enough to follow a seasonal shift.
Z_WINDOW = 168

# Diwali (Lakshmi Puja night) in IST. This is NCR: the largest predictable spike
# of the year is a DATE, and it was not in the features until now.
#
# Only 2025-10-20 falls inside the history, so the model sees ONE instance and
# no fold can validate it. It is here because the cost is a column and the
# alternative is a model that provably cannot see the biggest event in the year,
# but do not read a gain on this column as evidence at n=1.
DIWALI = ("2024-11-01", "2025-10-20", "2026-11-08")
DIWALI_WINDOW = 10   # days either side; beyond this the column saturates

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


def _tail_block(own: pd.Series, index: pd.Index) -> dict[str, pd.Series | np.ndarray]:
    """Spike-shaped features. Every one is a backward window on this station.

    `own` is already reindexed onto the complete hourly grid, so every shift and
    every rolling window below is an exact number of HOURS, including across an
    outage. Nothing here reads past its own row.
    """
    out: dict[str, object] = {}
    for lag in TAIL_LAGS:
        out[f"lag_{lag}"] = own.shift(lag)

    for w in ROLL_WINDOWS:
        # For a spike the recent MAXIMUM is the obvious signal and only the mean
        # and std existed. min comes free from the same window and says whether
        # the air ever cleared.
        roll = own.rolling(w, min_periods=max(1, int(math.ceil(w * MIN_PRESENT))))
        out[f"roll_max_{w}"] = roll.max()
        out[f"roll_min_{w}"] = roll.min()

    # A missing hour counts as "no exceedance" rather than as unknown. The
    # alternative propagates NaN across every gap and empties the column on the
    # stations that go dark for 10-19h at a time, which is most of them.
    over = (own > EXCEED_ABOVE).astype(float)
    for w in EXCEED_COUNT_WINDOWS:
        out[f"exceed_count_{w}"] = over.rolling(w, min_periods=1).sum()

    # Hours since the last exceedance, INCLUDING the current hour (0 when the
    # air is over the line right now). ffill of "the hour this last happened" is
    # backward-looking by construction; before the first exceedance it is NaN,
    # which is the truth rather than a made-up large number.
    hours = pd.Series(np.asarray(index, dtype=float), index=index)
    last = hours.where(over > 0).ffill()
    out["hours_since_exceed"] = (hours - last).clip(upper=SINCE_CAP)

    # Per-station normalisation: how unusual is right now FOR THIS STATION.
    win = own.rolling(Z_WINDOW, min_periods=int(Z_WINDOW * MIN_PRESENT))
    mean, std = win.mean(), win.std()
    out[f"z_{Z_WINDOW}"] = (own - mean) / std.where(std > 0)
    return out


def _diwali_days(index: pd.Index) -> np.ndarray:
    """Signed days from the nearest Diwali, clipped to +/- DIWALI_WINDOW.

    A calendar column, so it carries no information about the future beyond what
    a wall calendar already carries. Negative is before, positive is after -
    signed rather than absolute because the smoke lingers for days afterwards
    and the run-up is not symmetric with it.
    """
    days = (pd.to_datetime(np.asarray(index) * 3600, unit="s", utc=True)
            .tz_convert(IST).normalize().tz_localize(None))
    offsets = np.stack([(days - pd.Timestamp(d)).days.to_numpy(dtype=float)
                        for d in DIWALI])
    nearest = offsets[np.abs(offsets).argmin(axis=0), np.arange(len(days))]
    return np.clip(nearest, -DIWALI_WINDOW, DIWALI_WINDOW)


def _station_block(wide: pd.DataFrame, station: int, horizon: int,
                   nbrs: dict[int, list[int]] | None,
                   spatial: bool, cyclical: bool,
                   min_present: float, tail: bool = False,
                   weather: bool = False,
                   blh: str = "none",
                   weather_frame: pd.DataFrame | None = None,
                   target_window: int = 1,
                   stale: int = 0) -> pd.DataFrame:
    """One station's rows, indexed by ISSUE hour.

    Every column below is a backward shift or a trailing window, so no value in
    a row can carry a timestamp later than that row's index. Forecast values
    are also allowed to carry target-hour timestamps because they were issued
    before the issue hour; `_target` is future by definition.

    `stale` withholds the newest `stale` hours of this station's own readings
    from every feature, the target excepted. It answers what the margin becomes
    on the input a 07:00 IST send actually has: OpenAQ publishes ~6.5h behind
    real time `[measured 2026-08-22]`, so the freshest reading at send time is
    the previous evening's. Persistence is staled with everything else, since
    lag_0 is its prediction and it faces the same lag in production.
    """
    own = wide[station]
    seen = own.shift(stale) if stale else own
    block = pd.DataFrame(index=wide.index)

    for lag in LAGS:
        block[f"lag_{lag}"] = seen.shift(lag)

    for w in ROLL_WINDOWS:
        # center=False is the default and is passed anyway: a centred window
        # reads forward, and that is the one change that would reintroduce the
        # need for a purged split. tests/test_features.py greps for it.
        roll = seen.rolling(w, min_periods=max(1, int(math.ceil(w * min_present))),
                            center=False)
        block[f"roll_mean_{w}"] = roll.mean()
        block[f"roll_std_{w}"] = roll.std()

    for span in TREND_SPANS:
        block[f"trend_{span}"] = seen - seen.shift(span)

    if tail:
        for name, values in _tail_block(seen, wide.index).items():
            block[name] = values
        block["diwali_days"] = _diwali_days(wide.index)

    if spatial and nbrs is not None:
        near = wide[nbrs[station]].mean(axis=1).shift(stale)
        block["nbr_mean"] = near
        block[f"nbr_mean_lag_{NEIGHBOUR_LAG}"] = near.shift(NEIGHBOUR_LAG)
        block["regional_mean"] = wide.mean(axis=1).shift(stale)

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
    point = own.shift(-horizon)
    if target_window > 1:
        # "Is this evening safe?" is a question about a stretch of hours, not
        # about 19:00 exactly. Scoring a single hour marks a model that was right
        # about 18:00 and wrong about 19:00 as a total miss, which is not the
        # mistake the subscriber experienced.
        #
        # Admission still keys on the POINT target, so the admitted rows are
        # identical to the target_window=1 case and the two tables compare. Only
        # the label changes.
        window = pd.concat([own.shift(-(horizon + i)) for i in range(target_window)],
                           axis=1).max(axis=1)
        block["_target"] = window.where(point.notna())
    else:
        block["_target"] = point
    return block


def build(wide: pd.DataFrame, horizon: int,
          coords: dict[int, tuple[float, float]] | None = None,
          spatial: bool = True, cyclical: bool = True,
          min_present: float = MIN_PRESENT, tail: bool = False,
          weather: bool = False,
          blh: str = "none", target_window: int = TARGET_WINDOW,
          cells: dict[int, tuple[float, float]] | None = None,
          weather_frames: dict[tuple[float, float], pd.DataFrame] | None = None,
          require_target: bool = True,
          stale: int = 0
          ) -> pd.DataFrame:
    """All stations stacked. One row = one forecast, indexed by issue hour.

    ROW ADMISSION: a row survives only when the target is present AND lag_0 is
    present. Everything else may be NaN.

    require_target=False keeps the rows whose target has not happened yet, which
    is every row a live forecast is issued from — scripts/forecast.py is the only
    caller. Scoring MUST leave it True: a NaN target cannot be graded, and the
    two would silently score different sets of hours.

    stale>0 withholds the newest `stale` hours of readings from the features —
    see _station_block. It moves lag_0, so the admitted rows are NOT the same
    rows as a stale=0 run and the two tables do not compare directly. Every
    candidate inside one run still sits the same exam, which is the comparison
    the question is about.

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
        wide, s, horizon, nbrs, spatial, cyclical, min_present, tail, weather, blh,
        weather_frames[cells[s]] if weather_frames is not None else None,
        target_window, stale)
              for s in wide.columns]
    out = pd.concat(blocks)
    out.index.name = "issue_hour"
    keep = out["lag_0"].notna()
    if require_target:
        keep &= out["_target"].notna()
    return out[keep]


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
    # Off by default here for the same reason it is off in benchmark.py: the
    # 2026-08-20 ablation put every weather variant inside the fold noise.
    ap.add_argument("--weather", action="store_true",
                    help="include the archived-forecast columns at 24h and 48h")
    ap.add_argument("--blh", choices=["none", "issue", "target"], default="none",
                    help="boundary-layer-height variant, with --weather")
    # Off by default so the stage 1 and stage 3 tables stay reproducible; the
    # ablation is what decides whether they earn a place, exactly as --spatial
    # and --weather were decided.
    ap.add_argument("--tail", action="store_true",
                    help="include the spike-shaped columns (long lags, rolling "
                         "max/min, exceedance recency, z-score, Diwali)")
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
        frame = build(wide, h, coords, spatial=coords is not None,
                      tail=args.tail, weather=args.weather, blh=args.blh)
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

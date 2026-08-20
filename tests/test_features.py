"""THE leak gate. No DB, no network, no ML library.

    python tests/test_features.py

Written before the model, on purpose. A leak makes the score look excellent, so
it cannot be found by looking at results — only by asserting the property
directly.

The trick that makes the assertion possible: the fixture is a RAMP where every
station's value at epoch hour t is exactly t (plus a per-station offset). So a
feature's numeric value IS the timestamp it came from. "No feature may exceed
issue time" then becomes plain arithmetic instead of a code review.
"""

import math
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))

import features  # noqa: E402

failures = []


def check(label, got, want):
    ok = (got != got and want != want) or (
        isinstance(got, float) and isinstance(want, float)
        and abs(got - want) < 1e-9) or got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


START = 490000          # an arbitrary epoch hour, ~2025-11
N = 600
STATIONS = (1, 2, 3, 4)
# Far enough apart that nearest-neighbour order is unambiguous: 1-2 and 3-4 are
# the close pairs. If two stations tied, the k-nearest test would pass or fail
# on sort order rather than on distance.
COORDS = {1: (28.40, 77.00), 2: (28.41, 77.01),
          3: (29.50, 75.00), 4: (29.51, 75.01)}
# Per-station offset so a wrong-station lookup is visible. Multiples of 100000
# keep the ramp value and the offset separable by eye in a failure message.
OFFSET = {1: 0, 2: 100000, 3: 200000, 4: 300000}


def ramp(drop: dict[int, set[int]] | None = None) -> pd.DataFrame:
    """value at hour t == t + station offset. `drop` punches hours PER STATION.

    Per station on purpose: a fixture that blanks the same hour everywhere
    cannot tell "this station's row was dropped" from "every station's row was
    dropped", and the row-admission rule is per station.
    """
    drop = drop or {}
    index = pd.RangeIndex(START, START + N)
    return pd.DataFrame(
        {s: [float("nan") if h in drop.get(s, ()) else float(h + OFFSET[s])
             for h in index]
         for s in STATIONS},
        index=index)


WIDE = ramp()

# Weather fixture. Two cells for four stations, mirroring the real network where
# 6 of 30 stations share a cell with a neighbour.
#
# The three series are separated by constants large enough to read off a failure
# message, so a wrong column, a wrong hour or a wrong cell each show up as a
# different number rather than as a near miss:
#
#     day1[t] = t                 day2[t] = t + 1e6      blh[t] = t + 2e6
#
# and cell B adds 5e5 on top. So "which column, which hour, which cell" is
# answerable from the value alone. That is the same trick the PM2.5 ramp uses.
CELLS = {1: (0.0, 0.0), 2: (0.0, 0.0), 3: (1.0, 1.0), 4: (1.0, 1.0)}
DAY2_MARK, BLH_MARK, CELL_MARK = 1_000_000.0, 2_000_000.0, 500_000.0
WX_VARS = ("wind_speed_10m", "wind_direction_10m", "temperature_2m",
           "relative_humidity_2m", "precipitation")


def weather_frames() -> dict[tuple[float, float], pd.DataFrame]:
    index = pd.RangeIndex(START, START + N)
    frames = {}
    for cell, mark in (((0.0, 0.0), 0.0), ((1.0, 1.0), CELL_MARK)):
        base = pd.Series([float(h) + mark for h in index], index=index)
        cols = {}
        for v in WX_VARS:
            cols[f"{v}_previous_day1"] = base
            cols[f"{v}_previous_day2"] = base + DAY2_MARK
        cols["boundary_layer_height"] = base + BLH_MARK
        frames[cell] = pd.DataFrame(cols, index=index)
    return frames


WX = weather_frames()


def build(wide, horizon, coords=None, **kw):
    """features.build with the fixture injected, never the real cache.

    Every call site in this file goes through here. A leak gate that reads the
    gitignored data/weather.csv does not run on a fresh clone, and it cannot
    control what the forecast column holds - which is the one thing the weather
    cases below have to assert.
    """
    kw.setdefault("cells", CELLS)
    kw.setdefault("weather_frames", WX)
    return features.build(wide, horizon, coords, **kw)


print("-- the core assertion: nothing may come from after issue time --")
# Only own-history columns carry a timestamp-as-value. Calendar columns are
# trigonometric and station_id is an identifier, so both are excluded by name.
CALENDAR = {"hour_sin", "hour_cos", "dow_sin", "dow_cos", "month_sin",
            "month_cos", "station_id", "_target"}

worst_excess = float("-inf")
checked_cells = 0
for h in features.HORIZONS:
    frame = build(WIDE, h, COORDS)
    for issue_hour, row in frame.iterrows():
        station = int(row["station_id"])
        for col, value in row.items():
            if col in CALENDAR or value != value:
                continue
            if col.startswith("trend_") or col.startswith("roll_std_"):
                continue  # differences and spreads, not timestamps
            if col.startswith("nbr_") or col == "regional_mean":
                continue  # mixed offsets; covered by its own case below
            if col.startswith("wx_"):
                # Weather is the one family allowed to carry a target-hour
                # value, because the forecast was published before issue time.
                # "Not later than issue hour" is the wrong test for it; the
                # weather section below asserts the right one instead.
                continue
            # Every remaining column is a level in this station's own units.
            worst_excess = max(worst_excess, value - OFFSET[station] - issue_hour)
            checked_cells += 1

check("own-history cells inspected", checked_cells > 20000, True)
check("NO feature value exceeds its issue hour", worst_excess <= 0, True)
check("lag_0 reaches issue hour exactly (excess is 0, not negative)",
      worst_excess, 0.0)

print("\n-- the target IS the future, and by exactly the horizon --")
for h in features.HORIZONS:
    frame = build(WIDE, h, COORDS)
    row = frame.iloc[len(frame) // 2]
    issue = frame.index[len(frame) // 2]
    station = int(row["station_id"])
    check(f"h={h}: target is issue+{h}",
          row["_target"] - OFFSET[station] - issue, float(h))
    check(f"h={h}: lag_0 is issue time itself",
          row["lag_0"] - OFFSET[station] - issue, 0.0)

print("\n-- lag_0 IS the persistence prediction --")
# This identity is what makes the row-admission rule a fairness rule: if
# persistence can always answer, every candidate is scored on the same rows.
frame24 = build(WIDE, 24, COORDS)
persistence_error = (frame24["_target"] - frame24["lag_0"]).abs().max()
check("persistence error on a +1/hr ramp at h=24 is exactly 24",
      float(persistence_error), 24.0)

print("\n-- every lag is the right distance back --")
row = frame24.iloc[300]
issue = frame24.index[300]
station = int(row["station_id"])
for lag in features.LAGS:
    check(f"lag_{lag} == issue - {lag}",
          row[f"lag_{lag}"] - OFFSET[station] - issue, float(-lag))

print("\n-- rolling windows END AT issue time, and are never centred --")
# On a +1/hr ramp the mean of the w hours ending at t is t - (w-1)/2.
for w in features.ROLL_WINDOWS:
    check(f"roll_mean_{w} is the trailing mean, not the centred one",
          row[f"roll_mean_{w}"] - OFFSET[station] - issue, -(w - 1) / 2)
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
    __file__))), "scripts", "features.py"), encoding="utf-8").read()
check("no center=True anywhere in features.py", "center=True" in src, False)

print("\n-- trend terms --")
for span in features.TREND_SPANS:
    check(f"trend_{span} on a +1/hr ramp is {span}",
          row[f"trend_{span}"], float(span))

print("\n-- gaps are NOT forward-filled --")
GAP_AT = START + 400
gapped = ramp(drop={1: {GAP_AT}})   # station 1 only
g = build(gapped, 24, COORDS)
# The row issued 3h after the hole must show NaN at lag_3, not the last value.
probe = g[(g["station_id"] == 1)].loc[GAP_AT + 3]
check("lag_3 across a 1h hole is NaN", probe["lag_3"] != probe["lag_3"], True)
check("lag_2 (before the hole) is unaffected",
      probe["lag_2"] - (GAP_AT + 3), -2.0)
check("lag_6 (reaching past the hole) is unaffected, NOT shifted by the missing row",
      probe["lag_6"] - (GAP_AT + 3), -6.0)

print("\n-- a long gap stays NaN; there is no 3h fill and no 19h fill --")
LONG = set(range(START + 200, START + 219))   # 19 hours, the observed block size
longgap = build(ramp(drop={1: LONG}), 24, COORDS)
one_lg = longgap[longgap["station_id"] == 1]
after = one_lg.loc[START + 220]
check("lag_2 into a 19h block is NaN", after["lag_2"] != after["lag_2"], True)
check("lag_12 into a 19h block is NaN", after["lag_12"] != after["lag_12"], True)
check("every hour of the block is absent as an issue hour",
      any(h in one_lg.index for h in LONG), False)
check("a station that was NOT punched keeps those hours",
      all(h in longgap[longgap["station_id"] == 2].index for h in LONG), True)

print("\n-- a row with no issue-time value is not admitted --")
check("the punched hour is absent as an issue hour for station 1",
      GAP_AT in g[g["station_id"] == 1].index, False)
check("but present for station 2, which was not punched",
      GAP_AT in g[g["station_id"] == 2].index, True)

print("\n-- a rolling window under half present is NaN --")
# At START+220 the trailing 24h window holds 490219 and 490220 only — 2 of 24,
# under min_periods 12. The trailing 6h window holds the same 2 of 6, under 3.
check("roll_mean_24 with 2 of 24 present is NaN",
      after["roll_mean_24"] != after["roll_mean_24"], True)
check("roll_mean_6 with 2 of 6 present is NaN",
      after["roll_mean_6"] != after["roll_mean_6"], True)
# Six clear hours after the block, the 6h window is whole again.
recovered = one_lg.loc[START + 224]
check("roll_mean_6 with 6 of 6 present is a number",
      recovered["roll_mean_6"] == recovered["roll_mean_6"], True)
check("roll_mean_6 is still the trailing mean after recovery",
      recovered["roll_mean_6"] - (START + 224), -2.5)
check("roll_mean_24 is still NaN there (only 6 of 24 present)",
      recovered["roll_mean_24"] != recovered["roll_mean_24"], True)

print("\n-- neighbours are the nearest, and are read at or before issue time --")
nb = features.neighbours(COORDS, k=1)
check("station 1's nearest is 2", nb[1], [2])
check("station 3's nearest is 4", nb[3], [4])
check("distance is haversine, not lat/lon Euclidean: 1's nearest is not 3",
      3 in nb[1], False)
nb3 = features.neighbours(COORDS, k=3)
check("k=3 returns exactly the other three", sorted(nb3[1]), [2, 3, 4])
check("a station is never its own neighbour", 1 in nb3[1], False)
# Ordered by distance, and the order is NOT numeric: station 4 is nearer to 1
# than station 3 is, because 4 sits 0.01 deg further east and longitude is the
# dominant term at this separation. Asserting [2, 3, 4] here would have been
# asserting sort-by-id and would pass with the distance code deleted.
check("neighbours are ordered nearest-first", nb3[1], [2, 4, 3])
# nbr_mean for station 1 with k=3 is the mean of stations 2,3,4 at issue time.
expected = (issue + OFFSET[2] + issue + OFFSET[3] + issue + OFFSET[4]) / 3
one = frame24[frame24["station_id"] == 1].loc[issue]
check("nbr_mean is the neighbours' value AT issue time", one["nbr_mean"], expected)
check(f"nbr_mean_lag_{features.NEIGHBOUR_LAG} is that, {features.NEIGHBOUR_LAG}h back",
      one[f"nbr_mean_lag_{features.NEIGHBOUR_LAG}"],
      expected - features.NEIGHBOUR_LAG)

print("\n-- calendar encoding is on a circle --")
# 24 hours apart is the same hour, so the sin/cos pair must be identical.
b = frame24[frame24["station_id"] == 1]
check("hour_sin repeats every 24h", b["hour_sin"].iloc[0],
      float(b["hour_sin"].iloc[24]))
check("hour_cos repeats every 24h", b["hour_cos"].iloc[0],
      float(b["hour_cos"].iloc[24]))
check("hour_sin^2 + hour_cos^2 == 1",
      float(b["hour_sin"].iloc[7] ** 2 + b["hour_cos"].iloc[7] ** 2), 1.0)
# 23:00 and 00:00 must be adjacent, not 23 units apart.
ist = pd.to_datetime(b.index * 3600, unit="s", utc=True).tz_convert(features.IST)
i23 = list(ist.hour).index(23)
i00 = list(ist.hour).index(0)
d = math.hypot(float(b["hour_sin"].iloc[i23]) - float(b["hour_sin"].iloc[i00]),
               float(b["hour_cos"].iloc[i23]) - float(b["hour_cos"].iloc[i00]))
check("hour 23 and hour 0 are one step apart on the circle",
      abs(d - 2 * math.sin(math.pi / 24)) < 1e-9, True)

print("\n-- split_xy never hands the model the answer --")
X, y = features.split_xy(frame24)
check("_target is not a feature column", "_target" in X.columns, False)
check("y is the target", float(y.iloc[0]), float(frame24["_target"].iloc[0]))
check("X and y are the same length", len(X), len(y))

print("\n-- to_hour: epoch hours, with NO assumed datetime resolution --")
# The one function the ramp fixture bypasses, so it needs its own case.
# pandas 3.0 parses these timestamps to datetime64[us]. The obvious
# `.astype("int64") // 10**9 // 3600` is then off by 1000x, folds 13,136 hours
# of history into 14, and surfaces as every fold reporting zero test rows —
# which reads as a bad fold boundary rather than as a unit bug.
known = pd.Series(pd.to_datetime(
    ["2025-02-18T19:30:00+00:00", "2025-02-18T20:30:00+00:00",
     "2026-08-20T03:30:00+00:00"]))
hours = features.to_hour(known)
check("first timestamp -> its true epoch hour", int(hours.iloc[0]), 483307)
check("one hour later is one hour later", int(hours.iloc[1] - hours.iloc[0]), 1)
check("the real 18-month span, in hours",
      int(hours.iloc[2] - hours.iloc[0]), 13136)
for unit in ("s", "ms", "us", "ns"):
    check(f"same answer at datetime64[{unit}]",
          int(features.to_hour(known.astype(f"datetime64[{unit}, UTC]")).iloc[0]),
          483307)

print("\n-- exact zeros are masked as missing, not taken as readings --")
# 4,379 rows (2.27%) in the real history are exactly 0.0. The EDA showed 58% of
# those hours sit in runs over 24h and 81% of runs start right after a reading
# above 20 ug/m3 — a dead sensor, not clean air. features.DROP_EXACT_ZERO masks
# them so the gap policy handles them like any other absence.
check("the rule is on", features.DROP_EXACT_ZERO, True)
import tempfile  # noqa: E402
with tempfile.TemporaryDirectory() as tmp:
    csv_path = os.path.join(tmp, "h.csv")
    with open(csv_path, "w", encoding="utf-8") as fh:
        fh.write("station_id,observation_ts,value\n")
        for i, val in enumerate([61.3, 0.0, 49.2, 0.0, 0.0, 55.0]):
            fh.write(f"1,2025-02-18T{10 + i:02d}:30:00+00:00,{val}\n")
    w = features.load_wide(csv_path)
    col = w[1]
    check("six hours land on six rows", len(col), 6)
    check("a real reading survives", float(col.iloc[0]), 61.3)
    check("an isolated zero becomes NaN", col.iloc[1] != col.iloc[1], True)
    check("a run of zeros becomes NaN", bool(col.iloc[3:5].isna().all()), True)
    check("zeros are MASKED, not dropped (the hour still exists)",
          int(col.isna().sum()), 3)
    check("nothing else is touched", float(col.iloc[5]), 55.0)

print("\n-- determinism --")
a1 = build(WIDE, 24, COORDS)
a2 = build(WIDE, 24, COORDS)
check("same input, byte-identical output", a1.equals(a2), True)

print("\n-- a missing coordinate is refused, not silently NaN --")
try:
    build(WIDE, 24, {k: v for k, v in COORDS.items() if k != 4})
    check("missing coords raises", "no exception", "ValueError")
except ValueError as exc:
    check("missing coords raises ValueError naming the station",
          "4" in str(exc), True)

print("\n-- weather: the forecast column, never the actuals --")
# Weather is the only family whose value may carry a target-hour timestamp, so
# the ramp rule above cannot police it. What has to hold instead is narrower and
# stricter: the value must be the RIGHT lead, read at the RIGHT hour, from the
# RIGHT cell. The fixture separates those three by constants, so a value that is
# off by 1e6 read day2, off by 5e5 read the neighbouring cell, and off by the
# horizon read the wrong hour.
#
# This is the case that matters. Training on what the weather actually did,
# rather than on what the forecast said a day earlier, is an answer key the
# model will never have in production - and it is invisible in the metrics,
# because it makes the score better.
SAFE_ISSUE = START + 200      # clear of both the ramp start and any 24h lookback
SAFE_STATION = 3              # in cell B, so a wrong-cell read shows up as CELL_MARK


def pick(frame, station=SAFE_STATION, issue=SAFE_ISSUE):
    """One row, chosen by station and issue hour rather than by position.

    Positional picking is what made the first version of this section fail: with
    four station blocks concatenated, iloc[len // 2] lands on the FIRST issue
    hour of a block, where a 24h backward difference is legitimately NaN.
    """
    rows = frame[frame["station_id"] == station]
    assert issue in rows.index, f"issue hour {issue} not admitted for station {station}"
    return rows.loc[issue]


for h, mark in ((24, 0.0), (48, DAY2_MARK)):
    frame = build(WIDE, h, COORDS, weather=True, blh="none")
    row = pick(frame)
    cell_mark = CELL_MARK if CELLS[SAFE_STATION] == (1.0, 1.0) else 0.0
    want = float(SAFE_ISSUE + h) + mark + cell_mark
    # The other lead read at the same hour. day2 sits DAY2_MARK above day1, so
    # at h=24 the wrong answer is higher and at h=48 it is lower.
    other = want + DAY2_MARK if h == 24 else want - DAY2_MARK
    check(f"h={h}: wx_wind_speed is the day{1 if h == 24 else 2} value at the TARGET hour",
          float(row["wx_wind_speed"]), want)
    check(f"h={h}: it is NOT that lead read at the ISSUE hour",
          float(row["wx_wind_speed"]) != float(SAFE_ISSUE) + mark + cell_mark, True)
    check(f"h={h}: it is NOT the other lead at the target hour",
          float(row["wx_wind_speed"]) != other, True)
    check(f"h={h}: it is NOT the neighbouring cell",
          float(row["wx_wind_speed"]) != want - CELL_MARK, True)
    check(f"h={h}: wind direction is sin/cos of that same hour, period 360",
          float(row["wx_wind_dir_sin"]), math.sin(2 * math.pi * want / 360))

print("\n-- weather: 6h and 12h carry no weather at all --")
# The lead-time archive is indexed in whole days, so these horizons have no
# matching lead. Feeding them day1 would be a 24h-old forecast wearing a 6h
# label, so they get nothing rather than something mislabelled.
for h in (6, 12):
    frame = build(WIDE, h, COORDS, weather=True, blh="target")
    wx = [c for c in frame.columns if c.startswith("wx_")]
    check(f"h={h}: zero wx_ columns even with blh=target", wx, [])
for h in (24, 48):
    frame = build(WIDE, h, COORDS, weather=True, blh="none")
    wx = sorted(c for c in frame.columns if c.startswith("wx_"))
    check(f"h={h}: the six forecast columns are present", len(wx), 6)

print("\n-- weather: the lid at issue time is backward-looking --")
# Variant B. This one is servable, so it must obey the ordinary rule.
for h in (24, 48):
    frame = build(WIDE, h, COORDS, weather=True, blh="issue")
    worst = float("-inf")
    n_cells = 0
    for issue_hour, row in frame.iterrows():
        cell_mark = 0.0 if CELLS[int(row["station_id"])] == (0.0, 0.0) else CELL_MARK
        v = row["wx_blh_issue"]
        if v == v:
            worst = max(worst, float(v) - BLH_MARK - cell_mark - issue_hour)
            n_cells += 1
    check(f"h={h}: wx_blh_issue cells inspected", n_cells > 1000, True)
    check(f"h={h}: wx_blh_issue never reads past the issue hour", worst <= 0, True)
    check(f"h={h}: and it reaches the issue hour exactly", worst, 0.0)
    check(f"h={h}: wx_blh_trend_24 is a 24h backward difference",
          float(pick(frame)["wx_blh_trend_24"]), 24.0)
    check(f"h={h}: and it is NaN where there is no 24h of history behind it",
          math.isnan(float(frame[frame["station_id"] == SAFE_STATION]
                           .loc[START, "wx_blh_trend_24"])), True)

print("\n-- weather: the leaky ceiling variant is leaky ON PURPOSE, and opt-in --")
# Variant C reads the lid at the target hour, which nobody knows at issue time.
# It exists to price what a perfect lid forecast would buy and must never ship.
# Asserting that it IS a leak is what stops it being mistaken for a safe column.
frame = build(WIDE, 24, COORDS, weather=True, blh="target")
row = pick(frame)
cell_mark = CELL_MARK if CELLS[SAFE_STATION] == (1.0, 1.0) else 0.0
check("wx_blh_target reads the TARGET hour, i.e. it is the answer key",
      float(row["wx_blh_target"]) - BLH_MARK - cell_mark - SAFE_ISSUE, 24.0)
check("and the issue-time lid would have been 24 hours lower",
      float(row["wx_blh_target"]) - float(pick(
          build(WIDE, 24, COORDS, weather=True, blh="issue"))["wx_blh_issue"]), 24.0)
check("blh defaults to none, so the leak is never on by accident",
      [c for c in build(WIDE, 24, COORDS).columns if "blh" in c], [])
try:
    build(WIDE, 24, COORDS, blh="lid")
    check("blh='lid' raises", "no exception", "ValueError")
except ValueError as exc:
    check("blh='lid' raises ValueError naming the bad value", "lid" in str(exc), True)

print("\n-- weather: a station with no grid cell is refused, not silently NaN --")
try:
    build(WIDE, 24, COORDS, weather=True,
          cells={k: v for k, v in CELLS.items() if k != 4})
    check("missing cell raises", "no exception", "ValueError")
except ValueError as exc:
    check("missing cell raises ValueError naming the station", "4" in str(exc), True)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks PASS")
sys.exit(0)

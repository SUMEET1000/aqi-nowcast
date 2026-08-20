"""The gate on the baseline arithmetic.

    python tests/test_baselines.py

No database, no network — the series below are built by hand, so every expected
number is known in advance rather than eyeballed off a real run.

The four cases are chosen for what they can catch, not for coverage:
a flat series proves persistence carries the value forward; a 24h sawtooth
proves seasonal persistence looks back exactly one day; a punched hole proves
gaps are skipped rather than filled; a +1/hour ramp makes persistence MAE equal
the horizon, which is the only cheap way to catch an off-by-one in it.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import env  # noqa: E402, F401
from baselines import HORIZONS, score  # noqa: E402

# Half the series trains, half is held out. It has to be a real split: score()
# only keeps a target hour that ALL THREE baselines can predict, so a cutoff of
# 0 leaves climatology untrained, silent, and every pair discarded.
#
# Hour 0 is 1970-01-01 05:30 IST, and 240 hours is ten days inside one month,
# so the training half covers all 24 IST hours and every climatology cell
# exists. The assertions below still name only the two lag baselines —
# climatology's job here is to be available, not to be accurate.
CUTOFF = 120

failures = []


def check(label, got, want):
    ok = got == want or (isinstance(want, float) and abs(got - want) < 1e-9)
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got {got}, want {want}")
    if not ok:
        failures.append(label)


def series(values, station=1, start=0):
    return {station: {start + i: v for i, v in enumerate(values)}}


HOURS = 240

# 1. Flat. Every baseline that carries a past value forward is exactly right.
flat = score(series([50.0] * HOURS), CUTOFF)
for h in HORIZONS:
    check(f"flat, persistence {h}h MAE", flat[("persistence", h)]["all"][0], 0.0)
    check(f"flat, seasonal {h}h MAE", flat[("seasonal persistence", h)]["all"][0], 0.0)

# 2. A 24h sawtooth: hour-of-day repeats exactly, nothing else does. Seasonal
# persistence must be perfect; persistence at 6h must not be.
saw = score(series([float(i % 24) for i in range(HOURS)]), CUTOFF)
check("sawtooth, seasonal 6h MAE", saw[("seasonal persistence", 6)]["all"][0], 0.0)
check("sawtooth, seasonal 48h MAE", saw[("seasonal persistence", 48)]["all"][0], 0.0)
# 48h back is still the same hour of day, so the sawtooth stays perfect there.
if saw[("persistence", 6)]["all"][0] == 0.0:
    failures.append("sawtooth, persistence 6h should not be perfect")
    print("FAIL  sawtooth, persistence 6h MAE is 0 — it is not looking back 6h")
else:
    print("PASS  sawtooth, persistence 6h MAE is non-zero")

# 3. A ramp of +1/hour. Persistence is wrong by exactly the horizon, which no
# off-by-one in the lookback can survive.
ramp = score(series([float(i) for i in range(HOURS)]), CUTOFF)
for h in HORIZONS:
    check(f"ramp, persistence {h}h MAE", ramp[("persistence", h)]["all"][0], float(h))
    # Whole days back, rounded up: 24h for the first three horizons, 48h at 48h.
    # A flat 24h there would read a value from after the forecast was issued.
    check(f"ramp, seasonal {h}h MAE",
          ramp[("seasonal persistence", h)]["all"][0], 24.0 if h <= 24 else 48.0)

# 4. Punch one hour out of the held-out half. Three target hours need it, so
# three pairs go: hour 200 itself, hour 206 (its persistence-6h lookback), and
# hour 224 (its seasonal 24h lookback — and a target is kept only when every
# baseline can answer it). A filled gap would keep all three.
holed = series([float(i) for i in range(HOURS)])
del holed[1][200]
after = score(holed, CUTOFF)
check("hole, persistence 6h pair count",
      ramp[("persistence", 6)]["all"][2] - after[("persistence", 6)]["all"][2], 3)

# 5. Severe is conditional on the TRUTH, not the prediction. 30 hours above the
# cut in a 240-hour flat series must give 30 severe pairs at every horizon that
# has a lookback for them.
mixed = series([50.0] * (HOURS - 30) + [300.0] * 30)
sev = score(mixed, CUTOFF)
check("severe pair count, persistence 6h",
      sev[("persistence", 6)]["severe"][2], 30)

print("\n" + ("FAILED: " + ", ".join(failures) if failures else "ALL PASS"))
sys.exit(1 if failures else 0)

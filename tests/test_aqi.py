"""The gate on the arithmetic. If 45 does not give 75, nothing downstream matters.

    python tests/test_aqi.py

No database, no network. The three worked examples below are CPCB's own, quoted
in docs/cpcb_aqi_breakpoints.md; an implementation that misses them disagrees
with every other app, which is exactly the credibility failure build plan §3.1
warns about.

The clamp case is not decoration. Gurugram exceeds 250 µg/m³ PM2.5 routinely in
November, and extrapolating past the top breakpoint there would print an index
above 500.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

# aqi.py imports nothing at all, deliberately, so the UTF-8 console fix has to
# be pulled in here or the µg/m³ in the labels below prints as '?' (see env.py).
import env  # noqa: E402, F401
from aqi import (ADVISORY, ADVISORY_LONG, BANDS,  # noqa: E402
                 band_of_index, overall_aqi, pm25_band, sub_index)

failures = 0


def check(label: str, got, want) -> None:
    global failures
    if got == want:
        print(f"  ok    {label}")
    else:
        failures += 1
        print(f"  FAIL  {label}\n        got  {got!r}\n        want {want!r}")


def check_raises(label: str, fn, exc) -> None:
    global failures
    try:
        result = fn()
    except exc:
        print(f"  ok    {label}")
        return
    except Exception as e:
        failures += 1
        print(f"  FAIL  {label}\n        raised {type(e).__name__}: {e}, want {exc.__name__}")
        return
    failures += 1
    print(f"  FAIL  {label}\n        returned {result!r}, want {exc.__name__}")


print("CPCB's own worked examples (docs/cpcb_aqi_breakpoints.md):")
check("PM2.5 31 -> 51", sub_index("PM2.5", 31), 51)
check("PM2.5 45 -> 75", sub_index("PM2.5", 45), 75)
check("PM2.5 60 -> 100", sub_index("PM2.5", 60), 100)
print()

print("Band edges hold, so the table was transcribed and not approximated:")
check("PM2.5 0 -> 0", sub_index("PM2.5", 0), 0)
check("PM2.5 30 -> 50", sub_index("PM2.5", 30), 50)
check("PM2.5 90 -> 200", sub_index("PM2.5", 90), 200)
check("PM2.5 250 -> 400", sub_index("PM2.5", 250), 400)
check("PM10 100 -> 100", sub_index("PM10", 100), 100)
check("CO is mg/m³: 2.0 -> 100", sub_index("CO", 2.0), 100)
check("OZONE is 8-hourly but the table is the same shape: 168 -> 200",
      sub_index("OZONE", 168), 200)
print()

print("Above the top breakpoint the index clamps to 500 and never extrapolates:")
check("PM2.5 251 -> 500", sub_index("PM2.5", 251), 500)
check("PM2.5 400 -> 500", sub_index("PM2.5", 400), 500)
check("PM2.5 2000 -> 500 (not 1900-something)", sub_index("PM2.5", 2000), 500)
check("PM10 900 -> 500", sub_index("PM10", 900), 500)
print()

print("A pollutant with no CPCB breakpoints is a visible failure, not a skipped one:")
check_raises("an eighth pollutant raises", lambda: sub_index("PM1", 20), KeyError)
check_raises("a negative concentration raises", lambda: sub_index("PM2.5", -1), ValueError)
print()

print("The headline band, which is the concentration table and not the index:")
check("30 is Good", pm25_band(30), "Good")
check("31 is Satisfactory", pm25_band(31), "Satisfactory")
check("90 is Moderate", pm25_band(90), "Moderate")
check("121 is Very Poor", pm25_band(121), "Very Poor")
check("251 is Severe", pm25_band(251), "Severe")
print()

print("Index -> category:")
check("50 is Good", band_of_index(50), "Good")
check("101 is Moderate", band_of_index(101), "Moderate")
check("400 is Very Poor", band_of_index(400), "Very Poor")
check("500 is Severe", band_of_index(500), "Severe")
print()

print("CPCB's >=3-pollutant rule is enforced, and a refusal carries its reason:")

# These inputs are observations.value_avg, which the feed publishes as CPCB
# SUB-INDICES rather than concentrations (measured 2026-08-19 by
# scripts/compare_sources.py). So the numbers below are already index values and
# the overall AQI is the worst of them, untouched.
overall, reason = overall_aqi({"PM2.5": 139.0, "PM10": 120.0, "NO2": 45.0})
check("three pollutants with PM2.5 -> an AQI", (overall.aqi, overall.band),
      (139, "Moderate"))
check("the AQI is the worst sub-index, not an average", overall.aqi, 139)
check("the dominant pollutant is named", overall.dominant, "PM2.5")
check("no refusal reason when it computed", reason, None)

# THE regression. Until 2026-08-19 overall_aqi ran each value_avg through
# sub_index a second time, so a feed value of 157 — a true AQI of 157, which is
# Moderate — was read as 157 µg/m³ and came back 301+, Very Poor. That band
# selected the health sentence a subscriber read, so the bug was worst exactly
# where it mattered. Both halves are checked: the right answer, and the specific
# wrong one.
overall, _ = overall_aqi({"PM2.5": 157.0, "PM10": 100.0, "NO2": 50.0})
check("a feed value of 157 is an AQI of 157", overall.aqi, 157)
check("and its band is Moderate", overall.band, "Moderate")
check("NOT the Very Poor that double-applying the table produced",
      overall.band != "Very Poor", True)
check("and sub_index still converts a real concentration",
      sub_index("PM2.5", 45.0), 75)

# The case the advisory decision exists for: PM2.5 is not the worst pollutant,
# so keying CPCB's sentence to the PM2.5 band would understate the risk
# (docs/cpcb_aqi_breakpoints.md, "Resolved 2026-08-10").
overall, _ = overall_aqi({"PM2.5": 60.0, "PM10": 250.0, "NO2": 45.0})
check("PM10 dominates when it is worse", overall.dominant, "PM10")
check("and the band is PM10's, not PM2.5's Satisfactory", overall.band, "Poor")

overall, reason = overall_aqi({"PM2.5": 72.0, "PM10": 140.0})
check("two pollutants -> no AQI", overall, None)
check("and the reason says so", "CPCB requires 3" in (reason or ""), True)

overall, reason = overall_aqi({"NO2": 30.0, "SO2": 12.0, "OZONE": 40.0})
check("three pollutants but neither PM2.5 nor PM10 -> no AQI", overall, None)
check("and the reason names the rule",
      "neither PM2.5 nor PM10" in (reason or ""), True)

# The NULL path is the common one, not an edge case: 'NA' becomes NULL at
# ingest and Phase 0 measured 7 per snapshot.
overall, reason = overall_aqi({"PM2.5": 72.0, "PM10": None, "NO2": 30.0, "SO2": None})
check("a NULL contributes no sub-index, dropping this to 2", overall, None)
check("and it is refused rather than computed from what is left",
      "2 pollutant(s)" in (reason or ""), True)

overall, _ = overall_aqi({"PM2.5": None, "PM10": 140.0, "NO2": 30.0, "SO2": 12.0})
check("PM10 alone satisfies the either-or rule", overall is not None, True)

# CO is back in, and this is the other half of the same regression. Its unit was
# never unknown — the feed publishes a sub-index, so a CO of 88 is an AQI of 88.
# Read as mg/m³ it clamped to 500 and dominated 93.3% of station-hours, which is
# what EXCLUDED_FROM_OVERALL was invented to suppress.
overall, reason = overall_aqi({"PM2.5": 72.0, "NO2": 30.0, "CO": 88.0})
check("CO counts toward CPCB's 3 like any other pollutant",
      overall is not None, True)
check("a CO of 88 is an AQI of 88, not 500", overall.aqi, 88)
check("and CO is named as dominant when it is worst", overall.dominant, "CO")
overall, _ = overall_aqi({"PM2.5": 139.0, "NO2": 30.0, "SO2": 12.0, "CO": 88.0})
check("but it does not dominate when PM2.5 is worse", overall.dominant, "PM2.5")
check("and the AQI is PM2.5's", overall.aqi, 139)

overall, reason = overall_aqi({})
check("no readings at all -> refused", overall, None)
check("and it does not crash on the empty join", "none" in (reason or ""), True)
print()

print("Every band has an advisory sentence, so no message can KeyError at send time:")
for _lo, _hi, name in BANDS:
    check(f"{name} has Source B text", bool(ADVISORY.get(name)), True)
    check(f"{name} has Source A text", bool(ADVISORY_LONG.get(name)), True)

print()
if failures:
    sys.exit(f"FAILED — {failures} check(s). Do not send anything computed by this.")
print("aqi OK — CPCB's worked examples reproduce and the >=3-pollutant rule holds.")

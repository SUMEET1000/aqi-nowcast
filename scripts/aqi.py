"""CPCB AQI arithmetic and the advisory table. Pure — no database, no network.

Everything here is transcribed from docs/cpcb_aqi_breakpoints.md, which cites
CPCB's own documents page by page. Nothing in this file is invented and nothing
is paraphrased: the health sentences are quoted, and the quote is the liability
shield (build plan §5, "we do not write our own medical guidance").

Kept pure on purpose, like ingest.build_rows — it is the riskiest logic in
Phase 2 and it costs nothing to test. tests/test_aqi.py runs it against CPCB's
own worked examples in CI, with no Neon wake.

This computation lives in Python rather than in the bot Worker's JavaScript
because Phase 4 needs the identical numbers next to the model, and the same
breakpoint table written in two languages goes stale in one of them.
"""

from typing import NamedTuple

# Breakpoints, from the table in docs/cpcb_aqi_breakpoints.md — verified there
# against two independent sources that agree on all eight pollutants.
#
# Keys are the API's own pollutant_id strings, which is what observations
# stores. Note 'OZONE', not 'O3'.
#
# Each entry is (C_LO, C_HI, I_LO, I_HI). The concentration bounds are CPCB's
# integer band edges (31-60, not 30-60), and that detail is load-bearing: with
# 30 as the lower bound, CPCB's own worked example 45 -> 75 comes out 75.5.
#
# Units are µg/m³ except CO, which is mg/m³.
#
# The Severe band has no upper concentration in CPCB's table (PM2.5 is "250+"),
# so there is nothing to interpolate against. Concentrations past the top
# C_HI clamp to 500 — see sub_index. That leaves a discontinuity at the top
# edge (PM2.5 250 -> 400, 251 -> 500); it is CPCB's table shape, not ours.
BREAKPOINTS: dict[str, list[tuple[float, float, int, int]]] = {
    "PM2.5": [(0, 30, 0, 50), (31, 60, 51, 100), (61, 90, 101, 200),
              (91, 120, 201, 300), (121, 250, 301, 400)],
    "PM10":  [(0, 50, 0, 50), (51, 100, 51, 100), (101, 250, 101, 200),
              (251, 350, 201, 300), (351, 430, 301, 400)],
    "NO2":   [(0, 40, 0, 50), (41, 80, 51, 100), (81, 180, 101, 200),
              (181, 280, 201, 300), (281, 400, 301, 400)],
    "SO2":   [(0, 40, 0, 50), (41, 80, 51, 100), (81, 380, 101, 200),
              (381, 800, 201, 300), (801, 1600, 301, 400)],
    "NH3":   [(0, 200, 0, 50), (201, 400, 51, 100), (401, 800, 101, 200),
              (801, 1200, 201, 300), (1201, 1800, 301, 400)],
    "OZONE": [(0, 50, 0, 50), (51, 100, 51, 100), (101, 168, 101, 200),
              (169, 208, 201, 300), (209, 748, 301, 400)],
    "CO":    [(0, 1.0, 0, 50), (1.1, 2.0, 51, 100), (2.1, 10, 101, 200),
              (10.1, 17, 201, 300), (17.1, 34, 301, 400)],
}

# The index bands, in order. Used for both the overall AQI and — with the PM2.5
# concentration table below — for the headline band.
BANDS = [(0, 50, "Good"), (51, 100, "Satisfactory"), (101, 200, "Moderate"),
         (201, 300, "Poor"), (301, 400, "Very Poor"), (401, 500, "Severe")]

# PM2.5 concentration -> band, straight from the same doc. Not derived from
# sub_index: the product's headline is the concentration and its band, and
# deriving one from the other would make a change to the index table silently
# change the headline.
PM25_BANDS = [(30, "Good"), (60, "Satisfactory"), (90, "Moderate"),
              (120, "Poor"), (250, "Very Poor")]
PM25_BAND_ABOVE = "Severe"

# CPCB's Source B wording — "Health Statements for AQI Categories", page 13,
# column "Possible Health Impacts", CPCB Daily AQI Bulletin, 20 January 2025.
# Chosen over the longer Source A because it is what CPCB publishes every day,
# so it is unambiguously current, and it fits a Telegram message. Source A goes
# in the bot's /about, where length is free.
#
# Verbatim. Do not fix the grammar, shorten it, or "improve" it.
ADVISORY = {
    "Good":         "Minimal Impact",
    "Satisfactory": "Minor breathing discomfort to sensitive people",
    "Moderate":     "Breathing discomfort to the people with lungs, asthma and heart diseases",
    "Poor":         "Breathing discomfort to most people on prolonged exposure",
    "Very Poor":    "Respiratory illness on prolonged exposure",
    "Severe":       "Affects healthy people and seriously impacts those with existing diseases",
}

ADVISORY_CITATION = "CPCB Daily AQI Bulletin, p.13"

# CPCB's Source A, the canonical scheme document: Table 3.12 "Health Statements
# for AQI Categories", printed page 36 of the National Air Quality Index report
# (PDF issued 2015-06-22). Longer, and equally official. Carried for /about.
ADVISORY_LONG = {
    "Good": "Minimal Impact",
    "Satisfactory": "May cause minor breathing discomfort to sensitive people",
    "Moderate": "May cause breathing discomfort to the people with lung disease such as "
                "asthma and discomfort to people with heart disease, children and older adults",
    "Poor": "May cause breathing discomfort to people on prolonged exposure and discomfort "
            "to people with heart disease with short exposure",
    "Very Poor": "May cause respiratory illness to the people on prolonged exposure. Effect "
                 "may be more pronounced in people with lung and heart diseases",
    "Severe": "May cause respiratory effects even on healthy people and serious health "
              "impacts on people with lung/heart diseases. The health impacts may be "
              "experienced even during light physical activity",
}

ADVISORY_LONG_CITATION = ("CPCB, National Air Quality Index report, Table 3.12, p.36")

# CPCB's rule, quoted at the top of docs/cpcb_aqi_breakpoints.md: overall AQI is
# calculated only if at least three pollutants are available, one of which must
# be PM2.5 or PM10.
MIN_POLLUTANTS = 3
REQUIRED_EITHER = ("PM2.5", "PM10")

# CO is left out of the overall AQI because its unit in the data.gov.in feed is
# unknown, and the breakpoint table above cannot be applied to a number whose
# unit is unknown. The response carries no unit field for any pollutant.
#
# Measured 2026-08-13 over 2097 station-hours: read as mg/m³ — what CPCB's table
# expects — CO is the worst sub-index in 93.3% of them, 38.6% clamp to 500, and
# the median overall AQI is 382 (Very Poor) against 104 (Moderate) with CO left
# out. CPCB's own published AQI for Haryana in August is nothing like Very Poor,
# so mg/m³ is disproved by its consequence. What the unit actually is remains
# open: the feed's median of 31 would be plausible as 3.1 mg/m³, but a hypothesis
# that fits is not a measurement, and this number selects the health sentence a
# subscriber reads.
#
# Excluding it costs the ≥3-pollutant rule nothing — six pollutants remain — and
# understates only if CO is genuinely the worst, which PM2.5 and PM10 dominance
# in NCR makes unlikely. Settle it by comparing our AQI for a Haryana city
# against the same day's CPCB Daily AQI Bulletin, computed both ways; whichever
# matches names the unit. Then delete this constant.
EXCLUDED_FROM_OVERALL = ("CO",)


class Overall(NamedTuple):
    aqi: int
    band: str
    dominant: str  # the pollutant carrying the worst sub-index


def sub_index(pollutant: str, value: float) -> int:
    """CPCB's piecewise-linear sub-index. Raises on an unknown pollutant.

        I_p = (I_HI - I_LO) / (B_HI - B_LO) * (C_p - B_LO) + I_LO

    Rounded to an integer because CPCB publishes AQI as one, and because
    tests/test_aqi.py checks CPCB's worked examples exactly (45 -> 75, which is
    74.65 before rounding).

    A concentration above the top breakpoint clamps to 500 and is never
    extrapolated (docs/cpcb_aqi_breakpoints.md). Gurugram exceeds 250 µg/m³
    PM2.5 routinely in November, so this path will be hit every winter.

    Raises rather than returning None on an unknown pollutant (§0.5). CPCB
    adding an eighth pollutant must surface as a visible failure in one run,
    not as an AQI quietly computed from a subset — observations.pollutant_id is
    deliberately unconstrained precisely so an eighth one gets stored.
    """
    if pollutant not in BREAKPOINTS:
        raise KeyError(f"no CPCB breakpoints for pollutant {pollutant!r}")
    if value < 0:
        raise ValueError(f"negative concentration {value!r} for {pollutant}")

    for c_lo, c_hi, i_lo, i_hi in BREAKPOINTS[pollutant]:
        if value <= c_hi:
            # A value in the 1-unit gap between two bands (30 < v < 31) extends
            # the lower band rather than falling through. CPCB publishes
            # integers so the gap is not reachable from live data; this is what
            # the arithmetic does if that ever stops being true.
            return round((i_hi - i_lo) / (c_hi - c_lo) * (value - c_lo) + i_lo)

    return 500


def band_of_index(aqi: int) -> str:
    """Index -> CPCB category. 500 is the cap, so nothing lands above Severe."""
    for _lo, hi, name in BANDS:
        if aqi <= hi:
            return name
    return BANDS[-1][2]


def pm25_band(value: float) -> str:
    """PM2.5 concentration -> band. The headline the product actually reports."""
    for c_hi, name in PM25_BANDS:
        if value <= c_hi:
            return name
    return PM25_BAND_ABOVE


def overall_aqi(readings: dict[str, float | None]
                ) -> tuple[Overall | None, str | None]:
    """CPCB's overall AQI from one bulletin's pollutants, or a refusal reason.

    Exactly one half of the returned pair is set. A refusal is a normal outcome,
    not an error — CPCB's own rule produces one — so it is returned rather than
    raised, and the caller uses the documented degraded wording. What must never
    happen is an AQI computed from two pollutants and presented as CPCB's (§0.5).

    A NULL reading contributes no sub-index. 'NA' becomes NULL at ingest and
    Phase 0 measured 7 per snapshot, so this is the common case, not a guard.

    Pollutants in EXCLUDED_FROM_OVERALL are dropped before the count, so a
    station reporting PM2.5, CO and NO2 has two pollutants here, not three, and
    is refused. That is deliberate: an excluded pollutant is one we cannot
    score, and counting it toward CPCB's quorum would let it satisfy a rule it
    contributes nothing to.

    The returned pair is a tuple rather than an exception or a None-with-flags
    for the same reason cpcb_api.fetch returns (records, http_status): the
    caller has to handle both halves, and the signature says so.
    """
    available = {p: v for p, v in readings.items()
                 if v is not None and p in BREAKPOINTS
                 and p not in EXCLUDED_FROM_OVERALL}

    if len(available) < MIN_POLLUTANTS:
        return None, (f"only {len(available)} pollutant(s) reporting "
                      f"({', '.join(sorted(available)) or 'none'}); "
                      f"CPCB requires {MIN_POLLUTANTS}")

    if not any(p in available for p in REQUIRED_EITHER):
        return None, (f"neither PM2.5 nor PM10 is reporting "
                      f"({', '.join(sorted(available))}); CPCB requires one of them")

    indices = {p: sub_index(p, v) for p, v in available.items()}
    dominant = max(indices, key=lambda p: indices[p])
    aqi = indices[dominant]
    return Overall(aqi, band_of_index(aqi), dominant), None

# CPCB AQI — breakpoints and computation rules

Reference for Phase 2. Build plan §3.1: *"If we display 'AQI' we compute it ourselves.
Get the breakpoint table right or our numbers will disagree with every other app and users
will trust theirs."*

Recorded 2026-08-09.

---

## The rule that constrains our product

From CPCB, [How is AQI calculated?](https://airquality.cpcb.gov.in/ccr_docs/How_AQI_Calculated.pdf)
(read in full; quoted rules, not paraphrase):

1. Sub-indices are computed from the **24-hourly average** concentration — **8-hourly for CO
   and O₃**. The **worst sub-index is the AQI** for that location.
2. Overall AQI is calculated **only if at least three pollutants are available, one of which
   must be PM2.5 or PM10.** Otherwise data are insufficient and no AQI is produced.
3. A **minimum of 16 hours of data** is required to compute a sub-index.
4. The real-time system uses **running averages** (AQI at 6am covers 6am previous day → now).

### Consequence for this project — read before Phase 2

**We cannot compute a CPCB-comparable AQI from a single API snapshot.** The live feed gives
one reading per station per pollutant; a valid sub-index needs a 24h window with ≥16h of
data. Three options, in order of honesty:

- **Preferred: don't display "AQI" at all.** Show **PM2.5 in µg/m³** and the band it falls in.
  This is also better product design — the whole point of §1 is that we are not an AQI
  dashboard, and PM2.5 is the number the advisory actually keys off.
- If AQI is displayed later, compute it from **our own logged 24h history** once Phase 1 has
  ≥16h per station, and enforce the 3-pollutant / PM-required rule in code.
- **Never** label a single-hour sub-index as "AQI". It will disagree with every other app and
  §9's credibility argument collapses.

Also unresolved: it is **not documented whether the API's `avg_value` is an instantaneous
reading or an average over some window.** Phase 1 should log enough to determine this
empirically — if `avg_value` never changes faster than a 24h average plausibly could, it is
already averaged. Do not assume either way.

---

## Breakpoint table

Units µg/m³ except CO (mg/m³). Verified against two independent sources that agree exactly on
all eight pollutants: [aqihub.info/indices/india](https://aqihub.info/indices/india) and the
IND-AQI table reproduced from CPCB 2014
([ResearchGate](https://www.researchgate.net/figure/Breakpoints-of-different-pollutants-in-IND-AQI-CPCB-2014_tbl1_315725810)).

| Pollutant | Avg period | Good 0–50 | Satisfactory 51–100 | Moderate 101–200 | Poor 201–300 | Very Poor 301–400 | Severe 401–500 |
|---|---|---|---|---|---|---|---|
| **PM2.5** | 24h | 0–30 | 31–60 | 61–90 | 91–120 | 121–250 | 250+ |
| PM10 | 24h | 0–50 | 51–100 | 101–250 | 251–350 | 351–430 | 430+ |
| NO₂ | 24h | 0–40 | 41–80 | 81–180 | 181–280 | 281–400 | 400+ |
| SO₂ | 24h | 0–40 | 41–80 | 81–380 | 381–800 | 801–1600 | 1600+ |
| NH₃ | 24h | 0–200 | 201–400 | 401–800 | 801–1200 | 1201–1800 | 1800+ |
| O₃ | 8h | 0–50 | 51–100 | 101–168 | 169–208 | 209–748 | 748+ |
| CO (mg/m³) | 8h | 0–1.0 | 1.1–2.0 | 2.1–10 | 10.1–17 | 17.1–34 | 34+ |
| Pb | 24h | 0–0.5 | 0.51–1.0 | 1.1–2.0 | 2.1–3.0 | 3.1–3.5 | 3.5+ |

Note the **Severe band is unbounded above** (`250+` for PM2.5) while the index caps at 500.
Concentrations above the top breakpoint must clamp to 500, not extrapolate. Gurugram routinely
exceeds 250 µg/m³ in November, so this path *will* be hit.

## Sub-index formula

Piecewise-linear interpolation within the band:

```
I_p = (I_HI - I_LO) / (B_HI - B_LO) * (C_p - B_LO) + I_LO
```

`C_p` = concentration, `B_LO`/`B_HI` = band's concentration bounds, `I_LO`/`I_HI` = band's
index bounds. **AQI = max over available pollutants.**

Sanity checks from CPCB's own worked example — any implementation must reproduce these:

| PM2.5 µg/m³ | Expected sub-index |
|---|---|
| 31 | 51 |
| 45 | 75 |
| 60 | 100 |

## PM2.5 band lookup (what the bot actually needs)

Since the product reports PM2.5 directly, this is the table Phase 2 uses:

| PM2.5 (µg/m³) | Band |
|---|---|
| 0–30 | Good |
| 31–60 | Satisfactory |
| 61–90 | Moderate |
| 91–120 | Poor |
| 121–250 | Very Poor |
| 250+ | Severe |

For reference, at the 2026-08-09 probe: Ambala 49 (Satisfactory), Kurukshetra 48
(Satisfactory), Gurugram Teri Gram 169 (Very Poor), Charkhi Dadri 177 (Very Poor).

---

## ⚠ OPEN ITEM — blocking Phase 2, not Phase 0

**The verbatim CPCB health-advisory text for each band is NOT yet captured.**

§5 requires: *"Advisory text is quoted from CPCB's own published AQI advisory bands. We do not
write our own medical guidance. Health advice is a liability surface."*

Only paraphrases were obtainable from text sources; the authoritative table is inside scanned
CPCB PDFs that could not be parsed without adding a dependency (§0.6). The substance is
consistent across sources — Good "minimal impact" through Severe "affects healthy people,
serious impacts for those with existing disease" — but **substance is not a quote, and
paraphrased medical guidance is exactly the liability §5 forbids.**

Before Phase 2 ships any advisory string, obtain the exact wording from CPCB's
"Broad Guidelines for Public" / AQI report and paste it verbatim into this file with the page
cited. Until then the `profiles` table advisory column has no approved source text.

## Attribution

Air quality data © Central Pollution Control Board (CPCB), Ministry of Environment, Forest
and Climate Change, via data.gov.in. Attribution required in the README per §10.

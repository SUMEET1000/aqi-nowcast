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

These are **two separate decisions** and conflating them is what produced the blocker that
sat at the end of this file for a day.

**Decision 1 — the headline number is PM2.5 in µg/m³, and its band.** Not an AQI. §1's whole
point is that we are not an AQI dashboard, and PM2.5 is the pollutant we forecast. This does
not change.

**Decision 2 — the CPCB advisory sentence is keyed to the OVERALL AQI band**, because that is
the band CPCB wrote it against (fully specified in "Resolved" at the end of this file).
Keying it to the PM2.5 band understates risk whenever PM2.5 is not the worst pollutant.

Whether we can produce that overall AQI depends on one measurable fact, below.

**Never** label a single-hour sub-index as "AQI". It will disagree with every other app and
§9's credibility argument collapses.

### Is `avg_value` already averaged? — measured, not yet settled

It is **not documented whether the API's `avg_value` is an instantaneous reading or an average
over some window**, and the answer decides Decision 2: if CPCB already ships the averages, the
overall AQI is computable from a **single bulletin**; if not, we build the window ourselves out
of `observations` and CPCB's ≥16h-of-24 rule becomes our problem.

`scripts/probe_avg_window.py` measures it. Two tests, both of which assume `avg_value` is
already averaged over CPCB's own period for that pollutant and then try to **disprove** it:

- **A** — a running mean over `W` hours cannot move by more than `(max − min) / W` in one hour.
- **B** — a recently published average must fall inside this row's `[min, max]`, because the
  two windows overlap almost completely.

**Status 2026-08-11: NOT DISPROVED, on 26 bulletins over 40.0h (2026-08-09 17:30 → 2026-08-11
09:30 UTC), via `python scripts/probe_avg_window.py` (exit 0).** All seven pollutants — CO,
NH₃, NO₂, O₃, PM10, PM2.5, SO₂ — survived both tests: Test A found 0 impossible steps for the
five 24-hourly pollutants and ≤1% for the two 8-hourly ones, Test B found ≤1% envelope escapes
everywhere (PM2.5: 0 of 2293).

**This is a failure to reject, not a confirmation.** Neither test can prove a long window; they
can only break one, and neither broke. Three limits on what it is worth:

1. **The two tests are not independent** — both read the same `[min, max]` envelope. "A and B
   agree" is one measurement, not two.
2. **Test A's power is low exactly where it matters most.** The probe reports the share of pairs
   whose ceiling a 2-unit step could have exceeded: PM2.5 **20%**, PM10 15%, both barely over
   the `POWER_FLOOR = 0.10` that would have marked them `UNTESTABLE`. So for our headline
   pollutant, ~80% of pairs could not have registered a violation whatever the truth was.
3. Corroborated by hand on the Ambala seed station: PM2.5 drifted 48 → 40 over 18h, never moving
   more than 1 µg/m³ in an hour, with `min`/`max` spanning 8–56 — a day's range, not an hour's.

**Phase 2 therefore proceeds on the ASSUMPTION that the overall AQI is computable from a single
bulletin** — written here as an assumption with its date and sample size, which is the claim.
**Do not write it into the README as settled.** If Phase 4's lag features behave strangely, this
is the first thing to re-examine.

Test C priced the alternative on the same run: over 510 station-hours with a full 24h lookback,
CPCB's ≥16-of-24 rule would be met **89.2%** of the time, and the ≥3-pollutant overall-AQI rule
also 89.2%. So building the window ourselves is viable if this assumption ever fails.

**Probe bug found and fixed on this run:** the uncommitted 2026-08-11 edits (POWER column,
strict-inequality fix) added explanatory SQL comments containing literal `%` — `~17%`, `2%`,
`100%` — inside parameterised queries. psycopg scans the whole query string for placeholders, so
it raised `incomplete placeholder: '%'` and Test B crashed before running; Test C would have
crashed the same way. Doubled to `%%`. The 2026-08-10 run predates those comments, which is why
it got through. **A comment is not inert inside a `cur.execute()` string.**

Two things that probe got wrong at first, worth not repeating:

- **O₃ and CO are 8-hourly, everything else 24-hourly** (see the breakpoint table below).
  Testing all seven against 24h manufactured violations for the two 8-hourly ones.
- **The published values are integers**, so two rounded averages can overstate a step by up to
  1.0. Without that allowance the test flagged 14 PM2.5 pairs, every one of them a step of
  exactly 1.0 against a sub-1.0 ceiling — the probe's arithmetic, not CPCB's data.

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

## CPCB health-advisory text — VERBATIM (captured 2026-08-10)

This section closes the open item that blocked Phase 2. §5 requires: *"Advisory text is quoted
from CPCB's own published AQI advisory bands. We do not write our own medical guidance. Health
advice is a liability surface."* Everything below is CPCB's own wording, transcribed from
CPCB's own PDFs, with the source and page cited. **Nothing here is paraphrased. Do not
"improve" the wording, fix its grammar, or shorten it — the quote is the liability shield.**

> **Correction to the earlier note in this file.** It claimed the text was locked inside
> *scanned* CPCB PDFs. That was wrong. Only the front matter (the Chairman's covering letter)
> is a scan; the body is born-digital text — `About_AQI.pdf` is stamped *"Microsoft Word 2016"*
> — merely FlateDecode-compressed, which is why naive fetching returned binary. `pdftotext`
> reads it cleanly. The blocker was a tooling gap, not a source problem.

### CPCB publishes TWO different wordings. Both are official.

This was not expected and it is a real Phase 2 decision, not a formatting detail.

#### Source A — the canonical scheme document (long form)

**Table 3.12 "Health Statements for AQI Categories", printed page 36** (PDF page 38) of CPCB's
National Air Quality Index report — foreword by Shashi Shekhar, IAS, Chairman, CPCB, Ministry
of Environment, Forest & Climate Change; scheme launched in draft October 2014, PDF issued
2015-06-22. CPCB's NAQI page links it as **"Report on AQI"**:
`https://cpcb.gov.in/displaypdf.php?id=bmF0aW9uYWwtYWlyLXF1YWxpdHktaW5kZXgvRklOQUwtUkVQT1JUX0FRSV8ucGRm`

| AQI | Associated Health Impacts |
|---|---|
| Good (0–50) | Minimal Impact |
| Satisfactory (51–100) | May cause minor breathing discomfort to sensitive people |
| Moderate (101–200) | May cause breathing discomfort to the people with lung disease such as asthma and discomfort to people with heart disease, children and older adults |
| Poor (201–300) | May cause breathing discomfort to people on prolonged exposure and discomfort to people with heart disease with short exposure |
| Very Poor (301–400) | May cause respiratory illness to the people on prolonged exposure. Effect may be more pronounced in people with lung and heart diseases |
| Severe (401-500) | May cause respiratory effects even on healthy people and serious health impacts on people with lung/heart diseases. The health impacts may be experienced even during light physical activity |

Note the table's category label is **"Moderate"**, while the same document's narrative calls the
band "Moderately polluted". Both are CPCB's.

#### Source B — the daily AQI Bulletin (short form, and current)

**"Health Statements for AQI Categories", page 13, column headed "Possible Health Impacts"** —
CPCB Daily AQI Bulletin, 20 January 2025:
`https://cpcb.nic.in/upload/Downloads/AQI_Bulletin_20250120.pdf`

| AQI | Category | Possible Health Impacts |
|---|---|---|
| 0-50 | Good | Minimal Impact |
| 51-100 | Satisfactory | Minor breathing discomfort to sensitive people |
| 101-200 | Moderate | Breathing discomfort to the people with lungs, asthma and heart diseases |
| 201-300 | Poor | Breathing discomfort to most people on prolonged exposure |
| 301-400 | Very Poor | Respiratory illness on prolonged exposure |
| 401-500 | Severe | Affects healthy people and seriously impacts those with existing diseases |

**Confirmed 2026-08-10 (was a recommendation):** use **Source B** as the
`profiles` advisory text and cite it in the message. It is what CPCB publishes *every day*, so
it is unambiguously current, and it fits a Telegram message. Keep Source A for a `/about`
command where length is free. Whichever is chosen, the bot cites the document — never presents
the sentence as ours.

### Action guidance — only exists for Very Poor and Severe

CPCB's §3.4 is *"Broad Guidelines for Actions during Very Poor and Severe Categories of AQI"*.
There is **no per-band action advice** for Good through Poor; do not invent any to fill the
table. The public-facing half, verbatim from the same report (printed pages 36–37):

> People should maintain vehicles properly (e.g. get PUC checks, replace car air filter,
> maintain right tires pressure), follow lane discipline and speed limits, avoid prolong idling
> and turn off engines at red traffic signals. In addition, during severe or very poor AQI
> categories, people should minimize travel; avoid using private vehicles and instead use public
> transport, bikes or walk, and carpool; use smaller vehicles (e.g. avoid SUVs). The uses of
> diesel generators should be minimized. People, especially those suffering from heart diseases
> and asthma, may consider avoiding undue exposures.

CPCB also publishes this standalone as **"Broad guidelines for Public"**
(`https://cpcb.gov.in/displaypdf.php?id=bmF0aW9uYWwtYWlyLXF1YWxpdHktaW5kZXgvR3VpZGVsaW5lcy5wZGY=`),
which is the same text with one typo — it reads `filter,maintainright tires pressure`. Two
independent extractors reproduced that typo identically, so it is CPCB's, not ours. **If that
document is ever quoted, quote the report's clean version instead and say why.**

Note what this text mostly is: **civic advice about reducing emissions**, not personal health
protection. The only sentence a subscriber can act on for their own health is the last one. The
per-band *health statements* above, not this paragraph, are what the alert should carry.

### ✅ Resolved 2026-08-10 — the advisory is keyed to the overall AQI

**The problem.** CPCB's advisory is keyed to the overall AQI band — the *worst* sub-index
across ≥3 pollutants. Our headline number is PM2.5's sub-index alone. When PM2.5 dominates they
coincide (usually true in NCR winter); when it does not, the true AQI band is **worse** than the
band we show, so attaching CPCB's advisory to our PM2.5 band would **understate risk**, in the
direction that matters.

**The decision.** The message leads with **PM2.5 in µg/m³ and its band** — that is the product —
but the **CPCB advisory sentence is selected by the computed overall AQI band**. That removes
the understatement rather than disclaiming it.

Note what this is *not*: we do not become an AQI dashboard. The overall AQI selects a sentence;
it is not the headline, and §11's "no map, no city ranking, no current-AQI lookup" all still
hold.

**What Phase 2 implements.** Nothing here needs re-deciding; it is all specified above.

1. Per pollutant, compute the sub-index by piecewise-linear interpolation from the breakpoint
   table, using the pollutant's own averaging period (24h; **8h for O₃ and CO**).
2. **Clamp above the top breakpoint to index 500** — never extrapolate. Gurugram exceeds
   250 µg/m³ PM2.5 routinely in November, so this path *will* be hit.
3. Reproduce CPCB's worked examples exactly, as a test: PM2.5 31 → 51, 45 → 75, 60 → 100.
4. **Overall AQI = max of the available sub-indices**, but only if **≥3 pollutants** are
   available **and at least one of them is PM2.5 or PM10**. This is CPCB's rule, quoted at the
   top of this file. Enforce it in code.
5. Select the advisory sentence by that band. Use **Source B** (the daily-bulletin short form) —
   it is what CPCB publishes every day and it fits a Telegram message. Source A goes in
   `/about`, where length is free. Cite the document; never present the sentence as ours.

**NULL handling — the one place a silent fallback would be most tempting and most wrong (§0.5).**
`NA` becomes SQL NULL at ingest (Phase 0 found 7 per snapshot). A pollutant whose average is
NULL contributes **no sub-index**. If that leaves fewer than 3 pollutants, or leaves neither
PM2.5 nor PM10, then **no AQI is produced** — do not quietly compute one from what is left.

**The degraded path, when no AQI can be produced.** Fall back to naming exactly what the number
is, and say the official band may be worse:

> *"PM2.5 is 138 µg/m³ — Very Poor for PM2.5. Other pollutants are not included, so the official
> AQI may be higher."*

This was the old "Option 1". It is now the **documented fallback**, not the primary design. How
often it fires depends on the open measurement above: if CPCB already ships averaged values it
should be rare, and if we have to build the 24h window ourselves it may be the common path —
`probe_avg_window.py`'s Test C reports that fraction directly. **Settle that before writing the
message templates**, because it decides which of these two wordings users mostly see.

### Reproducing this capture

Sources were read with `pdftotext -layout` (poppler). Poppler is a **system binary, installed on
the dev machine — it is not a project dependency and `requirements.txt` is unchanged** (§0.6).
Every quote above was verified twice: the text extraction matches a rendered image of the same
page, and Sources A and B independently agree on substance while differing in wording.

## Attribution

Air quality data © Central Pollution Control Board (CPCB), Ministry of Environment, Forest
and Climate Change, via data.gov.in. Attribution required in the README per §10.

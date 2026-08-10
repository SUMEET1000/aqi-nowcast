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

**Recommendation for Phase 2, to be confirmed before any bot code:** use **Source B** as the
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

### ⚠ Still blocking, and it is a design question not a sourcing one

**CPCB's advisory is keyed to the overall AQI band. Our product reports the PM2.5 sub-index
band. These are not the same thing.**

Overall AQI is the *worst* sub-index across ≥3 pollutants. Our band is PM2.5's sub-index alone.
When PM2.5 dominates they coincide — usually true in NCR winter. When it does not, the true AQI
band is **worse** than the band we show, so attaching CPCB's advisory to our PM2.5 band would
**understate risk**, and would do it in the direction that matters.

Phase 2 must resolve this before it ships a message. Do not paper over it by relabelling. The
honest options, in order:

1. Say what the number is: *"PM2.5 is 138 µg/m³ — Very Poor for PM2.5. Other pollutants are not
   included, so the official AQI may be higher."* Quote the advisory for that band.
2. Compute a real multi-pollutant AQI from our own logged 24h history once Phase 1 has ≥16h
   across ≥3 pollutants (see the top of this file), and key the advisory off that.

Option 1 ships now and is truthful; option 2 is better and needs the data Phase 1 is collecting.

### Reproducing this capture

Sources were read with `pdftotext -layout` (poppler). Poppler is a **system binary, installed on
the dev machine — it is not a project dependency and `requirements.txt` is unchanged** (§0.6).
Every quote above was verified twice: the text extraction matches a rendered image of the same
page, and Sources A and B independently agree on substance while differing in wording.

## Attribution

Air quality data © Central Pollution Control Board (CPCB), Ministry of Environment, Forest
and Climate Change, via data.gov.in. Attribution required in the README per §10.

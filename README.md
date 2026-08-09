# AQI Nowcast

**Forecasts station-level PM2.5 6–48 hours ahead for Haryana/NCR and pushes a personalised
threshold alert to Telegram before you need to decide.** Every consumer air-quality tool tells
you what the air is like *right now*; this one tells a specific person whether a specific thing
— the school run, a morning walk, an outdoor shift — will be safe later today.

It is deliberately **not** an AQI dashboard, a map, or a city ranking. The forecast and the
personalised threshold are the entire product.

**Status: Phase 1 of 5 — the ingestion pipeline.** No model yet, by design: the data source is
a snapshot with no history, so every hour not logged is training data that can never be
recovered. Baselines land in Phase 3 and are written into this README *before* any model
exists, so the goalposts cannot quietly move.

---

## Why it is built in this order

| Phase | What | Gate | Status |
|---|---|---|---|
| 0 | Kill gates: does the data exist, do the IDs join | 3+ live PM2.5 stations, mapping doc | ✅ 2026-08-09 |
| 1 | The hourly logger | 72h of data, >95% fetch success | 🔨 in progress |
| 2 | Telegram bot, **no model** | 3 real users, 1 feedback row | ☐ |
| 3 | Baselines: persistence, seasonal, climatology | per-horizon table in this README | ☐ |
| 4 | The model — benchmarked, not assumed | beats persistence, or a documented negative | ☐ |
| 5 | Production discipline: drift, retraining, post-mortem | a dated real incident write-up | ☐ |

Shipping the bot before the model (Phase 2 before Phase 4) is intentional. It forces the
distribution problem to the front while it can still change decisions.

---

## Architecture

```
data.gov.in (CPCB hourly bulletin, 30 Haryana stations, 7 pollutants)
        │  snapshot only — no history, so it must be logged as it happens
        ▼
GitHub Actions cron (:05 and :35)  ──▶  scripts/ingest.py  ──▶  Neon Postgres
                                              │                    observations
                                              │                    fetch_log
                                              ▼                    stations
                                        idempotent upsert
                                    keyed on the bulletin timestamp
```

The ingester runs on GitHub Actions rather than inside a web service on purpose: a free web
dyno sleeps after 15 minutes idle and its cron dies silently.

---

## Setup

```bash
git clone <this repo> && cd "AQI project"
pip install -r requirements.txt
cp .env.example .env          # then fill it in
```

You need three things in `.env`:

1. **`DATA_GOV_IN_API_KEY`** — data.gov.in → My Account → API Key. Use a personal key; the demo
   key from blog tutorials is rate-limited across everyone who copied it.
2. **`OPENAQ_API_KEY`** — free signup at openaq.org. Goes in an `X-API-Key` header.
3. **`DATABASE_URL`** — neon.tech → new project → Connect. Must end in `?sslmode=require`.

Then:

```bash
python scripts/init_db.py        # create tables (idempotent)
python scripts/seed_stations.py  # load the 30 stations + their OpenAQ ids
python scripts/ingest.py         # one manual run
python tests/test_null_guard.py  # prove a null can never erase a reading
```

To run it unattended, push to GitHub and add `DATA_GOV_IN_API_KEY` and `DATABASE_URL` under
Settings → Secrets and variables → Actions. The workflow then fires every 30 minutes.

Gate commands (exit codes, not judgement calls):

```bash
python scripts/probe_cpcb.py --state Haryana   # Gate 0.1
python scripts/probe_history.py                # Gate 0.2 (~2 min, throttled)
python scripts/gate1_check.py                  # Gate 1, run 72h after go-live
```

`docs/stations.md` and `docs/station_mapping.md` are **generated**. Never hand-edit them; re-run
the script with `--write-doc`.

---

## What Phase 0 found that changed the design

These cost a day to discover and are the reason the probe scripts are kept rather than deleted.

- **The API's field names are `min_value` / `max_value` / `avg_value`**, not the
  `pollutant_min/max/avg` that most tutorials and our own draft spec assumed.
- **`last_update` is a single national bulletin timestamp**, identical on every row. So the
  usual "API time minus ingest time" staleness metric **cannot detect a single dead station** —
  it only detects a whole-feed stall. Per-station staleness has to come from value-change
  detection instead.
- **`NA` is the null sentinel.** `float()` raises on it and pandas silently reads it as the
  string `"NA"`. It becomes SQL `NULL` at parse time, and a `NULL` is never allowed to overwrite
  a real reading (there is a test for this, because the failure is otherwise silent).
- **Station names carry whitespace damage** — a trailing space, a double space — **and OpenAQ
  carries the identical damage.** So the exact-string join works, and "cleaning" one side alone
  would silently drop those stations from the join.
- **data.gov.in hangs for 45+ seconds on urllib's default User-Agent** and answers in ~0.4s with
  any ordinary one. It also drops the TLS handshake roughly 1 call in 3–4, which is why the
  fetch retries five times rather than three.
- **OpenAQ location date-spans overstate usable history by ~7×**, because a location's span
  covers every sensor it ever had *and the gaps between them*. Ambala advertises 2019→2026 but
  its PM2.5 sensors leave a ~3-year hole. History is measured per sensor.

---

## Known limitations

Stated here rather than discovered by a reader.

**Data**
- The live API is a snapshot. We are time-constrained by our own logging.
- **Station AQI is not doorstep AQI.** The Ambala station sits in Ambala City, several km from
  Ambala Cantt; the Kurukshetra station is ~5km from Pipli. Inherent to any station-based
  product.
- Missing hours are common. Forward-filling beyond ~3 hours is fabrication; the cutoff will be
  documented and enforced in code, not left to judgement.
- We **cannot** compute a CPCB-comparable AQI from the live snapshot — that needs a 24h window
  with ≥16h of data across ≥3 pollutants. The product reports **PM2.5 in µg/m³ and its band**
  instead. See `docs/cpcb_aqi_breakpoints.md`.

**Infrastructure**
- GitHub's scheduled runs are best-effort: delayed under load, occasionally skipped. The
  ingester runs every 30 minutes to absorb that, and `gate1_check.py` reports missing hours.
- **GitHub disables scheduled workflows on a repo with 60 days of no commits.** A dormant repo
  silently stops collecting.
- Neon's free tier is 0.5 GB. At 30 stations × 7 pollutants that is roughly a year before old
  data needs aggregating.

**Product**
- Telegram has a smaller India install base than WhatsApp. Chosen because the WhatsApp Business
  API is neither free nor solo-friendly — a tradeoff, not an oversight.
- Fixed thresholds fire constantly in November and never in July. Per-user cooldowns exist for
  this; relative thresholds may be needed.
- Seasonality cuts both ways: if the alert only matters for eight weeks a year, the pipeline and
  monitoring are what keep producing evidence the rest of the time.

---

## Privacy

The bot stores only a Telegram chat ID, a chosen station, and a chosen profile. **No
coordinates, no names, no health records** — under DPDP, the less held the less owed. Health
advisory text is quoted from CPCB's published bands; no medical guidance is written here.

Committed files name **stations, never people**. Tester identities live only in a gitignored
file.

---

## Attribution

- Air quality measurements: **Central Pollution Control Board (CPCB)**, via
  [data.gov.in](https://data.gov.in) resource `3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69` and
  [OpenAQ](https://openaq.org).
- Weather and air-quality reanalysis: [Open-Meteo](https://open-meteo.com) (CAMS model output on
  an ~11km grid — model data, not measurements).

This project is not affiliated with or endorsed by CPCB.

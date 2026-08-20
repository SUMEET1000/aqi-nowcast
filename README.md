# AQI Nowcast

**Forecasts station-level PM2.5 6–48 hours ahead for Haryana/NCR and pushes a personalised
threshold alert to Telegram before you need to decide.** Every consumer air-quality tool tells
you what the air is like *right now*; this one tells a specific person whether a specific thing
— the school run, a morning walk, an outdoor shift — will be safe later today.

It is deliberately **not** an AQI dashboard, a map, or a city ranking. The forecast and the
personalised threshold are the entire product.

**Status: Phase 3 of 5 — the baselines.** Phase 1's gate passed on 2026-08-13: 66 distinct
bulletins, 25 of 30 stations carrying a real PM2.5 value on every one, 98.6% run success over
214 runs. Phase 2's passed on 2026-08-14, the day the bot went live. No model yet, by design:
the data source is a snapshot with no history, so every hour not logged is training data that
can never be recovered. Baselines land in Phase 3 and are written into this README *before* any
model exists, so the goalposts cannot quietly move.

---

## Why it is built in this order

| Phase | What | Gate | Status |
|---|---|---|---|
| 0 | Kill gates: does the data exist, do the IDs join | 3+ live PM2.5 stations, mapping doc | ✅ 2026-08-09 |
| 1 | The hourly logger | 72h of data, >95% fetch success | ✅ 2026-08-13 |
| 2 | Telegram bot, **no model** | 3 real users, 1 feedback row | ✅ 2026-08-14 |
| 3 | Baselines: persistence, seasonal, climatology | per-horizon table in this README | 🔨 in progress |
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
Cloudflare Worker cron (:05, :35) ─┐
        │  workflow_dispatch       ├─▶ GitHub Actions ─▶ scripts/ingest.py ─▶ Neon Postgres
GitHub Actions cron (:13, :43) ────┘        job                  │              observations
        │  backup, throttled                                     │              fetch_log
                                                                 ▼              stations
                                                          idempotent upsert
                                                    keyed on the bulletin timestamp

Phase 2, reading the same database:

Cloudflare Worker cron (01:30 UTC = 07:00 IST)
        │  workflow_dispatch
        ▼
GitHub Actions ─▶ scripts/send_alerts.py ─▶ Telegram ─▶ one message per subscriber
                  PM2.5 + CPCB's advisory                    👍 / 👎
                  for the overall AQI band                     │
                                                               ▼
Telegram ──webhook──▶ Cloudflare Worker (bot/) ────────▶ Neon: subscribers, feedback
   /start, station, profile          takes taps, writes rows — no arithmetic
                                              │
                                              │  service binding (internal, no public URL)
                                              ▼
                                   Cloudflare Worker (trigger/) ─▶ same send, immediately
                                   holds the GitHub token
```

Subscribing fires that day's message straight away rather than leaving someone with nothing
until the next morning. It goes through `trigger/` rather than from `bot/` directly because
`bot/` is a public webhook URL and the GitHub token can start any workflow in this repo — one
that would run with the database credentials in scope. The send needs no "just this person"
argument: `send_alerts.py` skips anyone who already has a message for today, so an extra run
is a no-op for everybody else.

The ingester runs on GitHub Actions rather than inside a web service on purpose: a free web
dyno sleeps after 15 minutes idle and its cron dies silently.

**The trigger is external because GitHub's own one is throttled here.** GitHub Actions runs the
job reliably; what it does not do reliably is *start* it on a schedule. See `trigger/` for the
measurements and the setup.

---

## Setup

```bash
git clone <this repo> && cd "AQI project"
pip install -r requirements.txt
cp .env.example .env          # then fill it in
```

Two things in `.env` are required to run the logger:

1. **`DATA_GOV_IN_API_KEY`** — data.gov.in → My Account → API Key. Use a personal key; the demo
   key from blog tutorials is rate-limited across everyone who copied it.
2. **`DATABASE_URL`** — neon.tech → new project → Connect. Change the `sslmode=require`
   Neon gives you to **`?sslmode=verify-full`** plus an explicit CA bundle: `require` encrypts but
   authenticates nothing, so it does not stop anyone who can answer for the hostname from
   collecting the password and the data.

One more is optional, and only for one script:

3. **`OPENAQ_API_KEY`** — free signup at openaq.org, sent in an `X-API-Key` header. Read by
   `scripts/probe_history.py` and by nothing else, so the ingester, the gates, and every other
   script run fine without it. It was listed as required here for long enough to be worth
   saying plainly: skipping it costs you Gate 0.2, not the pipeline.

Then:

```bash
python scripts/init_db.py        # create tables (idempotent)
python scripts/seed_stations.py  # load the 30 stations + their OpenAQ ids
python scripts/ingest.py         # one manual run
python tests/test_null_guard.py  # prove a null can never erase a reading
```

For Phase 2 (the bot), add `TELEGRAM_BOT_TOKEN` from
[@BotFather](https://t.me/botfather) and:

```bash
python scripts/seed_profiles.py             # three profiles, idempotent
python tests/test_aqi.py                    # CPCB's worked examples: 31→51, 45→75, 60→100
python tests/test_message.py                # the message a person actually reads
python scripts/send_alerts.py --dry-run     # render every station, send nothing
python scripts/check_send_window.py         # is 07:00 IST still fresh enough?
```

The chat side is a separate Cloudflare Worker — see [`bot/`](bot/) for the deploy steps and
the webhook secret.

To run it unattended, push to GitHub and add `DATA_GOV_IN_API_KEY` and `DATABASE_URL` under
Settings → Secrets and variables → Actions. That alone gets you GitHub's own cron, which is
throttled on a new repository — see [`trigger/`](trigger/) to add the external clock that
actually fires on time.

Gate commands (exit codes, not judgement calls):

```bash
python scripts/probe_cpcb.py --state Haryana   # Gate 0.1
python scripts/probe_history.py                # Gate 0.2 (~2 min, throttled)
python scripts/gate1_check.py                  # Gate 1, run 72h after go-live
```

`docs/stations.md` and `docs/station_mapping.md` are **generated**. Never hand-edit them; re-run
the script with `--write-doc`.

---

## The daily message, and why it goes out at 07:00 IST

One message per subscriber per day: the measured PM2.5 at their station in µg/m³ and its band,
CPCB's AQI beside it labelled as the 24-hour index it is, then CPCB's own health statement for
the **overall** AQI band, quoted and cited. Two numbers from two sources, because they are two
different quantities over two different windows — saying so is the point. No forecast yet —
Phase 2 sends current readings only, and a threshold alert on a current reading would just
restate what is already out of the window.

The send time is measured, not chosen. **CPCB's feed freezes every morning**: over four days
the last morning bulletin was 05:00 IST and the next arrived between 10:00 and 13:00, with no
exceptions. Against the rule that a reading over 3 hours old must be flagged as stale, that
makes the hour load-bearing:

| Send at | Newest bulletin | Age | Result |
|---|---|---|---|
| 07:00 IST | 05:00 | 2.0h | clean |
| 08:00 IST | 05:00 | 3.0h | on the line |
| 09:00 IST | 05:00 | 4.0h | a staleness warning every single day |

A warning that fires daily teaches people to ignore warnings, which then hides the real one. So
07:00, and `scripts/check_send_window.py` re-measures the freeze on a rolling window and exits
non-zero naming a replacement hour if 07:00 ever stops clearing 3 hours. The constant is
checked, not remembered.

### Gate 2, on the day it passed

Deployed 2026-08-14. Four subscribers, four messages delivered, zero failed, four 👍 taps —
three of each from people who are not the author. That is the gate, and it is deliberately a
mechanical one: it tests that a stranger's tap reaches the database, not that anyone likes the
product.

**Retention is the number that matters here and it does not exist yet** — it cannot, on day
one. A subscriber count on its own says nothing, so this README will report retention alongside
it or drop the count entirely rather than let four look like traction. The feedback tap exists
precisely because asking friends produces politeness, and a 👍 six weeks from now will not.

---

## What a forecast has to beat

Measured **2026-08-20**, before any model exists, so the bar cannot move afterwards.

Three baselines over 29 Haryana stations, on **192,597 hourly OpenAQ readings** spanning
2025-02-18 to 2026-08-20. The held-out window is the **last 90 days**; climatology is fitted on
the training half only. Reproduce with `python scripts/baselines.py`.

| Baseline | Horizon | MAE | RMSE | n | MAE severe | RMSE severe | n severe |
|---|---|---|---|---|---|---|---|
| persistence | 6h | 23.57 | 51.57 | 6399 | 274.54 | 330.92 | 51 |
| persistence | 12h | 27.46 | 54.58 | 6354 | 275.15 | 324.50 | 50 |
| persistence | 24h | 27.91 | 57.85 | 6830 | 269.75 | 327.73 | 54 |
| persistence | 48h | 31.19 | 60.06 | 6694 | 283.87 | 334.74 | 55 |
| seasonal persistence | 6h | 28.08 | 58.17 | 6399 | 274.47 | 333.40 | 51 |
| seasonal persistence | 12h | 27.95 | 57.95 | 6354 | 260.36 | 317.40 | 50 |
| seasonal persistence | 24h | 27.91 | 57.85 | 6830 | 269.75 | 327.73 | 54 |
| seasonal persistence | 48h | 31.19 | 60.06 | 6694 | 283.87 | 334.74 | 55 |
| climatology | 6h | 33.01 | 52.15 | 6399 | 303.19 | 340.39 | 51 |
| climatology | 12h | 32.86 | 51.54 | 6354 | 294.47 | 329.54 | 50 |
| climatology | 24h | 32.63 | 51.51 | 6830 | 298.84 | 335.10 | 54 |
| climatology | 48h | 33.18 | 52.29 | 6694 | 302.66 | 338.07 | 55 |

MAE and RMSE in µg/m³. Severe = above 250 µg/m³, CPCB's top PM2.5 band.

**Persistence at 24h, MAE 27.91, is the number to beat.** Per horizon and with RMSE beside MAE,
because error grows with horizon and a single averaged figure hides that.

Four things about this table are worth stating rather than leaving a reader to find:

- **All three baselines are scored on the same hours.** A target hour counts only when every
  baseline can predict it. Letting each one use everything it happened to reach gave persistence
  49,993 pairs against climatology's 7,534 — climatology has nothing to say for a station whose
  training half contains no June, and most of these stations begin in September 2025. Two MAEs
  drawn from two different sets of hours invite exactly the comparison this table exists to
  support.
- **Gaps are never filled.** A missing hour removes the pair. Beyond about three hours a
  forward-fill is fabrication, and these sensors go dark in blocks of 10–19 hours.
- **The severe-band columns rest on ~50 pairs.** They are reported because a model that is
  accurate on ordinary air and blind to the spikes is useless for this product, but 50 pairs is
  not enough to separate two models. The August held-out window is the monsoon; the number that
  will count is the same table re-run after November.
- **Seasonal persistence reads the same hour a whole number of days back**, rounded up to the
  horizon — 24h at 6/12/24h, 48h at 48h. A flat 24h lag at the 48h horizon would read a value
  from after the forecast was issued, which is a leak. At the 24h and 48h horizons it is
  therefore identical to persistence by construction.

One station of thirty is absent: **NISE Gwal Pahari, Gurugram** returns HTTP 408 from OpenAQ's
hourly archive on every attempt. Live ingestion for it is unaffected.

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
- **The live feed's `avg_value` is CPCB's AQI sub-index, not a concentration — and this repo
  assumed it was µg/m³ for ten days, in a message real people received.** Caught on 2026-08-19
  by `scripts/compare_sources.py`, which checks the feed against OpenAQ's measured µg/m³ for the
  same CPCB sensors: over four stations and ~180 shared hours each, reading it as µg/m³ gives
  MAE 7.83–79.97, and reading it as a sub-index gives 0.20–15.18. The 0.20 is the
  integer-rounding floor. The discriminator was that the ratio between sources is not constant
  (~1.67 in the lowest band, 2.03 in a higher one) — a calibration factor cannot do that, a
  piecewise breakpoint table can.
  Two consequences shipped before it was found: the overall AQI ran every value through the
  breakpoint table a second time, reporting a true AQI of 157 (Moderate) as Very Poor, and the
  headline printed a sub-index labelled µg/m³. Both are fixed, and both are covered by named
  regression cases in `tests/test_aqi.py` and `tests/test_message.py`.
  **The lesson is the one worth carrying, not the bug.** An independent source had been mapped
  to all 30 stations since day one, by the Phase 0 gate, and went unused. `probe_avg_window.py`
  rigorously answered "is this a 24-hour average?" and never asked "is this a concentration at
  all?" — a correct answer to the wrong question. And when CO came out as the worst pollutant in
  93.3% of station-hours, that anomaly was suppressed with an exclusion constant and documented,
  rather than treated as evidence the input was misread. It was. CO's median of 31 is a
  sub-index of 31, the exclusion is deleted, and CO now scores like everything else.
  **Rule adopted: any number that reaches a user gets one independent-source check before it
  ships.** Self-consistency checks do not count — the ones here were self-consistent.
- The headline **PM2.5 in µg/m³ therefore comes from OpenAQ** (`pm25_history`), with CPCB's AQI
  reported beside it and labelled as a 24-hour index. OpenAQ is the fresher source at send
  time: measured 2026-08-19, CPCB's feed is frozen between 05:00 and ~11:00 IST while OpenAQ
  publishes through 06:00–09:00 IST, which is exactly when the 07:00 alert goes out.
- A CPCB-comparable AQI needs a 24h window (8h for O₃ and CO) with ≥16h of data across ≥3
  pollutants, so it cannot come from one reading. `scripts/probe_avg_window.py` failed to
  disprove that `avg_value` is already such an average, over 26 bulletins on 2026-08-11, and
  the cross-source check above independently supports it: correlation with OpenAQ rises from
  ~0.3 against raw hourly readings to **0.98** against a trailing 24-hour mean, on all four
  stations tested. That is now two lines of evidence rather than one, from different sources.

**Infrastructure**
- **GitHub's `schedule:` trigger is throttled on this repository, and the cron is not the
  lever.** Measured over the first 18 hours from the public Actions API: 11 of ~36 requested
  ticks delivered (31%), every one 15–22 minutes late, with holes of 102–167 minutes, and *not
  once* did two ticks land in the same hour. Moving the offsets off the contended `:05`/`:35`
  slots to `:13`/`:43` changed nothing and made the delay worse, which ruled out slot
  contention. Over the same window all four `workflow_dispatch` runs started **0 seconds** after
  being asked — the throttle is on the scheduled-event queue, not on the ability to run jobs.
  The fix is an external clock (`trigger/`) calling the dispatch API, with GitHub's cron left
  on as a free backup. Two bulletins survive in the database only because someone ran the
  ingester by hand during a 157-minute hole; CPCB publishes no archive, so a hole that wide
  loses data permanently.
- A missing bulletin hour has two possible authors and they need different fixes.
  `gate1_check.py` separates them: a run that returns a bulletin more than 2 hours old proves
  CPCB's feed was frozen across that span (`feed_stalled`), and only the remaining hours
  (`not_polled`) count against the reliability budget. CPCB's feed froze for 7 hours overnight
  on 2026-08-09/10 and published nothing; no polling cadence could have recovered those.
- **GitHub disables scheduled workflows on a repo with 60 days of no commits.** A dormant repo
  silently stops collecting.
- Neon's free tier is 0.5 GB storage and **100 CU-hours of compute per month**. At 30 stations ×
  7 pollutants, storage is roughly a year before old data needs aggregating. Compute is the
  constraint that governs how often the ingester may run, because **Neon bills per wake, not
  per second**: it suspends after 5 minutes idle (not disableable), so a 25-second run and a
  4-minute run cost the same. Cost therefore scales with the *number* of runs.
  - This README previously put usage at ~31 CU-hours. That was a **projection from 48 runs/day
    that never happened** — GitHub was only delivering ~15. **Measured in the Neon console
    2026-08-10: 0.78 of 100 CU-hours since 2026-08-09**, i.e. ~16/month at the old rate.
  - **The headroom is not yet known, and the honest reason is that the two available estimates
    disagree by a factor of ~2.** Modelling a wake as 5 minutes at the free tier's 0.25 CU gives
    63 runs/day → 1.31 CU-h/day → **~39 of 100**. Scaling the *measured* 0.78 instead gives
    **~66 of 100** if that figure covered ~36 hours, and **~98** if it covered 24. The model is
    the optimistic end of that range and this README used to quote it alone, which is exactly
    the mistake the ~31 figure already was. The measurement is the authority; the model is not.
  - Two terms the model omits and the measurement includes: the probes and `gate1_check.py` each
    wake the database on their own, and a wake costs the same whether a cron or a laptop caused
    it. That is why **a Neon connection opened to "just check something" is a real cost**, not a
    free read.
  - **Resolve it by re-reading the console over a known 24-hour window, not by re-projecting.**
    Until then, treat the cap as the figure to watch, since **exceeding it suspends the database
    until the next billing period** — days of silent data loss rather than a slowdown.
  - This is also why the ingester is *not* built as one long-lived polling job. Polling every
    few minutes inside a single run would multiply wakes, not amortise them, and would put
    usage past the cap.

**Product**
- Health advisory text is quoted verbatim from CPCB's published bands, never written here. CPCB
  writes it against the **overall** AQI — the worst sub-index across three or more pollutants —
  while the headline number here is **PM2.5**. Those agree only while PM2.5 dominates, so
  keying the advisory to the PM2.5 band would understate risk exactly when it matters. The
  advisory is therefore selected by the **computed overall AQI band** while PM2.5 stays the
  number shown. When CPCB's own ≥3-pollutant rule cannot be met, no AQI is computed and the
  alert says so explicitly rather than guessing. See `docs/cpcb_aqi_breakpoints.md`.
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

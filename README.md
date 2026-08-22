# AQI Nowcast

**Forecasts station-level PM2.5 6–48 hours ahead for Haryana/NCR and pushes a personalised
threshold alert to Telegram before you need to decide.** Every consumer air-quality tool tells
you what the air is like *right now*; this one tells a specific person whether a specific thing
— the school run, a morning walk, an outdoor shift — will be safe later today.

It is deliberately **not** an AQI dashboard, a map, or a city ranking. The forecast and the
personalised threshold are the entire product.

**Status: Phase 5 of 5 — production discipline.** Phase 1's gate passed on 2026-08-13 (66
distinct bulletins, 25 of 30 stations carrying a real PM2.5 value on every one, 98.6% run
success over 214 runs), Phase 2's on 2026-08-14 when the bot went live, Phase 3's on 2026-08-20
with the baseline table below — written *before* any model existed, so the goalposts could not
quietly move — and Phase 4's on the same day.

**A model does beat the baseline at warning people, at the one horizon the product needs. It is
wired into the message and it is switched off, because the air readings arrive about six and a
half hours late and the win does not survive that.** The forecast is written, tested, deployed,
and refuses to speak rather than issue a five-hour forecast wearing a twelve-hour label. Every
number is in [The model, and why it is not switched on](#the-model-and-why-it-is-not-switched-on).

Phase 5 is running: weekly drift and staleness monitoring since 2026-08-21, and a monthly
retrain with a promotion gate since 2026-08-22. Its gate — a dated post-mortem of a real drift
incident — cannot be written until the stubble-burning season shifts the data in October.

---

## Why it is built in this order

| Phase | What | Gate | Status |
|---|---|---|---|
| 0 | Kill gates: does the data exist, do the IDs join | 3+ live PM2.5 stations, mapping doc | ✅ 2026-08-09 |
| 1 | The hourly logger | 72h of data, >95% fetch success | ✅ 2026-08-13 |
| 2 | Telegram bot, **no model** | 3 real users, 1 feedback row | ✅ 2026-08-14 |
| 3 | Baselines: persistence, seasonal, climatology | per-horizon table in this README | ✅ 2026-08-20 |
| 4 | The model — benchmarked, not assumed | beats persistence, or a documented negative | ✅ 2026-08-20 |
| 5 | Production discipline: drift, retraining, post-mortem | a dated real incident write-up | 🔨 built, waiting on October |

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

Phase 5, on the same trigger and the same database:

Cloudflare Worker cron (Tue 02:00 UTC) ─▶ scripts/monitor.py  ─▶ drift_log, station_health
        │                                  exit 1 IS the alert
Cloudflare Worker cron (1st, 03:00 UTC) ▶ scripts/retrain.py  ─▶ model_runs
                                           model + score, stored in the database
```

The trigger Worker carries four schedules: the two ingest ticks, the daily send, the weekly
monitor and the monthly retrain. The model is stored **in Neon rather than on disk** because a
cloud runner is wiped when its job finishes, so a promotion decided this month would be
unreadable next month.

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
different quantities over two different windows — saying so is the point.

**The forecast is wired in and silent.** Since 2026-08-22 the sender loads the promoted model
and adds one line when it expects the evening to cross into Very Poor. At 07:00 IST it never
does, because no reading is fresh enough to forecast from, and it refuses rather than issue a
short forecast under a long label. Two rules hold whenever it does speak: **no percentage
appears, ever**, since nothing here has checked whether a predicted probability means anything;
and **"no warning" and "we could not look" produce the same output — no line at all**, so an
absent warning can never read as reassurance.

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

## The model, and why it is not switched on

Measured **2026-08-20**. Eight candidates × four horizons × four folds, one seed, identical
rows. Reproduce with `python scripts/benchmark.py`.

**Two sentences, and both are needed.** The best model cuts 24-hour average error from
**27.06 to 24.17 µg/m³ — 10.7%, larger than the fold-to-fold spread**, so the gate this project
set for itself in advance is met. **And no model beats "assume the next hours are like the last
one" at actually warning anyone about dangerous air**, so the forecast is not in the alert.

### How it is evaluated

Not one held-out window but **four, moving forward in time**. Train on everything up to a date,
test on the block that follows, extend, repeat. Every fold trains only on the past.

| Fold | Train up to | Test block | Test rows (24h) | Severe |
|---|---|---|---|---|
| 1 | 2025-12-31 | 2026-01-01 → 2026-03-01 | 23,461 | 463 |
| 2 | 2026-03-01 | 2026-03-02 → 2026-04-30 | 31,276 | 527 |
| 3 | 2026-04-30 | 2026-05-01 → 2026-06-29 | 29,282 | 322 |
| 4 | 2026-06-29 | 2026-06-30 → 2026-08-20 | 29,255 | 258 |

Phase 3 used a single 90-day window, which was right for measuring a baseline and cannot answer
the question this phase asks. "Beats persistence by more than split-to-split variance" needs
more than one split to *have* a variance. It also fixed the sample size that Phase 3's table
warned about: that window carried ~54 severe pairs, these four carry **1,558**.

### Mean absolute error, µg/m³, by horizon

Mean over folds ± the fold-to-fold standard deviation. n = 113,274 at 24h.

| Candidate | 6h | 12h | 24h | 48h |
|---|---|---|---|---|
| persistence | 26.98 ± 4.23 | 31.56 ± 4.16 | 27.06 ± 2.44 | 30.21 ± 2.35 |
| ridge | 25.44 ± 2.19 | 27.21 ± 1.73 | 26.65 ± 1.60 | 30.09 ± 2.28 |
| lightgbm | 22.40 ± 2.39 | 24.47 ± 2.22 | 24.77 ± 1.49 | 28.23 ± 1.72 |
| **lightgbm-tweedie** | **21.69 ± 2.42** | **23.90 ± 2.56** | **24.17 ± 2.40** | **27.54 ± 2.86** |
| lightgbm-weighted | 30.02 ± 1.89 | 32.20 ± 2.12 | 31.85 ± 3.36 | 35.41 ± 4.50 |
| lightgbm-q90 | 39.08 ± 3.05 | 43.62 ± 3.90 | 42.83 ± 1.28 | 46.82 ± 5.06 |
| xgboost | 25.13 ± 2.51 | 28.39 ± 2.63 | 28.99 ± 2.64 | 33.80 ± 2.73 |
| catboost | 24.35 ± 2.31 | 26.65 ± 2.58 | 26.23 ± 1.68 | 30.40 ± 3.19 |

**Gate 4: PASS.** `27.06 − 24.17 = 2.89`, against a fold-to-fold standard deviation of `2.40`.

**10.7% is below the 15–35% the literature reports**, and that is reported rather than tuned
toward. The explanation is the next section. The benchmark also prints an automatic leak
warning above 60% improvement, because a margin that large does not occur in real hourly
station work; this result is nowhere near it.

**XGBoost at library defaults is worse than persistence at 24h.** Left in the table rather than
tuned away — the comparison asks which family wins on equal terms, and tuning one candidate
against untuned baselines would rig it.

### The finding that matters more than the gate

A subscriber does not experience 24.17 µg/m³. They experience a yes or a no: *was I warned when
it mattered?* So the same predictions are also scored as that decision. Severe hours are 2% of
the data, so plain accuracy is useless — a model that never warns scores 98%.

At 24h, with each candidate's warning threshold calibrated on validation data:

| Candidate | MAE | severe MAE | recall | CSI |
|---|---|---|---|---|
| **persistence** | 27.06 | 245.3 | **0.27** | **0.11** |
| lightgbm-q90 | 42.83 | **207.1** | **0.27** | 0.10 |
| xgboost | 28.99 | 266.3 | 0.22 | 0.07 |
| ridge | 26.65 | 268.9 | 0.21 | 0.09 |
| lightgbm-weighted | 31.85 | 232.8 | 0.21 | 0.09 |
| catboost | 26.23 | 272.9 | 0.19 | 0.08 |
| lightgbm | 24.77 | 270.3 | 0.18 | 0.09 |
| lightgbm-tweedie | **24.17** | 286.2 | 0.13 | 0.07 |

**Read the first and last columns together — they are close to inverted.** The better a
candidate's average error, the worse it warns anybody. Before threshold calibration the gap is
starker still: persistence catches 18% of severe hours and the gate-winning model catches 1%.

This is not a bug. Severe hours are 2% of rows, so a model minimising squared error does best
by predicting near the middle and never calling a spike. The Tweedie objective was added
*specifically* because the target is right-skewed and the tail needed handling; it improved the
average instead and has the worst recall in the table.

Three fixes were tried, each justified by that failure rather than assumed in advance — a
calibrated warning threshold (the largest single effect: lightgbm recall 0.03 → 0.18),
severe-row sample weights, and a 90th-percentile quantile objective. **All helped. None
overtook persistence.** The one real win is `lightgbm-q90`'s severe-band error, **207 against
persistence's 245** — when a spike is genuinely happening, its number is the closest here.

**So nothing is served.** Wiring a forecast into the daily message would mean shipping a
product that is better on a number nobody experiences and worse at the only job it has.

### Weather was the obvious fix. It did not work, and that is the finding.

Spikes happen when the wind drops and the boundary layer collapses, so the natural conclusion
from the table above was that the model is blind without weather. That was tested rather than
assumed, and it is wrong here.

Training used **archived forecasts**, not weather that actually happened. A 24-hour forecast is
issued a day early, when nobody knows the wind yet — only what the forecast said it would be.
Open-Meteo's Previous Runs archive serves exactly that, at a fixed lead, and the gap is real:
a day-ahead wind forecast carries **MAE 2.607 m/s** against what the wind actually did, which is
~69% of the difference between the two most distant stations in the network, 312 km apart.
Training on the actuals would have absorbed all of that silently and produced a better score
with no way to earn it in production.

Measured 2026-08-20, `benchmark.py --ablate`, `lightgbm-q90` at 24h over four folds on inner
validation, ranked on CSI because MAE and recall run opposite here:

| weather variant | inner CSI | recall | columns |
|---|---|---|---|
| **no weather** | **0.135** | **0.337** | 21 |
| forecast only | 0.133 | 0.333 | 27 |
| forecast + boundary layer at issue time | 0.130 | 0.326 | 29 |
| forecast + boundary layer at target time *(leaky, ceiling only)* | 0.138 | 0.324 | 28 |

Spread **0.008** against a CSI fold-to-fold std of **0.087** — ten times inside the noise. No
weather is second, and recall falls as weather is added. Weather is therefore behind a
`--weather` flag rather than in the model, and the default reproduces the table above exactly.

**The last row is the one worth reading.** It is given the *true* boundary layer height at the
hour being predicted — an answer key no production system can have — and it still reaches only
0.138 against 0.135. A perfect boundary-layer forecast would buy about 0.003 CSI. That result
exists to stop a plausible next step: chasing a better weather product is not worth the week it
would cost.

**Three independent measurements now say the same thing.** Cross-station PM2.5 correlation is
0.23 within 30 km against 0.24 beyond 150 km. Two stations sharing an ~8 km weather cell
disagree by 50.9 µg/m³ hour to hour, against 47.8 µg/m³ for all 406 pairs up to 312 km apart.
And weather at that grid cannot move the alert. Station-level PM2.5 in this network is driven by
local conditions — traffic, construction, the sensor itself — not by anything a regional weather
model resolves.

### Asking the model the actual question

Every candidate above is a **regressor**: it predicts a concentration, and the alert comes from
thresholding it. That is the wrong shape of model for a yes/no decision, and the inverted table
above is what it looks like. So the question was re-asked directly — predict the *probability
of exceedance*, and grade it on the decision.

Three things change, and only the first is a modelling choice.

**The event is 121 µg/m³, CPCB's Very Poor band, not the 250 the table above used.** At 250 the
positive rate is 2.0%; at 121 it is 13.4%, seven times more events to learn from. That is a
product decision: a parent deciding about a child with asthma does not wait for CPCB to say
Severe. The comparison stays honest because persistence is scored at the same threshold on the
same rows.

**The label spans three hours from the target, not one.** A subscriber asks whether the evening
is safe, not whether 19:00 exactly crosses a line. Row admission still keys on the point target,
so the rows are identical to the tables above and everything stays comparable.

**The objective is F2, and the baseline can tune.** F2 weights recall four times precision: a
miss sends a child outside, a false alarm keeps them in on a clean day. The baseline detail is
the one that mattered most — see the honesty note below.

Measured **2026-08-20**, `python scripts/classify.py --tail --tune`. Five candidates × four
horizons × four folds, warning cutoff fitted on inner validation and applied to test once.

| horizon | persistence F2 | best model F2 | margin | fold std | clears the bar | persistence AP | best AP |
|---|---|---|---|---|---|---|---|
| 6h | 0.508 | 0.618 | +0.110 | 0.125 | no | 0.377 | 0.521 |
| **12h** | 0.468 | **0.594** | **+0.125** | 0.122 | **yes** | 0.297 | 0.467 |
| 24h | 0.525 | 0.579 | +0.054 | 0.139 | no | 0.387 | 0.472 |
| 48h | 0.496 | 0.559 | +0.063 | 0.120 | no | 0.333 | 0.401 |

**One horizon of four clears the bar, and it is the one the product needs.** The message goes
out at 07:00 IST; 12h from there is the evening peak. Persistence is strong at 24h and 48h
because those are whole multiples of the daily cycle — "the same hour yesterday" lands on the
same point of the pollution day — and weak at 6h and 12h, where it compares opposite phases.

**Average precision needs no cutoff and favours the model at every horizon**, by 38% at 6h and
57% at 12h. The ranking is genuinely better than persistence everywhere; a single tuned cutoff
converts that into a passing margin at one horizon. That gap between "ranks well" and "decides
well" is the honest summary of this stage.

Getting there took two things, and their effects are worth separating:

| horizon | at library defaults, base features | + spike features | + hyperparameter search |
|---|---|---|---|
| 6h | 0.610 | 0.615 | 0.618 |
| 12h | 0.573 | **0.582** | 0.594 |
| 24h | 0.563 | 0.575 | 0.579 |
| 48h | 0.518 | 0.532 | 0.559 |

**The features moved the gate; the tuning did not.** Rolling max and min, 72h and 168h lags,
exceedance recency, a per-station z-score and a signed distance from Diwali took 12h from just
short to just clear. The random search then raised F2 and average precision at every horizon —
and raised the fold-to-fold spread by about as much, so no verdict changed. Reporting the F2
gain without the variance beside it would make tuning look like a win it is not. Nothing in this
project had ever been hyperparameter-tuned before this run; the answer is that it was worth
about as much as the noise it added.

**An honesty note, because it changed the entire result.** The first version of this table had
the model beating persistence at all four horizons. It was wrong. Persistence was implemented as
a hard yes/no at 121 µg/m³ while every rival slid its cutoff to wherever F2 peaked — and under a
recall-weighted objective the side allowed to tune wins by construction. The fix is one line:
persistence returns its reading on a 0–1 scale, so tuning that cutoff *is* tuning "warn when the
current reading is above X". Persistence F2 rose from 0.294–0.388 to 0.468–0.525 and a 4-of-4
win became 0-of-4. Two smaller faults were found in the same pass: the label was one clock hour
when the decision is about an evening, and the tuner optimised CSI while the table reported F2.
**The corrected metric produced a better-looking answer than the broken one, which is exactly
when to audit hardest.**

### What the average over 30 stations was hiding

Every number above is a mean over 30 stations and four folds. Measured **2026-08-21**, the same
test predictions regrouped, nothing refitted: `python scripts/classify.py --tail --route --breakdown`.

**The win holds at the send hour.** Pooling all 24 issue hours could have been flattering the
result, since the message only ever goes out at one of them.

| horizon | rows scored | events | persistence F2 | best F2 | margin |
|---|---|---|---|---|---|
| 6h | all 24 issue hours | 14,144 | 0.528 | 0.639 | +0.111 |
| 6h | **07:00 IST only** | 378 | 0.385 | 0.417 | **+0.032** |
| **12h** | all 24 issue hours | 13,811 | 0.488 | 0.603 | +0.115 |
| **12h** | **07:00 IST only** | 695 | 0.541 | 0.641 | **+0.100** |

12h holds. 6h collapses — from a 07:00 send, 6h ahead is early afternoon, a different regime
from the pooled average. 6h had not cleared the bar anyway, but it is the clearest evidence that
pooling issue hours hides things.

**Eight of 29 stations score below persistence — and reading that as "eight stations are served
badly" would be wrong.** Only **two** are below it in every fold. The other six flip sign
between folds, which means their pooled result was decided by one noisy draw. A per-station
margin without a fold count beside it turns an artefact into a claim about a place.

**Serving each station whichever forecaster won on its own validation window lost all 16
horizon-fold cells** to simply using the model everywhere. The cause is sample size: the median
station-fold validation window holds 51 exceedance hours and the bottom quarter under 8, against
13,811 for the pooled decision. No minimum-sample floor rescues it — raising the floor sends
more stations to persistence, and persistence is the worse option on average. The routing code
is kept, off by default, as the record that it was tried.

### The measurement that stopped the forecast shipping

Every score above was computed on an air reading from the current hour. **At 07:00 IST the
service does not have one.** OpenAQ publishes about six and a half hours behind real time, so
the freshest reading at send time is from roughly midnight.

That was never tested until the model was already wired into the message. Measured
**2026-08-22**, `python scripts/classify.py --tail --stale 7`, which withholds the newest seven
hours from every feature — persistence included, since it faces the same lag in production.

| run | persistence F2 at 12h | best F2 | margin | fold std | clears the bar |
|---|---|---|---|---|---|
| current-hour reading | 0.468 | 0.582 | +0.114 | 0.112 | yes, 1 of 4 horizons |
| **the reading it will really get** | 0.500 | 0.584 | **+0.084** | 0.116 | **no, 0 of 4** |

At 07:00 IST specifically, the 12h margin falls from +0.100 to +0.063.

**The model did not get worse — the baseline got better, and that distinction is the finding.**
Best F2 at 12h moves 0.582 to 0.584, which is nothing. Persistence rises 0.468 to 0.500, because
a stale reading sits about 19 hours from the target instead of 12, and 19 hours is nearer a whole
daily cycle. It lands on roughly the same point of the pollution day — the same reason
persistence is strong at 24h. The margin closed from the baseline's side. Describing this as
model degradation would be a false sentence about a true table.

So the forecast line is deliberately dark. The guard that produces that refusal must not be
relaxed to make the line appear: raising the staleness limit does not buy a twelve-hour
forecast, only a five-hour forecast with a twelve-hour label on it.

### The obvious fix, and why it is also a negative

CPCB's own feed is fresh every thirty minutes. Serving from it instead of OpenAQ looks like the
way past the lag. Measured **2026-08-22**, `python scripts/probe_cpcb_signal.py` — read-only,
no model trained, because it compares two *inputs* rather than grading a forecaster and so needs
13 days rather than three months.

**CPCB is genuinely fresh at the send hour:** median age 2.0 hours at 07:00 IST, inside the
three-hour limit. That half was right.

**And it buys nothing.** Ranking evenings above 121 µg/m³ twelve hours ahead, on identical rows:

| serve-time input | how well it ranks bad evenings |
|---|---|
| CPCB, fresh at issue | **0.863** |
| OpenAQ, 7h stale, raw reading | 0.782 |
| **OpenAQ, 7h stale, 24-hour average** | **0.851** |

Against the raw stale reading, CPCB looks worth +0.081. Against a **stale but averaged** OpenAQ
it is worth **+0.012, on a sampling error of 0.032**. CPCB's published number is itself a
24-hour average, and averaging on its own cancels hour-to-hour noise — so the apparent gain was
smoothing, not freshness. The existing feature set already computes a 24-hour rolling mean.

**The first version of that probe had no averaged rival in it and printed the opposite verdict.**
It was added before the result was written down, and it reversed it. That is the same fault as
the persistence baseline further up: a rival that cannot compete makes the candidate look good
by construction.

### What is still untried

Calibration. Nothing yet checks whether a predicted probability means anything, and a message
saying "70% chance" would require it. Until then the alert carries no percentage at all — the
tuned cutoff is the only part of the model's output with a measurement behind it.

### Method notes

- **Every feature is counted from the moment the forecast is issued**, never from the target
  hour. For a 24-hour forecast of 4pm today, that moment is 4pm yesterday. Counting a lag back
  from the target instead is a leak that makes the score look excellent and the model useless;
  a test asserts the property directly, on a fixture where each value equals its own timestamp,
  and it was written before the model existed.
- **A second test asserts no fold trains on a label from its own test block.** That is a
  different leak: a row's features can be impeccably backward-looking while its *label* sits
  inside the test window.
- **Gaps are never filled.** 97% of missing hours sit in runs longer than 3 hours, past the
  point where a forward-fill is an estimate rather than fabrication.
- **4,379 readings of exactly 0.0 µg/m³ (2.27%) are masked as missing.** 58% of those hours sit
  in runs longer than a day and 81% of runs begin in the hour after a reading above 20 µg/m³ —
  a sensor dropping out, not clean air. The existing range check could not see them, because
  0.0 is inside the valid range.
- **Preprocessing choices were measured, not assumed**, and every one landed inside the
  fold-to-fold noise. Scaler, imputer and station encoding all moved MAE by less than the
  spread between folds. Where that happened, the simpler option was taken.
- **Neighbouring-station features were built and then dropped.** Cross-station correlation is
  0.23 within 30 km and 0.24 beyond 150 km — no distance decay to exploit across ~300 km and
  several airsheds — and the ablation found removing them nominally better.
- **No shuffled splits, no purging or embargo** (the label is a point value and every feature
  is strictly backward, so the conditions requiring it do not hold), and **no significance
  test** — at four folds the standard one's assumptions do not hold, so the fold-to-fold spread
  is the test.
- One station of thirty, **NISE Gwal Pahari, Gurugram**, has no usable history: OpenAQ's archive
  returns HTTP 408 on every attempt. Live ingestion for it is unaffected.

Exploratory analysis, with the plots and the cleaning evidence, is in
[`notebooks/01_eda_cleaning.ipynb`](notebooks/01_eda_cleaning.ipynb) and its generated summary
[`docs/eda.md`](docs/eda.md).

---

## Keeping it working: drift, staleness, and retraining

A forecasting service is mostly the machinery around the forecast. Two jobs run on their own
schedule, and neither needs anyone to look at a dashboard.

### Weekly: is a station dark, and has the air changed shape?

`python scripts/monitor.py`, every Tuesday. **The alert channel is the exit code** — the job
fails, and the failure notification is the alert. That deliberately adds no second place for a
credential to live.

- **Staleness** reads the CPCB feed, because it is the one that is live. The limit is 12 hours,
  not 3, because CPCB's morning freeze runs up to 8 hours and a 3-hour rule would report all 30
  stations dark every morning.
- **Drift** reads the measured concentrations instead, because those are real µg/m³. Both checks
  fire on **change**, never on state, so a bad week that stays bad alerts once.

**The threshold was validated by replaying it against a real event, which is the only reason to
believe it.** The whole history was backfilled first — 1,393 station-weeks over 77 weeks back to
2025-02-17 — and the rule raises **exactly one alert in those 77 weeks: 2025-10-20**, the start
of that year's stubble-burning season.

**The first version of the check alerted on nothing at all, and only the replay found it.** It
guarded against the station list changing by rejecting any week whose station count had moved —
and the network was being built out from 1 station to 29 across exactly the weeks of the 2025
stubble season. The guard discarded the event it existed to catch, while still firing on
single-station weeks elsewhere. Both faults had one cause: comparing an average over one set of
stations against an average over a different set. Fixed by intersecting the two station sets
before averaging. **A check that cannot fire is the same defect as a check that cannot fail, and
neither shows up until it is replayed against something real.**

### Monthly: retrain, and refuse to promote unless it earns it

`python scripts/retrain.py`, on the first of the month. The model and its score are stored in
the database, not on disk, because a cloud runner is wiped when its job ends.

The scoring window is derived from the data rather than fixed, so a monthly job cannot re-score
the same rows forever. **Training is the whole history, never a rolling window** — on 18 months
of data a rolling window would delete the only winter the model has seen.

**The recipe is judged on a held-out month, then refit on everything before it is stored.** A
model fitted only up to the judging window stops learning about two and a half months before it
is served, so one promoted on 1 November would have been trained on August air and sent into
peak stubble season.

The gate refuses four ways, and a test proves each one can fire:

| Refusal | Why |
|---|---|
| The margin is inside the noise | Two models a hair apart is not evidence |
| Fewer than 100 exceedance hours in the month | A quiet month would decide it on a coin flip |
| There is no incumbent yet | Nothing to compare against |
| The incumbent was refit on hours it is now being graded on | It would be marked on work it had memorised |

That last one is measured, not argued: run twice in one month, the incumbent scores **0.373 on
the block it had memorised against a challenger's honest 0.321** — a gate that cannot unseat
anyone while still printing a verdict.

**A challenger losing is the normal monthly result, so this job exits successfully when that
happens.** Only a broken job goes red. A second red notification for a non-event would devalue
the one that means something.

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

**Weather**
- **Six of thirty stations share an Open-Meteo grid cell** and receive bit-identical weather:
  three in Gurugram, three in Faridabad. The grid was measured at 0.0703° × 0.1023°
  (~7.8 × ~10.0 km), not taken from the documentation, which quotes coarser figures.
- **A day-ahead wind forecast carries 2.607 m/s of error**, ~69% of the spread between the two
  most distant stations in the network. Any weather-driven forecast inherits that.
- **The boundary layer height is not available at lead time.** The archive of past forecasts
  serves it as a column that exists and is entirely empty, so half the spike mechanism cannot be
  trained on honestly. It was priced instead, by fitting a variant on the true value — see
  above.
- **The 6h and 12h horizons carry no weather at all.** The lead-time archive is indexed in whole
  days, so there is no forecast at a 6-hour lead; feeding them a day-old one would be a 24-hour
  forecast wearing a 6-hour label.

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
- **The forecast is deployed and silent, and it stays that way until the input lag is solved.**
  The measured air readings arrive about six and a half hours late, so at the send hour the
  model would be forecasting from the previous evening. Rather than relax the freshness guard,
  the message goes out exactly as it did before the model existed.

**The model**
- **One winter.** The training history reaches back to February 2025, so the model has seen the
  2025 stubble season once and nothing before it. That is a real limit on a seasonal problem
  and it fixes itself next year rather than by any change to the code.
- **Two stations are served worse than persistence in every fold**, and six more are worse only
  on the pooled average. Routing each station to whichever forecaster suits it was built,
  measured, and lost every cell — so the model is served everywhere and those two stations are
  a known open item, not a solved one.
- **One station is not graded at all.** The historical archive returns an error for it on every
  attempt, so it is named in the output rather than dropped quietly.
- **No calibration.** The model outputs a probability and nothing has checked whether that
  probability means what it says, so the message states no percentage.

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
- Weather reanalysis and forecast: [Open-Meteo](https://open-meteo.com) — model output, not
  measurements, on a grid measured at 0.0703° × 0.1023° (~7.8 km × ~10 km) over Haryana.

This project is not affiliated with or endorsed by CPCB.

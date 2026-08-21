-- AQI Nowcast — Phase 1, Phase 2 and Phase 3 schema.
--
-- Applied by scripts/init_db.py. Every statement is IF NOT EXISTS, so running
-- it twice is a no-op and never destroys data. There is no DROP in this file
-- and there must never be one: observations accumulate hourly and cannot be
-- refetched (build plan §4).


-- stations — the lookup table.
--
-- Deviation from build plan §4, which puts latitude/longitude on every
-- observation row. Coordinates are a property of a station, not of an hourly
-- reading, and repeating them plus a ~45-character station name across ~1.8M
-- rows a year spends roughly a third of the free tier storing constants.
CREATE TABLE IF NOT EXISTS stations (
    station_id          SERIAL PRIMARY KEY,

    -- The EXACT string the CPCB API returns, byte for byte. Two of the 30 are
    -- dirty: 'Municipal Corporation Office, Dharuhera -  HSPCB' has a double
    -- space, 'Sector-6, Panchkula - HSPCB ' has a trailing space.
    --
    -- Do not trim, normalise or clean this column. Phase 0 (Gate 0.2) proved
    -- OpenAQ carries the identical damage, so the exact-string join between the
    -- two sources currently succeeds. Cleaning one side alone silently drops
    -- those stations from the join — no error, just fewer rows.
    station_name        TEXT UNIQUE NOT NULL,

    city                TEXT NOT NULL,
    latitude            DOUBLE PRECISION,
    longitude           DOUBLE PRECISION,

    -- From docs/station_mapping.md. Needed in Phase 3 to pull training history
    -- from OpenAQ. Stored now so the mapping cannot drift away from the live
    -- station list without something failing.
    openaq_location_id  INTEGER,

    -- Lets a station be retired without deleting its history.
    is_active           BOOLEAN NOT NULL DEFAULT TRUE
);


-- observations — the table that accumulates and can never be rebuilt.
CREATE TABLE IF NOT EXISTS observations (
    station_id       INTEGER     NOT NULL REFERENCES stations(station_id),

    -- 'PM2.5' | 'PM10' | 'NO2' | 'OZONE' | 'SO2' | 'CO' | 'NH3'.
    -- Deliberately not a CHECK constraint or an ENUM: if CPCB starts reporting
    -- an eighth pollutant we want to store it, not reject the whole hour.
    pollutant_id     TEXT        NOT NULL,

    -- The API's bulletin timestamp, parsed from IST and stored as UTC. Used
    -- as-is, not truncated to an hour bucket — it is the only time the API
    -- gives us, and bucketing would invent precision we do not have.
    --
    -- This column is what makes the ingester idempotent: a second run against
    -- an unchanged bulletin collides on the primary key instead of duplicating.
    observation_ts   TIMESTAMPTZ NOT NULL,

    -- Nullable on purpose. The API's null sentinel is the string 'NA', which
    -- is mapped to SQL NULL at parse time (Phase 0 correction 3). Never
    -- coerce a missing reading to 0 — it would look like clean air.
    value_min        DOUBLE PRECISION,
    value_max        DOUBLE PRECISION,
    value_avg        DOUBLE PRECISION,

    -- Equal to observation_ts today, kept separately anyway. Phase 0
    -- correction 2 proved this is one national bulletin timestamp identical on
    -- every row, so it can never identify a single dead station — that has to
    -- come from value-change detection. It does still catch a whole-feed stall,
    -- which is a real and different failure mode.
    last_update_api  TIMESTAMPTZ NOT NULL,

    -- First time we saw this row, never updated afterwards (see the upsert in
    -- scripts/ingest.py). Keeping it immutable is what makes bulletin-to-ingest
    -- latency measurable, and what makes a repeat run a true no-op.
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (station_id, pollutant_id, observation_ts)
);


-- fetch_log — one row per run, plus one row per anomaly.
--
-- Deviation from build plan §4, whose shape implies a row per station per run.
-- The ingester makes one HTTP call covering all of Haryana, so 30 identical
-- rows an hour would be ~21,600 redundant rows a month describing a single
-- event. Instead one station_id IS NULL row carries the run outcome, and a row
-- with station_id set is written only when that station is anomalous.
CREATE TABLE IF NOT EXISTS fetch_log (
    id             BIGSERIAL   PRIMARY KEY,
    run_ts         TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- NULL = this row describes the run as a whole.
    station_id     INTEGER     REFERENCES stations(station_id),

    -- Run-level:    'success' | 'stale' | 'http_error' | 'parse_error' | 'crash'
    -- Station-level: 'station_missing' | 'unknown_station'
    --
    -- 'crash' is written by scripts/ingest.py's run() wrapper for anything the
    -- normal paths failed to catch. Deliberately not a CHECK constraint or an
    -- ENUM: a run that cannot record why it died is invisible to Gate 1, so
    -- rejecting an unanticipated outcome would reopen the exact hole this
    -- column exists to close.
    outcome        TEXT        NOT NULL,

    http_status    INTEGER,
    rows_returned  INTEGER,

    -- The bulletin timestamp this run saw. The next run compares against the
    -- newest value here to decide 'success' vs 'stale'.
    bulletin_ts    TIMESTAMPTZ,

    error_detail   TEXT
);


-- Indexes. Only the two access patterns that actually exist: "what is the
-- newest reading" (Phase 2 bot) and "how did the last N runs go" (Gate 1).
-- Every index costs write time on an hourly job, so nothing speculative.
--
-- observations_ts_idx is not what answers the newest-reading query: the PRIMARY
-- KEY (station_id, pollutant_id, observation_ts) already serves it by a backward
-- index scan. Nor does it help the gate queries, which wrap observation_ts in
-- date_trunc('hour', ...) and are therefore not sargable. Kept because it is
-- cheap at this volume and does serve a plain ORDER BY observation_ts DESC. If
-- check_gaps gets slow at ~1.8M rows, the fix is an expression index on
-- date_trunc('hour', observation_ts), not this one.
CREATE INDEX IF NOT EXISTS observations_ts_idx ON observations (observation_ts DESC);
CREATE INDEX IF NOT EXISTS fetch_log_run_ts_idx ON fetch_log (run_ts DESC);

-- One OpenAQ location must not back two CPCB stations.
--
-- Without this, two stations sharing an id is accepted silently and Phase 3
-- pulls the SAME history for both, then treats them as independent series —
-- a silent modelling error, which is the failure class Gate 0.2 exists for.
-- scripts/seed_stations.py checks the mapping doc for the same thing; this is
-- the constraint that makes it true of the table rather than of one input file.
--
-- Postgres allows multiple NULLs in a unique index, so a station without an
-- OpenAQ id is still permitted.
CREATE UNIQUE INDEX IF NOT EXISTS stations_openaq_location_id_key
    ON stations (openaq_location_id);


-- ---------------------------------------------------------------------------
-- Phase 2 — the bot. Everything below is written by the Telegram Worker in
-- bot/ or by scripts/send_alerts.py.


-- profiles — who a subscriber is, as config rather than as branching code.
--
-- Build plan §1: "The profile is a row in a config table, never hardcoded
-- branching logic. Adding a fourth profile later must be a row insert, not a
-- refactor." Seeded by scripts/seed_profiles.py.
CREATE TABLE IF NOT EXISTS profiles (
    -- The slug from build plan §1: 'asthma_child' | 'copd_elderly' |
    -- 'outdoor_worker'. A TEXT primary key rather than a serial, because it
    -- travels inside a Telegram callback_data string (limit 64 bytes) and a
    -- readable value there is what makes a bad tap diagnosable from a log line.
    --
    -- Deviation from PHASE2_PLAN.md, which listed both `name` and `label`:
    -- with a slug as the key, `name` would be a third copy of the same string.
    profile_id      TEXT PRIMARY KEY,

    -- What the button says.
    label           TEXT NOT NULL,

    -- Shown when picking, and in /about. Describes who the profile is for —
    -- never health guidance. All health wording in this product is CPCB's,
    -- quoted with its citation (build plan §5).
    description     TEXT NOT NULL,

    -- Populated now, UNUSED until Phase 4. A threshold alert on a current
    -- reading only restates what is already out of the window; what makes one
    -- useful is firing before the bad air arrives, which needs a forecast. The
    -- columns exist so switching that on is an INSERT and a code path, not a
    -- migration on a live table.
    threshold_pm25  DOUBLE PRECISION NOT NULL,
    cooldown_hours  INTEGER          NOT NULL
);


-- subscribers — chat ID, station, profile. Nothing else, ever.
--
-- Build plan §5: "Store only: Telegram chat ID, chosen station, chosen
-- profile. No coordinates, no names, no health records. Under DPDP, the less
-- we hold the less we owe." A first_name or username column would be trivial
-- to add and is exactly what this line forbids.
CREATE TABLE IF NOT EXISTS subscribers (
    -- BIGINT, not INTEGER. Telegram chat ids for users created since ~2021
    -- exceed 2^31, and a group id is negative and larger still. An INTEGER
    -- here fails at signup for newer accounts only — a bug that looks like
    -- "the bot works for me".
    chat_id      BIGINT      PRIMARY KEY,

    station_id   INTEGER     NOT NULL REFERENCES stations(station_id),
    profile_id   TEXT        NOT NULL REFERENCES profiles(profile_id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- /pause stops the daily message and keeps the row. /stop deletes the row
    -- outright — a person asking to be removed is removed, not flagged.
    is_paused    BOOLEAN     NOT NULL DEFAULT FALSE
);


-- sent_log — one row per message actually delivered.
--
-- Two jobs. It stops a double-send when the sender is re-run or the workflow
-- fires twice, and it is the only place retention is measurable at Gate 2
-- ("N subscribers, Z% still active at week 8" — build plan §9.3).
CREATE TABLE IF NOT EXISTS sent_log (
    id             BIGSERIAL   PRIMARY KEY,
    chat_id        BIGINT      NOT NULL,
    sent_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The IST calendar date this message counts as, computed by the sender.
    --
    -- A separate column rather than an expression index on sent_at, because
    -- `sent_at AT TIME ZONE 'Asia/Kolkata'` is STABLE, not IMMUTABLE — Postgres
    -- refuses it in an index, since the tz database can change underneath it.
    -- Storing the date the sender decided on also means a run that starts at
    -- 06:59:58 IST and finishes at 07:00:01 cannot straddle two dates.
    send_date      DATE        NOT NULL,

    -- What the message actually said, so a complaint about a number is
    -- answerable. NULL observation_ts/pm25_value is the dark-station message.
    --
    -- pm25_value is µg/m³ from pm25_history, the number the message printed.
    -- It held observations.value_avg until 2026-08-19, which is a sub-index,
    -- so rows written before then are AQI values in a column read as µg/m³.
    -- Do not compare across that date without splitting on it.
    --
    -- station_id is stored rather than read back through subscribers, because
    -- a subscriber can change station or leave: without it, a feedback tap on
    -- last week's message would attach to this week's station, or to no
    -- station at all.
    station_id     INTEGER     REFERENCES stations(station_id),
    observation_ts TIMESTAMPTZ,
    pm25_value     DOUBLE PRECISION,
    overall_aqi    INTEGER,
    band           TEXT,

    -- One message per subscriber per IST day. This is the constraint the
    -- double-send guard rests on: the sender checks first, but a check plus a
    -- unique index is the pair that survives two runs racing.
    UNIQUE (chat_id, send_date)
);


-- feedback — the 👍/👎 tap under each message.
--
-- Build plan §5: behavioural data instead of social data. Friends say "cool"
-- when asked; they do not tap 👍 out of politeness for six weeks. Gate 2 needs
-- at least one row here.
CREATE TABLE IF NOT EXISTS feedback (
    id          BIGSERIAL   PRIMARY KEY,

    -- Which message was rated. Written by the bot Worker from the callback
    -- data, which carries this id.
    sent_log_id BIGINT      NOT NULL REFERENCES sent_log(id),

    -- Deliberately NOT a foreign key to subscribers. /stop deletes that row,
    -- and a person leaving must not delete the record that they once found a
    -- message useful — that record is the retention measurement.
    chat_id     BIGINT      NOT NULL,

    -- 1 or -1. Not an ENUM and not a CHECK, following the convention above:
    -- fetch_log.outcome and observations.pollutant_id are both unconstrained
    -- because rejecting an unanticipated value loses the row that would have
    -- explained it. A third rating would be a product decision, not a defect.
    rating      SMALLINT    NOT NULL,

    -- Copied from the sent_log row at tap time so feedback reads on its own —
    -- "which station and what number did they rate" without a join.
    station_id  INTEGER     REFERENCES stations(station_id),
    pm25_value  DOUBLE PRECISION,

    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- A second tap on the same message replaces the first (the Worker upserts
    -- on this). Without it, one person tapping 👍 twenty times reads as twenty
    -- pieces of evidence at Gate 2, which would make the gate meaningless in
    -- the direction that flatters us.
    UNIQUE (sent_log_id, chat_id)
);


-- The sender's one hot query: "has this subscriber already had today's
-- message?" The UNIQUE (chat_id, send_date) above already provides the index,
-- so nothing further is added here. The station picker's liveness query reads
-- observations at the newest bulletin, which the observations PRIMARY KEY
-- serves.



-- ---------------------------------------------------------------------------
-- Phase 3 — the baselines. Written by scripts/backfill_openaq.py, read by
-- scripts/baselines.py and scripts/compare_sources.py. Nothing else touches it.


-- pm25_history — OpenAQ hourly PM2.5, backfilled once.
--
-- A separate table from `observations` on purpose, and the two are NOT
-- interchangeable:
--
--   observations   CPCB's national bulletin, arriving hourly, one row per
--                  station per pollutant. Irreplaceable — every hour not
--                  logged is gone. Its value_avg is, under the 2026-08-11
--                  probe_avg_window assumption, ALREADY a CPCB-period average.
--   pm25_history   OpenAQ's archive of the same CPCB sensors, pulled in bulk
--                  and rebuildable at any time from the API. Hourly
--                  measurements.
--
-- Merging them would put a rebuildable bulk import into the one table that
-- cannot be rebuilt, and would give the ingester's idempotency (keyed on the
-- bulletin timestamp) a second writer that knows nothing about bulletins.
-- Whether the two carry the same QUANTITY is what scripts/compare_sources.py
-- measures; until that reaches a verdict, do not join them without reading it.
CREATE TABLE IF NOT EXISTS pm25_history (
    station_id      INTEGER     NOT NULL REFERENCES stations(station_id),

    -- Start of the hour the measurement covers, UTC, as OpenAQ reports it.
    observation_ts  TIMESTAMPTZ NOT NULL,

    -- µg/m³. NOT NULL, unlike observations.value_avg: a missing hour here is an
    -- absent row, never a null row. The baselines pair hour t with hour t+h and
    -- score the pair only when both exist, so one representation of "missing"
    -- means one code path instead of two.
    value           DOUBLE PRECISION NOT NULL,

    PRIMARY KEY (station_id, observation_ts)
);


-- drift_log — Phase 5. One row per station per ISO week: the shape of the
-- PM2.5 distribution that week.
--
-- Written by scripts/monitor.py. It exists for one reason: in October the
-- stubble-burning season shifts the distribution hard and the model degrades,
-- and a post-mortem needs a BEFORE to point at. That before cannot be collected
-- in November. Build plan §8.
--
-- Why the PM2.5 distribution and not the 35 model features: every lag, roll and
-- z-score in scripts/features.py is a trailing window over this one series, and
-- weather is off by default (it did not help — see the stage 2 ablation). So
-- this table is the source distribution, not a proxy for it. Per-feature stats
-- would be strictly more data and, today, no more information.
--
-- Source is pm25_history (OpenAQ, real µg/m³) and NOT observations.value_avg,
-- which is CPCB's AQI sub-index — a piecewise transform of concentration, so
-- its distribution moves for reasons that are not air.
--
-- Exact 0.0 readings are excluded on write. 2.27% of pm25_history is exactly
-- zero and those are dead sensors, not clean air (58% sit in runs longer than
-- 24h; 81% begin in the hour after a reading above 20 µg/m³). Including them
-- drags every mean toward a sensor fault. Same rule as features.DROP_EXACT_ZERO.
CREATE TABLE IF NOT EXISTS drift_log (
    -- Monday of the ISO week, UTC. date_trunc('week', ...) in a session pinned
    -- to UTC, for the reason gate1_check.main() gives: IST is a half-hour
    -- offset, so buckets computed on this laptop and on a runner land 30
    -- minutes apart and the same data yields two different tables.
    week_start      DATE        NOT NULL,
    station_id      INTEGER     NOT NULL REFERENCES stations(station_id),

    -- Readings behind the row. A partial week is written like any other and is
    -- excluded at READ time by monitor.py, not here — deciding it on write
    -- would mean the newest week never lands until it is over, and a re-run
    -- mid-week would have nothing to update.
    n               INTEGER     NOT NULL,

    mean            DOUBLE PRECISION NOT NULL,
    std             DOUBLE PRECISION NOT NULL,
    p50             DOUBLE PRECISION NOT NULL,
    p90             DOUBLE PRECISION NOT NULL,
    p99             DOUBLE PRECISION NOT NULL,
    max_value       DOUBLE PRECISION NOT NULL,

    -- First seen, not last written, so a re-run is a true no-op in this column.
    -- Same convention as observations.ingested_at.
    snapshot_ts     TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (week_start, station_id)
);


-- station_health — Phase 5. Current liveness per station, overwritten each run.
--
-- Deliberately NOT part of drift_log. That table is history and is keyed by
-- week; this is a single now-value, and a station dark for six days has no row
-- in the current week to hang it on. One table each, both obvious.
--
-- Its whole job is `was_dark`: monitor.py alerts only when a station CHANGES
-- state. Station 26 (Shastri Nagar, Narnaul) left the CPCB feed on 2026-08-20
-- and has not returned, so an alert keyed on "is dark" rather than "just went
-- dark" would fire every single run forever, which trains the reader to ignore
-- it and then hides the real one. Same reasoning as the 3h staleness rule in
-- send_alerts.compose.
--
-- Read from observations, not pm25_history: OpenAQ's archive runs ~19h behind
-- real time (measured 2026-08-21), so every station would read as dark under a
-- 12h rule. CPCB's feed is the current one. value_avg being a sub-index does
-- not matter here — the question is whether the sensor reported anything at
-- all, not what it said.
CREATE TABLE IF NOT EXISTS station_health (
    station_id      INTEGER     PRIMARY KEY REFERENCES stations(station_id),

    -- Newest bulletin carrying a non-NULL PM2.5 value_avg. NULL means this
    -- station has never reported one. A row is not a reading: ingest.py writes
    -- a row per pollutant per bulletin, NULL where CPCB sent 'NA'.
    last_seen       TIMESTAMPTZ,

    -- The verdict at the previous run. Compared against the current one to
    -- produce the alert, then overwritten.
    was_dark        BOOLEAN     NOT NULL,

    checked_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

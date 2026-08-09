-- AQI Nowcast — Phase 1 schema.
--
-- Applied by scripts/init_db.py. Every statement is IF NOT EXISTS, so running
-- it twice is a no-op and never destroys data. There is no DROP in this file
-- and there must never be one: observations accumulate hourly and cannot be
-- refetched (build plan §4).
--
-- Design notes live in the comments below rather than in a separate doc,
-- because the reasons are what stop a later phase from "fixing" something
-- that is deliberate.


-- ---------------------------------------------------------------------------
-- stations — the lookup table.
--
-- Deviation from build plan §4, which puts latitude/longitude on every
-- observation row. Coordinates are a property of a station, not of an hourly
-- reading. Repeating them plus a ~45-character station name across ~1.8M rows
-- a year would spend roughly a third of the free tier storing constants.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stations (
    station_id          SERIAL PRIMARY KEY,

    -- The EXACT string the CPCB API returns, byte for byte. Two of the 30 are
    -- dirty: 'Municipal Corporation Office, Dharuhera -  HSPCB' has a double
    -- space, 'Sector-6, Panchkula - HSPCB ' has a trailing space.
    --
    -- DO NOT TRIM, NORMALISE, OR CLEAN THIS COLUMN. Phase 0 (Gate 0.2) proved
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


-- ---------------------------------------------------------------------------
-- observations — the table that accumulates and can never be rebuilt.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS observations (
    station_id       INTEGER     NOT NULL REFERENCES stations(station_id),

    -- 'PM2.5' | 'PM10' | 'NO2' | 'OZONE' | 'SO2' | 'CO' | 'NH3'.
    -- Deliberately not a CHECK constraint or an ENUM: if CPCB starts reporting
    -- an eighth pollutant we want to store it, not reject the whole hour.
    pollutant_id     TEXT        NOT NULL,

    -- The API's bulletin timestamp, parsed from IST and stored as UTC. Used
    -- as-is, NOT truncated to an hour bucket — it is the only time the API
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
    -- every row, so it can NEVER identify a single dead station — that has to
    -- come from value-change detection. It does still detect a whole-feed
    -- stall, which is a real and different failure mode.
    last_update_api  TIMESTAMPTZ NOT NULL,

    -- FIRST time we saw this row, never updated afterwards (see the upsert in
    -- scripts/ingest.py). Keeping it immutable is what makes bulletin-to-ingest
    -- latency measurable, and what makes a repeat run a true no-op.
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (station_id, pollutant_id, observation_ts)
);


-- ---------------------------------------------------------------------------
-- fetch_log — one row per run, plus one row per anomaly.
--
-- Deviation from build plan §4, whose shape implies a row per station per run.
-- The ingester makes ONE HTTP call covering all of Haryana, so 30 identical
-- rows an hour would be ~21,600 redundant rows a month describing a single
-- event. Instead: one station_id IS NULL row carries the run outcome, and a
-- row with station_id set is written only when that specific station is
-- anomalous.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fetch_log (
    id             BIGSERIAL   PRIMARY KEY,
    run_ts         TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- NULL = this row describes the run as a whole.
    station_id     INTEGER     REFERENCES stations(station_id),

    -- Run-level : 'success' | 'stale' | 'http_error' | 'parse_error'
    -- Station-level: 'station_missing' | 'unknown_station'
    outcome        TEXT        NOT NULL,

    http_status    INTEGER,
    rows_returned  INTEGER,

    -- The bulletin timestamp this run saw. The next run compares against the
    -- newest value here to decide 'success' vs 'stale'.
    bulletin_ts    TIMESTAMPTZ,

    error_detail   TEXT
);


-- ---------------------------------------------------------------------------
-- Indexes. Only the two access patterns that actually exist: "what is the
-- newest reading" (Phase 2 bot) and "how did the last N runs go" (Gate 1).
-- Every index costs write time on an hourly job, so nothing speculative.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS observations_ts_idx ON observations (observation_ts DESC);
CREATE INDEX IF NOT EXISTS fetch_log_run_ts_idx ON fetch_log (run_ts DESC);

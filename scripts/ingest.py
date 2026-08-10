"""The hourly logger. This is the whole of Phase 1 (build plan §4).

    python scripts/ingest.py

Runs every 30 minutes from .github/workflows/ingest.yml. Every hour not logged
is training data that can never be recovered, so this script is written to
degrade rather than abort: if one station is anomalous, the other 29 are still
committed, and the process THEN exits non-zero so the failure is visible.

No silent fallbacks (§0.5). Every failure either writes a fetch_log row with
the real error and exits non-zero, or raises. Nothing is swallowed.
"""

import sys
from datetime import datetime, timezone

from cpcb_api import FetchError, IST, fetch, load_key, parse_bulletin_ts, parse_value
from db import connect, redact

STATE = "Haryana"

UPSERT = """
INSERT INTO observations
    (station_id, pollutant_id, observation_ts,
     value_min, value_max, value_avg, last_update_api)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (station_id, pollutant_id, observation_ts) DO UPDATE SET
    value_min = COALESCE(EXCLUDED.value_min, observations.value_min),
    value_max = COALESCE(EXCLUDED.value_max, observations.value_max),
    value_avg = COALESCE(EXCLUDED.value_avg, observations.value_avg)
"""
# COALESCE is build plan §4's "never overwrite a non-null reading with a null".
# If a station reports 62 and later reports 'NA' for the SAME bulletin, 62
# survives; a genuine correction 62 -> 58 still applies. Phase 0 found 7 live
# 'NA's in one snapshot, so this is load-bearing, not defensive decoration.
#
# ingested_at is deliberately absent from the DO UPDATE list. It means FIRST
# SEEN. Keeping it immutable is what makes bulletin-to-ingest latency
# measurable later, and what makes a repeat run a genuine no-op.

LOG = """
INSERT INTO fetch_log
    (station_id, outcome, http_status, rows_returned, bulletin_ts, error_detail)
VALUES (%s, %s, %s, %s, %s, %s)
"""


def log_row(conn, outcome, *, station_id=None, http_status=None,
            rows_returned=None, bulletin_ts=None, error_detail=None) -> None:
    with conn.cursor() as cur:
        cur.execute(LOG, (station_id, outcome, http_status,
                          rows_returned, bulletin_ts, error_detail))


def previous_bulletin(conn) -> datetime | None:
    """The bulletin timestamp the last successful run saw. None on first run."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bulletin_ts FROM fetch_log
            WHERE station_id IS NULL AND bulletin_ts IS NOT NULL
            ORDER BY run_ts DESC LIMIT 1
            """
        )
        row = cur.fetchone()
    return row[0] if row else None


def main() -> int:
    key = load_key()

    # -------------------------------------------------------------------
    # Fetch BEFORE connecting, and hold the failure rather than handling it
    # here. A failure is logged before the process dies, which is the entire
    # reason cpcb_api.fetch raises instead of calling sys.exit.
    #
    # WHY: connect() used to run first, so the connection sat idle for the whole
    # fetch — and a data.gov.in hang costs 330s (5 attempts x 60s + 30s
    # backoff). Measured against Neon 2026-08-10: a connection idle for 334s
    # SURVIVES if it is outside a transaction, but is terminated
    # (IdleInTransactionSessionTimeout) if any query has opened one. The old
    # code happened to be on the safe side of that line by accident — nothing
    # queried before fetch(). Adding one SELECT above the fetch would have
    # silently armed it.
    #
    # So this is hazard removal, not the fix for the 2026-08-10 04:15 UTC
    # failure; that one was an uncaught TLS exception escaping cpcb_api.fetch's
    # retry loop, fixed there. Do not move connect() back above fetch().
    #
    # Failing fast on a bad credential is not lost: the workflow's preflight
    # step checks DATABASE_URL before ingest.py is invoked at all, and a
    # healthy fetch takes ~0.4s locally.
    # -------------------------------------------------------------------
    fetch_error: FetchError | None = None
    records: list[dict] = []
    try:
        records = fetch(STATE, key)
    except FetchError as e:
        fetch_error = e

    conn = connect()
    exit_code = 0

    try:
        if fetch_error is not None:
            log_row(conn, "http_error", http_status=fetch_error.http_status,
                    error_detail=str(fetch_error))
            conn.commit()
            print(f"FETCH FAILED: {fetch_error}", file=sys.stderr)
            return 1

        # ---------------------------------------------------------------
        # The bulletin timestamp keys every row. Without it we cannot write
        # anything, so this one IS fatal.
        # ---------------------------------------------------------------
        try:
            bulletin_ts = parse_bulletin_ts(records[0]["last_update"])
        except (KeyError, ValueError) as e:
            log_row(conn, "parse_error", rows_returned=len(records),
                    error_detail=f"unparseable last_update: {e}")
            conn.commit()
            print(f"BULLETIN PARSE FAILED: {e}", file=sys.stderr)
            return 1

        prev = previous_bulletin(conn)
        # 'stale' means the national feed has not moved since our last run. We
        # still upsert: CPCB does backfill 'NA' -> value within a bulletin, and
        # the upsert is a no-op if nothing changed. First run ever has no
        # previous bulletin, so it is 'success', not 'stale'.
        run_outcome = "stale" if prev is not None and prev == bulletin_ts else "success"

        with conn.cursor() as cur:
            cur.execute("SELECT station_name, station_id FROM stations WHERE is_active")
            known: dict[str, int] = dict(cur.fetchall())

        # ---------------------------------------------------------------
        # Build rows. Anomalies are collected, not raised: losing 29 stations
        # because CPCB added a 30th would be the worse failure.
        # ---------------------------------------------------------------
        rows = []
        seen: set[str] = set()
        unknown: dict[str, int] = {}
        parse_errors: list[tuple[str, str]] = []

        for r in records:
            name = r["station"]
            station_id = known.get(name)
            if station_id is None:
                # Never auto-insert. A station appearing under a new name is
                # usually a RENAME, and auto-inserting it would fork that
                # station's history into two ids that never join again.
                unknown[name] = unknown.get(name, 0) + 1
                continue
            seen.add(name)

            try:
                values = (parse_value(r["min_value"]),
                          parse_value(r["max_value"]),
                          parse_value(r["avg_value"]))
            except (KeyError, ValueError) as e:
                parse_errors.append((name, f"{r.get('pollutant_id')}: {e}"))
                continue

            rows.append((station_id, r["pollutant_id"], bulletin_ts, *values, bulletin_ts))

        missing = sorted(set(known) - seen)

        # ---------------------------------------------------------------
        # Write. Observations first, so the data is safe even if a later
        # anomaly makes us exit non-zero.
        # ---------------------------------------------------------------
        with conn.cursor() as cur:
            cur.executemany(UPSERT, rows)

        log_row(conn, run_outcome, http_status=200, rows_returned=len(records),
                bulletin_ts=bulletin_ts)

        for name in missing:
            log_row(conn, "station_missing", station_id=known[name],
                    bulletin_ts=bulletin_ts,
                    error_detail=f"expected station absent from response: {name!r}")
        for name, n in sorted(unknown.items()):
            log_row(conn, "unknown_station", bulletin_ts=bulletin_ts,
                    error_detail=f"response carried unknown station {name!r} ({n} rows)")
        for name, detail in parse_errors:
            log_row(conn, "parse_error", station_id=known.get(name),
                    bulletin_ts=bulletin_ts, error_detail=f"{name!r} {detail}")

        conn.commit()

        # ---------------------------------------------------------------
        # Report. Anything anomalous exits non-zero so GitHub emails you —
        # after the good data is already committed.
        # ---------------------------------------------------------------
        age_h = (datetime.now(timezone.utc) - bulletin_ts).total_seconds() / 3600
        print(f"{run_outcome}: bulletin {bulletin_ts.astimezone(IST):%Y-%m-%d %H:%M} IST "
              f"({age_h:.1f}h old) | {len(records)} rows -> {len(rows)} upserted "
              f"across {len(seen)} stations")

        if missing:
            print(f"  STATION_MISSING ({len(missing)}): {missing}", file=sys.stderr)
            exit_code = 1
        if unknown:
            # A rename shows up as station_missing AND unknown_station together,
            # which is a clean diagnostic pair rather than a mystery.
            print(f"  UNKNOWN_STATION ({len(unknown)}): {sorted(unknown)}", file=sys.stderr)
            print("  If this is a rename, update stations.station_name — do NOT "
                  "insert a new row, it would fork that station's history.", file=sys.stderr)
            exit_code = 1
        if parse_errors:
            print(f"  PARSE_ERROR ({len(parse_errors)}): {parse_errors[:5]}", file=sys.stderr)
            exit_code = 1

        return exit_code

    finally:
        conn.close()


def run() -> int:
    """main(), plus a last-resort record of anything main() failed to catch.

    A run that dies without writing a fetch_log row is INVISIBLE: gate1_check.py
    computes the run success rate from the rows that exist, so an uncaught crash
    is counted as if it never happened and the gate reports a clean pipeline.
    That is exactly what the 2026-08-10 04:15 UTC failure did — 10 rows in
    fetch_log, all 'success' or 'stale', for a day that contained a failure.

    The reordering in main() removes the known cause, but "we fixed the one we
    found" is not a guarantee, so the outcome is recorded generically instead.
    """
    try:
        return main()
    except Exception as e:
        # Redacted: fetch_log.error_detail is dumped by gate scripts whose
        # output is screenshot as evidence, and an arbitrary exception may
        # carry the connection string (see db.redact).
        detail = redact(f"{type(e).__name__}: {e}")
        print(f"CRASH: {detail}", file=sys.stderr)
        try:
            # A FRESH connection on purpose — whatever killed the run may well
            # have been the old one.
            conn = connect()
            try:
                log_row(conn, "crash", error_detail=detail[:2000])
                conn.commit()
                print("  recorded as fetch_log outcome='crash'", file=sys.stderr)
            finally:
                conn.close()
        except Exception as log_error:
            # Nothing left to do but say so. Never re-raise over the original.
            print(f"  could not record the crash either: "
                  f"{redact(str(log_error))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())

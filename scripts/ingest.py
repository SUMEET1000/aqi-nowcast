"""The hourly logger. This is the whole of Phase 1 (build plan §4).

    python scripts/ingest.py

Runs every 30 minutes from .github/workflows/ingest.yml. Every hour not logged
is training data that can never be recovered, so this script degrades rather
than aborts: if one station is anomalous the other 29 are still committed, and
the process exits non-zero afterwards so the failure stays visible.

No silent fallbacks (§0.5). Every failure either writes a fetch_log row with
the real error and exits non-zero, or raises. Nothing is swallowed.
"""

import os
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
WHERE (observations.value_min, observations.value_max, observations.value_avg)
   IS DISTINCT FROM (COALESCE(EXCLUDED.value_min, observations.value_min),
                     COALESCE(EXCLUDED.value_max, observations.value_max),
                     COALESCE(EXCLUDED.value_avg, observations.value_avg))
"""
# The WHERE gives cur.rowcount a meaning: it becomes the number of rows that
# actually changed, so a repeat run is a provably empty no-op rather than an
# assumed one, and a bulletin CPCB restates with different numbers stops being
# invisible. That was a data-integrity hole, not a cosmetic one — DO UPDATE
# applies the changed value, ingested_at stays at first-seen by design, there is
# no updated_at, and a restated bulletin classifies as 'stale' (the timestamps
# match), which gate1_check counts as healthy. The training set could mutate
# between a Phase 3 baseline and a Phase 4 model with no record of it anywhere.
#
# COALESCE is build plan §4's "never overwrite a non-null reading with a null".
# A station reporting 62 and later 'NA' for the same bulletin keeps the 62; a
# genuine correction 62 -> 58 still applies. Phase 0 found 7 live 'NA's in one
# snapshot, so this is load-bearing rather than defensive decoration.
#
# ingested_at is deliberately absent from the DO UPDATE list — it means first
# seen. Keeping it immutable is what makes bulletin-to-ingest latency
# measurable later, and what makes a repeat run a genuine no-op.

LOG = """
INSERT INTO fetch_log
    (station_id, outcome, http_status, rows_returned, bulletin_ts, error_detail)
VALUES (%s, %s, %s, %s, %s, %s)
"""


def annotate(level: str, message: str) -> None:
    """Print to stderr, and as a GitHub Actions annotation when running there.

    `::error::` lines surface on the run's summary page. Locally the prefix is
    just noise on a line that would have been printed anyway, so this costs
    nothing off-CI.
    """
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level}::{message}", file=sys.stderr)
    else:
        print(f"  {level.upper()}: {message}", file=sys.stderr)


def step_summary(text: str) -> None:
    """Append one line to the Actions run summary, if we are in Actions.

    Best-effort by design: a failure to write a cosmetic summary must never
    affect the run. That is not a silent fallback in the §0.5 sense — nothing
    depends on it, and the same text has already gone to stdout.
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError as e:
        print(f"  (could not write step summary: {e})", file=sys.stderr)


def clean_detail(detail: str | None) -> str | None:
    """Make an error_detail string safe to store, whatever CPCB put in it.

    Postgres TEXT cannot hold a NUL byte — psycopg raises DataError
    ("PostgreSQL text fields cannot contain NUL (0x00) bytes") before the value
    ever reaches the server. That exception aborts the OPEN TRANSACTION, and
    the anomaly rows share a transaction with the observations, so one NUL byte
    in a station name would have cost the whole bulletin.

    Not hypothetical by construction: the unknown_station path interpolates a
    station name we have never seen and never validated (that is what makes it
    unknown), and it is the one string in this file that comes straight from
    third-party JSON into SQL.

    Truncated too: error_detail has no length limit in the schema, and a
    pathological response should not write a megabyte per run.
    """
    if detail is None:
        return None
    return detail.replace("\x00", "\\x00")[:2000]


def log_row(conn, outcome, *, station_id=None, http_status=None,
            rows_returned=None, bulletin_ts=None, error_detail=None) -> None:
    with conn.cursor() as cur:
        cur.execute(LOG, (station_id, outcome, http_status,
                          rows_returned, bulletin_ts, clean_detail(error_detail)))


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


def build_rows(records: list[dict], known: dict[str, int], bulletin_ts: datetime
               ) -> tuple[list[tuple], set[str], dict[str, int], list[tuple[str, str]]]:
    """Turn one bulletin's records into upsert rows, plus the three anomaly sets.

    Pure — no database, no network — so the riskiest logic in the ingester can be
    tested directly. See tests/test_build_rows.py.

    Every field access on a third-party record happens inside a try, which is
    the whole point of the function: one bad row must cost one row, not the
    other 209. Until 2026-08-10 it did not, in two ways. r["station"] and
    r["pollutant_id"] were read bare, so a record missing either key raised
    KeyError out of main() before the only commit() — all 30 stations lost for
    a bulletin CPCB does not archive. And parse_value raises AttributeError on
    a non-string ('int' object has no attribute 'strip'), which
    `except (KeyError, ValueError)` does not catch, so CPCB emitting 62 instead
    of "62" for one row killed the run.

    Hence the broad except clauses. Not a silent fallback (§0.5): every skipped
    record lands in parse_errors, which is written to fetch_log and forces a
    non-zero exit.
    """
    rows: list[tuple] = []
    seen: set[str] = set()
    unknown: dict[str, int] = {}
    parse_errors: list[tuple[str, str]] = []

    for r in records:
        # str() rather than a bare read: a JSON null station would otherwise put
        # None into `unknown`, and sorted() on a mixed None/str set raises
        # TypeError in the reporting path — after the commit, so the data is
        # safe but the diagnosis is lost.
        try:
            name = str(r["station"])
        except Exception as e:
            parse_errors.append(("<no station key>", f"{type(e).__name__}: {e}"))
            continue

        station_id = known.get(name)
        if station_id is None:
            # Never auto-insert. A station appearing under a new name is
            # usually a rename, and auto-inserting it would fork that
            # station's history into two ids that never join again.
            unknown[name] = unknown.get(name, 0) + 1
            continue
        seen.add(name)

        try:
            pollutant = r["pollutant_id"]
            values = (parse_value(r["min_value"]),
                      parse_value(r["max_value"]),
                      parse_value(r["avg_value"]))
        except Exception as e:
            parse_errors.append(
                (name, f"{r.get('pollutant_id')}: {type(e).__name__}: {e}"))
            continue

        rows.append((station_id, pollutant, bulletin_ts, *values, bulletin_ts))

    return rows, seen, unknown, parse_errors


def main() -> int:
    key = load_key()

    # Fetch before connecting, and hold the failure rather than handling it
    # here — logging a failure before the process dies is the entire reason
    # cpcb_api.fetch raises instead of calling sys.exit.
    #
    # Do not move connect() back above fetch(). It was there until 2026-08-10,
    # which left the connection idle for the whole fetch, and a data.gov.in
    # hang costs 330s (5 attempts x 60s + 30s backoff). Measured against Neon
    # 2026-08-10: an idle connection survives 334s outside a transaction but is
    # terminated (IdleInTransactionSessionTimeout) once any query has opened
    # one. Nothing queried before fetch(), so the old order was on the safe side
    # of that line by accident, and one SELECT above the fetch would have armed
    # it silently. (Hazard removal only — the 04:15 UTC failure that day was an
    # uncaught TLS exception in cpcb_api.fetch, fixed there.)
    #
    # Failing fast on a bad credential is not lost: the workflow's preflight
    # step checks DATABASE_URL before ingest.py is invoked at all, and a healthy
    # fetch takes ~0.4s locally.
    fetch_error: FetchError | None = None
    records: list[dict] = []
    http_status: int | None = None
    try:
        records, http_status = fetch(STATE, key)
    except FetchError as e:
        fetch_error = e

    conn = connect()
    exit_code = 0

    try:
        if fetch_error is not None:
            # redact() on both, like the crash path at the bottom of this file.
            # This path did not redact at all until 2026-08-10, and it is the
            # one whose message is most likely to carry the fetch URL — which
            # holds the data.gov.in key in its query string, and goes both to
            # fetch_log and to a public Actions log.
            detail = redact(str(fetch_error))
            log_row(conn, "http_error", http_status=fetch_error.http_status,
                    error_detail=detail)
            conn.commit()
            print(f"FETCH FAILED: {detail}", file=sys.stderr)
            return 1

        # The bulletin timestamp keys every row, so unlike the anomalies below
        # this one is fatal — without it there is nothing to write.
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
        #
        # The comparison was equality alone until 2026-08-10, so a bulletin
        # older than one already stored fell through to 'success' and the run
        # claimed a fresh capture for a step backwards. Never observed — but so
        # were the last three defects in this file, right up until they fired.
        regressed = prev is not None and bulletin_ts < prev
        run_outcome = "success" if prev is None or bulletin_ts > prev else "stale"

        with conn.cursor() as cur:
            cur.execute("SELECT station_name, station_id FROM stations WHERE is_active")
            known: dict[str, int] = dict(cur.fetchall())

        # Anomalies are collected, not raised: losing 29 stations because CPCB
        # added a 30th would be the worse failure.
        rows, seen, unknown, parse_errors = build_rows(records, known, bulletin_ts)
        missing = sorted(set(known) - seen)

        # Observations get their own transaction, committed before any anomaly
        # row is attempted. Until 2026-08-10 every write below shared one
        # uncommitted transaction, so a failure in the anomaly loops discarded
        # the whole bulletin — while the comment here claimed the opposite.
        #
        # The anomaly rows are the risky ones: they interpolate unvalidated
        # third-party station names, and clean_detail() covers the known NUL
        # case but that is one failure mode, not proof there are no others.
        # Committing first makes the irreplaceable thing durable before the
        # fragile thing is attempted. If the second commit fails, run()'s
        # handler writes a 'crash' row on a fresh connection, so the outcome is
        # "data saved, run flagged" rather than a clean-looking run that would
        # fool Gate 1.
        with conn.cursor() as cur:
            cur.executemany(UPSERT, rows)
            # psycopg 3 sums rowcount across an executemany. With the WHERE in
            # UPSERT this is the number of rows that actually changed.
            changed = cur.rowcount
        conn.commit()

        # What the run did, kept in the database rather than only in an Actions
        # log that needs auth to read and expires. upserted = rows offered;
        # changed = rows the database actually wrote (0 on a true no-op); run =
        # GITHUB_RUN_ID, so a fetch_log row joins to its Actions run exactly
        # instead of by eyeballing two timestamps in two systems.
        run_id = os.environ.get("GITHUB_RUN_ID", "local")
        detail = f"upserted={len(rows)} changed={changed} run={run_id}"
        if regressed:
            # Visible in the row and on stderr, but deliberately not an exit 1:
            # this has never been observed, and inventing a new failure mode
            # that fires every 30 minutes lands straight in the alert-fatigue
            # trap that already threatens our only alerting channel.
            detail += f" BULLETIN_REGRESSED prev={prev.isoformat()}"
            print(f"  BULLETIN REGRESSED: {bulletin_ts} is older than the "
                  f"previously seen {prev}", file=sys.stderr)

        log_row(conn, run_outcome, http_status=http_status,
                rows_returned=len(records), bulletin_ts=bulletin_ts,
                error_detail=detail)

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

        # Anything anomalous exits non-zero so GitHub emails you — after the
        # good data is already committed.
        age_h = (datetime.now(timezone.utc) - bulletin_ts).total_seconds() / 3600
        summary = (f"{run_outcome}: bulletin "
                   f"{bulletin_ts.astimezone(IST):%Y-%m-%d %H:%M} IST "
                   f"({age_h:.1f}h old) | {len(records)} rows -> {len(rows)} "
                   f"upserted, {changed} changed, across {len(seen)} stations")
        print(summary)
        step_summary(summary)

        if missing:
            # ::error:: puts the reason on the run's summary page. Without it a
            # failed run shows a bare red X and the reason lives only in the
            # log — and the log-download API returns 403 without admin rights
            # even on a public repo, so for anyone but the owner the diagnosis
            # is unreachable.
            annotate("error", f"STATION_MISSING ({len(missing)}): {missing}")
            exit_code = 1
        if unknown:
            # A rename shows up as station_missing AND unknown_station together,
            # which is a clean diagnostic pair rather than a mystery.
            annotate("error", f"UNKNOWN_STATION ({len(unknown)}): {sorted(unknown)}. "
                              "If this is a RENAME, update stations.station_name — "
                              "do NOT insert a new row, it would fork that "
                              "station's history. If the station is in `stations` "
                              "but is_active = FALSE, it is retired and resuming: "
                              "set is_active = TRUE. If it is genuinely NEW, re-run "
                              "probe_history.py --write-doc then seed_stations.py.")
            exit_code = 1
        if parse_errors:
            annotate("error", f"PARSE_ERROR ({len(parse_errors)}): {parse_errors[:5]}")
            exit_code = 1

        return exit_code

    finally:
        conn.close()


def run() -> int:
    """main(), plus a last-resort record of anything main() failed to catch.

    A run that dies without writing a fetch_log row is invisible: gate1_check.py
    computes the run success rate from the rows that exist, so an uncaught crash
    counts as if it never happened and the gate reports a clean pipeline. That
    is what the 2026-08-10 04:15 UTC failure did — 10 rows in fetch_log, all
    'success' or 'stale', for a day that contained a failure. The known cause is
    fixed in cpcb_api.fetch, but "we fixed the one we found" is not a guarantee,
    so the outcome is recorded generically here instead.

    BaseException, not Exception, because two live paths are not Exceptions:
    require_env() calls sys.exit on a missing credential (the workflow preflight
    covers that in CI but not on a laptop), and `timeout-minutes` kills the
    process. Worst case inside this script is ~6.4 min — fetch 5x60s + 30s
    backoff, then connect 3x15s + 6s — before checkout, setup-python and pip, so
    a slow pip plus a slow Neon wake can cross 10 minutes. That is why
    ingest.yml sets 15.

    A timeout kill is SIGKILL and nothing catches that, but the ordinary
    Python-level exits now leave a row instead of slipping through the one net
    that exists to catch them.
    """
    try:
        return main()
    except BaseException as e:
        # Redacted: fetch_log.error_detail is dumped by gate scripts whose
        # output is screenshot as evidence, and an arbitrary exception may
        # carry the connection string (see db.redact).
        detail = redact(f"{type(e).__name__}: {e}")
        run_id = os.environ.get("GITHUB_RUN_ID", "local")
        annotate("error", f"CRASH: {detail}")
        step_summary(f"CRASH: {detail}")
        try:
            # A fresh connection on purpose — whatever killed the run may well
            # have been the old one.
            conn = connect()
            try:
                log_row(conn, "crash", error_detail=f"run={run_id} {detail}")
                conn.commit()
                print("  recorded as fetch_log outcome='crash'", file=sys.stderr)
            finally:
                conn.close()
        except BaseException as log_error:
            # BaseException here too: connect() -> database_url() -> require_env
            # calls sys.exit when DATABASE_URL is missing, and a SystemExit
            # escaping this handler would skip the message below — the one line
            # that says the failure could not be recorded at all.
            print(f"  could not record the crash either: "
                  f"{redact(str(log_error))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())

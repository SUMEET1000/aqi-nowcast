"""The daily alert. One message per subscriber, once a day, at 07:00 IST.

    python scripts/send_alerts.py --dry-run          # render, send nothing
    python scripts/send_alerts.py --dry-run --station 21
    python scripts/send_alerts.py --only 123456789   # one subscriber, for real
    python scripts/send_alerts.py

Run from .github/workflows/send_alerts.yml. No forecast and no model — Phase 2
sends the current reading only (build plan §5), and it ships before the model
on purpose, to force the distribution problem to the front while it can still
change decisions.

Why once a day at a fixed time rather than when a threshold trips: a threshold
alert on a *current* reading only restates what is already out of the window.
What makes an alert useful is firing before the bad air arrives, and that needs
Phase 4. profiles.threshold_pm25 and cooldown_hours exist and are unused.

Why 07:00 IST: CPCB's feed freezes every morning. Measured over four days, the
last morning bulletin is 05:00 IST and the next is between 10:00 and 13:00, so
at 07:00 the reading is 2.0h old, at 08:00 exactly 3.0h, at 09:00 4.0h — and
build plan §5 requires the message to say so past 3h. A staleness warning that
fires every single day teaches people to ignore warnings, which then hides the
real one. scripts/check_send_window.py re-measures that window and fails when
07:00 stops clearing the threshold, so the constant is checked rather than
remembered.

Degrades per subscriber and exits non-zero afterwards, the same shape as
ingest.py: one person's dead chat must not cost everyone else their message.
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import NamedTuple

import aqi
import telegram_api
from cpcb_api import IST
from db import connect, redact
from ingest import annotate, clean_detail, step_summary

# Build plan §5: "if now - last_update_api > 3 hours, the message says so
# explicitly. Never silently present a stale reading as current."
STALE_AFTER_H = 3.0

# fetch_log outcomes owned by this script. Deliberately outside
# gate1_check.RUN_OUTCOMES and ANOMALY_OUTCOMES, which are explicit whitelists —
# so the sender is observable in the same table without inflating or deflating
# the ingester's run success rate, which Gate 1 is computed from.
OUTCOME_OK = "alerts_sent"
OUTCOME_ERROR = "alerts_error"
OUTCOME_CRASH = "alerts_crash"

LOG = """
INSERT INTO fetch_log (station_id, outcome, rows_returned, bulletin_ts, error_detail)
VALUES (%s, %s, %s, %s, %s)
"""


class Message(NamedTuple):
    text: str
    overall_aqi: int | None
    band: str | None


def feedback_keyboard(sent_log_id: int) -> dict:
    """The one tappable question per alert (build plan §5).

    Behavioural data instead of social data: friends say "cool" when asked
    directly, and do not tap 👍 out of politeness for six weeks. Gate 2 needs a
    row in `feedback`.

    callback_data carries the sent_log id, so the bot Worker can copy the
    station and the number that were rated without the tap having to re-derive
    them. Telegram caps callback_data at 64 bytes; "fb:<bigint>:-1" is ~22.
    """
    return {"inline_keyboard": [[
        {"text": "👍 Useful", "callback_data": f"fb:{sent_log_id}:1"},
        {"text": "👎 Not useful", "callback_data": f"fb:{sent_log_id}:-1"},
    ]]}


def compose(station_name: str, readings: dict[str, float | None],
            observation_ts: datetime | None, now: datetime,
            profile_label: str,
            pm25_ugm3: float | None = None,
            concentration_ts: datetime | None = None) -> Message:
    """The whole message, as a pure function. See tests/test_message.py.

    Pure for the same reason ingest.build_rows is: this is the text a real
    person reads about their own health, and it must be checkable without a
    Neon wake or a Telegram send.

    Two numbers, from two sources, and they are NOT the same quantity:

    - `pm25_ugm3` is a real concentration in µg/m³, measured this hour, from
      pm25_history (OpenAQ). It is the headline, and aqi.pm25_band applies to it.
    - `readings` are observations.value_avg, which are CPCB's AQI SUB-INDICES of
      a 24-hour mean, not concentrations. They drive the overall AQI only.

    Mixing them is the bug this signature exists to prevent. Until 2026-08-19
    the headline printed a sub-index labelled "µg/m³": Ambala read "51 µg/m³"
    for a true 30, and a station at AQI 157 was reported Very Poor when CPCB
    calls it Moderate. The message must never call a value_avg a concentration.

    OpenAQ is also the fresher of the two at send time. CPCB's feed freezes each
    morning — last bulletin 05:00 IST, next between 10:00 and 13:00 — while
    OpenAQ carries 06:00 to 09:00 IST. Measured 2026-08-19 across four stations.

    Three paths, and the difference between them is not cosmetic:

    - Neither source has PM2.5 — the station's sensor is dark. Say so. Sending
      nothing would look like the bot broke, and Phase 1 measured six outages of
      10 to 19 bulletins each, so this is a regular occurrence rather than an edge.
    - The overall AQI computes — lead with PM2.5, and attach CPCB's advisory
      sentence for the OVERALL band. Keying it to the PM2.5 band would
      understate risk whenever PM2.5 is not the worst pollutant, which is the
      resolved decision in docs/cpcb_aqi_breakpoints.md.
    - CPCB's ≥3-pollutant rule fails — no AQI is produced and NO advisory
      sentence is attached, because CPCB wrote every one of them against an
      overall band we do not have. The documented degraded wording says what
      the number is and that the official AQI may be higher.

    The profile appears as a label and changes nothing else. Build plan §5 asks
    for a "profile-specific advisory" and also forbids writing our own medical
    guidance; the only per-band health text that exists is CPCB's, and CPCB
    keys it to the band, not to the person. The profile gains real behaviour in
    Phase 4, where threshold_pm25 decides whether a message is sent at all.
    """
    esc = telegram_api.escape
    pm25 = readings.get("PM2.5")

    if (observation_ts is None or pm25 is None) and pm25_ugm3 is None:
        return Message(
            f"<b>{esc(station_name)}</b>\n\n"
            "This station is not reporting PM2.5 right now, so there is no "
            "reading to send. Your subscription is fine — CPCB's sensor at "
            "this station is dark, which usually lasts a few hours.\n\n"
            "Use /stations to switch to another station.",
            None, None)

    # %I is zero-padded ("05:00 AM"); Windows has no %-I, so strip by hand.
    def ist(ts: datetime) -> str:
        return ts.astimezone(IST).strftime("%I:%M %p").lstrip("0")

    # Staleness is judged on the freshest thing we are showing. At 07:00 IST
    # that is normally OpenAQ's 07:00 reading rather than CPCB's 05:00 bulletin,
    # which is what stops the 3h warning firing every single morning.
    headline_ts = concentration_ts or observation_ts
    age_h = (now - headline_ts).total_seconds() / 3600
    reading_time = ist(headline_ts)

    lines = [f"<b>{esc(station_name)} — {reading_time} IST</b>", ""]

    if pm25_ugm3 is not None:
        lines += [f"PM2.5: <b>{pm25_ugm3:.0f} µg/m³</b> — "
                  f"{aqi.pm25_band(pm25_ugm3)}", ""]

    overall, refusal = aqi.overall_aqi(readings)
    if overall is not None:
        lines += [
            f"CPCB AQI {overall.aqi} ({overall.band}), worst pollutant "
            f"{overall.dominant}",
            f"“{aqi.ADVISORY[overall.band]}”",
            f"— {aqi.ADVISORY_CITATION}",
        ]
    elif pm25 is not None:
        # Our own wording, and deliberately so: it describes what our number is
        # and is not, which is not something CPCB has published a sentence for.
        lines += [
            f"This is PM2.5 only — {aqi.band_of_index(int(round(pm25)))} for "
            "PM2.5. Other pollutants are not included, so the official AQI may "
            "be higher.",
            f"({refusal})",
        ]
    else:
        lines += ["CPCB has published no AQI for this station yet today, so "
                  "only the measured PM2.5 above is available."]

    # The two numbers cover different windows and would otherwise look like a
    # contradiction whenever they disagree — which is most of the time, since a
    # 24-hour index lags an hourly reading by design.
    if pm25_ugm3 is not None and overall is not None:
        lines += ["", "PM2.5 is this hour's measurement; the AQI is CPCB's "
                      "24-hour index."]

    lines += ["", f"Reading from {reading_time} IST, {age_h:.1f}h old."]
    if age_h > STALE_AFTER_H:
        lines.append(
            f"⚠ CPCB has not published a newer bulletin since {reading_time} "
            f"IST. This reading is {age_h:.1f} hours old.")
    lines.append(f"Profile: {esc(profile_label)}")

    return Message("\n".join(lines),
                   overall.aqi if overall else None,
                   overall.band if overall else None)


def newest_bulletin(conn) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("SELECT max(observation_ts) FROM observations")
        return cur.fetchone()[0]


def readings_at(conn, bulletin_ts: datetime, station_ids: list[int]
                ) -> dict[int, dict[str, float | None]]:
    """Every pollutant this bulletin carries, per station.

    Rows absent from the response and rows present with a NULL are both
    absences here, and overall_aqi treats them the same way — a row is not a
    reading.
    """
    out: dict[int, dict[str, float | None]] = {sid: {} for sid in station_ids}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT station_id, pollutant_id, value_avg FROM observations
            WHERE observation_ts = %s AND station_id = ANY(%s)
            """,
            (bulletin_ts, station_ids),
        )
        for station_id, pollutant, value in cur.fetchall():
            out[station_id][pollutant] = value
    return out


def concentrations(conn, station_ids: list[int]
                   ) -> dict[int, tuple[datetime, float] | None]:
    """Newest measured PM2.5 in µg/m³ per station, from pm25_history (OpenAQ).

    The real concentration, and at 07:00 IST usually the fresher of our two
    sources — CPCB's feed is frozen between 05:00 and about 11:00 IST while
    OpenAQ keeps publishing. Separate from readings_at because the two are
    different quantities from different feeds on different timestamps, and
    returning them in one dict is how they got confused in the first place.

    No time floor here. A station whose sensor died a week ago returns its
    week-old hour, and compose reports the age rather than hiding it — the same
    rule build plan §5 sets for CPCB's own staleness.
    """
    out: dict[int, tuple[datetime, float] | None] = {s: None for s in station_ids}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (station_id) station_id, observation_ts, value
            FROM pm25_history
            WHERE station_id = ANY(%s)
            ORDER BY station_id, observation_ts DESC
            """,
            (station_ids,),
        )
        for station_id, ts, value in cur.fetchall():
            out[station_id] = (ts, value)
    return out


def subscribers(conn, only: int | None) -> list[tuple]:
    """(chat_id, station_id, station_name, profile_label), unpaused only."""
    sql = """
        SELECT s.chat_id, s.station_id, st.station_name, p.label
        FROM subscribers s
        JOIN stations st ON st.station_id = s.station_id
        JOIN profiles p ON p.profile_id = s.profile_id
        WHERE NOT s.is_paused
    """
    params: list = []
    if only is not None:
        sql += " AND s.chat_id = %s"
        params.append(only)
    sql += " ORDER BY s.chat_id"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def sample_rows(conn, station_id: int | None) -> list[tuple]:
    """Fake subscribers for --dry-run, so the message can be read before anyone
    is subscribed to receive it. chat_id 0 never reaches Telegram — --dry-run
    is the only caller."""
    sql = ("SELECT 0, station_id, station_name, 'Child with asthma' "
           "FROM stations WHERE is_active")
    params: list = []
    if station_id is not None:
        sql += " AND station_id = %s"
        params.append(station_id)
    sql += " ORDER BY station_id"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    if not rows:
        sys.exit(f"no such active station: {station_id}")
    return rows


def already_sent(conn, send_date) -> set[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT chat_id FROM sent_log WHERE send_date = %s", (send_date,))
        return {r[0] for r in cur.fetchall()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="render every message to stdout and send nothing")
    ap.add_argument("--station", type=int, metavar="ID",
                    help="with --dry-run, render for this station only")
    ap.add_argument("--only", type=int, metavar="CHAT_ID",
                    help="send to one subscriber (used before inviting testers)")
    args = ap.parse_args()

    if args.station is not None and not args.dry_run:
        sys.exit("--station only makes sense with --dry-run")

    token = "" if args.dry_run else telegram_api.load_token()
    now = datetime.now(timezone.utc)
    send_date = now.astimezone(IST).date()

    conn = connect()
    sent = failed = skipped = 0
    exit_code = 0

    try:
        bulletin_ts = newest_bulletin(conn)
        if bulletin_ts is None:
            # Nothing has ever been ingested. Not a message worth sending to
            # anyone, and not something to paper over (§0.5).
            log_row(conn, OUTCOME_ERROR, error_detail="observations is empty")
            conn.commit()
            print("observations is empty — nothing to send", file=sys.stderr)
            return 1

        rows = (sample_rows(conn, args.station) if args.dry_run
                else subscribers(conn, args.only))
        if not rows:
            print("no subscribers to send to" if not args.dry_run
                  else "no active stations to render")

        done = set() if args.dry_run else already_sent(conn, send_date)
        station_ids = sorted({r[1] for r in rows})
        by_station = readings_at(conn, bulletin_ts, station_ids)
        by_conc = concentrations(conn, station_ids)

        for chat_id, station_id, station_name, profile_label in rows:
            if chat_id in done:
                # The UNIQUE (chat_id, send_date) index is the real guard; this
                # is what stops a re-run costing a Telegram call per subscriber
                # before hitting it.
                skipped += 1
                continue

            conc = by_conc.get(station_id)
            message = compose(station_name, by_station[station_id],
                              bulletin_ts, now, profile_label,
                              pm25_ugm3=conc[1] if conc else None,
                              concentration_ts=conc[0] if conc else None)

            if args.dry_run:
                print(f"\n--- chat {chat_id} | station {station_id} "
                      f"{station_name!r} | aqi={message.overall_aqi} "
                      f"band={message.band} ---")
                print(message.text)
                sent += 1
                continue

            # sent_log first, so the feedback buttons can carry its id, and
            # committed only after Telegram accepts the message — a row here
            # means a message a person actually received, which is what Gate
            # 2's retention number is counted from.
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO sent_log
                            (chat_id, sent_at, send_date, station_id,
                             observation_ts, pm25_value, overall_aqi, band)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        # pm25_value is the µg/m³ the message actually printed,
                        # not observations.value_avg — that column is a
                        # sub-index, and storing it here made "what number did
                        # we show this person" unanswerable, which is the one
                        # job the column has.
                        (chat_id, now, send_date, station_id, bulletin_ts,
                         conc[1] if conc else None,
                         message.overall_aqi, message.band),
                    )
                    sent_log_id = cur.fetchone()[0]

                telegram_api.send_message(token, chat_id, message.text,
                                          feedback_keyboard(sent_log_id))
                conn.commit()
                sent += 1

            except telegram_api.TelegramError as e:
                conn.rollback()
                failed += 1
                exit_code = 1
                detail = telegram_api.redact(str(e), token)
                annotate("error", f"send failed for chat {chat_id}: {detail}")
                log_row(conn, OUTCOME_ERROR, station_id=station_id,
                        bulletin_ts=bulletin_ts,
                        error_detail=f"chat={chat_id} {detail}")

                # 403 is Telegram saying this person blocked the bot or deleted
                # the chat. Retrying it every morning forever is noise, and
                # deleting the row would throw away a subscription they may
                # resume with /start. Paused, loudly — never silently (§0.5).
                if e.http_status == 403:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE subscribers SET is_paused = TRUE WHERE chat_id = %s",
                            (chat_id,))
                    annotate("warning", f"chat {chat_id} blocked the bot — paused. "
                                        "/start un-pauses it.")
                conn.commit()

        if not args.dry_run:
            log_row(conn, OUTCOME_OK, rows_returned=sent, bulletin_ts=bulletin_ts,
                    error_detail=f"sent={sent} failed={failed} skipped={skipped} "
                                 f"run={os.environ.get('GITHUB_RUN_ID', 'local')}")
            conn.commit()

        age_h = (now - bulletin_ts).total_seconds() / 3600
        summary = (f"{'dry-run' if args.dry_run else 'sent'}: bulletin "
                   f"{bulletin_ts.astimezone(IST):%Y-%m-%d %H:%M} IST "
                   f"({age_h:.1f}h old) | {sent} message(s), {failed} failed, "
                   f"{skipped} already had today's")
        print(f"\n{summary}")
        step_summary(summary)
        return exit_code

    finally:
        conn.close()


def log_row(conn, outcome, *, station_id=None, rows_returned=None,
            bulletin_ts=None, error_detail=None) -> None:
    with conn.cursor() as cur:
        cur.execute(LOG, (station_id, outcome, rows_returned, bulletin_ts,
                          clean_detail(error_detail)))


def run() -> int:
    """main(), plus a last-resort record of anything it failed to catch.

    Same argument as ingest.run(): a run that dies without a fetch_log row is
    invisible, and "nobody got a message today" is exactly the failure nobody
    notices until a subscriber mentions it.
    """
    try:
        return main()
    except BaseException as e:
        detail = redact(f"{type(e).__name__}: {e}")
        annotate("error", f"CRASH: {detail}")
        step_summary(f"CRASH: {detail}")
        try:
            conn = connect()
            try:
                log_row(conn, OUTCOME_CRASH,
                        error_detail=f"run={os.environ.get('GITHUB_RUN_ID', 'local')} "
                                     f"{detail}")
                conn.commit()
                print(f"  recorded as fetch_log outcome='{OUTCOME_CRASH}'",
                      file=sys.stderr)
            finally:
                conn.close()
        except BaseException as log_error:
            print(f"  could not record the crash either: "
                  f"{redact(str(log_error))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())

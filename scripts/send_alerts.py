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
import re
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


LANGS = ("en", "hi")


def lang_of(code: str | None) -> str:
    """Any unknown or NULL language falls back to English rather than raising.

    A subscriber row written before the lang column existed carries the default
    'en'; this guards the other direction, a code arriving from somewhere the
    two-value set was not enforced. Never a KeyError inside the send loop — one
    odd row must not cost everyone else their message.
    """
    return code if code in LANGS else "en"


# CPCB's six band names, said in words a person with no technical or medical
# background reads once and understands. These translate the BAND, never the
# health sentence: every aqi.ADVISORY string stays CPCB's own, quoted and
# cited, because build plan §5 forbids writing our own medical guidance and a
# paraphrase of a health statement is our own guidance.
#
# An ADJECTIVE, not a sentence, and that is load-bearing. The message shows two
# bands — one for this hour's dust, one for the government's 24-hour score —
# and they legitimately disagree most of the time. Rendered as sentences the
# reader met "Air is clean" and "Air is not good" three lines apart and had to
# decide which one was the bug. As an adjective each lands on the number it
# belongs to. TEXT["dust_band"] wraps it into the one sentence there is room
# for, so both Hindi and English keep their own word order.
BAND_PLAIN = {
    "en": {
        "Good":         ("🟢", "clean"),
        "Satisfactory": ("🟡", "mostly OK"),
        "Moderate":     ("🟠", "not good"),
        "Poor":         ("🔴", "bad"),
        "Very Poor":    ("🟣", "very bad"),
        "Severe":       ("⚫", "dangerous"),
    },
    "hi": {
        "Good":         ("🟢", "साफ़"),
        "Satisfactory": ("🟡", "ठीक-ठाक"),
        "Moderate":     ("🟠", "अच्छी नहीं"),
        "Poor":         ("🔴", "खराब"),
        "Very Poor":    ("🟣", "बहुत खराब"),
        "Severe":       ("⚫", "खतरनाक"),
    },
}

# The pollution board's own name, which is on the end of every station string
# CPCB publishes. It tells a person choosing where they live nothing at all, so
# it never reaches them. The stored station_name keeps it, byte for byte —
# CPCB and OpenAQ join on that exact string, whitespace damage included.
BOARD_SUFFIX = re.compile(r"\s*-\s*(HSPCB|IITM)\s*$")

# The pollutant codes CPCB publishes, each with a plain gloss. The code is kept
# beside the gloss rather than replaced by it: "PM10" is what every other app
# on the same phone shows, so dropping it would make the two disagree.
POLLUTANT_PLAIN = {
    "en": {"PM2.5": "fine dust", "PM10": "bigger dust", "NO2": "vehicle fumes",
           "SO2": "factory gas", "NH3": "ammonia", "OZONE": "ozone",
           "CO": "carbon monoxide"},
    "hi": {"PM2.5": "बारीक धूल", "PM10": "मोटी धूल",
           "NO2": "गाड़ियों का धुआँ", "SO2": "फैक्ट्री की गैस",
           "NH3": "अमोनिया", "OZONE": "ओज़ोन",
           "CO": "कार्बन मोनोऑक्साइड"},
}

# Every sentence of the daily message except CPCB's quoted health statement.
#
# Written for someone who does not know what a sub-index, a concentration or a
# bulletin is. Four technical strings survive, all four on purpose: "PM2.5",
# "PM10", "AQI" and "µg/m³". The reader meets every one of them on the other
# air apps on the same phone, so replacing them with invented plain words made
# our numbers look like a DIFFERENT quantity instead of the same one said
# simply. "Air score" was such an invention and it was the worse mistake:
# CPCB already named this number, and a private synonym for a public name is
# not simpler. Simplify the sentence around a term, not the term itself.
TEXT = {
    "en": {
        "checked":   "Air checked at {t}",
        "dust_band": "Air is {band} right now",
        "dust":      "Fine dust in the air (PM2.5): <b>{v} µg/m³</b>",
        "dust_note": "Under 30 counts as clean air.",
        "warn":      ("⚠️ <b>Air is likely to get very bad around {t}.</b>\n"
                      "Try to finish outdoor work before then."),
        "score":     "Government AQI (last 24 hours): <b>{aqi} out of 500</b> — {band}",  # noqa: E501
        "worst":     "Worst thing in the air: {code} ({plain})",
        "advice":    "<b>Government health note:</b>",
        "pm_only":   ("Only fine dust was measured this hour, so there is no "
                      "full government AQI. The real AQI may be higher."),
        "no_score":  ("The government has not put out an AQI for this place "
                      "today. Only the dust number above is available."),
        "age":       "This reading is {h} hours old.",
        "stale":     ("⚠️ No newer reading since {t}. This one is {h} hours "
                      "old."),
        "for":       "For: {p}",
        "rate":      "Was this useful today?",
        "dark":      ("There is no reading right now. The government sensor "
                      "here has stopped sending data. This usually lasts a few "
                      "hours and fixes itself.\n\n"
                      "You are still subscribed. Send /stations to pick "
                      "another place."),
    },
    "hi": {
        "checked":   "हवा देखी गई: {t}",
        "dust_band": "अभी हवा {band} है",
        "dust":      "हवा में बारीक धूल (PM2.5): <b>{v} µg/m³</b>",
        "dust_note": "30 से कम मतलब साफ़ हवा।",
        "warn":      ("⚠️ <b>{t} के आसपास हवा बहुत खराब हो सकती है।</b>\n"
                      "बाहर का काम उससे पहले निपटा लें।"),
        "score":     "सरकारी AQI (पिछले 24 घंटे): <b>500 में से {aqi}</b> — {band}",  # noqa: E501
        "worst":     "हवा में सबसे खराब चीज़: {code} ({plain})",
        "advice":    ("<b>सरकार की सेहत सलाह — जैसी अंग्रेज़ी में छपी है, "
                      "वैसी ही:</b>"),
        "pm_only":   ("इस घंटे सिर्फ़ बारीक धूल नापी गई, इसलिए पूरा सरकारी "
                      "AQI नहीं है। असली AQI इससे ज़्यादा हो सकता है।"),
        "no_score":  ("सरकार ने आज इस जगह का AQI नहीं दिया है। सिर्फ़ ऊपर "
                      "वाला धूल का नंबर है।"),
        "age":       "यह रीडिंग {h} घंटे पुरानी है।",
        "stale":     ("⚠️ {t} के बाद नई रीडिंग नहीं आई। यह {h} घंटे पुरानी "
                      "है।"),
        "for":       "किसके लिए: {p}",
        "rate":      "क्या यह आज काम आया?",
        "dark":      ("अभी कोई रीडिंग नहीं है। इस जगह की सरकारी मशीन फ़िलहाल "
                      "डेटा नहीं भेज रही। यह आम बात है और कुछ घंटों में ठीक "
                      "हो जाता है।\n\n"
                      "आपका सब्सक्रिप्शन चालू है। दूसरी जगह चुनने के लिए "
                      "/stations भेजें।"),
    },
}

FEEDBACK_BUTTONS = {
    "en": ("👍 Useful", "👎 Not useful"),
    "hi": ("👍 काम आया", "👎 काम नहीं आया"),
}


def feedback_keyboard(sent_log_id: int, lang: str = "en") -> dict:
    """The one tappable question per alert (build plan §5).

    Behavioural data instead of social data: friends say "cool" when asked
    directly, and do not tap 👍 out of politeness for six weeks. Gate 2 needs a
    row in `feedback`.

    callback_data carries the sent_log id, so the bot Worker can copy the
    station and the number that were rated without the tap having to re-derive
    them. Telegram caps callback_data at 64 bytes; "fb:<bigint>:-1" is ~22.
    """
    yes, no = FEEDBACK_BUTTONS[lang_of(lang)]
    return {"inline_keyboard": [[
        {"text": yes, "callback_data": f"fb:{sent_log_id}:1"},
        {"text": no, "callback_data": f"fb:{sent_log_id}:-1"},
    ]]}


def compose(station_name: str, readings: dict[str, float | None],
            observation_ts: datetime | None, now: datetime,
            profile_label: str,
            pm25_ugm3: float | None = None,
            concentration_ts: datetime | None = None,
            warn_target: datetime | None = None,
            lang: str = "en") -> Message:
    """The whole message, as a pure function. See tests/test_message.py.

    Pure for the same reason ingest.build_rows is: this is the text a real
    person reads about their own health, and it must be checkable without a
    Neon wake or a Telegram send.

    Written for a reader with no technical background. Every word naming a
    mechanism rather than a fact is gone — "bulletin", "sub-index", "overall
    AQI", "µg/m³", "the station is dark", "degraded". What is left is the
    number, what it means in ordinary words, and who said so.

    ONE THING IS NEVER SIMPLIFIED AND NEVER TRANSLATED: aqi.ADVISORY. Those are
    CPCB's own health statements, quoted and cited, and the quote is the
    liability shield (build plan §5, "we do not write our own medical
    guidance"). A plain-language paraphrase of a health statement IS our own
    guidance, and so is a translation of one. So on the Hindi path the sentence
    stays in CPCB's English and the label above it says exactly that. Shipping
    it in Hindi needs CPCB's own Hindi wording, cited to the document; nobody
    has captured that, so it is not here. `[assumed: CPCB publishes a Hindi
    bulletin; capturing it page-cited would test this]`

    Two numbers, from two sources, and they are NOT the same quantity:

    - `pm25_ugm3` is a real concentration in µg/m³, measured this hour, from
      pm25_history (OpenAQ). It is the headline, and aqi.pm25_band applies to it.
    - `readings` are observations.value_avg, which are CPCB's AQI SUB-INDICES of
      a 24-hour mean, not concentrations. They drive the overall AQI only.

    Mixing them is the bug this signature exists to prevent. Until 2026-08-19
    the headline printed a sub-index labelled "µg/m³": Ambala read "51 µg/m³"
    for a true 30, and a station at AQI 157 was reported Very Poor when CPCB
    calls it Moderate. The message must never call a value_avg a concentration.

    The two numbers cover different windows and read as a contradiction
    whenever they disagree, which is most of the time — a 24-hour index lags
    an hourly reading by design. That used to be handled by a sentence under
    the table explaining it. The sentence is gone: it talked down to the
    reader, and a paragraph explaining a table is a paragraph nobody reads.
    Each window is stated ON the line it belongs to instead — the dust hour in
    the header, "(last 24 hours)" inside the AQI label.

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

    warn_target is the hour the promoted 12h model expects to exceed 121 µg/m³,
    or None when it does not, when no model is promoted, or when the station's
    reading is too stale to forecast from. All four are one branch on purpose:
    the message says nothing rather than implying calm, because "no warning"
    and "we could not look" must not read the same to a person deciding whether
    to send a child outside — so neither one produces a sentence.

    NO PROBABILITY EVER APPEARS HERE. See scripts/forecast.py: the cutoff is the
    only part of the model's output with a measurement behind it, and a percent
    would be a confidence nobody has earned.

    The profile appears as a label and changes nothing else. Build plan §5 asks
    for a "profile-specific advisory" and also forbids writing our own medical
    guidance; the only per-band health text that exists is CPCB's, and CPCB
    keys it to the band, not to the person. The profile gains real behaviour in
    Phase 4, where threshold_pm25 decides whether a message is sent at all.
    """
    esc = telegram_api.escape
    lang = lang_of(lang)
    t = TEXT[lang]
    plain = BAND_PLAIN[lang]
    pm25 = readings.get("PM2.5")
    head = f"📍 <b>{esc(BOARD_SUFFIX.sub('', station_name).strip())}</b>"

    if (observation_ts is None or pm25 is None) and pm25_ugm3 is None:
        return Message(f"{head}\n\n{t['dark']}", None, None)

    # %I is zero-padded ("05:00 AM"); Windows has no %-I, so strip by hand.
    def ist(ts: datetime) -> str:
        return ts.astimezone(IST).strftime("%I:%M %p").lstrip("0")

    # Staleness is judged on the freshest thing we are showing. At 07:00 IST
    # that is normally OpenAQ's 07:00 reading rather than CPCB's 05:00 bulletin,
    # which is what stops the 3h warning firing every single morning.
    headline_ts = concentration_ts or observation_ts
    age_h = (now - headline_ts).total_seconds() / 3600
    reading_time = ist(headline_ts)

    lines = [head, t["checked"].format(t=reading_time), ""]

    if pm25_ugm3 is not None:
        icon, word = plain[aqi.pm25_band(pm25_ugm3)]
        lines += [f"{icon} <b>{t['dust_band'].format(band=word)}</b>",
                  t["dust"].format(v=f"{pm25_ugm3:.0f}"),
                  t["dust_note"], ""]

    if warn_target is not None:
        lines += [t["warn"].format(t=ist(warn_target)), ""]

    overall, refusal = aqi.overall_aqi(readings)
    if overall is not None:
        lines += [
            t["score"].format(aqi=overall.aqi, band=plain[overall.band][1]),
            t["worst"].format(
                code=overall.dominant,
                plain=POLLUTANT_PLAIN[lang].get(overall.dominant,
                                                overall.dominant)),
            "",
            t["advice"],
            f"“{aqi.ADVISORY[overall.band]}”",
            f"— {aqi.ADVISORY_CITATION}",
        ]
    elif pm25 is not None:
        # Our own wording, and deliberately so: it describes what our number is
        # and is not, which is not something CPCB has published a sentence for.
        lines += [t["pm_only"], f"({refusal})"]
    else:
        lines += [t["no_score"]]

    lines += ["", t["stale"].format(t=reading_time, h=f"{age_h:.1f}")
                  if age_h > STALE_AFTER_H
                  else t["age"].format(h=f"{age_h:.1f}"),
              t["for"].format(p=esc(profile_label)),
              "", t["rate"]]

    return Message("\n".join(lines),
                   overall.aqi if overall else None,
                   overall.band if overall else None)


def warnings_by_station(now: datetime) -> dict[int, datetime]:
    """{station_id: target hour} for stations the promoted model warns about.

    Every failure here degrades to an empty dict and a line on stderr, and that
    is the one place in this script where swallowing is right rather than a §0.5
    violation: the forecast is an addition to a message that was worth sending
    for four months without it, so a missing model must cost the warning line
    and not the reading. The failure is still visible — it prints, and the run's
    fetch_log row carries the outcome — it just does not take the send with it.

    Runs on its own connection, before the send opens its own, so the heavy
    pandas work never happens with an open transaction on the sending
    connection. Neon suspends after five minutes idle and bills per wake, so
    two connections seconds apart are one wake, not two.
    """
    try:
        import forecast
        from baselines import CACHE, pull
    except ImportError as exc:
        # requirements-model.txt is not installed. Expected on a machine that
        # only ever runs --dry-run; NOT expected in the workflow, which
        # installs it.
        print(f"no forecast — model dependencies missing ({exc})",
              file=sys.stderr)
        return {}

    try:
        pull(CACHE)
        with connect() as conn, conn.cursor() as cur:
            calls = forecast.outlook(cur, CACHE, now)
    except forecast.NoModel as exc:
        print(f"no forecast — {exc}", file=sys.stderr)
        return {}
    except Exception as exc:
        print(f"no forecast — {redact(str(exc))}", file=sys.stderr)
        return {}

    if not calls:
        # Not an exception, so it would otherwise leave no trace at all — and
        # "the model warned nobody" and "the model was never consulted" are the
        # two states this script must never confuse (§0.5).
        print(f"no forecast — no station has a reading within "
              f"{forecast.MAX_ISSUE_AGE_H}h of now; pm25_history lags OpenAQ's "
              f"publishing", file=sys.stderr)
        return {}

    warned = {s: v["target_ts"] for s, v in calls.items() if v["warn"]}
    print(f"forecast: {len(calls)} stations, {len(warned)} warned",
          file=sys.stderr)
    return warned


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
    """(chat_id, station_id, station_name, profile_label, lang), unpaused only.

    The profile label is picked in SQL rather than by a branch here, and it
    COALESCEs: a profile added tomorrow without a Hindi label shows its English
    one instead of an empty button. Build plan section 1's promise is that a
    fourth profile is one INSERT, and a NOT NULL label_hi would have broken it.
    """
    sql = """
        SELECT s.chat_id, s.station_id, st.station_name,
               CASE WHEN s.lang = 'hi' THEN COALESCE(p.label_hi, p.label)
                    ELSE p.label END,
               s.lang
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


def sample_rows(conn, station_id: int | None, lang: str) -> list[tuple]:
    """Fake subscribers for --dry-run, so the message can be read before anyone
    is subscribed to receive it. chat_id 0 never reaches Telegram — --dry-run
    is the only caller."""
    sql = ("SELECT 0, station_id, station_name, %s, %s "
           "FROM stations WHERE is_active")
    params: list = [{"en": "Child with asthma", "hi": "दमे वाला बच्चा"}[lang], lang]
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


def already_sent(conn, send_date) -> set[tuple[int, int]]:
    """(chat_id, station_id) pairs sent today.

    Keyed on the station, not the chat alone, so someone who changes station
    with /stations gets the new station's reading now rather than nothing until
    tomorrow. Mirrors sent_log's unique index; changing one without the other
    turns the double-send guard into an integrity error.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT chat_id, station_id FROM sent_log WHERE send_date = %s",
                    (send_date,))
        return set(cur.fetchall())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="render every message to stdout and send nothing")
    ap.add_argument("--station", type=int, metavar="ID",
                    help="with --dry-run, render for this station only")
    ap.add_argument("--only", type=int, metavar="CHAT_ID",
                    help="send to one subscriber (used before inviting testers)")
    ap.add_argument("--lang", choices=LANGS, default="en",
                    help="with --dry-run, render in this language")
    ap.add_argument("--no-forecast", action="store_true",
                    help="send the reading only, as this script did before the "
                         "model was wired in")
    args = ap.parse_args()

    if args.station is not None and not args.dry_run:
        sys.exit("--station only makes sense with --dry-run")

    token = "" if args.dry_run else telegram_api.load_token()
    now = datetime.now(timezone.utc)
    send_date = now.astimezone(IST).date()

    warn_at = {} if args.no_forecast else warnings_by_station(now)

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

        rows = (sample_rows(conn, args.station, args.lang) if args.dry_run
                else subscribers(conn, args.only))
        if not rows:
            print("no subscribers to send to" if not args.dry_run
                  else "no active stations to render")

        done = set() if args.dry_run else already_sent(conn, send_date)
        station_ids = sorted({r[1] for r in rows})
        by_station = readings_at(conn, bulletin_ts, station_ids)
        by_conc = concentrations(conn, station_ids)

        for chat_id, station_id, station_name, profile_label, lang in rows:
            if (chat_id, station_id) in done:
                # The UNIQUE (chat_id, send_date, station_id) index is the real
                # guard; this is what stops a re-run costing a Telegram call
                # per subscriber before hitting it.
                skipped += 1
                continue

            conc = by_conc.get(station_id)
            message = compose(station_name, by_station[station_id],
                              bulletin_ts, now, profile_label,
                              pm25_ugm3=conc[1] if conc else None,
                              concentration_ts=conc[0] if conc else None,
                              warn_target=warn_at.get(station_id),
                              lang=lang)

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
                                          feedback_keyboard(sent_log_id, lang))
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

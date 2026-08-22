"""The message a real person reads about their own health. No DB, no network.

    python tests/test_message.py

Four things this exists to prevent, all of them quiet failures rather than
crashes:

  - CPCB's health sentence attached to a band CPCB did not write it against.
    Keying it to PM2.5 instead of the overall AQI understates risk exactly when
    PM2.5 is not the worst pollutant (docs/cpcb_aqi_breakpoints.md, "Resolved
    2026-08-10"). Nothing errors; the message is just wrong in the direction
    that matters.
  - CPCB's sentence presented as ours. Build plan §5: "We do not write our own
    medical guidance. Health advice is a liability surface." The citation is
    the shield, so a quote without one is a defect.
  - CPCB's sentence *paraphrased or translated*. Same rule, and the tempting
    one, because the Hindi message is otherwise fully Hindi. A translated
    health statement is our own health statement.
  - Jargon creeping back. The message is for a reader with no technical
    background. A word like "sub-index" or "bulletin" is meaningless to them
    and reads as a broken app, so the vocabulary is asserted, not trusted.
    The opposite error is guarded in the same list: "air score" was our own
    invented name for the AQI, and a private synonym for a public name is not
    simpler — it makes our number look like a different one.

The degraded path is tested here rather than against live data because it does
not occur in live data: measured 2026-08-13, zero of 2097 station-hours have a
PM2.5 reading and fewer than three scoreable pollutants. A path that cannot be
reached today will be reached eventually, and this is the only place it is
exercised.
"""

import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import env  # noqa: E402, F401  (UTF-8 console, for the Devanagari below)
from aqi import ADVISORY, ADVISORY_CITATION  # noqa: E402
from send_alerts import (LANGS, STALE_AFTER_H, TEXT, compose,  # noqa: E402
                         feedback_keyboard, lang_of)

OBS = datetime(2026, 8, 13, 23, 30, tzinfo=timezone.utc)  # 05:00 IST next day
PROFILE = "Child with asthma"

failures = 0


def check(label: str, got, want) -> None:
    global failures
    if got == want:
        print(f"  ok    {label}")
    else:
        failures += 1
        print(f"  FAIL  {label}\n        got  {got!r}\n        want {want!r}")


def at(hours_after: float) -> datetime:
    return OBS + timedelta(hours=hours_after)


print("The full path — the dust number leads, the advisory follows the OVERALL band:")

# Two sources, two quantities. `readings` are CPCB sub-indices from
# observations.value_avg; pm25_ugm3 is a measured concentration from
# pm25_history. PM10's sub-index of 250 puts the overall AQI in Poor while the
# 40 µg/m³ concentration is Satisfactory on its own — the advisory must be
# Poor's, or we understate exactly when it matters.
m = compose("Sector-51, Gurugram - HSPCB",
            {"PM2.5": 75.0, "PM10": 250.0, "NO2": 30.0}, OBS, at(2.0), PROFILE,
            pm25_ugm3=40.0, concentration_ts=OBS)
check("the headline is the measured dust number, with its unit",
      "Fine dust in the air (PM2.5): <b>40 µg/m³</b>" in m.text, True)
check("with its band said in plain words, not a CPCB label alone",
      "Air is mostly OK" in m.text, True)
check("the AQI is reported out of 500, under the name CPCB gave it",
      "Government AQI (last 24 hours): <b>250 out of 500</b>" in m.text,
      True)
check("the advisory is the OVERALL band's", ADVISORY["Poor"] in m.text, True)
check("not the dust band's", ADVISORY["Satisfactory"] in m.text, False)
check("the quote is cited, never presented as ours",
      ADVISORY_CITATION in m.text, True)
check("the worst pollutant is named, with a plain gloss",
      "PM10 (bigger dust)" in m.text, True)
check("sent_log gets the numbers", (m.overall_aqi, m.band), (250, "Poor"))
# The regression that shipped to real people: a sub-index printed as a
# concentration. 75 is PM2.5's value_avg here and must never reach the headline.
check("the sub-index is NEVER printed as the dust number",
      "(PM2.5): <b>75 µg/m³</b>" in m.text, False)
check("the AQI carries its own window, on its own line",
      "AQI (last 24 hours)" in m.text, True)
check("and nothing explains that in a sentence underneath",
      "Why two numbers" in m.text, False)
# "- HSPCB" is the pollution board's name. It means nothing to a person picking
# where they live, and the stored string keeps it because CPCB and OpenAQ join
# on that exact byte sequence.
check("the pollution board suffix never reaches the reader",
      "HSPCB" in m.text, False)
check("but the place itself survives", "Sector-51, Gurugram" in m.text, True)
# Two bands are shown and they legitimately disagree. Rendered as two full
# sentences the reader met "Air is clean" above "Air is not good" and had to
# guess which was the bug. Only ONE line may be a sentence about the air.
check("only the dust line is phrased as a sentence about the air",
      sum(1 for ln in m.text.split("\n")
          if ln.startswith(("🟢", "🟡", "🟠", "🔴", "🟣", "⚫"))), 1)
check("the score line carries a bare adjective, not a rival sentence",
      "500</b> — bad" in m.text, True)
print()

print("No jargon reaches the reader — the vocabulary is asserted, not trusted:")
# Every one of these named a mechanism rather than a fact. A reader with no
# technical background cannot act on any of them, and several read as an error.
# "AQI" and "µg/m³" are NOT on this list, and banning them was the mistake
# this comment exists to stop being repeated. They are the names CPCB and
# every other air app already use. Replacing them with invented plain words
# ("air score") made our number look like a different quantity from the one on
# the reader's other app. Simplify the sentence around a term, never the term.
BANNED = ["sub-index", "sub index", "bulletin", "air score",
          "concentration", "station is dark", "degraded",
          "pollutant sub", "observation"]

# The citation is exempt and cannot be otherwise: "CPCB Daily AQI Bulletin,
# p.13" is the name of a document, quoted so a reader can go and find it. Two
# banned words live inside it. Rewriting a citation to dodge a word test would
# break the only thing that makes the health sentence quotable at all.
body = "\n".join(ln for ln in m.text.split("\n")
                 if ADVISORY_CITATION not in ln)
for word in BANNED:
    check(f"never says {word!r}", word.lower() in body.lower(), False)
check("and the citation itself is untouched", ADVISORY_CITATION in m.text, True)
print()

print("The degraded path — CPCB's >=3-pollutant rule fails, so no score:")
# 72 µg/m³ and a PM2.5 sub-index of 138 are the same air: sub_index(72) is 138,
# and both read Moderate. Inconsistent fixtures here would let the message
# contradict itself and the test still pass.
m = compose("Patti Mehar, Ambala - HSPCB",
            {"PM2.5": 138.0, "NO2": 30.0}, OBS, at(2.0), PROFILE,
            pm25_ugm3=72.0, concentration_ts=OBS)
check("the documented fallback wording fires",
      "The real AQI may be higher" in m.text, True)
check("it still says what the number is",
      "(PM2.5): <b>72 µg/m³</b>" in m.text, True)
check("and the dust band is still named plainly",
      "Air is not good" in m.text, True)
# The whole reason no sentence appears: every one of CPCB's is written against
# an overall band we do not have here.
check("and NO CPCB health sentence is attached",
      any(s in m.text for s in ADVISORY.values()), False)
check("the reason is in the message, not swallowed",
      "CPCB requires 3" in m.text, True)
check("sent_log records no AQI", (m.overall_aqi, m.band), (None, None))
print()

print("The dark-station path — a row is not a reading:")
m = compose("Sector-18, Panipat - HSPCB",
            {"PM2.5": None, "PM10": 140.0, "NO2": 30.0, "SO2": 12.0},
            OBS, at(2.0), PROFILE)
check("it says there is no reading, without saying 'dark'",
      "There is no reading right now" in m.text, True)
check("it does not read as a broken subscription",
      "You are still subscribed" in m.text, True)
check("it says the outage is normal and self-fixing",
      "fixes itself" in m.text, True)
check("no number is invented", "PM2.5): <b>" in m.text, False)
check("and no AQI is sent even though 3 pollutants are present",
      (m.overall_aqi, m.band), (None, None))

m = compose("Nowhere", {}, None, at(2.0), PROFILE)
check("a station with no bulletin at all takes the same path",
      "There is no reading right now" in m.text, True)
print()

print("CPCB frozen but OpenAQ live — the 07:00 IST case the two sources exist for:")
# Measured 2026-08-19: CPCB's last morning bulletin is 05:00 IST and the next is
# between 10:00 and 13:00, while OpenAQ publishes 06:00 to 09:00 IST. A send at
# 07:00 therefore has a concentration and no fresh bulletin, and must not fall
# into the dark-station path — that would tell a subscriber their sensor is
# broken every single morning.
m = compose("Patti Mehar, Ambala - HSPCB", {}, None, at(2.0), PROFILE,
            pm25_ugm3=36.0, concentration_ts=OBS)
check("it is not treated as a dark station",
      "There is no reading right now" in m.text, False)
check("the measured number is the headline",
      "(PM2.5): <b>36 µg/m³</b>" in m.text, True)
check("no AQI is invented from a bulletin we do not have",
      (m.overall_aqi, m.band), (None, None))
check("and it says why there is no AQI",
      "has not put out an AQI" in m.text, True)
print()

print(f"Staleness is user-facing from day one (build plan §5, >{STALE_AFTER_H}h):")
readings = {"PM2.5": 72.0, "PM10": 140.0, "NO2": 30.0}
# The boundary is read from the constant, never typed twice, so raising the
# threshold to quiet a daily warning cannot also quiet this check.
under, over = STALE_AFTER_H - 1.0, STALE_AFTER_H + 1.0
ALARM = "Nothing new since"
check(f"at {under}h — OpenAQ's ordinary publishing lag — no alarm",
      ALARM in compose("S", readings, OBS, at(under), PROFILE).text, False)
check(f"at {over}h — later than OpenAQ ever runs — the alarm fires",
      ALARM in compose("S", readings, OBS, at(over), PROFILE).text, True)
check("and the age is stated, not just flagged",
      f"{over:.1f} hours" in compose("S", readings, OBS, at(over), PROFILE).text,
      True)
check("the age is always stated, alarm or not",
      f"{under:.1f} hours old" in compose("S", readings, OBS, at(under),
                                          PROFILE).text, True)
check("and the quiet line says why a reading is hours old",
      "reach us a few hours late" in compose("S", readings, OBS, at(under),
                                             PROFILE).text, True)
print()

print("A station name is third-party text and goes through HTML escaping:")
m = compose("<b>Sector-6</b> & Co", readings, OBS, at(2.0), PROFILE)
check("angle brackets are escaped", "&lt;b&gt;Sector-6&lt;/b&gt;" in m.text, True)
check("and the ampersand", "&amp; Co" in m.text, True)
check("no raw tag survives to break Telegram's parser",
      "<b>Sector-6</b>" in m.text, False)
print()

print("The profile is shown and changes nothing else (it gains behaviour in Phase 4):")
a = compose("S", readings, OBS, at(2.0), "Child with asthma")
b = compose("S", readings, OBS, at(2.0), "Works outdoors")
check("the label appears", "For: Works outdoors" in b.text, True)
check("and the two messages differ only by that line",
      a.text.replace("Child with asthma", "Works outdoors"), b.text)
print()

print("Hindi — everything is translated EXCEPT the one thing that must not be:")
hi = compose("Sector-51, Gurugram - HSPCB",
             {"PM2.5": 75.0, "PM10": 250.0, "NO2": 30.0}, OBS, at(2.0),
             "दमे वाला बच्चा", pm25_ugm3=40.0, concentration_ts=OBS, lang="hi")
check("the band is in Hindi", "हवा ठीक-ठाक है" in hi.text, True)
check("the AQI line is in Hindi", "सरकारी AQI" in hi.text, True)
check("the pollutant gloss is in Hindi", "PM10 (मोटी धूल)" in hi.text, True)
check("the profile label is in Hindi", "दमे वाला बच्चा" in hi.text, True)
# The load-bearing one. CPCB's health statement is a quote and the quote is the
# liability shield; translating it makes it ours. It stays in CPCB's English,
# and the Hindi label above it tells the reader that is what they are seeing.
check("CPCB's health sentence is still CPCB's English, word for word",
      ADVISORY["Poor"] in hi.text, True)
check("still cited to the page it came from",
      ADVISORY_CITATION in hi.text, True)
check("and the Hindi label says it is quoted from English",
      "अंग्रेज़ी में छपी है" in hi.text, True)
check("the numbers are identical to the English message",
      (hi.overall_aqi, hi.band), (250, "Poor"))
check("no English body sentence leaks into the Hindi message",
      "Government AQI" in hi.text, False)

hi_dark = compose("Sector-18, Panipat - HSPCB", {"PM2.5": None, "PM10": 140.0},
                  OBS, at(2.0), "दमे वाला बच्चा", lang="hi")
check("the dark-station path is Hindi too",
      "अभी कोई रीडिंग नहीं है" in hi_dark.text, True)

check("every key exists in both languages",
      {k: sorted(TEXT[k]) for k in LANGS},
      {k: sorted(TEXT["en"]) for k in LANGS})
check("an unknown language falls back to English rather than raising",
      lang_of("fr"), "en")
check("and so does a NULL from a row written before the column existed",
      lang_of(None), "en")
check("the feedback buttons are translated",
      feedback_keyboard(1, "hi")["inline_keyboard"][0][0]["text"],
      "👍 काम आया")
check("and the callback payload is identical in both languages",
      feedback_keyboard(1, "hi")["inline_keyboard"][0][0]["callback_data"],
      feedback_keyboard(1, "en")["inline_keyboard"][0][0]["callback_data"])
print()

print("The 12h forecast line — the only part of the model a person ever sees:")
EVENING = datetime(2026, 8, 14, 13, 30, tzinfo=timezone.utc)   # 07:00 PM IST
quiet = compose("S", readings, OBS, at(2.0), PROFILE, pm25_ugm3=72.0)
loud = compose("S", readings, OBS, at(2.0), PROFILE, pm25_ugm3=72.0,
               warn_target=EVENING)
check("no warning by default, so four months of messages are unchanged",
      "likely to get very bad" in quiet.text, False)
check("the warning appears when the model fires",
      "likely to get very bad" in loud.text, True)
check("and it names the hour, on a 12-hour clock",
      "7:00 PM" in loud.text, True)
check("it says what to do about it, in one short sentence",
      "finish outdoor work before then" in loud.text, True)
# The one claim the project cannot support. Nothing has drawn a calibration
# curve, so a percent would be a confidence nobody measured; the cutoff is the
# only part of the model's output with evidence behind it.
check("no probability, no percent, no 'chance' anywhere",
      bool(re.search(r"\d+\s*%|chance|probabilit", loud.text, re.I)), False)
check("a quiet forecast and no forecast at all produce the SAME text",
      quiet.text,
      compose("S", readings, OBS, at(2.0), PROFILE, pm25_ugm3=72.0,
              warn_target=None).text)
check("the warning does not disturb the reading above it",
      "(PM2.5): <b>72 µg/m³</b>" in loud.text, True)
check("nor CPCB's quoted advisory below it", "CPCB" in loud.text, True)

hi_loud = compose("S", readings, OBS, at(2.0), "दमे वाला बच्चा",
                  pm25_ugm3=72.0, warn_target=EVENING, lang="hi")
check("the warning is translated too",
      "हवा बहुत खराब हो सकती है" in hi_loud.text, True)
check("and carries no percent either",
      bool(re.search(r"\d+\s*%", hi_loud.text)), False)
print()


print()
if failures:
    sys.exit(f"FAILED — {failures} check(s). Do not send this to anyone.")
print("message OK — plain words throughout, and CPCB's wording still quoted, "
      "cited, untranslated, and keyed to the overall band.")

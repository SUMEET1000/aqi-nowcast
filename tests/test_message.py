"""The message a real person reads about their own health. No DB, no network.

    python tests/test_message.py

Two things this exists to prevent, both of which are quiet failures rather
than crashes:

  - CPCB's health sentence attached to a band CPCB did not write it against.
    Keying it to PM2.5 instead of the overall AQI understates risk exactly when
    PM2.5 is not the worst pollutant (docs/cpcb_aqi_breakpoints.md, "Resolved
    2026-08-10"). Nothing errors; the message is just wrong in the direction
    that matters.
  - CPCB's sentence presented as ours. Build plan §5: "We do not write our own
    medical guidance. Health advice is a liability surface." The citation is
    the shield, so a quote without one is a defect.

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

import env  # noqa: E402, F401  (UTF-8 console, for the µg/m³ below)
from aqi import ADVISORY, ADVISORY_CITATION  # noqa: E402
from send_alerts import STALE_AFTER_H, compose  # noqa: E402

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


print("The full path — µg/m³ headline, advisory selected by the OVERALL band:")

# Two sources, two quantities. `readings` are CPCB sub-indices from
# observations.value_avg; pm25_ugm3 is a measured concentration from
# pm25_history. PM10's sub-index of 250 puts the overall AQI in Poor while the
# 40 µg/m³ concentration is Satisfactory on its own — the advisory must be
# Poor's, or we understate exactly when it matters.
m = compose("Sector-51, Gurugram - HSPCB",
            {"PM2.5": 75.0, "PM10": 250.0, "NO2": 30.0}, OBS, at(2.0), PROFILE,
            pm25_ugm3=40.0, concentration_ts=OBS)
check("the headline is PM2.5 in µg/m³", "PM2.5: <b>40 µg/m³</b>" in m.text, True)
check("and its own band is shown", "— Satisfactory" in m.text, True)
check("the CPCB AQI is reported", "CPCB AQI 250 (Poor)" in m.text, True)
check("the advisory is the OVERALL band's",
      ADVISORY["Poor"] in m.text, True)
check("not the PM2.5 band's", ADVISORY["Satisfactory"] in m.text, False)
check("the quote is cited, never presented as ours",
      ADVISORY_CITATION in m.text, True)
check("the worst pollutant is named", "worst pollutant PM10" in m.text, True)
check("sent_log gets the numbers", (m.overall_aqi, m.band), (250, "Poor"))
# The regression that shipped to real people: a sub-index printed as a
# concentration. 75 is PM2.5's value_avg here and must never reach the headline.
check("the sub-index is NEVER printed as µg/m³",
      "75 µg/m³" in m.text, False)
check("and the two windows are distinguished, not left to collide",
      "24-hour index" in m.text, True)
print()

print("The degraded path — CPCB's >=3-pollutant rule fails, so no AQI:")
# 72 µg/m³ and a PM2.5 sub-index of 138 are the same air: sub_index(72) is 138,
# and both read Moderate. Inconsistent fixtures here would let the message
# contradict itself and the test still pass.
m = compose("Patti Mehar, Ambala - HSPCB",
            {"PM2.5": 138.0, "NO2": 30.0}, OBS, at(2.0), PROFILE,
            pm25_ugm3=72.0, concentration_ts=OBS)
check("the documented fallback wording fires",
      "the official AQI may be higher" in m.text, True)
check("it still says what the number is", "PM2.5: <b>72 µg/m³</b>" in m.text, True)
check("and names the PM2.5 band", "Moderate for PM2.5" in m.text, True)
check("no AQI is claimed", "Overall AQI" in m.text, False)
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
check("it says the station is not reporting",
      "not reporting PM2.5" in m.text, True)
check("it does not read as a broken subscription",
      "Your subscription is fine" in m.text, True)
check("no number is invented", "µg/m³" in m.text, False)
check("and no AQI is sent even though 3 pollutants are present",
      (m.overall_aqi, m.band), (None, None))

m = compose("Nowhere", {}, None, at(2.0), PROFILE)
check("a station with no bulletin at all takes the same path",
      "not reporting PM2.5" in m.text, True)
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
      "not reporting PM2.5" in m.text, False)
check("the measured concentration is the headline",
      "PM2.5: <b>36 µg/m³</b>" in m.text, True)
check("no AQI is invented from a bulletin we do not have",
      (m.overall_aqi, m.band), (None, None))
check("and it says why there is no AQI",
      "no AQI for this station" in m.text, True)
print()

print(f"Staleness is user-facing from day one (build plan §5, >{STALE_AFTER_H}h):")
readings = {"PM2.5": 72.0, "PM10": 140.0, "NO2": 30.0}
check("at 2.0h — the 07:00 IST send — there is no warning",
      "CPCB has not published" in compose("S", readings, OBS, at(2.0), PROFILE).text,
      False)
check("at 3.0h — the 08:00 IST limit — still none",
      "CPCB has not published" in compose("S", readings, OBS, at(3.0), PROFILE).text,
      False)
check("at 4.0h — 09:00 IST — the warning fires",
      "CPCB has not published" in compose("S", readings, OBS, at(4.0), PROFILE).text,
      True)
check("and the age is stated, not just flagged",
      "4.0 hours old" in compose("S", readings, OBS, at(4.0), PROFILE).text, True)
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
b = compose("S", readings, OBS, at(2.0), "Outdoor worker")
check("the label appears", "Profile: Outdoor worker" in b.text, True)
check("and the two messages differ only by that line",
      a.text.replace("Child with asthma", "Outdoor worker"), b.text)

print("The 12h forecast line — the only part of the model a person ever sees:")
EVENING = datetime(2026, 8, 14, 13, 30, tzinfo=timezone.utc)   # 07:00 PM IST
quiet = compose("S", readings, OBS, at(2.0), PROFILE, pm25_ugm3=72.0)
loud = compose("S", readings, OBS, at(2.0), PROFILE, pm25_ugm3=72.0,
               warn_target=EVENING)
check("no warning by default, so four months of messages are unchanged",
      "likely to be Very Poor" in quiet.text, False)
check("the warning appears when the model fires",
      "likely to be Very Poor" in loud.text, True)
check("and it names the hour, in IST, 12-hour clock",
      "7:00 PM IST" in loud.text, True)
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
      "PM2.5: <b>72 µg/m³</b>" in loud.text, True)
check("nor CPCB's quoted advisory below it",
      "CPCB" in loud.text, True)
print()

print()
if failures:
    sys.exit(f"FAILED — {failures} check(s). Do not send this to anyone.")
print("message OK — CPCB's wording is quoted, cited, and keyed to the overall band.")

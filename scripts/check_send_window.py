"""Is 07:00 IST still a send time that produces a fresh reading?

    python scripts/check_send_window.py
    python scripts/check_send_window.py --max-age-hours 1.5   # force a failure

Read-only and re-runnable. Exit 0 means the configured send hour still clears
build plan §5's 3-hour staleness rule on every day it could be measured.

Why this exists rather than a constant somebody remembers. CPCB's feed freezes
every morning: measured over four days on 2026-08-13, the last morning bulletin
was 05:00 IST and the next was between 10:00 and 13:00. At 07:00 the reading is
2.0h old, at 08:00 exactly 3.0h, at 09:00 4.0h. Four days is a small sample and
CPCB can change its publishing schedule without telling anyone, and the failure
would be quiet: every message would carry a staleness warning, every day, which
teaches people to ignore warnings and then hides the real one.

send_alerts.py is the safety net — it reads the bulletin's real timestamp and
computes the actual age at send time, so a shift produces an honest message
rather than a wrong one. This script is the other half: it detects the shift
and names the replacement hour.
"""

import argparse
import sys
from collections import defaultdict
from datetime import datetime, time, timedelta

from cpcb_api import IST
from db import connect

# Build plan §5. The same number lives in send_alerts.STALE_AFTER_H, which is
# what the message is actually judged against; this one is the threshold the
# recommendation is derived from, and --max-age-hours overrides it.
MAX_AGE_H = 3.0

# The configured send time, in IST. Change this and .github/workflows/
# send_alerts.yml's cron together — the cron is in UTC.
SEND_HOUR = 7

# Candidate hours to report on. Bounded by CPCB's freeze: 05:00 is the last
# morning bulletin and nothing new arrives before 10:00, so hours outside this
# span answer no question anyone is asking.
CANDIDATE_HOURS = range(5, 13)

WINDOW_DAYS = 14

# Below this the script refuses to reach a verdict, the same discipline as
# gate1_check.check_gaps declining to name a culprit on a small sample: one
# unusual morning out of two is 50% of the evidence.
MIN_DAYS = 3


def bulletins(conn, since: datetime) -> list[datetime]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT observation_ts FROM observations
            WHERE observation_ts >= %s ORDER BY observation_ts
            """,
            (since,),
        )
        return [r[0] for r in cur.fetchall()]


def worst_age_at(hour: int, stamps: list[datetime], now: datetime
                 ) -> tuple[float | None, int]:
    """The worst age a message sent at `hour` IST would have carried.

    Returns (worst age in hours, number of days that could be measured). A day
    contributes only if that hour has already passed and at least one bulletin
    exists at or before it — otherwise the day says nothing about this hour and
    counting it as 0h would be a pass invented out of missing data.
    """
    ist = [s.astimezone(IST) for s in stamps]
    by_day: dict = defaultdict(list)
    for s in ist:
        by_day[s.date()].append(s)

    worst = None
    days = 0
    for day, day_stamps in by_day.items():
        send_at = datetime.combine(day, time(hour), tzinfo=IST)
        if send_at > now:
            continue
        earlier = [s for s in ist if s <= send_at and s.date() == day]
        if not earlier:
            continue
        age = (send_at - max(earlier)).total_seconds() / 3600
        days += 1
        worst = age if worst is None else max(worst, age)

    return worst, days


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-age-hours", type=float, default=MAX_AGE_H,
                    help=f"staleness threshold to judge against (default {MAX_AGE_H})")
    ap.add_argument("--days", type=int, default=WINDOW_DAYS,
                    help=f"rolling window in days (default {WINDOW_DAYS})")
    args = ap.parse_args()

    now = datetime.now(IST)
    since = now - timedelta(days=args.days)

    with connect() as conn:
        stamps = bulletins(conn, since)

    if not stamps:
        sys.exit(f"no bulletins in the last {args.days} days — nothing to measure")

    span_days = len({s.astimezone(IST).date() for s in stamps})
    print(f"check_send_window — {len(stamps)} bulletins across {span_days} IST day(s), "
          f"threshold {args.max_age_hours}h\n")
    print("  hour (IST)   days   worst age   clears?")

    # One pass produces the table, the recommendation and the exit code, so the
    # three cannot disagree. Five checks in this repo once printed PASS from a
    # separate expression than the one that decided it.
    clearing: list[int] = []
    ages: dict[int, float | None] = {}
    measured_days = 0
    for hour in CANDIDATE_HOURS:
        age, days = worst_age_at(hour, stamps, now)
        ages[hour] = age
        measured_days = max(measured_days, days)
        clears = age is not None and days >= MIN_DAYS and age <= args.max_age_hours
        if clears:
            clearing.append(hour)
        shown = "—" if age is None else f"{age:.1f}h"
        print(f"  {hour:02d}:00        {days:>3}   {shown:>9}   "
              f"{'yes' if clears else 'no'}")

    print()
    if measured_days < MIN_DAYS:
        sys.exit(f"WITHHELD — only {measured_days} measurable day(s), "
                 f"{MIN_DAYS} required. This is not a pass and not a failure.")

    if not clearing:
        sys.exit(f"FAIL — no hour in {CANDIDATE_HOURS.start:02d}:00-"
                 f"{CANDIDATE_HOURS.stop - 1:02d}:00 IST clears "
                 f"{args.max_age_hours}h. CPCB's publishing schedule has changed "
                 f"enough that no send time in this window produces a fresh "
                 f"reading; re-measure the freeze before choosing one.")

    latest = max(clearing)
    if SEND_HOUR not in clearing:
        age = ages[SEND_HOUR]
        sys.exit(f"FAIL — the configured send hour {SEND_HOUR:02d}:00 IST now "
                 f"carries a worst age of "
                 f"{'no measurement' if age is None else f'{age:.1f}h'}, over the "
                 f"{args.max_age_hours}h threshold. Latest hour that still "
                 f"clears: {latest:02d}:00 IST. Change SEND_HOUR here and the "
                 f"cron in .github/workflows/send_alerts.yml together — the "
                 f"cron is in UTC.")

    print(f"OK — {SEND_HOUR:02d}:00 IST clears {args.max_age_hours}h "
          f"(worst {ages[SEND_HOUR]:.1f}h over {measured_days} day(s)). "
          f"Latest hour that still clears: {latest:02d}:00 IST.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

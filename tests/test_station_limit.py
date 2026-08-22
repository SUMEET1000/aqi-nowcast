"""Proves the station-change cap actually refuses the third change.

    python tests/test_station_limit.py

The cap exists to stop one person spending a GitHub Actions run and a Neon wake
per tap. A cap that silently allows everything costs money without ever
erroring, so it gets a test rather than a code read.

The gate lives in bot/src/index.js and is SQL, not Python, so it cannot be
imported the way tests/test_null_guard.py imports ingest.UPSERT. Instead the
two limits are READ OUT OF THAT FILE below and used here, so the numbers cannot
drift apart. A structural rewrite of the statement would still need this test
re-read; the numbers are what change in practice.

Everything runs in a transaction that is always rolled back, so this writes
nothing to station_changes even when it fails.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from db import connect          # noqa: E402

BOT = os.path.join(os.path.dirname(__file__), "..", "bot", "src", "index.js")

# A chat id Telegram cannot issue to a user. Telegram user ids are positive;
# negative ids are groups, and this one is far outside any real range. Even if
# the rollback failed, this row could never rate-limit a real person.
CHAT_ID = -999999999999

# The gate from bot/src/index.js, with the two limits substituted in. psycopg
# needs %s, the Worker uses $1 — same statement, different placeholder syntax.
GATE = """
    WITH purge AS (
      DELETE FROM station_changes WHERE changed_at < now() - interval '7 days'
    )
    INSERT INTO station_changes (chat_id)
    SELECT %s
    WHERE (SELECT count(*) FROM station_changes
           WHERE chat_id = %s
             AND changed_at > now() - interval '24 hours') < {day}
      AND (SELECT count(*) FROM station_changes
           WHERE chat_id = %s
             AND changed_at > now() - interval '7 days') < {week}
    RETURNING chat_id
"""

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def read_limits() -> tuple[int, int]:
    """The two constants, from the Worker source, so this test cannot drift."""
    with open(BOT, encoding="utf-8") as fh:
        src = fh.read()
    found = {}
    for name in ("MAX_CHANGES_DAY", "MAX_CHANGES_WEEK"):
        m = re.search(rf"^const {name} = (\d+);", src, re.M)
        if not m:
            sys.exit(f"{name} not found in {os.path.normpath(BOT)} — "
                     "the gate was renamed or restructured; re-read this test")
        found[name] = int(m.group(1))
    return found["MAX_CHANGES_DAY"], found["MAX_CHANGES_WEEK"]


def attempt(cur, day: int, week: int) -> bool:
    """One station change through the real gate. True when it was allowed."""
    cur.execute(GATE.format(day=day, week=week), (CHAT_ID, CHAT_ID, CHAT_ID))
    return cur.fetchone() is not None


def main() -> int:
    day, week = read_limits()
    print(f"limits read from bot/src/index.js: {day}/day, {week}/week")
    if day < 1 or week < day:
        sys.exit(f"nonsensical limits: {day}/day, {week}/week")

    conn = connect()
    try:
        with conn.cursor() as cur:
            # Nothing recorded yet, so the first `day` attempts pass and the
            # next one does not.
            allowed = [attempt(cur, day, week) for _ in range(day + 1)]
            check("first attempts allowed", allowed[:day], [True] * day)
            check("attempt over the daily cap refused", allowed[day], False)

            # Age everything past 24h. The daily window is now clear, so the
            # weekly cap is the only thing left holding — this is the half a
            # daily-only limit would let through.
            cur.execute(
                "UPDATE station_changes SET changed_at = now() - interval '25 hours' "
                "WHERE chat_id = %s",
                (CHAT_ID,),
            )
            after = [attempt(cur, day, week) for _ in range(week)]
            still_allowed = sum(after)
            check("weekly cap allows only the remainder",
                  still_allowed, week - day)

            # Age everything past 7 days. Both windows are empty again, and the
            # purge in the gate should have removed the rows outright.
            cur.execute(
                "UPDATE station_changes SET changed_at = now() - interval '8 days' "
                "WHERE chat_id = %s",
                (CHAT_ID,),
            )
            check("allowed again once the week has passed",
                  attempt(cur, day, week), True)
            cur.execute(
                "SELECT count(*) FROM station_changes "
                "WHERE chat_id = %s AND changed_at < now() - interval '7 days'",
                (CHAT_ID,),
            )
            check("purge removed the aged rows", cur.fetchone()[0], 0)
    finally:
        # Never committed. This test must not leave a row that rate-limits
        # anybody, and must not depend on cleanup code running to be safe.
        conn.rollback()
        conn.close()

    if failures:
        print(f"\nFAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

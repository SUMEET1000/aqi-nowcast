"""Populate the profiles table. Idempotent — safe to re-run.

    python scripts/seed_profiles.py

Three rows, from build plan §1. The point of this table is that a fourth
profile is an INSERT here and nothing else: no branch in send_alerts.py, no
new button hardcoded in the bot Worker. If adding one ever needs a code
change, that is the bug, not the missing profile.
"""

import sys

from db import connect

# threshold_pm25 and cooldown_hours are stored now and read by nothing until
# Phase 4 (see db/schema.sql).
#
# The thresholds are CPCB band edges from docs/cpcb_aqi_breakpoints.md — 61 is
# where PM2.5 enters Moderate, the band whose CPCB health statement first names
# asthma and lung disease; 91 is the Poor edge. They are NOT clinical numbers
# and nothing here invents any: build plan §5 forbids writing our own medical
# guidance, and the same rule applies to a number as to a sentence. Phase 4
# revisits them against real subscriber taps.
#
# The descriptions say who the profile is for, in build plan §1's own words.
PROFILES = [
    (
        "asthma_child",
        "Child with asthma",
        "For a parent deciding about school and outdoor play.",
        61.0,
        24,
    ),
    (
        "copd_elderly",
        "Older adult with COPD",
        "For deciding about a morning walk.",
        61.0,
        24,
    ),
    (
        "outdoor_worker",
        "Outdoor worker",
        "For deciding about shift timing and masking.",
        91.0,
        24,
    ),
]


def main() -> int:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO profiles
                    (profile_id, label, description, threshold_pm25, cooldown_hours)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (profile_id) DO UPDATE SET
                    label          = EXCLUDED.label,
                    description    = EXCLUDED.description,
                    threshold_pm25 = EXCLUDED.threshold_pm25,
                    cooldown_hours = EXCLUDED.cooldown_hours
                """,
                PROFILES,
            )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT profile_id FROM profiles ORDER BY profile_id")
            present = [r[0] for r in cur.fetchall()]

    print(f"profiles table: {len(present)} rows — {', '.join(present)}")

    # A profile row referenced by a subscriber but absent from PROFILES would
    # mean someone deleted a row by hand while people were subscribed to it.
    # The FK stops the delete, so this can only fire if the constraint is gone.
    missing = {p[0] for p in PROFILES} - set(present)
    if missing:
        sys.exit(f"seeded but missing afterwards: {sorted(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

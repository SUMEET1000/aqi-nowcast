"""Apply db/schema.sql. Idempotent — safe to run any number of times.

    python scripts/init_db.py

There is no teardown flag and there must never be one. observations accumulate
hourly from a snapshot API and cannot be refetched (build plan §4).
"""

import os
import sys

from db import connect

SCHEMA = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")

# Everything the schema is supposed to create. Checked after applying, so a
# silently partial apply fails loudly instead of leaving the ingester to
# discover it at 3am (§0.5).
EXPECTED_TABLES = ("stations", "observations", "fetch_log")


def main() -> int:
    if not os.path.exists(SCHEMA):
        sys.exit(f"schema not found at {os.path.normpath(SCHEMA)}")

    with open(SCHEMA, encoding="utf-8") as fh:
        sql = fh.read()

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = ANY(%s)
                """,
                (list(EXPECTED_TABLES),),
            )
            found = {r[0] for r in cur.fetchall()}

    missing = set(EXPECTED_TABLES) - found
    if missing:
        sys.exit(f"schema applied but these tables are missing: {sorted(missing)}")

    print(f"schema OK — tables present: {', '.join(sorted(found))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""One bad record must never cost the whole bulletin.

    python tests/test_build_rows.py

No database, no network — build_rows is pure, so this runs in CI and costs
nothing. That matters: tests/test_null_guard.py needs Neon, so it costs billed
compute and in practice runs rarely.

A regression test: before 2026-08-10 the record loop read r["station"] and
r["pollutant_id"] outside its try and guarded only (KeyError, ValueError), so
each case below raised straight out of main() ahead of the single commit(),
losing all 30 stations for a bulletin CPCB does not archive.
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from ingest import build_rows  # noqa: E402

TS = datetime(2026, 8, 11, 4, 30, tzinfo=timezone.utc)
KNOWN = {"Sector-6, Panchkula - HSPCB": 1, "Patti Mehar, Ambala - HSPCB": 2}

failures = 0


def check(label: str, got, want) -> None:
    global failures
    if got == want:
        print(f"  ok    {label}")
    else:
        failures += 1
        print(f"  FAIL  {label}\n        got  {got!r}\n        want {want!r}")


def good(station: str, pollutant: str = "PM2.5") -> dict:
    return {"station": station, "pollutant_id": pollutant,
            "min_value": "8", "max_value": "56", "avg_value": "42"}


print("Each case: one poisoned record among two healthy ones.")
print("The healthy rows must survive, and the bad one must be reported.\n")

healthy = [good("Sector-6, Panchkula - HSPCB"),
           good("Patti Mehar, Ambala - HSPCB", "PM10")]

cases = [
    ("missing 'station' key",
     {"pollutant_id": "PM2.5", "min_value": "8", "max_value": "56", "avg_value": "42"}),
    ("missing 'pollutant_id' key",
     {"station": "Sector-6, Panchkula - HSPCB",
      "min_value": "8", "max_value": "56", "avg_value": "42"}),
    ("avg_value as a JSON number, not a string",
     {"station": "Sector-6, Panchkula - HSPCB", "pollutant_id": "NO2",
      "min_value": "8", "max_value": "56", "avg_value": 42}),
    ("missing 'avg_value' key",
     {"station": "Sector-6, Panchkula - HSPCB", "pollutant_id": "SO2",
      "min_value": "8", "max_value": "56"}),
    ("station is JSON null",
     {"station": None, "pollutant_id": "CO",
      "min_value": "8", "max_value": "56", "avg_value": "42"}),
    ("avg_value is unparseable text",
     {"station": "Sector-6, Panchkula - HSPCB", "pollutant_id": "NH3",
      "min_value": "8", "max_value": "56", "avg_value": "twelve"}),
    # A NaN reaching observations is worse than a lost row: it propagates
    # silently through every Phase 3 baseline and Phase 4 rolling mean.
    ("avg_value is NaN",
     {"station": "Sector-6, Panchkula - HSPCB", "pollutant_id": "OZONE",
      "min_value": "8", "max_value": "56", "avg_value": "NaN"}),
    ("avg_value overflows to inf",
     {"station": "Sector-6, Panchkula - HSPCB", "pollutant_id": "OZONE",
      "min_value": "8", "max_value": "56", "avg_value": "1e400"}),
]

for label, poison in cases:
    print(f"{label}:")
    rows, seen, unknown, parse_errors = build_rows(healthy + [poison], KNOWN, TS)
    check("the 2 healthy rows survived", len(rows), 2)
    check("the bad record was reported, not swallowed",
          len(parse_errors) + sum(unknown.values()), 1)
    print()

print("A JSON null station is reportable (sorted() must not raise):")
_, _, unknown, _ = build_rows([{"station": None, "pollutant_id": "CO",
                               "min_value": "1", "max_value": "2",
                               "avg_value": "1"}], KNOWN, TS)
try:
    check("sorted(unknown) works", sorted(unknown), ["None"])
except TypeError as e:
    failures += 1
    print(f"  FAIL  sorted(unknown) raised {e}")
print()

print("The happy path still works:")
rows, seen, unknown, parse_errors = build_rows(healthy, KNOWN, TS)
check("both rows built", len(rows), 2)
check("no anomalies", (unknown, parse_errors), ({}, []))
check("'NA' becomes SQL NULL, never 0",
      build_rows([{"station": "Sector-6, Panchkula - HSPCB", "pollutant_id": "PM2.5",
                   "min_value": "NA", "max_value": "NA", "avg_value": "NA"}],
                 KNOWN, TS)[0][0][3:6],
      (None, None, None))
check("an unknown station is counted, not inserted",
      build_rows([good("Brand New Station - HSPCB")], KNOWN, TS)[2],
      {"Brand New Station - HSPCB": 1})

print()
if failures:
    sys.exit(f"FAILED — {failures} check(s). One bad record can still cost the bulletin.")
print("build_rows OK — a poisoned record costs that record only.")

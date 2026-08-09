"""Phase 0 / Gate 0.1 probe — throwaway.

Answers one question: do at least 3 stations report PM2.5 with last_update
inside the last 3 hours? Exits non-zero if not, so the gate is a command and
not a vibe (build plan §0.4).

Also regenerates docs/stations.md. Stdlib only — no dependencies.

    python scripts/probe_cpcb.py --state Haryana
    python scripts/probe_cpcb.py --state Haryana --write-doc
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

RESOURCE = "3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69"
BASE = f"https://api.data.gov.in/resource/{RESOURCE}"

# data.gov.in silently HANGS on requests with urllib's default User-Agent —
# no response, no error, just a read timeout after 45s+. The identical request
# with any ordinary UA returns in ~0.4s. Measured 2026-08-09. Do not remove:
# in the Phase 1 hourly cron this presents as a random ingestion failure.
HEADERS = {"User-Agent": "aqi-nowcast/0.1 (portfolio project; contact via repo)"}

# The API stamps every row with the same bulletin time in IST and carries no
# timezone marker. See docs/stations.md "Correction 2".
IST = timezone(timedelta(hours=5, minutes=30))

FRESH_HOURS = 3
MIN_FRESH_STATIONS = 3
NULL_SENTINEL = "NA"  # see Correction 3


def load_key() -> str:
    """Read the API key from .env or the environment. Never printed."""
    key = os.environ.get("DATA_GOV_IN_API_KEY")
    if not key:
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        if os.path.exists(env_path):
            with open(env_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("DATA_GOV_IN_API_KEY=") and "=" in line:
                        key = line.split("=", 1)[1].strip().strip("\"'")
                        break
    if not key:
        sys.exit(
            "DATA_GOV_IN_API_KEY is not set. Put it in .env (see .env.example).\n"
            "Get a personal key from data.gov.in -> My Account -> API Key."
        )
    return key


def fetch(state: str, key: str, limit: int = 1000) -> list[dict]:
    params = {
        "api-key": key,
        "format": "json",  # without this the API returns XML
        "limit": str(limit),  # defaults to 10; 500+ rows exist nationally
        "filters[state]": state,
    }
    url = f"{BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=HEADERS)

    # data.gov.in intermittently drops the TLS handshake — observed 3 times in
    # one Phase 0 session, ~1 in 4 calls. Retried, not swallowed (§0.5): the
    # attempt is logged, and exhausting retries is a hard failure. In Phase 1
    # this same behaviour is what fetch_log.outcome='http_error' exists to count.
    payload = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status != 200:
                    sys.exit(f"HTTP {resp.status} from data.gov.in")
                payload = json.load(resp)
            break
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"  data.gov.in transient failure ({type(e).__name__}), "
                  f"attempt {attempt}/3", file=sys.stderr)
            if attempt == 3:
                sys.exit(f"data.gov.in unreachable after 3 attempts: {e}")
            time.sleep(2 ** attempt)

    if payload.get("status") != "ok":
        sys.exit(f"API returned status={payload.get('status')!r}: {payload.get('message')!r}")

    records = payload.get("records", [])
    # No silent fallbacks (§0.5): an empty result is a hard stop, not a warning.
    if not records:
        sys.exit(f"Zero records for state={state!r}. Widen to NCR before giving up.")
    if len(records) >= limit:
        sys.exit(f"Hit the limit ({limit}) — raise it, results are being truncated.")
    return records


def parse_value(raw: str) -> float | None:
    """Map the 'NA' sentinel to None. Never coerce it to 0."""
    if raw is None or raw.strip() == NULL_SENTINEL or raw.strip() == "":
        return None
    return float(raw)


def parse_bulletin_ts(raw: str) -> datetime:
    """last_update is 'DD-MM-YYYY HH:MM:SS' in IST, with no timezone marker."""
    return datetime.strptime(raw, "%d-%m-%Y %H:%M:%S").replace(tzinfo=IST)


def analyse(records: list[dict]) -> dict:
    now = datetime.now(IST)
    stations: dict[str, dict] = defaultdict(dict)
    for r in records:
        stations[r["station"]][r["pollutant_id"]] = r

    rows = []
    for name, pollutants in sorted(stations.items()):
        any_row = next(iter(pollutants.values()))
        ts = parse_bulletin_ts(any_row["last_update"])
        age_h = (now - ts).total_seconds() / 3600
        pm = pollutants.get("PM2.5")
        rows.append(
            {
                "station": name,
                "city": any_row["city"],
                "lat": any_row["latitude"],
                "lon": any_row["longitude"],
                "pollutants": sorted(pollutants),
                "has_pm25": pm is not None,
                "pm25": parse_value(pm["avg_value"]) if pm else None,
                "last_update": any_row["last_update"],
                "age_h": age_h,
                # These are the silent row-droppers (§0.2). Flag, never auto-strip:
                # both CPCB and OpenAQ carry the same dirt, so normalising one
                # side alone would break the join that currently works.
                "dirty_name": name != name.strip() or "  " in name,
            }
        )

    with_pm25 = [r for r in rows if r["has_pm25"]]
    fresh = [r for r in with_pm25 if r["age_h"] <= FRESH_HOURS]
    return {
        "now": now,
        "rows": rows,
        "with_pm25": with_pm25,
        "fresh": fresh,
        "bulletins": Counter(r["last_update"] for r in records),
        "na_count": sum(1 for r in records if r["avg_value"].strip() == NULL_SENTINEL),
        "passed": len(fresh) >= MIN_FRESH_STATIONS,
    }


def render_doc(state: str, a: dict) -> str:
    lines = [
        "# Stations — Gate 0.1",
        "",
        f"Generated by `scripts/probe_cpcb.py --state {state} --write-doc` "
        f"on {a['now']:%Y-%m-%d %H:%M} IST.",
        "Source: data.gov.in CPCB resource `3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69`.",
        "Do not hand-edit — re-run the script.",
        "",
        "## Gate result",
        "",
        f"- Stations returned: **{len(a['rows'])}**",
        f"- Reporting PM2.5: **{len(a['with_pm25'])}**",
        f"- Fresh (last_update <= {FRESH_HOURS}h): **{len(a['fresh'])}**",
        f"- Gate needs {MIN_FRESH_STATIONS}. **{'PASS' if a['passed'] else 'FAIL'}**",
        "",
        "## Corrections to the build plan found by this probe",
        "",
        "1. **Field names in build plan §4 are wrong.** The API returns `min_value`, "
        "`max_value`, `avg_value` — not `pollutant_min/max/avg`. The Phase 1 schema must "
        "use the real names.",
        "",
        "2. **`last_update` is a national bulletin timestamp, not a per-station "
        "measurement time.** Every row in this pull carries an identical value "
        f"({len(a['bulletins'])} distinct value(s) across all rows). This **breaks the "
        "staleness design in §4**: the gap between `last_update_api` and `ingested_at` is "
        "identical for every station and can never identify a single dead station. "
        "Per-station staleness must instead be derived from **value change** — a station "
        "whose `avg_value` is unchanged across N consecutive bulletins is flagged. Keep "
        "storing `last_update_api` anyway: it detects a whole-feed stall, which is a "
        "different and also-real failure mode.",
        "",
        f"3. **`NA` is the null sentinel** — {a['na_count']} occurrence(s) in `avg_value` "
        "in this single snapshot. `float()` raises on it and `pandas` will read it as the "
        "string `\"NA\"`. Must become SQL NULL at parse time. This is what makes §4's "
        "\"never overwrite a non-null reading with a null\" rule load-bearing.",
        "",
        "4. **Station-name whitespace anomalies are live**, exactly as §0.2 predicted. "
        "Listed below. Do **not** auto-strip them: OpenAQ carries the same dirt, so "
        "normalising one side alone would break a join that currently works. "
        "See `docs/station_mapping.md`.",
        "",
    ]

    dirty = [r for r in a["rows"] if r["dirty_name"]]
    if dirty:
        lines.append("### Dirty station strings")
        lines.append("")
        for r in dirty:
            why = []
            if r["station"] != r["station"].strip():
                why.append("leading/trailing space")
            if "  " in r["station"]:
                why.append("double space")
            lines.append(f"- `{r['station']!r}` — {', '.join(why)}")
        lines.append("")

    lines += [
        "## All stations",
        "",
        "| City | Station (exact string) | PM2.5 | Age | Lat | Lon | Pollutants |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in a["rows"]:
        pm = "—" if r["pm25"] is None else f"{r['pm25']:.0f}"
        flag = " ⚠" if r["dirty_name"] else ""
        lines.append(
            f"| {r['city']} | `{r['station']}`{flag} | {pm} | {r['age_h']:.1f}h | "
            f"{r['lat']} | {r['lon']} | {len(r['pollutants'])} |"
        )

    lines += [
        "",
        "All stations report the identical pollutant set: "
        f"`{', '.join(a['rows'][0]['pollutants'])}`.",
        "",
        "## Phase 1 shortlist",
        "",
        "Postgres free tiers have row limits (§10), so Phase 1 logs a subset, not all "
        f"{len(a['rows'])} stations.",
        "",
        "**Seed stations (pinned — these are where the Gate 0.3 testers live).**",
        "Tester identities live only in the gitignored `docs/distribution.md`; this file is "
        "committed, so it names stations, never people.",
        "",
        "- `Patti Mehar, Ambala - HSPCB` — 2 testers",
        "- `Sector-7, Kurukshetra - HSPCB` — 1 tester",
        "",
        "Two seed stations rather than one is deliberate: **if both ever report identical "
        "values, that is a pipeline bug, not weather.**",
        "",
        "**Market stations (`asthma_child` outreach targets, high PM2.5):**",
        "",
        "- `Vikas Sadan, Gurugram - HSPCB`",
        "- `Sector-51, Gurugram - HSPCB`",
        "- `Teri Gram, Gurugram - HSPCB`",
        "- `Sector 11, Faridabad - HSPCB`",
        "- `Sector 30, Faridabad - HSPCB`",
        "- `New Industrial Town, Faridabad - HSPCB`",
        "",
        "**Known limitation (README §10, Data):** Patti Mehar is in Ambala City, several km "
        "from Ambala Cantt; the Kurukshetra station is ~5km from Pipli. **Station AQI is not "
        "doorstep AQI.** Inherent to any station-based product; stated, not glossed.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="Haryana")
    ap.add_argument("--write-doc", action="store_true")
    args = ap.parse_args()

    records = fetch(args.state, load_key())
    a = analyse(records)

    print(f"state={args.state}  rows={len(records)}  stations={len(a['rows'])}")
    print(f"distinct last_update values across all rows: {len(a['bulletins'])}")
    print(f"'NA' sentinel occurrences in avg_value: {a['na_count']}")
    print()
    for r in a["fresh"]:
        pm = "—" if r["pm25"] is None else f"{r['pm25']:.0f}"
        print(f"  {r['age_h']:5.1f}h  pm25={pm:>4}  {r['city']:<16} {r['station']}")
    print()
    print(f"with PM2.5: {len(a['with_pm25'])} | fresh (<={FRESH_HOURS}h): {len(a['fresh'])}")

    if args.write_doc:
        out = os.path.join(os.path.dirname(__file__), "..", "docs", "stations.md")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(render_doc(args.state, a))
        print(f"wrote {os.path.normpath(out)}")

    print(f"GATE 0.1: {'PASS' if a['passed'] else 'FAIL'} "
          f"(need {MIN_FRESH_STATIONS} fresh PM2.5 stations, have {len(a['fresh'])})")
    if not a["passed"]:
        print("Widen to NCR (Gurugram, Faridabad, Bahadurgarh, Rohtak, Bhiwani, Jind) "
              "before concluding there is no data source.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

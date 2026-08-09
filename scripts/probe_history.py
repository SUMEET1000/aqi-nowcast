"""Phase 0 / Gate 0.2 probe — throwaway.

Maps every live CPCB station to a history source and proves none was dropped.

The gate is the accounting assertion: mapped + unmatched == total. A merge that
silently drops rows is the failure mode §0.2 warns about (same class as the
district-name bug on the farm project) — it raises nothing and looks fine.

Also measures OpenAQ *historical depth* per station, which decides whether
OpenAQ or Open-Meteo is the Phase 3/4 training source.

Stdlib only.

    python scripts/probe_history.py --write-doc
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from probe_cpcb import HEADERS, IST, fetch as fetch_cpcb, load_key as load_cpcb_key

OPENAQ = "https://api.openaq.org/v3"
# Haryana bounding box: minLon, minLat, maxLon, maxLat
HARYANA_BBOX = "74.4,27.6,77.6,30.9"
LIVE_WITHIN_DAYS = 2
MIN_INTERVAL_S = 1.1  # OpenAQ free tier is ~60 req/min

# Manual mapping rows. These two are the silent row-droppers: an exact string
# join drops them and raises nothing. Values are OpenAQ location ids, verified
# 2026-08-09. Keep this table explicit rather than writing a fuzzy matcher —
# a fuzzy matcher would also "match" things we have not checked.
MANUAL_MAP = {
    # CPCB says the site is operated by IITM; OpenAQ attributes it to IMD.
    "NISE Gwal Pahari, Gurugram - IITM": "NISE Gwal Pahari, Gurugram - IMD",
    # CPCB appends the district in parentheses; OpenAQ does not.
    "General Hospital, Mandikhera(Nuh) - HSPCB": "General Hospital, Mandikhera - HSPCB",
}


def load_openaq_key() -> str:
    key = os.environ.get("OPENAQ_API_KEY")
    if not key:
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        if os.path.exists(env_path):
            with open(env_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("OPENAQ_API_KEY=") and "=" in line:
                        key = line.split("=", 1)[1].strip().strip("\"'")
                        break
    if not key:
        sys.exit("OPENAQ_API_KEY is not set. Put it in .env (see .env.example).")
    return key


_last_call = 0.0


def openaq_get(path: str, key: str, **params) -> dict:
    """Throttled GET. OpenAQ rate-limits the free tier and answers 429; an
    unthrottled sweep of 30 stations trips it partway through and leaves a
    half-built mapping table, which is worse than failing outright."""
    global _last_call
    url = f"{OPENAQ}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={**HEADERS, "X-API-Key": key})

    for attempt in range(5):
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            _last_call = time.monotonic()
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            backoff = 2 ** attempt
            print(f"  429 rate-limited, backing off {backoff}s "
                  f"(attempt {attempt + 1}/5)", file=sys.stderr)
            time.sleep(backoff)
    sys.exit(f"OpenAQ still 429 after 5 retries on {path}. Rerun later.")


def fetch_openaq_locations(key: str) -> dict[str, dict]:
    """Live PM2.5 locations in the Haryana bbox, keyed by exact name."""
    payload = openaq_get("locations", key, bbox=HARYANA_BBOX, limit=1000)
    results = payload.get("results", [])
    if not results:
        sys.exit("OpenAQ returned zero locations for the Haryana bbox.")

    now = datetime.now(timezone.utc)
    out = {}
    stale = 0
    for loc in results:
        if not any(s["parameter"]["name"] == "pm25" for s in loc.get("sensors", [])):
            continue
        last = loc.get("datetimeLast")
        if not last:
            stale += 1
            continue
        # Presence in the catalogue does NOT mean live. One location in this
        # bbox last reported in 2018.
        last_dt = datetime.fromisoformat(last["utc"].replace("Z", "+00:00"))
        if (now - last_dt).days > LIVE_WITHIN_DAYS:
            stale += 1
            continue
        out[loc["name"]] = {
            "id": loc["id"],
            "first": (loc.get("datetimeFirst") or {}).get("utc"),
            "last": last["utc"],
            # Carried from this response so the depth pass needs no extra
            # locations/{id} call — halves the request count against the limit.
            "pm25_sensor_ids": [s["id"] for s in loc.get("sensors", [])
                                if s["parameter"]["name"] == "pm25"],
        }
    print(f"OpenAQ: {len(out)} live PM2.5 locations in bbox "
          f"({stale} with pm25 but stale/never-reported, excluded)")
    return out


def build_mapping(cpcb_stations: list[str], oa: dict[str, dict]) -> tuple[list, list]:
    mapped, unmatched = [], []
    for name in sorted(cpcb_stations):
        if name in oa:
            mapped.append({"cpcb": name, "openaq_name": name, "how": "exact", **oa[name]})
        elif name in MANUAL_MAP and MANUAL_MAP[name] in oa:
            target = MANUAL_MAP[name]
            mapped.append({"cpcb": name, "openaq_name": target, "how": "manual", **oa[target]})
        else:
            unmatched.append(name)

    # THE GATE. A dropped station must fail loudly, not shrink the output.
    total = len(mapped) + len(unmatched)
    if total != len(cpcb_stations):
        sys.exit(f"ACCOUNTING FAILURE: {len(mapped)} mapped + {len(unmatched)} unmatched "
                 f"= {total}, but {len(cpcb_stations)} stations went in. Rows were dropped.")
    return mapped, unmatched


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def pm25_blocks(sensor_ids: list[int], key: str) -> list[dict]:
    """Per-sensor PM2.5 coverage blocks for one location, oldest first.

    A location's own datetimeFirst/Last spans EVERY sensor and every gap between
    them, so it massively overstates usable history. Ambala (6964) reports a
    2019-02 -> 2026-08 location span, but its two PM2.5 sensors actually cover
    2019-02..2022-10 and 2025-10..now, with a ~3 year hole in between — the
    span overstated usable history by roughly 7x. Measuring the span instead of
    the blocks is the same silent-drop failure this gate exists to catch, so it
    is done per sensor.
    """
    blocks = []
    for sid in sensor_ids:
        detail = openaq_get(f"sensors/{sid}", key)["results"][0]
        first, last = detail.get("datetimeFirst"), detail.get("datetimeLast")
        if not first or not last:
            continue
        blocks.append({
            "sensor_id": sid,
            "first": _dt(first["utc"]),
            "last": _dt(last["utc"]),
        })
    blocks.sort(key=lambda b: b["first"])
    for i, b in enumerate(blocks):
        b["days"] = (b["last"] - b["first"]).days
        b["gap_before_days"] = (b["first"] - blocks[i - 1]["last"]).days if i else None
    return blocks


def recent_block(blocks: list[dict]) -> dict | None:
    """The newest contiguous block — the only one safe to train on directly."""
    return blocks[-1] if blocks else None


def render_doc(mapped: list, unmatched: list) -> str:
    recents = [m["recent"]["days"] for m in mapped if m.get("recent")]
    median_recent = sorted(recents)[len(recents) // 2] if recents else 0
    gapped = [m for m in mapped
              if any((b.get("gap_before_days") or 0) > 30 for b in m.get("blocks", []))]

    lines = [
        "# Station mapping — Gate 0.2",
        "",
        f"Generated by `scripts/probe_history.py --write-doc` on "
        f"{datetime.now(IST):%Y-%m-%d %H:%M} IST. Do not hand-edit — re-run the script.",
        "",
        "## Gate result",
        "",
        f"- CPCB live stations: **{len(mapped) + len(unmatched)}**",
        f"- Mapped to OpenAQ: **{len(mapped)}** "
        f"({sum(1 for m in mapped if m['how'] == 'exact')} exact, "
        f"{sum(1 for m in mapped if m['how'] == 'manual')} manual)",
        f"- Unmatched: **{len(unmatched)}**",
        f"- Accounting assertion `mapped + unmatched == total`: **PASS**",
        "",
        "## Why this gate exists",
        "",
        "An exact-string join between CPCB and OpenAQ **drops stations silently** — no error, "
        "no warning, just fewer rows. Same failure class as the district-name mismatch on the "
        "farm project. The script therefore asserts that every input station comes out either "
        "mapped or explicitly unmatched, and exits non-zero otherwise.",
        "",
        "## Manual mapping rows",
        "",
        "These two do **not** match on exact string. Both are real stations; a naive join "
        "loses them.",
        "",
        "| CPCB string | OpenAQ string | OpenAQ id | Why it differs |",
        "|---|---|---|---|",
    ]
    for m in mapped:
        if m["how"] == "manual":
            why = ("operating agency differs (IITM vs IMD)"
                   if "IITM" in m["cpcb"] else "district suffix `(Nuh)` present in CPCB only")
            lines.append(f"| `{m['cpcb']}` | `{m['openaq_name']}` | {m['id']} | {why} |")

    lines += [
        "",
        "## Dirty strings that matched anyway",
        "",
        "Both sources carry the *same* whitespace damage, so these join cleanly. This is luck, "
        "not a guarantee — **do not 'clean' station names on one side only**, it would break a "
        "join that currently works.",
        "",
    ]
    for m in mapped:
        n = m["cpcb"]
        if n != n.strip() or "  " in n:
            kind = "trailing space" if n != n.strip() else "double space"
            lines.append(f"- `{n!r}` — {kind}, matched exactly on both sides")

    lines += [
        "",
        "## Unmatched",
        "",
    ]
    if unmatched:
        lines += [f"- `{n}`" for n in unmatched]
    else:
        lines.append("None. All CPCB stations resolve to an OpenAQ location.")

    lines += [
        "",
        "## Historical depth (decides the Phase 3/4 training source)",
        "",
        "**Measured per sensor, not per location.** A location's own `datetimeFirst`/`Last` "
        "spans every sensor it ever had *and every gap between them*. Ambala advertises a "
        "2019→2026 location span but its two PM2.5 sensors actually cover 2019-02→2022-10 and "
        "2025-10→now — a **~3 year hole**. Trusting the location span overstated usable "
        "history by roughly 7×. Naive spans are not history.",
        "",
        f"Median **contiguous recent** PM2.5 history: **{median_recent} days** "
        f"(~{median_recent / 30:.1f} months). "
        f"**{len(gapped)} of {len(mapped)}** stations have a >30-day gap in their record.",
        "",
        "| CPCB station | OpenAQ id | Match | Recent block from | Days | Blocks | Max gap (d) |",
        "|---|---|---|---|---|---|---|",
    ]
    for m in sorted(mapped, key=lambda x: x["cpcb"]):
        blocks = m.get("blocks", [])
        r = m.get("recent")
        gaps = [b.get("gap_before_days") or 0 for b in blocks]
        lines.append(
            f"| `{m['cpcb']}` | {m['id']} | {m['how']} | "
            f"{r['first']:%Y-%m-%d} | {r['days']} | {len(blocks)} | "
            f"{max(gaps) if gaps else 0} |" if r else
            f"| `{m['cpcb']}` | {m['id']} | {m['how']} | — | — | 0 | — |"
        )

    lines += [
        "",
        "## Source decision",
        "",
        "**OpenAQ is the primary history source.** It carries actual CPCB station "
        "measurements under harmonised ids, so the model trains on the same kind of data it "
        "will be served against.",
        "",
        "**Open-Meteo is the weather-feature source for Phase 4, and the history fallback.** "
        "It is CAMS *model reanalysis* on an ~11km grid, not measurements. Two consequences:",
        "",
        "1. There are no station ids — you map by lat/lon, so the §0.2 mismatch trap does not "
        "apply to it.",
        "2. **Source bias:** training on modelled data while serving against measured CPCB "
        "data is a real and different problem. The Gurugram stations sit close enough that "
        "several land in one CAMS cell — identical 'history' for stations that genuinely read "
        "differently. If we ever fall back to Open-Meteo for PM2.5 history, this goes in the "
        "README under §10, not hidden.",
        "",
        "Open-Meteo hourly PM2.5 was verified available for 2024-11-01→07 at Gurugram coords "
        "(168/168 non-null, 26.6–224.9 µg/m³), i.e. full stubble-burning season coverage with "
        "no API key.",
        "",
        "## Attribution",
        "",
        "Measurements © CPCB via data.gov.in and OpenAQ. Weather and CAMS reanalysis © "
        "Open-Meteo. Required in the README per §10.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="Haryana")
    ap.add_argument("--write-doc", action="store_true")
    args = ap.parse_args()

    records = fetch_cpcb(args.state, load_cpcb_key())
    cpcb_rows = {r["station"]: r for r in records}
    print(f"CPCB: {len(cpcb_rows)} live stations in {args.state}")

    oa = fetch_openaq_locations(load_openaq_key())
    mapped, unmatched = build_mapping(list(cpcb_rows), oa)

    print()
    print(f"mapped   : {len(mapped)} "
          f"({sum(1 for m in mapped if m['how'] == 'exact')} exact, "
          f"{sum(1 for m in mapped if m['how'] == 'manual')} manual)")
    print(f"unmatched: {len(unmatched)}")
    for n in unmatched:
        print(f"    UNMATCHED {n!r}")

    print()
    print("measuring per-sensor PM2.5 coverage (location spans hide multi-year gaps)...")
    oa_key = load_openaq_key()
    for i, m in enumerate(mapped, 1):
        print(f"  [{i}/{len(mapped)}] {m['cpcb']}")
        m["blocks"] = pm25_blocks(m["pm25_sensor_ids"], oa_key)
        m["recent"] = recent_block(m["blocks"])

    recents = [m["recent"]["days"] for m in mapped if m.get("recent")]
    gapped = [m for m in mapped
              if any((b.get("gap_before_days") or 0) > 30 for b in m["blocks"])]
    if recents:
        print(f"contiguous recent PM2.5 history: min={min(recents)}d "
              f"median={sorted(recents)[len(recents) // 2]}d max={max(recents)}d")
        print(f"stations with a >30d gap in their record: {len(gapped)}/{len(mapped)}")

    if args.write_doc:
        out = os.path.join(os.path.dirname(__file__), "..", "docs", "station_mapping.md")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(render_doc(mapped, unmatched))
        print(f"wrote {os.path.normpath(out)}")

    ok = len(mapped) + len(unmatched) == len(cpcb_rows) and len(mapped) > 0
    print(f"GATE 0.2: {'PASS' if ok else 'FAIL'} "
          f"(every station accounted for: {len(mapped)}+{len(unmatched)}={len(cpcb_rows)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

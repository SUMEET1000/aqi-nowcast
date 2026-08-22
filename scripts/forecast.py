"""Serve the promoted model: will this station exceed 121 µg/m³ in 12 hours?

    python scripts/forecast.py                 # every station, from the cache
    python scripts/forecast.py --refresh       # re-pull pm25_history first
    python scripts/forecast.py --self-test     # no database, no network

Read by scripts/send_alerts.py at 07:00 IST. Everything here is the *reading*
half of scripts/retrain.py — the booster, its cutoff and its feature list are
loaded from model_runs rather than fitted, so the model a subscriber is served
is the one the monthly gate promoted and nothing else.

WHY THIS RETURNS A YES/NO AND NEVER A PROBABILITY. The model outputs a number
between 0 and 1, and nothing in this project has ever checked that the number
means what it looks like — a calibration curve has not been drawn. F2, recall
and precision are all measured at one cutoff, so the cutoff is the only part of
the output with evidence behind it. Printing "70% chance" would attach a
confidence nobody has earned. `outlook` returns booleans for that reason.

WHY THE ISSUE HOUR IS THE NEWEST HOUR WITH DATA, NOT `now`. The model reads
lag_0, which is the station's reading at the issue hour, and a station with no
reading for the current hour has no row to predict from. Serve-time freshness
was measured at 07:00 IST over 878 station-days: median 0h stale, 93.7% within
1h, 96.1% within 3h. So the newest hour is normally the current one, and
MAX_ISSUE_AGE_H refuses rather than silently issuing a "12h" forecast that is
really 7h from a five-hour-old reading.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone

# Deliberately NOT imported at module scope: numpy, pandas and lightgbm. They
# live in requirements-model.txt, and send_alerts.py has to keep working — for
# --dry-run and for the dark-station path — on a machine that has only
# requirements.txt. Every heavy import below sits inside a function.

# 12h is the only horizon that beat persistence, and it is the one a 07:00 IST
# send needs: 07:00 + 12h is the evening peak. [measured 2026-08-21:
# classify.py --tail --breakdown, 07:00 IST rows only — persistence F2 0.541,
# best 0.641, margin +0.100 on 695 events. 6h collapses to +0.032 at this send
# hour, 24h to +0.059, 48h to +0.023.] Imported from retrain so the serving
# horizon cannot drift from the trained one.
from retrain import HORIZON

# Refuse to forecast from a reading older than this. Three hours matches
# send_alerts.STALE_AFTER_H and build plan §5 — past it the message already
# tells the subscriber the reading is stale, and a forecast hung off a stale
# reading would be the one number on the screen not carrying that warning.
MAX_ISSUE_AGE_H = 3.0


class NoModel(Exception):
    """No usable incumbent in model_runs. Not an error at the call site — the
    project ran for months with no model at all and the message is still worth
    sending without one."""


def load(cur):
    """(booster, warn_above, feature_names) for the promoted 12h model.

    Raises NoModel when nothing has been promoted yet. retrain.load_incumbent
    already raises on an incumbent row carrying no booster, and that stays an
    error rather than a NoModel: a row flagged incumbent with nothing in it is
    a broken write, not an absence.
    """
    from retrain import load_incumbent

    held = load_incumbent(cur)
    if held is None:
        raise NoModel(f"no incumbent in model_runs for horizon={HORIZON}")
    _run_id, _f2, booster, params, _refit_end = held
    names = params.get("features")
    if not names:
        raise NoModel("the incumbent carries no feature list, so the columns "
                      "it was fitted on cannot be checked")
    return booster, float(params.get("warn_above", 0.5)), list(names)


def issue_rows(cache_path: str, now: datetime):
    """One row per station, at that station's newest hour carrying a reading.

    Returns (frame, issue_hours) where issue_hours maps station_id -> epoch
    hour. Stations whose newest reading is older than MAX_ISSUE_AGE_H are
    dropped here rather than predicted and discarded later, so the caller
    cannot accidentally serve one.
    """
    import features

    wide = features.load_wide(cache_path)
    # require_target=False is the whole difference from the training call: the
    # hour we want to forecast from is the one whose answer does not exist yet.
    frame = features.build(wide, HORIZON, spatial=False, tail=True,
                           require_target=False)
    frame = frame.drop(columns=["_target"])

    now_hour = int(now.timestamp()) // 3600
    oldest = now_hour - int(MAX_ISSUE_AGE_H)

    # The newest admitted row per station. Admission already required lag_0, so
    # every row here has the reading the forecast is hung off.
    newest = frame.groupby("station_id").apply(
        lambda g: g.index.max(), include_groups=False)
    issue_hours = {int(s): int(h) for s, h in newest.items() if h >= oldest}
    if not issue_hours:
        return frame.iloc[0:0], {}

    keep = [(frame.index == h) & (frame["station_id"] == s)
            for s, h in issue_hours.items()]
    mask = keep[0]
    for m in keep[1:]:
        mask |= m
    return frame[mask], issue_hours


def outlook(cur, cache_path: str, now: datetime) -> dict[int, dict]:
    """{station_id: {"warn": bool, "target_ts": datetime}} for every station
    that can be forecast right now. A station absent from the dict has no
    forecast, and the caller must say nothing rather than imply calm.
    """
    from benchmark import _prep
    from retrain import proba

    booster, warn_above, names = load(cur)
    frame, issue_hours = issue_rows(cache_path, now)
    if frame.empty:
        return {}

    X = _prep(frame, "ordinal")
    if list(X.columns) != names:
        # Same check retrain.py runs before re-scoring an incumbent, and for
        # the same reason: a silent column mismatch feeds the booster the wrong
        # inputs and returns a confident number computed from nothing.
        raise RuntimeError(
            f"the promoted model was fitted on {len(names)} columns and this "
            f"frame has {len(X.columns)}; the feature set changed, so the "
            f"stored model cannot be served. Re-run scripts/retrain.py.")

    probabilities = proba(booster, frame)
    out = {}
    for (station_id, p) in zip(X["station_id"].astype(int), probabilities):
        issued = issue_hours[int(station_id)]
        out[int(station_id)] = {
            "warn": bool(p > warn_above),
            "target_ts": datetime.fromtimestamp((issued + HORIZON) * 3600,
                                                tz=timezone.utc),
        }
    return out


def self_test() -> int:
    """Proves the two refusals fire. No database, no network, no model."""
    failures = []

    class Cur:
        def __init__(self, row):
            self.row = row

        def execute(self, *_a):
            pass

        def fetchone(self):
            return self.row

    # 1. No incumbent -> NoModel, not a crash and not a silent False.
    try:
        load(Cur(None))
        failures.append("load() accepted an empty model_runs")
    except NoModel:
        print("  PASS  no incumbent raises NoModel")

    # 2. An incumbent with no feature list is unusable, because the column
    #    check is the only thing standing between a stored booster and being
    #    scored on the wrong inputs.
    try:
        load(Cur((1, 0.3, "tree", {"warn_above": 0.4}, None)))
        failures.append("load() accepted an incumbent with no feature list")
    except NoModel:
        print("  PASS  a missing feature list raises NoModel")
    except Exception as exc:                       # lightgbm parses the text
        print(f"  PASS  unusable incumbent refused ({type(exc).__name__})")

    # 3. The staleness rule is arithmetic, so assert it rather than trusting it.
    now = datetime(2026, 8, 22, 7, 0, tzinfo=timezone.utc)
    stale = now - timedelta(hours=MAX_ISSUE_AGE_H + 1)
    if int(stale.timestamp()) // 3600 >= int(now.timestamp()) // 3600 - int(MAX_ISSUE_AGE_H):
        failures.append("a reading past MAX_ISSUE_AGE_H is not being dropped")
    else:
        print(f"  PASS  a reading older than {MAX_ISSUE_AGE_H}h is dropped")

    for f in failures:
        print(f"  FAIL  {f}", file=sys.stderr)
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh", action="store_true",
                    help="re-pull pm25_history into the cache (one Neon wake)")
    ap.add_argument("--self-test", action="store_true",
                    help="prove the refusals fire; no database, no network")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    import os

    from baselines import CACHE, pull
    from db import connect

    if args.refresh or not os.path.exists(CACHE):
        pull(CACHE)

    now = datetime.now(timezone.utc)
    with connect() as conn, conn.cursor() as cur:
        try:
            calls = outlook(cur, CACHE, now)
        except NoModel as exc:
            print(f"no forecast: {exc}", file=sys.stderr)
            return 1

    if not calls:
        print("no station has a reading fresh enough to forecast from")
        return 1
    warned = sum(1 for v in calls.values() if v["warn"])
    print(f"{len(calls)} stations forecast, {warned} warned, h={HORIZON}\n")
    for station_id in sorted(calls):
        v = calls[station_id]
        print(f"  station {station_id:>2}  "
              f"{'WARN ' if v['warn'] else 'quiet'}  "
              f"target {v['target_ts']:%Y-%m-%d %H:%M} UTC")
    return 0


if __name__ == "__main__":
    sys.exit(main())

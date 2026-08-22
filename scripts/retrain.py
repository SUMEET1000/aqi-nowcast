"""Phase 5 — monthly retrain with a promotion gate.

    python scripts/retrain.py --self-test      # no database, no network
    python scripts/retrain.py --dry-run        # fit, score, print, write nothing
    python scripts/retrain.py --refresh        # re-pull pm25_history first
    python scripts/retrain.py --force-promote  # seed the first incumbent
    python scripts/retrain.py                  # what retrain.yml runs, monthly

Build plan §8 asks for a scheduled retrain where a new model is promoted ONLY
if it beats the incumbent on a held-out window, with both results logged either
way. This is that gate.

WHAT IS COMPARED, AND WHY IT IS NOT LAST MONTH'S STORED NUMBER
--------------------------------------------------------------
The challenger and the incumbent are scored in the SAME run on the SAME test
block. A score stored a month ago was computed on different rows, so comparing
against it would measure which month was easier to forecast. Stricter than §8
asks for, and the only version that means anything.

classify.py's FOLDS cannot be reused here. They are hardcoded dates ending
2026-08-20 (benchmark.py:58), so a monthly job importing them re-scores the
identical rows forever and the gate could never fire. The window below is
derived from the data instead.

THE SCORE DESCRIBES THE RECIPE; THE STORED MODEL IS A REFIT
------------------------------------------------------------
Fitting on `train` alone stops learning at today - 75 days. Promoted on
1 November that model would have been trained on August air — clean — and
served into peak stubble season, which makes the whole exercise theatre.

So the two jobs are split. To JUDGE: fit on train, tune on inner, score on
test, strictly backward. To SHIP: take the winning recipe and refit it on every
row up to today, test included. `model_runs.refit_end` records the difference,
because `f2` is the recipe's score on the holdout and is NOT this booster's
measured score. Do not read it as one.

classify.fit_tuned deliberately does not refit, and that rule is right FOR
SCORING — refitting would change the model that produced the trusted number. It
does not govern the shipped artefact.

OCTOBER IS EXPECTED TO BE THE WORST MONTH AND THAT IS THE DELIVERABLE
----------------------------------------------------------------------
The season shifts and the 1 October run has not seen it; the 1 November run
recovers it. Build plan §9 words the outcome as "automated retrain RECOVERED
24h MAE from C to D" — recovered, not prevented. Measure the curve and write
docs/incidents/, do not engineer it away.

The reverse flip, a smoke-trained model serving clean February air, is handled
by the --tail features carrying the current regime as input (exceed_count_72,
hours_since_exceed, roll_max_24, z_168) and by MIN_TEST_EVENTS below.

THE EXIT CODE IS NOT AN ALERT HERE, WHICH IS THE OPPOSITE OF monitor.py.
A challenger losing is the normal monthly result. monitor.py already owns the
email-on-failure channel for real drift, and a second job emailing every month
for a non-event is what teaches the reader to ignore both. This exits 0 on a
loss and writes the numbers to model_runs, which is queryable whenever wanted.
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import env  # noqa: F401  — import-time UTF-8 console fix
import features
import metrics
from baselines import CACHE, IST
from benchmark import RANDOM_SEED, _git_sha, _prep
from classify import (DEFAULT_THRESHOLD, OBJECTIVE, PROBABILITY_GRID,
                      TUNE_DRAWS, draw_params, fit_persistence, score)
from db import connect, redact
from ingest import annotate, clean_detail, step_summary

# The horizon the product needs and the only one that ever cleared the gate.
# 12h from a 07:00 IST send is the evening peak. 6h/24h/48h all lost to
# persistence `[measured 2026-08-20: classify.py --tail --tune]`, so retraining
# them monthly would be runtime spent on numbers nobody acts on.
HORIZON = 12

# The candidate. Plain lightgbm beat both the routed variant (16 of 16 cells)
# and lightgbm-balanced at 12h `[measured 2026-08-21]`.
CANDIDATE = "lightgbm"

# Days at the end of the history held back to grade on. 30, not 60: this is the
# gap between the newest data and the newest thing the grade can describe, and
# it is the whole reason a monthly retrain can keep up with a season at all.
HOLDOUT_DAYS = 30

# Days before the holdout used to pick hyperparameters and the warning cutoff.
# Same size as benchmark.INNER_VALIDATION_DAYS, imported by value rather than by
# reference because that constant is tied to the fixed FOLDS and this window is
# not — they are free to diverge and a shared name would hide it.
INNER_DAYS = 45

# Everything before `inner` is training data, with NO lower bound, and that is
# deliberate. On 18 months of history a rolling training window would delete the
# only stubble season the model has ever seen. The roster held at 28-30 stations
# from 2025-09-29 `[measured 2026-08-21: monitor.py --backfill, 1,393
# station-week rows over 77 weeks]`, so the 2025 season is covered properly
# rather than being a one-station stub.
#
# The honest limit is n=1 winter. That belongs in the README; no window choice
# creates more of it, and it fixes itself next year.

# How much better the challenger has to be before it takes over.
#
# JUDGEMENT, NOT MEASUREMENT, and it must keep saying so until several months of
# real runs exist. The fold-to-fold F2 std at 12h is 0.122 `[measured
# 2026-08-21]`, but that is spread across four different test blocks and this
# gate compares two models on ONE block, where most of that variance is shared
# and cancels. So 0.122 is an upper bound on the noise here, not the noise here.
# 0.02 says "clear the incumbent visibly"; a tie keeps the incumbent, because
# swapping models on noise is churn with a changelog.
MIN_MARGIN = 0.02

# Below this many exceedance hours in the test block, the gate refuses to reach
# a verdict at all.
#
# A clean-air month cannot tell two spike forecasters apart — with nothing to
# catch, every candidate scores about the same and the winner is whichever way
# two or three hours fell. Letting July promote would throw away a good winter
# model on a coin flip, which is the reverse of the seasonal failure this whole
# script exists to handle.
#
# 100 is judgement of the same kind as MIN_MARGIN. The pooled 12h test set
# carries 13,811 events across ~4 folds of ~2 months `[measured 2026-08-21]`, so
# an ordinary 30-day block holds a few hundred: 100 sits well below a normal
# month and well above a dead one. Replace it with a measured value once a year
# of monthly counts exists.
MIN_TEST_EVENTS = 100

# fetch_log outcomes owned by this script. Deliberately outside
# gate1_check.RUN_OUTCOMES and ANOMALY_OUTCOMES, same rule as monitor.py's and
# send_alerts.py's: this run is observable in the same table without moving the
# ingester's success rate, which Gate 1 is computed from. Do not add them to
# either tuple.
OUTCOME_OK = "retrain_ok"
OUTCOME_PROMOTED = "retrain_promoted"
OUTCOME_CRASH = "retrain_crash"

LOG = """
INSERT INTO fetch_log (station_id, outcome, rows_returned, bulletin_ts, error_detail)
VALUES (%s, %s, %s, %s, %s)
"""

INCUMBENT = """
SELECT run_id, f2, booster, params, refit_end
FROM model_runs
WHERE horizon = %s AND is_incumbent
"""

INSERT = """
INSERT INTO model_runs (
    horizon, candidate, train_end, test_start, test_end, refit_end,
    n_test, n_events, f2, recall, prec, ap, ets, warn_above,
    params, incumbent_f2, persistence_f2, promoted, abstained, is_incumbent,
    booster, git_sha, data_sha256)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, %s, %s, %s)
RETURNING run_id
"""

# Cleared BEFORE the new row is inserted, so the partial unique index on
# (horizon) WHERE is_incumbent never sees two at once inside the transaction.
DEMOTE = "UPDATE model_runs SET is_incumbent = FALSE WHERE horizon = %s AND is_incumbent"


def ok(msg: str) -> None:
    print(f"  PASS  {msg}")


def note(msg: str) -> None:
    print(f"  ..    {msg}")


# ------------------------------------------------------------------ windowing

def windows(frame: pd.DataFrame, horizon: int
            ) -> tuple[pd.Series, pd.Series, pd.Series, dict]:
    """(train, inner, test) masks over `frame`, derived from the data itself.

    Sliced on TARGET time, not issue time, for the reason benchmark.fold_masks
    gives: a training row whose label lands after the boundary is learning from
    the future even when its features are clean. The label is data too.

    Boundaries come from the newest target in the frame rather than from
    `now()`, so a stalled pm25_history produces a window that is visibly old in
    the printed dates instead of an empty test block that reads as a bug.
    """
    target = pd.Series(frame.index.to_numpy() + horizon, index=frame.index)
    newest = int(target.max())
    test_from = newest - HOLDOUT_DAYS * 24
    inner_from = test_from - INNER_DAYS * 24
    return (target <= inner_from,
            (target > inner_from) & (target <= test_from),
            target > test_from,
            {"train_end": _date(inner_from), "test_start": _date(test_from + 1),
             "test_end": _date(newest), "refit_end": _date(newest)})


def _date(epoch_hour: int) -> str:
    """Epoch hour -> ISO date in IST. The pollution day is IST, so the recorded
    boundary should read as the day a person would call it."""
    return datetime.fromtimestamp(epoch_hour * 3600, tz=IST).date().isoformat()


# ------------------------------------------------------------------- fitting

def fit(X: pd.DataFrame, y: np.ndarray, params: dict):
    """One LightGBM classifier. Returns the fitted model, not its predictions.

    classify._lgbm returns probabilities and drops the estimator on the floor,
    which is right for a CV table and useless here — the whole point of this
    script is that something survives the run.
    """
    import lightgbm as lgb

    model = lgb.LGBMClassifier(random_state=RANDOM_SEED, n_jobs=-1, verbose=-1,
                               **params)
    model.fit(_prep(X, "ordinal"), y)
    return model


def proba(model, X: pd.DataFrame) -> np.ndarray:
    """P(exceedance) from either a fitted LGBMClassifier or a loaded Booster."""
    prepped = _prep(X, "ordinal")
    if hasattr(model, "predict_proba"):
        return model.predict_proba(prepped)[:, 1]
    # A raw Booster restored from text. For a binary objective predict() already
    # returns the positive-class probability, so there is no [:, 1] to take.
    return np.asarray(model.predict(prepped), dtype=float)


def search(Xtr, ytr, Xin, yin, threshold: float, draws: int, rng
           ) -> tuple[dict, float, float]:
    """Pick hyperparameters and the warning cutoff, both on inner validation.

    Same discipline as classify.fit_tuned and ranked by the same objective the
    gate reads, so the search cannot optimise one quantity while the verdict
    grades another. Unlike fit_tuned this refits per draw rather than predicting
    inner and test in one call — the test block must not be visible while the
    recipe is being chosen, because the recipe is what gets refit on everything.
    """
    best = None
    grid = [{}] + [draw_params(rng, CANDIDATE) for _ in range(max(0, draws))]
    for i, params in enumerate(grid):
        model = fit(Xtr, ytr, params)
        p = proba(model, Xin)
        warn, inner_f2 = metrics.best_threshold(
            list(zip(yin.tolist(), p.tolist())), threshold,
            grid=PROBABILITY_GRID, default=0.5, objective=OBJECTIVE)
        print(f"    draw {i:>2}/{len(grid) - 1}  p>{warn:.2f}  "
              f"inner F2 {inner_f2:.3f}", flush=True)
        if best is None or inner_f2 > best[2]:
            best = (params, warn, inner_f2)
    return best


# -------------------------------------------------------------------- verdict

def decide(challenger_f2: float, incumbent_f2: float | None,
           n_events: int, force: bool, contaminated: bool = False
           ) -> tuple[str, str]:
    """The gate. Pure, so --self-test can prove it fails in every direction.

    Order matters. The abstain check comes FIRST, so a huge apparent margin on a
    quiet month still cannot promote; checking the margin first would make
    MIN_TEST_EVENTS decorative. The contamination check comes before the margin
    for the same reason.
    """
    if n_events < MIN_TEST_EVENTS:
        return "abstain", (f"only {n_events} event(s) in the test block, "
                           f"below MIN_TEST_EVENTS={MIN_TEST_EVENTS} — this "
                           f"month cannot separate two forecasters")
    if incumbent_f2 is None:
        if force:
            return "seed", "no incumbent; --force-promote given, seeding it"
        return "no-incumbent", ("no incumbent stored for this horizon; re-run "
                                "with --force-promote to seed the first one")
    if contaminated:
        return "contaminated", (
            "the incumbent was refit on data that reaches into this test "
            "block, so it is being graded on hours it memorised and cannot "
            "lose. Nothing decided. Wait until the holdout has moved past the "
            "incumbent's refit_end — on the monthly schedule that is automatic")
    margin = challenger_f2 - incumbent_f2
    if margin > MIN_MARGIN:
        return "promote", (f"F2 {challenger_f2:.3f} vs incumbent "
                           f"{incumbent_f2:.3f}, margin +{margin:.3f} clears "
                           f"MIN_MARGIN={MIN_MARGIN}")
    return "keep", (f"F2 {challenger_f2:.3f} vs incumbent {incumbent_f2:.3f}, "
                    f"margin {margin:+.3f} does not clear "
                    f"MIN_MARGIN={MIN_MARGIN} — incumbent stays")


def self_test() -> int:
    """Prove the gate can reach every verdict. No database, no network.

    This repo has a scar: five separate checks could not fail and all five
    reported PASS, every one of them written as evidence. The test for a gate is
    what input makes it refuse — if the answer needs a code change, it is not a
    gate.
    """
    print("Self-test — can this gate refuse?\n")
    bad = 0
    plenty = MIN_TEST_EVENTS + 1

    def case(name: str, got, want) -> None:
        nonlocal bad
        if got == want:
            print(f"  PASS  {name}: {got}")
        else:
            print(f"  FAIL  {name}: got {got}, want {want}")
            bad += 1

    case("a clear win promotes",
         decide(0.70, 0.50, plenty, False)[0], "promote")
    case("a clear loss keeps the incumbent",
         decide(0.30, 0.50, plenty, False)[0], "keep")

    # The one that stops churn. Half of MIN_MARGIN is an improvement on paper
    # and noise in fact, and swapping on it means the served model changes every
    # month for no measured reason.
    case("an improvement inside MIN_MARGIN keeps the incumbent",
         decide(0.50 + MIN_MARGIN / 2, 0.50, plenty, False)[0], "keep")
    case("just under MIN_MARGIN keeps the incumbent",
         decide(0.50 + MIN_MARGIN * 0.99, 0.50, plenty, False)[0], "keep")
    case("comfortably over MIN_MARGIN promotes",
         decide(0.50 + MIN_MARGIN * 1.01, 0.50, plenty, False)[0], "promote")
    # Nothing asserts the behaviour AT exactly MIN_MARGIN, on purpose. Binary
    # floats cannot represent 0.02, so 0.52 - 0.50 is 0.020000000000000018 and
    # the comparison lands on whichever side the rounding fell. That is a
    # property of the CPU, not of this gate, and pinning it in a test would make
    # the suite fail on a different machine for no reason anyone could act on.
    # A margin that close is a tie in every sense that matters.

    # THE ONE THAT MATTERS IN JULY. A quiet month must not promote even when the
    # margin looks enormous, because with almost no events the margin is decided
    # by two or three hours. Ordering this after the margin check would make
    # MIN_TEST_EVENTS decorative, and the failure would only show up in summer.
    case("a quiet month abstains despite a huge margin",
         decide(0.99, 0.10, MIN_TEST_EVENTS - 1, False)[0], "abstain")
    case("...and --force-promote does not override the abstain",
         decide(0.99, 0.10, MIN_TEST_EVENTS - 1, True)[0], "abstain")

    case("no incumbent refuses by default",
         decide(0.70, None, plenty, False)[0], "no-incumbent")
    case("no incumbent seeds under --force-promote",
         decide(0.70, None, plenty, True)[0], "seed")

    # A promotion must be an improvement, never merely a fresher model. Without
    # this, "retrain monthly" quietly becomes "replace monthly".
    case("a worse challenger cannot promote under --force-promote either",
         decide(0.10, 0.90, plenty, True)[0], "keep")

    # THE HAZARD OF THE REFIT, AND THE ONLY PLACE IT BITES. The stored incumbent
    # was refit on everything up to its refit_end, test block included. Run this
    # script twice inside one month and the incumbent is graded on hours it
    # memorised, so it wins whatever the challenger does — a gate that cannot
    # dethrone anyone while still printing a verdict. On the monthly schedule
    # the holdout has moved clear by then (HOLDOUT_DAYS = 30 = the interval), so
    # this fires only on an off-schedule re-run, which is exactly when a person
    # is watching and would believe the number.
    case("an incumbent refit into this test block cannot be compared",
         decide(0.99, 0.10, plenty, False, contaminated=True)[0], "contaminated")
    case("...and --force-promote does not override that either",
         decide(0.99, 0.10, plenty, True, contaminated=True)[0], "contaminated")

    # windows() is the other half that can silently be wrong: an off-by-one in
    # the boundaries produces overlapping blocks, which leaks test rows into
    # training and inflates every number after it.
    frame = pd.DataFrame({"_target": 1.0},
                         index=pd.Index(range(500_000, 500_000 + 200 * 24),
                                        name="issue_hour"))
    train, inner, test, dates = windows(frame, HORIZON)
    case("the three blocks partition the frame",
         int((train | inner | test).sum()), len(frame))
    case("the three blocks do not overlap",
         int((train & inner).sum() + (inner & test).sum() + (train & test).sum()), 0)
    case("the holdout is HOLDOUT_DAYS long",
         int(test.sum()), HOLDOUT_DAYS * 24)
    case("the inner window is INNER_DAYS long",
         int(inner.sum()), INNER_DAYS * 24)
    # refit_end must reach the END of the data, not the end of training. If these
    # were equal the shipped model would be ~2.5 months stale, which is the exact
    # failure the refit exists to prevent and is invisible in every score.
    case("refit_end is later than train_end",
         dates["refit_end"] > dates["train_end"], True)
    case("refit_end is the end of the test block",
         dates["refit_end"], dates["test_end"])

    print()
    if bad:
        print(f"SELF-TEST: FAIL ({bad} case(s))")
        return 1
    print("SELF-TEST: PASS — the gate refuses on a tie, on a quiet month and "
          "on a missing incumbent, and the windows partition cleanly.")
    return 0


# ----------------------------------------------------------------------- main

def data_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_incumbent(cur):
    """(run_id, f2, Booster, params, refit_end) for the incumbent, or None.

    The feature list is checked against the stored one before the booster is
    used. A silent column mismatch would score the incumbent on a model reading
    the wrong inputs, which loses the comparison for the wrong reason and reads
    as a genuine improvement by the challenger.
    """
    import lightgbm as lgb

    cur.execute(INCUMBENT, (HORIZON,))
    row = cur.fetchone()
    if row is None:
        return None
    run_id, f2, text, params, refit_end = row
    if not text:
        raise RuntimeError(f"model_runs run_id={run_id} is the incumbent but "
                           f"carries no booster; the row is unusable")
    return (run_id, float(f2), lgb.Booster(model_str=text), (params or {}),
            refit_end)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh", action="store_true",
                    help="re-pull pm25_history into the cache first (one Neon wake)")
    ap.add_argument("--dry-run", action="store_true",
                    help="fit, score and print; write nothing; exit 0")
    ap.add_argument("--self-test", action="store_true",
                    help="prove the gate can refuse; no database, no network")
    ap.add_argument("--force-promote", action="store_true",
                    help="seed the first incumbent when none exists; it does "
                         "NOT override the abstain or a losing score")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help=f"exceedance threshold in µg/m³ (default {DEFAULT_THRESHOLD})")
    ap.add_argument("--draws", type=int, default=TUNE_DRAWS,
                    help=f"hyperparameter draws (default {TUNE_DRAWS}); 0 uses "
                         f"library defaults only")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    now = datetime.now(timezone.utc)
    print(f"retrain — {now:%Y-%m-%d %H:%M} UTC  h={HORIZON}  "
          f"threshold {args.threshold:.0f} µg/m³"
          f"{'  (dry run, nothing written)' if args.dry_run else ''}\n")

    if args.refresh or not os.path.exists(CACHE):
        from baselines import pull
        pull(CACHE)

    print("1. Features")
    wide = features.load_wide(CACHE)
    frame = features.build(wide, HORIZON, spatial=False, tail=True)
    X, y = features.split_xy(frame)
    train, inner, test, dates = windows(frame, HORIZON)
    ok(f"{len(frame):,} rows, {X.shape[1]} columns")
    note(f"train  <= {dates['train_end']}  ({int(train.sum()):,} rows)")
    note(f"inner  -> {dates['test_start']}  ({int(inner.sum()):,} rows)")
    note(f"test   -> {dates['test_end']}  ({int(test.sum()):,} rows)")
    note(f"refit  -> {dates['refit_end']}  (all {len(frame):,} rows)")

    if not (train.any() and inner.any() and test.any()):
        raise RuntimeError("one of train/inner/test is empty — pm25_history is "
                           "too short or has stopped advancing")

    Xtr, ytr = X[train], (y[train] > args.threshold).astype(int).to_numpy()
    Xin, yin = X[inner], y[inner]
    Xte, yte = X[test], y[test].to_numpy()
    n_events = int((yte > args.threshold).sum())

    print("\n2. Challenger")
    rng = np.random.default_rng(RANDOM_SEED)
    params, warn, inner_f2 = search(Xtr, ytr, Xin, yin, args.threshold,
                                    args.draws, rng)
    challenger = fit(Xtr, ytr, params)
    row = score(yte, proba(challenger, Xte), args.threshold, warn)
    ok(f"F2 {row['f2']:.3f}  recall {row['recall']:.2f}  "
       f"prec {row['precision']:.2f}  AP {row['ap']:.3f}  p>{warn:.2f}")
    note(f"{n_events:,} event(s) in {len(Xte):,} test rows "
         f"({row['base_rate']:.1%})")

    print("\n3. Incumbent and floor")
    # Persistence is scored on the same rows every run. It is not part of the
    # gate, but a month where BOTH models fall under it is the signal that the
    # whole modelling approach has stopped working, and that is invisible unless
    # the floor is recorded beside them.
    # Its cutoff is tuned on inner exactly as the model's is. A baseline pinned
    # to one operating point while the rival slides its own loses by
    # construction under a recall-weighted objective — the rigged-baseline bug
    # classify.fit_persistence's docstring records.
    pers_warn, _ = metrics.best_threshold(
        list(zip(yin.tolist(),
                 fit_persistence(None, None, Xin, args.threshold).tolist())),
        args.threshold, grid=PROBABILITY_GRID, default=0.5, objective=OBJECTIVE)
    pers = score(yte, fit_persistence(None, None, Xte, args.threshold),
                 args.threshold, pers_warn)
    note(f"persistence F2 {pers['f2']:.3f}")

    # --dry-run still CONNECTS and reads, and only the writes are skipped, same
    # as monitor.py --dry-run. A dry run that cannot see the incumbent cannot
    # predict the verdict, which is the one question it is asked.
    conn = connect()
    try:
        with conn.cursor() as cur:
            held = load_incumbent(cur)
        contaminated = False
        if held is None:
            note("no incumbent stored for this horizon")
            incumbent_f2 = None
        else:
            run_id, stored_f2, booster, stored_params, stored_refit = held
            want = list(_prep(Xte, "ordinal").columns)
            have = stored_params.get("features")
            if have and have != want:
                raise RuntimeError(
                    f"incumbent run_id={run_id} was fitted on {len(have)} "
                    f"columns and this frame has {len(want)}; the feature set "
                    f"changed, so the two are not comparable. Seed a new "
                    f"incumbent with --force-promote after reviewing why.")
            inc = score(yte, proba(booster, Xte), args.threshold,
                        float(stored_params.get("warn_above", 0.5)))
            incumbent_f2 = inc["f2"]
            contaminated = (stored_refit is not None
                            and stored_refit.isoformat() >= dates["test_start"])
            ok(f"incumbent run_id={run_id} re-scored on THIS test block: "
               f"F2 {incumbent_f2:.3f}  (stored score was {stored_f2:.3f}, on "
               f"different rows — not comparable)")
            if contaminated:
                note(f"incumbent refit_end {stored_refit} reaches into the test "
                     f"block starting {dates['test_start']}")

        print("\n4. Verdict")
        outcome, why = decide(row["f2"], incumbent_f2, n_events,
                              args.force_promote, contaminated)
        promoting = outcome in ("promote", "seed")
        print(f"  {outcome.upper()}: {why}")

        booster_text = None
        if promoting:
            print("\n5. Refit on all data")
            # The shipped model, not the judged one. See the module docstring:
            # without this the promoted model stops learning 75 days before
            # today, which is invisible in every score above.
            all_y = (y > args.threshold).astype(int).to_numpy()
            shipped = fit(X, all_y, params)
            # model_to_string(), not save_model(): save_model writes to a FILE
            # and takes a filename. The text is identical either way; only one
            # of them hands it back in memory, which is what a TEXT column needs.
            booster_text = shipped.booster_.model_to_string()
            ok(f"refit on {len(X):,} rows to {dates['refit_end']}, "
               f"{len(booster_text):,} chars of booster text")
        else:
            note("nothing refit — the incumbent is unchanged")

        record = dict(params)
        record["warn_above"] = warn
        record["inner_f2"] = inner_f2
        record["features"] = list(_prep(Xte, "ordinal").columns)
        record["threshold"] = args.threshold

        summary = (f"retrain h={HORIZON}: {outcome} — challenger F2 "
                   f"{row['f2']:.3f}, incumbent "
                   f"{'none' if incumbent_f2 is None else f'{incumbent_f2:.3f}'}"
                   f", persistence {pers['f2']:.3f}, {n_events} event(s)")
        print(f"\n{summary}")

        if args.dry_run:
            return 0

        with conn.cursor() as cur:
            if promoting:
                cur.execute(DEMOTE, (HORIZON,))
            cur.execute(INSERT, (
                HORIZON, CANDIDATE, dates["train_end"], dates["test_start"],
                dates["test_end"], dates["refit_end"] if promoting else None,
                len(Xte), n_events, row["f2"], row["recall"], row["precision"],
                row["ap"], row["ets"], warn, json.dumps(record), incumbent_f2,
                pers["f2"], promoting,
                # abstained means "no verdict reached", which covers both
                # a month too quiet to judge and an incumbent contaminated
                # by its own refit. Neither is evidence about the model, and
                # a query counting losses must not pick them up as losses.
                outcome in ("abstain", "contaminated"), promoting,
                booster_text, _git_sha(), data_sha256(CACHE)))
            new_id = cur.fetchone()[0]
            cur.execute(LOG, (
                None, OUTCOME_PROMOTED if promoting else OUTCOME_OK,
                len(Xte), None,
                clean_detail(f"run={os.environ.get('GITHUB_RUN_ID', 'local')} "
                             f"model_runs={new_id} {summary}")))
        conn.commit()
        ok(f"model_runs run_id={new_id} written")

        annotate("notice", summary)
        step_summary(f"{summary}\n\n- {why}\n- model_runs run_id={new_id}")

        # Exit 0 whatever the verdict. See the module docstring: a losing
        # challenger is the expected monthly result, and monitor.py owns the
        # email channel that a second monthly red run would devalue.
        return 0
    finally:
        conn.close()


def run() -> int:
    """main(), plus a last-resort record of anything it failed to catch.

    Same argument as ingest.run(), send_alerts.run() and monitor.run(): a run
    that dies without a fetch_log row is invisible, and this one fires once a
    month, so a silent death goes unnoticed until the next season.
    """
    try:
        return main()
    except BaseException as e:
        detail = redact(f"{type(e).__name__}: {e}")
        annotate("error", f"CRASH: {detail}")
        step_summary(f"CRASH: {detail}")
        try:
            conn = connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(LOG, (
                        None, OUTCOME_CRASH, None, None,
                        clean_detail(
                            f"run={os.environ.get('GITHUB_RUN_ID', 'local')} "
                            f"{detail}")))
                conn.commit()
                print(f"  recorded as fetch_log outcome='{OUTCOME_CRASH}'",
                      file=sys.stderr)
            finally:
                conn.close()
        except BaseException as log_error:
            print(f"  could not record the crash either: "
                  f"{redact(str(log_error))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())

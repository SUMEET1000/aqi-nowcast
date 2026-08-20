"""Phase 4 stage 3 — predict the exceedance directly, instead of thresholding a mean.

    python scripts/classify.py                    # the table, at the Very Poor band
    python scripts/classify.py --threshold 250    # at CPCB's Severe band
    python scripts/classify.py --only persistence
    python scripts/classify.py --self-test        # proves the scoring discriminates

Reads only the gitignored data/pm25_history.csv. No Neon wake.

WHY THIS EXISTS
---------------
Stage 1 benchmarked eight candidates and every one of them was a REGRESSOR. The
alert was then produced by thresholding the predicted concentration. That is the
wrong shape of model for the question, and the table showed it: the better a
candidate's MAE, the worse its recall, because squared error is minimised by
answering near the conditional mean and never calling a spike.

metrics.exceedance's docstring asserted the opposite — that a classifier "would
mean class weighting and resampling this project does not need". Nobody had
tested it. This script is that test.

TWO THINGS CHANGE HERE, AND THE SECOND ONE IS NOT A MODELLING DECISION
----------------------------------------------------------------------
1. The model predicts P(truth > threshold) directly, so class weighting is
   available and the loss is about the decision rather than about the level.

2. The default threshold is 121 ug/m3, CPCB's Very Poor band, NOT the 250
   Severe band stage 1 used. At 250 the positive rate is 2.0%; at 121 it is
   13.4%, seven times more events to learn from
   `[measured 2026-08-20: 165,608 target hours at h=24]`.

   That is a PRODUCT decision as much as a statistical one. profiles.threshold_pm25
   has existed since Phase 2 and has never been read by anything. A parent
   deciding about a child with asthma does not wait for CPCB to say Severe. 250
   was CPCB's number for describing air; 121 is closer to the number a subscriber
   would set for themselves.

   The comparison stays honest because persistence is scored at the same
   threshold on the same rows. An easier problem does not flatter one side.

WHAT IS UNCHANGED, DELIBERATELY
-------------------------------
The folds, the row-admission rule, the seed, and the discipline that any cutoff
is fitted on inner validation and applied to test. Those come from benchmark.py
by import rather than by copy, so the two tables cannot drift apart.
"""

import argparse
import sys

import numpy as np
import pandas as pd

import env  # noqa: F401  — import-time UTF-8 console fix
import features
import metrics
from baselines import HORIZONS
from benchmark import FOLDS, RANDOM_SEED, _md, _prep, fold_masks

# CPCB's Very Poor band starts here. See the module docstring for why this is
# not 250, and why that is a product decision rather than a convenience.
DEFAULT_THRESHOLD = 121.0

# Candidate warning cutoffs on the PREDICTED PROBABILITY, searched on inner
# validation. A classifier trained on a 13% positive rate rarely puts 0.5 on a
# true event, so the textbook 0.5 is one candidate here and not the default.
PROBABILITY_GRID = [round(0.01 * i, 2) for i in range(1, 100)]


def fit_persistence(Xtr, ytr, Xte, threshold, **_):
    """The bar. Warn when the reading AT ISSUE TIME is already above the line.

    Returned as a probability of exactly 0 or 1 so it goes through the identical
    scoring path as everything else. It cannot be improved by cutoff tuning,
    which is the point: it is the honest thing a person does without a model.
    """
    return (Xte["lag_0"] > threshold).to_numpy(dtype=float)


def fit_logistic(Xtr, ytr, Xte, **_):
    """The cheap sanity check. If this ties the trees, the trees are unjustified."""
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    tr, te = _prep(Xtr, "onehot"), _prep(Xte, "onehot")
    te = te.reindex(columns=tr.columns, fill_value=0.0)
    # Scaling matters here in a way it did not for Ridge: LogisticRegression's
    # lbfgs solver will not converge on features spanning 0-1650 without it.
    pipe = Pipeline([
        ("impute", SimpleImputer(strategy="median", add_indicator=True)),
        ("scale", StandardScaler()),
        ("model", LogisticRegression(max_iter=2000, class_weight="balanced",
                                     random_state=RANDOM_SEED)),
    ]).fit(tr, ytr)
    return pipe.predict_proba(te)[:, 1]


def _lgbm(Xtr, ytr, Xte, balanced: bool, **kw):
    import lightgbm as lgb

    tr, te = _prep(Xtr, "ordinal"), _prep(Xte, "ordinal")
    if balanced:
        # neg/pos, the standard correction. Not tuned — a tuned candidate against
        # untuned baselines is a rigged table, the same rule stage 1 ran under.
        pos = float((ytr == 1).sum())
        kw["scale_pos_weight"] = float((ytr == 0).sum()) / pos if pos else 1.0
    model = lgb.LGBMClassifier(random_state=RANDOM_SEED, n_jobs=-1, verbose=-1, **kw)
    model.fit(tr, ytr)
    return model.predict_proba(te)[:, 1]


def fit_lgbm(Xtr, ytr, Xte, **kw):
    return _lgbm(Xtr, ytr, Xte, balanced=False, **kw)


def fit_lgbm_balanced(Xtr, ytr, Xte, **kw):
    """Same model, positive class weighted up by the imbalance ratio.

    Stage 1's regression equivalent (SEVERE_WEIGHT = 10.0) helped and did not
    overtake persistence. Here the weighting acts on a loss that is already about
    the decision, so it has something to move.
    """
    return _lgbm(Xtr, ytr, Xte, balanced=True, **kw)


def fit_xgboost(Xtr, ytr, Xte, **_):
    import xgboost as xgb

    tr, te = _prep(Xtr, "ordinal"), _prep(Xte, "ordinal")
    pos = float((ytr == 1).sum())
    model = xgb.XGBClassifier(
        random_state=RANDOM_SEED, n_jobs=-1, tree_method="hist",
        scale_pos_weight=float((ytr == 0).sum()) / pos if pos else 1.0)
    model.fit(tr, ytr)
    return model.predict_proba(te)[:, 1]


CANDIDATES = {
    "persistence": fit_persistence,
    "logistic": fit_logistic,
    "lightgbm": fit_lgbm,
    "lightgbm-balanced": fit_lgbm_balanced,
    "xgboost": fit_xgboost,
}


def score(truth: np.ndarray, proba: np.ndarray, threshold: float,
          warn_above: float) -> dict:
    """Confusion counts and rates for one (candidate, horizon, fold).

    `truth` stays in ug/m3 rather than arriving pre-binarised, so this reuses
    metrics.exceedance_at unchanged — the same function that scored the stage 1
    table. One scoring path for both stages means the two tables are comparable.
    """
    pairs = list(zip(truth.tolist(), proba.tolist()))
    out = metrics.exceedance_at(pairs, threshold, warn_above)
    out["warn_above"] = warn_above
    # Average precision is the threshold-free companion. It answers "is the
    # RANKING any good" separately from "is this cutoff any good", so a candidate
    # cannot look bad purely because its cutoff landed badly.
    from sklearn.metrics import average_precision_score

    actual = (truth > threshold).astype(int)
    out["ap"] = (float(average_precision_score(actual, proba))
                 if actual.min() != actual.max() else float("nan"))
    out["base_rate"] = float(actual.mean())
    return out


def run(frames: dict, chosen: list[str], threshold: float) -> pd.DataFrame:
    rows = []
    for horizon in HORIZONS:
        X, y = features.split_xy(frames[horizon])
        for fold_i, fold in enumerate(FOLDS, start=1):
            train, inner, test = fold_masks(frames[horizon], horizon, fold)
            if not train.any() or not inner.any() or not test.any():
                print(f"  fold {fold_i} h={horizon}: EMPTY — skipped", file=sys.stderr)
                continue
            Xtr, ytr = X[train], (y[train] > threshold).astype(int)
            Xin, yin = X[inner], y[inner]
            Xte, yte = X[test], y[test]
            both = pd.concat([Xin, Xte])
            for name in chosen:
                proba = np.asarray(CANDIDATES[name](
                    Xtr, ytr, both, threshold=threshold), dtype=float)
                # Cutoff chosen on inner, applied to test. Never the other way.
                warn, _ = metrics.best_threshold(
                    list(zip(yin.tolist(), proba[:len(Xin)].tolist())),
                    threshold, grid=PROBABILITY_GRID, default=0.5)
                row = score(yte.to_numpy(), proba[len(Xin):], threshold, warn)
                row.update(candidate=name, horizon=horizon, fold=fold_i)
                rows.append(row)
                print(f"  h={horizon:>2} fold {fold_i}  {name:<18} "
                      f"p>{warn:.2f}  recall {row['recall']:.2f}  "
                      f"prec {row['precision']:.2f}  CSI {row['csi']:.3f}  "
                      f"AP {row['ap']:.3f}", flush=True)
    return pd.DataFrame(rows)


def summarise(results: pd.DataFrame) -> pd.DataFrame:
    g = results.groupby(["candidate", "horizon"])
    return pd.DataFrame({
        "csi": g["csi"].mean(), "csi_std": g["csi"].std(ddof=1),
        "recall": g["recall"].mean(), "precision": g["precision"].mean(),
        "f1": g["f1"].mean(), "ap": g["ap"].mean(),
        "events": g["events"].sum(), "n": g["n"].sum(),
    }).reset_index()


def verdict(summary: pd.DataFrame, threshold: float) -> str:
    """The gate, per horizon, from one expression per row.

    Reported at every horizon rather than at 24h alone. Persistence is strong at
    24h and 48h because those are whole multiples of the daily cycle: "same hour
    yesterday" lands on the same point of the pollution day. At 6h and 12h it
    compares opposite phases and collapses, which is where a model has something
    to add. Judging this stage on 24h alone would hide that - and 12h from a
    07:00 IST send is the evening peak, the horizon the alert exists to answer.
    """
    rows, won = [], []
    for h in HORIZONS:
        at = summary[summary["horizon"] == h].set_index("candidate")
        if "persistence" not in at.index or len(at) < 2:
            continue
        base = float(at.loc["persistence", "csi"])
        rivals = at.drop(index="persistence")
        win = rivals["csi"].idxmax()
        got, spread = float(rivals.loc[win, "csi"]), float(at.loc[win, "csi_std"])
        beat = (got - base) > spread
        won.append(beat)
        rows.append({"horizon": f"{h}h", "persistence CSI": round(base, 3),
                     "best": win, "CSI": round(got, 3),
                     "margin": round(got - base, 3), "fold std": round(spread, 3),
                     "beats baseline": "yes" if beat else "no",
                     "persistence AP": round(float(at.loc["persistence", "ap"]), 3),
                     "best AP": round(float(rivals.loc[win, "ap"]), 3)})
    if not rows:
        return "NO BASELINE - run persistence to have a bar"
    out = [f"event: truth above {threshold:.0f} ug/m3", "",
           _md(pd.DataFrame(rows), "{:.3f}"), "",
           f"VERDICT: beats persistence at {sum(won)} of {len(won)} horizons on "
           f"CSI, by more than the fold-to-fold spread.",
           "AP is threshold-free and is the honest read on ranking quality. CSI "
           "rests on one tuned cutoff and carries the larger variance, so a tie "
           "on CSI beside a large AP gap means the ranking improved and the "
           "cutoff did not convert it."]
    if not any(won):
        out.append("A documented honest negative is a passing outcome (build "
                   "plan section 7). Do not tune until it looks better.")
    return "\n".join(out)

def self_test() -> int:
    """Prove the scoring separates a good classifier from a useless one.

    Without this the table is a number generator: every candidate could score
    identically and nothing would say so.
    """
    print("=== SELF-TEST — does the scoring discriminate? ===")
    truth = np.array([50.0, 300.0, 60.0, 400.0, 70.0, 500.0])
    perfect = np.array([0.0, 1.0, 0.0, 1.0, 0.0, 1.0])
    useless = np.zeros(6)
    inverted = 1.0 - perfect
    good = score(truth, perfect, 121.0, 0.5)
    bad = score(truth, useless, 121.0, 0.5)
    wrong = score(truth, inverted, 121.0, 0.5)
    print(f"  perfect ranking : CSI {good['csi']:.3f}  recall {good['recall']:.3f}  AP {good['ap']:.3f}")
    print(f"  never warns     : CSI {bad['csi']:.3f}  recall {bad['recall']:.3f}  AP {bad['ap']:.3f}")
    print(f"  exactly wrong   : CSI {wrong['csi']:.3f}  recall {wrong['recall']:.3f}  AP {wrong['ap']:.3f}")
    ok = (good["csi"] == 1.0 and bad["csi"] == 0.0 and wrong["csi"] == 0.0
          and good["ap"] > wrong["ap"])
    # The cutoff search must also be able to move off its default.
    pairs = list(zip(truth.tolist(), np.array([.1, .9, .2, .8, .3, .7]).tolist()))
    cut, csi = metrics.best_threshold(pairs, 121.0, grid=PROBABILITY_GRID, default=0.5)
    print(f"  cutoff search   : chose p>{cut:.2f} at CSI {csi:.3f}")
    ok = ok and csi == 1.0
    print("SELF-TEST PASSED" if ok else "SELF-TEST FAILED — the scoring cannot "
          "tell a perfect classifier from a broken one", file=sys.stderr if not ok else sys.stdout)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help=f"alert threshold in ug/m3 (default {DEFAULT_THRESHOLD:.0f}, "
                         "CPCB's Very Poor band)")
    ap.add_argument("--only", action="append", choices=list(CANDIDATES))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    wide = features.load_wide()
    print(f"building features ({len(wide.columns)} stations, {len(wide):,} hours)...",
          flush=True)
    frames = {h: features.build(wide, h, None, spatial=False) for h in HORIZONS}

    chosen = args.only or list(CANDIDATES)
    if "persistence" not in chosen:
        chosen = ["persistence"] + chosen
    print(f"seed {RANDOM_SEED}   event: truth > {args.threshold:.0f} ug/m3   "
          f"candidates: {', '.join(chosen)}\n")

    results = run(frames, chosen, args.threshold)
    if results.empty:
        sys.exit("no folds produced rows")

    summary = summarise(results)
    print("\n## Per-horizon summary (mean over folds)\n")
    print(_md(summary, "{:.3f}"))
    print("\n## Gate\n\n" + verdict(summary, args.threshold))
    return 0


if __name__ == "__main__":
    sys.exit(main())

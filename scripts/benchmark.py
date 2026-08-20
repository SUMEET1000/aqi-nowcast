"""Phase 4 — the benchmark. GATE 4.

    python scripts/benchmark.py --folds          # fold boundaries, train nothing
    python scripts/benchmark.py --serve-check    # is lag_0 there at 07:00 IST?
    python scripts/benchmark.py --only persistence
    python scripts/benchmark.py                  # the whole table

Reads only the gitignored data/pm25_history.csv, so iterating costs zero Neon
wakes. --refresh re-pulls (one wake).

WHY THE SPLIT LOOKS LIKE THIS
-----------------------------
Blocked forward-chaining cross-validation, expanding window, boundaries on
DATES. Phase 3 used one 90-day holdout, which was right for measuring a baseline
and cannot answer Gate 4's actual question — "beats persistence by more than
split-to-split variance" needs more than one split to have a variance at all.

Not sklearn's TimeSeriesSplit: it cuts on row index, and with 30 interleaved
stations an index cut lands mid-station and mixes dates across folds.

No purging or embargo. Those exist for labels spanning an interval that overlaps
the next fold; ours is a point value and every feature is strictly backward from
issue time, so a date boundary is sufficient. A centred rolling window would
reintroduce the need — tests/test_features.py greps for one.

No Diebold-Mariano test. It assumes a stationary loss differential and is
unreliable in small samples, especially multi-step; at four folds its
asymptotics do not hold. Build plan §7's own "larger than split-to-split
variance" is the right test at this scale.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import env  # noqa: F401  — import-time UTF-8 console fix, needed for µg/m³
import features
import metrics
from baselines import CACHE, HORIZONS, IST, SEVERE_ABOVE

RANDOM_SEED = 20260820

# Fold boundaries as dates, visible and editable in one place. Fold 1 is winter
# on purpose: it is where the severe events live, and Phase 3's single holdout
# was the monsoon with ~50 severe pairs, too few to separate two models.
#
# Fold 1's training block is thin and station-skewed — before October 2025 only
# about five stations reported. That is what forward chaining honestly shows and
# it goes in the README rather than being tuned away by sliding this boundary.
FOLDS = [
    ("2025-12-31", "2026-01-01", "2026-03-01"),
    ("2026-03-01", "2026-03-02", "2026-04-30"),
    ("2026-04-30", "2026-05-01", "2026-06-29"),
    ("2026-06-29", "2026-06-30", "2026-08-20"),
]

# Days at the END of each training block held back to decide preprocessing.
# Choosing a scaler by its test score is tuning on the test set, one level up
# from fitting a scaler on all the data.
INNER_VALIDATION_DAYS = 45

# The alert cutoff for the exceedance metrics. profiles.threshold_pm25 is
# per-profile and unused until the forecast ships; the severe band is the one
# threshold that is defensible today, and it is read from aqi via baselines so
# the number lives in one place.
ALERT_THRESHOLD = SEVERE_ABOVE

# Expected improvement over persistence at 24h, from the PM2.5 literature and
# build plan §1. Printed beside the result so the sanity check is automatic.
EXPECTED_GAIN = (0.15, 0.35)
# Above this, stop and hunt for a leak. A margin this large does not appear in
# real-world hourly station work; it means a feature is reading past issue time.
LEAK_SUSPICION = 0.60

MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(CACHE)), "models")


def _hour(date_str: str, end_of_day: bool = False) -> int:
    """ISO date (IST) -> epoch hour. Boundaries are IST because the pollution
    day is IST; a UTC boundary would cut the evening peak in half."""
    d = datetime.fromisoformat(date_str).replace(tzinfo=IST)
    if end_of_day:
        d += timedelta(hours=23)
    return int(d.timestamp()) // 3600


def fold_masks(frame: pd.DataFrame, horizon: int, fold: tuple
               ) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(train, inner-validation, test) masks over `frame`.

    Sliced on TARGET time, not issue time. A training row whose label lands
    after the train boundary would be learning from the future even though its
    features are clean — the label is data too.
    """
    train_end, test_start, test_end = fold
    target = frame.index.to_series().reset_index(drop=True).values + horizon
    target = pd.Series(target, index=frame.index)

    train_all = target <= _hour(train_end, end_of_day=True)
    inner_from = _hour(train_end, end_of_day=True) - INNER_VALIDATION_DAYS * 24
    return (train_all & (target <= inner_from),
            train_all & (target > inner_from),
            (target >= _hour(test_start)) & (target <= _hour(test_end, True)))


# ---------------------------------------------------------------- candidates

def _prep(X: pd.DataFrame, categorical: str) -> pd.DataFrame:
    """station_id handled three ways, because the libraries differ and the
    ablation table measures which is better."""
    X = X.copy()
    if categorical == "onehot":
        return pd.get_dummies(X, columns=["station_id"], dtype=float)
    if categorical == "category":
        X["station_id"] = X["station_id"].astype("category")
        return X
    return X  # ordinal: leave the integer alone


def fit_persistence(Xtr, ytr, Xte, **_):
    """No fitting. lag_0 IS the value at issue time, so this is exactly the
    Phase 3 baseline, re-scored here on the same rows every other candidate
    sees rather than quoted from the README off a different set of hours."""
    return Xte["lag_0"].to_numpy()


# The defaults on the next three fitters are the ABLATION WINNERS, measured
# 2026-08-20 on the inner validation window at h=24 over 4 folds (`--ablate`).
# Every question came back INSIDE the fold-to-fold noise, so "simpler wins
# within noise" decided them. Two overturned the textbook answer:
#
#   scaler   none 27.792 | robust 27.792 | standard 27.793 | quantile 29.434
#            Identical to three decimals. Ridge's penalty is scale-sensitive in
#            principle, but at the default alpha it is negligible against
#            features of this magnitude, so a scaler has nothing to change.
#            RobustScaler was predicted to win. It tied.
#   imputer  median | mean | constant, all 27.792 exactly. add_indicator hands
#            the model a missingness flag, and a linear model then absorbs any
#            constant fill into that column's coefficient.
def fit_ridge(Xtr, ytr, Xte, scaler="none", imputer="median", **_):
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import (QuantileTransformer, RobustScaler,
                                       StandardScaler)

    scalers = {"none": "passthrough", "standard": StandardScaler(),
               "robust": RobustScaler(),
               "quantile": QuantileTransformer(output_distribution="normal",
                                               random_state=RANDOM_SEED)}
    steps = [
        # add_indicator: a missing lag is information — the sensor was dark —
        # and filling it with the median erases that without the flag.
        ("impute", SimpleImputer(strategy=imputer, add_indicator=True)),
        ("scale", scalers[scaler]),
        ("model", Ridge(random_state=RANDOM_SEED)),
    ]
    # The Pipeline is a correctness mechanism, not decoration: it fits the
    # imputer's median and the scaler's centre on the TRAINING FOLD ONLY,
    # automatically, every fold. Doing that by hand is the most common leak.
    tr, te = _prep(Xtr, "onehot"), _prep(Xte, "onehot")
    te = te.reindex(columns=tr.columns, fill_value=0.0)
    pipe = Pipeline(steps).fit(tr, ytr)
    return pipe.predict(te)


#   station encoding  ordinal 27.066 | onehot 27.212 | category 27.841. Ordinal
#            is both best and simplest. The 0.775 gap sits well inside a fold
#            std of 4.258, so this is a tie reported honestly, not a win.
def _lgbm(Xtr, ytr, Xte, objective, categorical="ordinal", **kw):
    import lightgbm as lgb

    tr, te = _prep(Xtr, categorical), _prep(Xte, categorical)
    if categorical == "category":
        te["station_id"] = te["station_id"].cat.set_categories(
            tr["station_id"].cat.categories)
    weight = kw.pop("_weight", None)
    model = lgb.LGBMRegressor(objective=objective, random_state=RANDOM_SEED,
                              n_jobs=-1, verbose=-1, **kw)
    model.fit(tr, ytr, sample_weight=weight)
    return model.predict(te)


def fit_lgbm(Xtr, ytr, Xte, **kw):
    return _lgbm(Xtr, ytr, Xte, "regression", **kw)


def fit_lgbm_tweedie(Xtr, ytr, Xte, **kw):
    """Tweedie is built for right-skewed non-negative targets. Ours is median
    59, mean 73, max 1650. It is the principled version of the log-transform
    reflex, without the retransformation bias that understates the tail — which
    is the one part of the distribution this product exists for."""
    return _lgbm(Xtr, ytr, Xte, "tweedie", tweedie_variance_power=1.5, **kw)


def fit_xgboost(Xtr, ytr, Xte, categorical="ordinal", **_):
    import xgboost as xgb

    tr, te = _prep(Xtr, categorical), _prep(Xte, categorical)
    model = xgb.XGBRegressor(random_state=RANDOM_SEED, n_jobs=-1,
                             enable_categorical=categorical == "category",
                             tree_method="hist")
    model.fit(tr, ytr)
    return model.predict(te)


# --- three answers to the recall collapse, all justified BY MEASUREMENT ---
#
# The first full run (2026-08-20) passed Gate 4 on MAE and was a disaster as an
# alert. At 24h, persistence: recall 0.18, CSI 0.10, severe MAE 245.26.
# lightgbm-tweedie: recall 0.01, CSI 0.01, severe MAE 277.79. Every model beat
# persistence on average error and lost to it on every hour that matters.
#
# The cause is not a bug. Severe hours are 2% of rows, so a squared-error model
# minimises loss by predicting near the mean and never calling a spike. Tweedie,
# added to help the tail, scored the WORST recall of all — winning the average
# harder is losing the alert harder.
#
# The plan's rule was "report the conditional metric first, weight only if it
# fails". It failed, so these are earned, not assumed.

# Severe rows count this much more in training. Not tuned: 1/0.02 would fully
# balance the classes and swamp the ordinary hours the message also has to get
# right, so this is deliberately partial. The ablation is where it earns its
# value; the point is to test the direction, not to land on a magic number.
SEVERE_WEIGHT = 10.0

# For an ALERT, the 90th percentile is arguably the right quantity to predict:
# the question is "could it be bad", not "what is the expected value".
QUANTILE_ALPHA = 0.9


def fit_lgbm_weighted(Xtr, ytr, Xte, weight=SEVERE_WEIGHT, **kw):
    """Same model, severe training rows weighted up. The most direct answer to
    a model that never calls a spike: make missing one cost more."""
    return _lgbm(Xtr, ytr, Xte, "regression",
                 _weight=np.where(ytr > SEVERE_ABOVE, weight, 1.0), **kw)


def fit_lgbm_quantile(Xtr, ytr, Xte, alpha=QUANTILE_ALPHA, **kw):
    """Predict the 90th percentile, not the mean.

    Deliberately a BIASED point forecast, and it will look worse on MAE by
    construction — MAE is minimised by the median. That is the trade being
    tested: an alert that fires when the upper end of the plausible range is
    dangerous is more useful than one that fires on the expected value and
    therefore never fires at all.
    """
    return _lgbm(Xtr, ytr, Xte, "quantile", alpha=alpha, **kw)


def fit_catboost(Xtr, ytr, Xte, **_):
    from catboost import CatBoostRegressor

    tr, te = Xtr.copy(), Xte.copy()
    for f in (tr, te):
        f["station_id"] = f["station_id"].astype(int).astype(str)
    model = CatBoostRegressor(random_seed=RANDOM_SEED, verbose=0,
                              allow_writing_files=False)
    # CatBoost rejects NaN in a categorical column but takes it in numeric ones,
    # which is why station_id is stringified above and nothing else is touched.
    model.fit(tr, ytr, cat_features=["station_id"])
    return model.predict(te)


CANDIDATES = {
    "persistence": fit_persistence,
    "ridge": fit_ridge,
    "lightgbm": fit_lgbm,
    "lightgbm-tweedie": fit_lgbm_tweedie,
    "lightgbm-weighted": fit_lgbm_weighted,
    "lightgbm-q90": fit_lgbm_quantile,
    "xgboost": fit_xgboost,
    "catboost": fit_catboost,
}


# ------------------------------------------------------------------ scoring

def score_predictions(truth: np.ndarray, pred: np.ndarray,
                      warn_above: float | None = None) -> dict:
    """Everything reported about one (candidate, horizon, fold).

    Predictions are clipped at zero first: a negative PM2.5 is not a forecast,
    and Ridge produces them freely on a skewed target. Clipping is a property of
    the quantity, not a fix for the model, so it happens here where every
    candidate gets it equally.
    """
    pred = np.clip(pred, 0.0, None)
    pairs = list(zip(truth.tolist(), pred.tolist()))
    severe = [(t, p) for t, p in pairs if t > SEVERE_ABOVE]
    mae, rmse, n = metrics.errors(pairs)
    smae, srmse, sn = metrics.errors(severe)
    out = {"mae": mae, "rmse": rmse, "n": n,
           "severe_mae": smae, "severe_rmse": srmse, "severe_n": sn,
           "nonfinite": int((~np.isfinite(pred)).sum())}
    # Two exceedance blocks, not one. `ex_` reads the model's number literally
    # against CPCB's band; `tuned_` reads it against a warning cutoff learned on
    # validation. The first is what a naive deployment would do and the second is
    # what a competent one would, and the gap between them is the finding.
    out.update({f"ex_{k}": v for k, v in
                metrics.exceedance(pairs, ALERT_THRESHOLD).items()})
    warn = ALERT_THRESHOLD if warn_above is None else warn_above
    out["warn_above"] = warn
    out.update({f"tuned_{k}": v for k, v in
                metrics.exceedance_at(pairs, ALERT_THRESHOLD, warn).items()})
    return out


def run(frames: dict, chosen: list[str], quiet: bool = False) -> pd.DataFrame:
    """Every candidate x horizon x fold. Returns one tidy row per combination."""
    rows = []
    for horizon in HORIZONS:
        frame = frames[horizon]
        X, y = features.split_xy(frame)
        for fold_i, fold in enumerate(FOLDS, start=1):
            train, inner, test = fold_masks(frame, horizon, fold)
            if not train.any() or not inner.any() or not test.any():
                print(f"  fold {fold_i} h={horizon}: EMPTY "
                      f"(train {int(train.sum())}, inner {int(inner.sum())}, "
                      f"test {int(test.sum())}) — skipped", file=sys.stderr)
                continue
            # Fitted on `train` ONLY. `inner` is spent on calibrating the
            # warning threshold, so it cannot also be training data — that is
            # the whole point of holding it out.
            Xtr, ytr = X[train], y[train]
            Xin, yin = X[inner], y[inner]
            Xte, yte = X[test], y[test]
            # One fit, predictions for both blocks: the threshold is calibrated
            # on inner and applied to test, never fitted on what it scores.
            both = pd.concat([Xin, Xte])
            for name in chosen:
                started = time.perf_counter()
                pred = np.clip(np.asarray(
                    CANDIDATES[name](Xtr, ytr, both), dtype=float), 0.0, None)
                elapsed = time.perf_counter() - started
                warn, _ = metrics.best_threshold(
                    list(zip(yin.tolist(), pred[:len(Xin)].tolist())),
                    ALERT_THRESHOLD)
                row = score_predictions(yte.to_numpy(), pred[len(Xin):], warn)
                row.update(candidate=name, horizon=horizon, fold=fold_i,
                           train_rows=int(train.sum()), seconds=elapsed)
                rows.append(row)
                if not quiet:
                    print(f"  h={horizon:>2} fold {fold_i}  {name:<17} "
                          f"MAE {row['mae']:7.2f}  n={row['n']:>6,}  "
                          f"warn>{warn:5.0f}  recall {row['ex_recall']:.2f}"
                          f"->{row['tuned_recall']:.2f}  {elapsed:5.1f}s",
                          flush=True)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- ablations

# Preprocessing decided by measurement, not by folklore. "Scaling helps" and
# "sin/cos beats integers" are true in general and may not be true here.
#
# EVERY comparison below is scored on the INNER VALIDATION window carved from
# the end of each fold's training block — never on the test block. Choosing a
# scaler by its test score is tuning on the test set, the same mistake as
# fitting a scaler on all the data, one level up.
#
# One horizon (24h, the one Gate 4 judges) and one model per question, across
# all folds. Not a cross product: this is meant to settle nine questions in
# minutes, not to search a space.
ABLATION_HORIZON = 24

# (question, candidate, kwarg, options) — these reuse one feature frame.
MODEL_ABLATIONS = [
    ("Ridge: which scaler", "ridge", "scaler",
     ["none", "standard", "robust", "quantile"]),
    ("Ridge: which imputer", "ridge", "imputer", ["median", "mean", "constant"]),
    ("LightGBM: station encoding", "lightgbm", "categorical",
     ["category", "onehot", "ordinal"]),
]

# (question, options as build() kwargs) — these need the frame rebuilt.
FEATURE_ABLATIONS = [
    ("spatial features", "lightgbm", "mae",
     [("with", {"spatial": True}), ("without", {"spatial": False})]),
    ("calendar sin/cos", "lightgbm", "mae",
     [("with", {"cyclical": True}), ("without", {"cyclical": False})]),
    ("rolling presence threshold", "lightgbm", "mae",
     [("50%", {"min_present": 0.5}), ("75%", {"min_present": 0.75}),
      ("any", {"min_present": 0.0})]),
    ("weather features", "lightgbm-q90", "csi",
     [("no weather", {"weather": False}),
      ("forecast only", {"weather": True, "blh": "none"}),
      ("forecast + lid now", {"weather": True, "blh": "issue"}),
      ("forecast + lid future (LEAKY, ceiling only)",
       {"weather": True, "blh": "target"})]),
]


def _inner_score(frame, candidate, **kw) -> dict[str, float]:
    """Inner-validation MAE and fixed-cutoff alert scores across folds."""
    X, y = features.split_xy(frame)
    scores = []
    for fold in FOLDS:
        train, inner, _ = fold_masks(frame, ABLATION_HORIZON, fold)
        if not train.any() or not inner.any():
            continue
        pred = CANDIDATES[candidate](X[train], y[train], X[inner], **kw)
        pairs = list(zip(y[inner].tolist(), np.clip(pred, 0, None).tolist()))
        alert = metrics.exceedance(pairs, ALERT_THRESHOLD)
        scores.append({"inner MAE": metrics.errors(pairs)[0],
                       "inner recall": alert["recall"], "inner CSI": alert["csi"]})
    result = pd.DataFrame(scores)
    return {"mae": float(result["inner MAE"].mean()),
            "mae_std": float(result["inner MAE"].std(ddof=1)),
            "ex_recall": float(result["inner recall"].mean()),
            "ex_csi": float(result["inner CSI"].mean()),
            "csi_std": float(result["inner CSI"].std(ddof=1))}


def ablate(wide: pd.DataFrame, coords, weather: bool = True,
           blh: str = "none") -> str:
    """The table that turns preprocessing folklore into measurements."""
    out = ["## Ablations — decided on inner validation, never on test",
           f"\nHorizon {ABLATION_HORIZON}h, {len(FOLDS)} folds, "
           f"seed {RANDOM_SEED}. Lower MAE is better, higher CSI is better, and each question names the one it is ranked on.\n"]
    base = features.build(wide, ABLATION_HORIZON, coords,
                          spatial=coords is not None, weather=weather, blh=blh)

    for question, candidate, kwarg, options in MODEL_ABLATIONS:
        if len(options) < 2:
            continue
        rows = []
        for opt in options:
            score = _inner_score(base, candidate, **{kwarg: opt})
            rows.append({"option": opt, "inner MAE": round(score["mae"], 3),
                         "fold std": round(score["mae_std"], 3)})
            print(f"  {question:<38} {opt:<10} {score['mae']:7.3f}", flush=True)
        df = pd.DataFrame(rows).sort_values("inner MAE")
        spread = df["fold std"].max()
        gap = df["inner MAE"].iloc[-1] - df["inner MAE"].iloc[0]
        out += [f"\n**{question}** ({candidate})\n", _md(df, "{:.3f}"),
                f"\nBest: `{df['option'].iloc[0]}`. Spread across options "
                f"{gap:.3f} vs fold-to-fold std {spread:.3f} — "
                + ("a real difference." if gap > spread else
                   "**within the noise, so the simpler option wins.**")]

    # Trees are expected to be indifferent to scaling. Demonstrated, not asserted.
    plain = _inner_score(base, "lightgbm")["mae"]
    out += [f"\n**LightGBM and scaling**: trees split on order, and order does not "
            f"change under scaling, so a scaler cannot move a tree. Inner MAE "
            f"{plain:.3f} either way — the row exists to show it rather than to "
            f"assert it. Scaling a tree is cargo cult."]

    for question, candidate, rank, options in FEATURE_ABLATIONS:
        rows = []
        for label, kwargs in options:
            if kwargs.get("spatial") and coords is None:
                continue
            frame = features.build(wide, ABLATION_HORIZON, coords,
                                   **{"spatial": coords is not None,
                                      "weather": weather, "blh": blh, **kwargs})
            score = _inner_score(frame, candidate)
            row = {"option": label, "inner MAE": round(score["mae"], 3),
                   "fold std": round(score["mae_std"], 3),
                   "cols": frame.shape[1] - 1}
            if rank == "csi":
                row.update({"inner recall": round(score["ex_recall"], 3),
                            "inner CSI": round(score["ex_csi"], 3),
                            "CSI fold std": round(score["csi_std"], 3)})
            rows.append(row)
            print(f"  {question:<38} {label:<42} {score['mae']:7.3f}", flush=True)
        if len(rows) < 2:
            continue
        # Weather is ranked by fixed-cutoff CSI. MAE is shown beside it and is
        # explicitly not the decision metric: ranking these variants on average
        # error would pick by the number stage 1 already proved misleading.
        #
        # The spread has to be the fold std OF THE RANKED METRIC. A CSI gap
        # cannot exceed 1.0, so measuring it against an MAE fold std of about 5
        # puts "a real difference" out of reach whatever the data says.
        df = pd.DataFrame(rows)
        if rank == "csi":
            df = df.sort_values("inner CSI", ascending=False)
            measure, spread_col = "inner CSI", "CSI fold std"
        else:
            df = df.sort_values("inner MAE")
            measure, spread_col = "inner MAE", "fold std"
        gap = df[measure].max() - df[measure].min()
        spread = df[spread_col].max()
        out += [f"\n**{question}** ({candidate})\n", _md(df, "{:.3f}"),
                ("\nRanked by inner CSI at the fixed alert threshold; MAE is shown but not ranked on."
                 if rank == "csi" else "") +
                f"\nBest: `{df['option'].iloc[0]}`. Spread {gap:.3f} vs fold std "
                f"{spread:.3f} — " + ("a real difference." if gap > spread else
                 "**within the noise, so the simpler option wins.**")]
    return "\n".join(out)


def summarise(results: pd.DataFrame) -> pd.DataFrame:
    """Mean and fold-to-fold std per candidate per horizon.

    The std is not decoration — it IS Gate 4's test. ddof=1 because four folds
    are a sample of the splits we could have drawn, not the population.
    """
    g = results.groupby(["candidate", "horizon"])
    return pd.DataFrame({
        "mae": g["mae"].mean(), "mae_std": g["mae"].std(ddof=1),
        "rmse": g["rmse"].mean(), "rmse_std": g["rmse"].std(ddof=1),
        "n": g["n"].sum(), "severe_n": g["severe_n"].sum(),
        "severe_mae": g["severe_mae"].mean(),
        "recall": g["ex_recall"].mean(), "precision": g["ex_precision"].mean(),
        "csi": g["ex_csi"].mean(), "events": g["ex_events"].sum(),
        "warn_above": g["warn_above"].mean(),
        "t_recall": g["tuned_recall"].mean(),
        "t_precision": g["tuned_precision"].mean(),
        "t_csi": g["tuned_csi"].mean(),
        "seconds": g["seconds"].sum(),
    }).reset_index()


def verdict(summary: pd.DataFrame) -> tuple[str, dict]:
    """GATE 4, from ONE expression so the printed line cannot disagree with the
    table above it. Either outcome passes Phase 4 — build plan §7 asks for a
    win or a documented honest negative, and forbids tuning until it looks good.
    """
    at24 = summary[summary["horizon"] == 24].set_index("candidate")
    base = float(at24.loc["persistence", "mae"])
    rivals = at24.drop(index="persistence", errors="ignore")
    if rivals.empty:
        return "NO RIVAL — persistence only", {}
    winner = rivals["mae"].idxmin()
    got, spread = float(rivals.loc[winner, "mae"]), float(at24.loc[winner, "mae_std"])
    margin = base - got
    share = margin / base if base else float("nan")

    passed = margin > spread
    lines = [
        f"persistence 24h MAE {base:.2f}   {winner} {got:.2f}   "
        f"margin {margin:+.2f} ({share:+.1%})",
        f"fold-to-fold std of {winner}: {spread:.2f}",
        f"GATE 4: {'PASS' if passed else 'FAIL'} — margin "
        f"{'>' if passed else '<='} split-to-split variance",
    ]
    if share > LEAK_SUSPICION:
        lines.append(f"*** {share:.0%} is above the {LEAK_SUSPICION:.0%} leak "
                     "threshold. This margin does not occur in real hourly "
                     "station work. Hunt for a leak BEFORE celebrating. ***")
    elif not EXPECTED_GAIN[0] <= share <= EXPECTED_GAIN[1]:
        lines.append(f"note: outside the expected {EXPECTED_GAIN[0]:.0%}-"
                     f"{EXPECTED_GAIN[1]:.0%} band from the literature.")
    return "\n".join(lines), {"baseline": base, "winner": winner, "winner_mae": got,
                              "margin": margin, "share": share, "std": spread,
                              "passed": bool(passed)}


# ------------------------------------------------------------------ reports

def _md(df: pd.DataFrame, floats: str = "{:.2f}") -> str:
    head = "| " + " | ".join(df.columns) + " |"
    rule = "|" + "---|" * len(df.columns)
    out = [head, rule]
    for _, r in df.iterrows():
        cells = [floats.format(v) if isinstance(v, float) else f"{v:,}"
                 if isinstance(v, (int, np.integer)) else str(v) for v in r]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def serve_check(wide: pd.DataFrame, days: int = 30) -> str:
    """Is lag_0 available at 07:00 IST, when the alert sends?

    If OpenAQ runs hours behind at send time then lag_0 does not exist in
    production and this whole feature design is unservable — a model that scores
    well offline and cannot run.

    THIS IS AN UPPER BOUND, NOT A MEASUREMENT, and the caveat prints with it. It
    asks "does a row with this timestamp exist in today's snapshot", which
    assumes OpenAQ published instantly. Nothing in this repo records when a row
    became fetchable; probe_cpcb and check_send_window both measure CPCB.
    """
    newest = int(wide.index.max())
    ages = []
    for day in range(days):
        # 01:30 UTC == 07:00 IST. The :30 is the stamp both sources use.
        cutoff = newest - day * 24
        cutoff -= (cutoff - _hour("2026-01-01")) % 24  # snap to a fixed clock hour
        window = wide.loc[:cutoff]
        for station in wide.columns:
            present = window[station].dropna()
            if len(present):
                ages.append(cutoff - int(present.index[-1]))
    if not ages:
        return "serve-check: no data"
    a = np.array(ages)
    rows = pd.DataFrame([
        {"staleness": "0 h (fresh)", "share": f"{(a == 0).mean():.1%}"},
        {"staleness": "within 1 h", "share": f"{(a <= 1).mean():.1%}"},
        {"staleness": "within 3 h", "share": f"{(a <= 3).mean():.1%}"},
        {"staleness": "over 3 h", "share": f"{(a > 3).mean():.1%}"},
    ])
    return (f"Serve-time freshness at 07:00 IST, {len(ages):,} station-days.\n"
            f"median {np.median(a):.0f}h, p90 {np.percentile(a, 90):.0f}h, "
            f"max {a.max():.0f}h\n\n" + _md(rows) + "\n\n"
            "UPPER BOUND, not a measurement: it assumes OpenAQ published each\n"
            "hour instantly. Nothing here records when a row became fetchable.")


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception as exc:                      # noqa: BLE001 — never fatal
        return f"unavailable: {exc}"


def model_card(results, summary, gate, frames, path) -> None:
    """Phase 5's promotion gate is "only if it beats the incumbent", which is
    impossible without a record of what the incumbent scored. The reference
    feature distribution is here for the same reason: drift monitoring needs
    something to compare against and it cannot be reconstructed later.
    """
    with open(CACHE, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    X, _ = features.split_xy(frames[24])
    numeric = X.select_dtypes("number")
    card = {
        "written": datetime.now(IST).isoformat(),
        "git_sha": _git_sha(),
        "random_seed": RANDOM_SEED,
        "folds": FOLDS,
        "horizons": list(HORIZONS),
        "inner_validation_days": INNER_VALIDATION_DAYS,
        "alert_threshold": ALERT_THRESHOLD,
        # Rows are per horizon and must be labelled that way. A single "rows"
        # field beside the CSV path reads as the CSV's row count, and the sum
        # across four horizons counts every hour four times.
        "data": {"sha256": digest, "path": os.path.basename(CACHE),
                 "feature_rows_by_horizon": {str(h): int(len(frames[h]))
                                             for h in frames}},
        "gate4": gate,
        "per_fold": json.loads(results.to_json(orient="records")),
        "summary": json.loads(summary.to_json(orient="records")),
        "reference_distribution": json.loads(
            numeric.describe(percentiles=[.05, .25, .5, .75, .95]).to_json()),
        "pip_freeze": subprocess.run([sys.executable, "-m", "pip", "freeze"],
                                     capture_output=True, text=True).stdout.split(),
        # Filled in by hand after reading the table: one sentence on why this
        # candidate was chosen. Left empty deliberately — a model promoted
        # without a stated reason is a model nobody can argue with later.
        "rationale": "",
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(card, fh, indent=2)
    print(f"\nmodel card -> {path}   (rationale field is EMPTY — fill it in)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh", action="store_true", help="re-pull (one Neon wake)")
    ap.add_argument("--folds", action="store_true", help="boundaries only, no training")
    ap.add_argument("--serve-check", action="store_true", help="freshness at 07:00 IST")
    ap.add_argument("--ablate", action="store_true",
                    help="preprocessing ablations on the inner validation window")
    ap.add_argument("--only", action="append", choices=list(CANDIDATES),
                    help="run just these candidates (repeatable)")
    ap.add_argument("--no-weather", action="store_true",
                    help="omit weather columns from every feature frame")
    ap.add_argument("--blh", choices=["none", "issue", "target"], default="none",
                    help="boundary-layer-height feature variant")
    # Spatial features are OFF by default, on two independent measurements:
    #   1. EDA 2026-08-20 — median cross-station correlation is 0.23 within
    #      30 km and 0.24 beyond 150 km, and corr(distance, correlation) is
    #      -0.15. There is essentially no distance decay to exploit; these 30
    #      stations span ~300 km and several airsheds, not one city.
    #   2. Ablation 2026-08-20 — LightGBM inner MAE 27.443 WITHOUT them against
    #      27.841 with. Dropping them is nominally better, not merely harmless.
    # Kept behind a flag rather than deleted: this is the first thing to revisit
    # if a later winter fold ever shows regional transport.
    ap.add_argument("--spatial", action="store_true",
                    help="add neighbour and regional columns (the EDA does not "
                         "support them — see the note above this flag)")
    args = ap.parse_args()

    if args.no_weather and args.blh != "none":
        sys.exit(f"--no-weather removes the weather columns, so --blh {args.blh} "
                 "would have nothing to attach to. Pick one.")

    if args.refresh:
        from baselines import pull
        pull(CACHE)
        features.pull_stations()

    wide = features.load_wide()
    if args.serve_check:
        print(serve_check(wide))
        if not (args.folds or args.only):
            return 0

    coords = (features.load_coords()
              if args.spatial and os.path.exists(features.STATIONS_CACHE) else None)
    if args.spatial and coords is None:
        sys.exit("--spatial needs the coordinate cache: "
                 "python scripts/features.py --pull-stations")

    print(f"building features for {len(HORIZONS)} horizons "
          f"({len(wide.columns)} stations, {len(wide):,} hours)...", flush=True)
    if args.ablate:
        # Before the other frames are built: ablations only ever touch the 24h
        # horizon, and building the other three would be a minute of nothing.
        print(f"ablations, seed {RANDOM_SEED}:", flush=True)
        print("\n" + ablate(wide, coords, weather=not args.no_weather, blh=args.blh))
        return 0

    frames = {h: features.build(wide, h, coords, spatial=coords is not None,
                                weather=not args.no_weather, blh=args.blh)
              for h in HORIZONS}

    if args.folds:
        rows = []
        for horizon in HORIZONS:
            for i, fold in enumerate(FOLDS, start=1):
                train, inner, test = fold_masks(frames[horizon], horizon, fold)
                y = frames[horizon]["_target"]
                rows.append({"horizon": f"{horizon}h", "fold": i,
                             "train_end": fold[0], "test": f"{fold[1]}..{fold[2]}",
                             "train": int(train.sum()), "inner": int(inner.sum()),
                             "test_rows": int(test.sum()),
                             "test_severe": int((y[test] > SEVERE_ABOVE).sum())})
        print("\n" + _md(pd.DataFrame(rows)))
        return 0

    chosen = args.only or list(CANDIDATES)
    print(f"seed {RANDOM_SEED}   candidates: {', '.join(chosen)}\n")
    results = run(frames, chosen)
    if results.empty:
        sys.exit("no folds produced rows — check FOLDS against the data span")

    bad = int(results["nonfinite"].sum())
    if bad:
        sys.exit(f"{bad} non-finite predictions — refusing to report a table")

    summary = summarise(results)
    print("\n## Per-horizon summary (mean over folds, +/- fold-to-fold std)\n")
    print(_md(summary.drop(columns=["seconds"])))

    text, gate = verdict(summary)
    print("\n## Gate 4\n\n" + text)

    if len(chosen) > 1:
        model_card(results, summary, gate, frames,
                   os.path.join(MODELS_DIR, "model_card.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

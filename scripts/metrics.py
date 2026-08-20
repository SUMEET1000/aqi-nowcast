"""Phase 4 — scoring. Regression error, and the alert-shaped error beside it.

Split out of benchmark.py so tests/test_metrics.py runs without importing
LightGBM, for the same reason aqi.py imports nothing: the arithmetic a number
depends on should be checkable without the machinery that produced it.

Two families live here and they answer different questions.

MAE and RMSE ask "how far off is the number". They come from baselines._errors,
imported rather than rewritten, so the Phase 4 table and the Phase 3 table
cannot drift apart.

The exceedance family asks "was the person warned". For a threshold alert that
is the question the subscriber actually experiences, and a model can carry a
respectable MAE while missing every spike. Severe hours are ~2% of the data, so
plain accuracy is ~98% for a model that never warns anybody — which is why CSI
is here and accuracy is not.
"""

from baselines import _errors as errors  # noqa: F401  — re-exported on purpose


def _ratio(numerator: int, denominator: int) -> float:
    """numerator/denominator, or NaN when the denominator is empty.

    Zero would be a lie with the same shape as a real answer: precision 0.0
    reads as "every warning was wrong" when the truth is "no warning was
    issued, so precision is not defined". Build plan §0.5 — a failure is
    visible or it does not happen.
    """
    return numerator / denominator if denominator else float("nan")


def exceedance(pairs: list[tuple[float, float]], threshold: float) -> dict:
    """Score continuous forecasts as the yes/no decision a subscriber sees.

    pairs are (truth, prediction) in ug/m3; both sides are thresholded at the
    same cutoff, so this measures the point forecast rather than a separate
    classifier.

    This docstring used to add that a dedicated classifier "would mean class
    weighting and resampling this project does not need — the regression output
    already carries the information". That was written before anything was
    measured and it was wrong: every regressor here reached the alert by
    thresholding a conditional mean, which is what dragged recall to 0.13 while
    MAE improved. scripts/classify.py predicts the exceedance directly instead.
    The sentence is kept as a correction rather than deleted, because it is a
    clean example of an assumption hardening into a fact by being written down.

    Returns the confusion counts alongside the rates, because a rate computed
    on four events is not a rate and the counts are the only way to see that.

    CSI (Critical Success Index) is tp/(tp+fp+fn) — the meteorology standard
    for rare threshold events. It differs from accuracy by ignoring true
    negatives, which here are the overwhelming majority and would otherwise
    swamp every other term.
    """
    return exceedance_at(pairs, threshold, threshold)


def best_threshold(pairs: list[tuple[float, float]], event_above: float,
                   grid=None, default: float | None = None) -> tuple[float, float]:
    """The decision threshold on the PREDICTION that maximises CSI.

    The event is still "truth above event_above" — that is fixed by CPCB and is
    not being tuned. What is tuned is where the model's own output has to sit
    before a warning goes out, and those are two different numbers.

    They must be, because a regression model on a 2%-event target is biased
    toward the middle: it answers 180 for an hour that turns out to be 300, and
    at a 250 cutoff it never warns anybody. Learning that "this model saying 150
    means danger" is not a trick, it is reading a score correctly. Calibrating a
    decision threshold separately from the score is ordinary practice.

    Returns (threshold, CSI at that threshold). MUST be fitted on validation
    data and applied to test — fitting it on test is choosing the answer.
    """
    if not pairs:
        return event_above, float("nan")
    # Any iterable of numbers. scripts/classify.py passes a 0-1 probability
    # grid, where the concentration default below would mean "never warn".
    grid = grid if grid is not None else range(5, int(event_above) + 1, 5)
    best = float(event_above if default is None else default)
    best_csi = 0.0
    for t in grid:
        csi = exceedance_at(pairs, event_above, float(t))["csi"]
        if csi == csi and csi > best_csi:      # NaN never wins
            best, best_csi = float(t), csi
    # The floor is 0.0, not -1.0, and that is load-bearing. In a validation
    # window containing no severe hours, EVERY threshold scores CSI 0.0 — so a
    # -1.0 floor accepts the first one tried, which is the lowest, and the model
    # then warns on every hour of the test block. Refusing to move unless some
    # threshold beats zero leaves the honest default in place instead.
    return best, best_csi


def exceedance_at(pairs: list[tuple[float, float]], event_above: float,
                  warn_above: float) -> dict:
    """exceedance() with the truth cutoff and the warning cutoff separated."""
    tp = fp = fn = tn = 0
    for truth, pred in pairs:
        actual, warned = truth > event_above, pred > warn_above
        if actual and warned:
            tp += 1
        elif actual:
            fn += 1
        elif warned:
            fp += 1
        else:
            tn += 1
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "n": tp + fp + fn + tn, "events": tp + fn,
        "precision": _ratio(tp, tp + fp), "recall": _ratio(tp, tp + fn),
        "f1": _ratio(2 * tp, 2 * tp + fp + fn), "csi": _ratio(tp, tp + fp + fn),
    }


def residual_quantiles(pairs: list[tuple[float, float]],
                       lower: float = 0.05, upper: float = 0.95
                       ) -> tuple[float, float]:
    """Empirical quantiles of (truth - prediction), for a prediction interval.

    Deliberately the cheap kind and described as such: one interval width per
    horizon, taken from held-out residuals. It does not widen for a hour the
    model finds hard, which a quantile-objective model would. Conformal
    prediction is the textbook upgrade and assumes exchangeability, which an
    autocorrelated series violates.

    Linear interpolation between order statistics, matching numpy's default,
    so the number does not change if this is ever rewritten with numpy.
    """
    if not pairs:
        return float("nan"), float("nan")
    residuals = sorted(t - p for t, p in pairs)

    def q(frac: float) -> float:
        pos = frac * (len(residuals) - 1)
        low = int(pos)
        high = min(low + 1, len(residuals) - 1)
        return residuals[low] + (pos - low) * (residuals[high] - residuals[low])

    return q(lower), q(upper)


def coverage(pairs: list[tuple[float, float]],
             lo: float, hi: float) -> float:
    """Share of pairs whose truth falls inside prediction + [lo, hi].

    The honesty check on residual_quantiles: a 90% interval that covers 62% of
    held-out truth is not a 90% interval, and only measuring it on a later
    window says so.
    """
    if not pairs:
        return float("nan")
    inside = sum(1 for t, p in pairs if p + lo <= t <= p + hi)
    return inside / len(pairs)

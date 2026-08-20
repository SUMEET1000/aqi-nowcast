"""The gate on Phase 4's scoring arithmetic. No DB, no network, no ML library.

    python tests/test_metrics.py

Same shape as tests/test_baselines.py: hand-computed expectations, PASS/FAIL a
line, non-zero exit if anything fails.

The load-bearing cases are the empty denominators. A confusion matrix with no
positives is the normal state of a summer month, and a metrics module that
returns 0.0 there reports "the model warned nobody correctly" for a window in
which there was nothing to warn about. Those two readings lead to opposite
decisions, so the tests below pin NaN rather than zero.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))

import metrics  # noqa: E402

failures = []


def check(label, got, want):
    ok = (got != got and want != want) or (  # NaN == NaN, deliberately
        isinstance(got, float) and isinstance(want, float)
        and abs(got - want) < 1e-9) or got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


NAN = float("nan")

print("-- MAE and RMSE, re-exported from baselines --")
# Errors of -2, +1, 0, +3. MAE = 6/4 = 1.5. RMSE = sqrt(14/4) = 1.8708...
pairs = [(10.0, 12.0), (10.0, 9.0), (10.0, 10.0), (10.0, 7.0)]
mae, rmse, n = metrics.errors(pairs)
check("MAE", mae, 1.5)
check("RMSE", rmse, (14 / 4) ** 0.5)
check("n", n, 4)
check("empty MAE is NaN not 0", metrics.errors([])[0], NAN)

print("\n-- exceedance: the ordinary case --")
# threshold 250. truth/pred pairs chosen to give tp=2, fn=1, fp=1, tn=3.
ex = metrics.exceedance([
    (300.0, 400.0),   # tp
    (900.0, 260.0),   # tp
    (300.0, 100.0),   # fn  <- the miss that matters
    (100.0, 300.0),   # fp
    (10.0, 20.0),     # tn
    (50.0, 60.0),     # tn
    (249.0, 250.0),   # tn — both strictly-greater, so 250 is NOT an exceedance
], 250.0)
check("tp", ex["tp"], 2)
check("fn", ex["fn"], 1)
check("fp", ex["fp"], 1)
check("tn", ex["tn"], 3)
check("n", ex["n"], 7)
check("events (tp+fn)", ex["events"], 3)
check("precision 2/3", ex["precision"], 2 / 3)
check("recall 2/3", ex["recall"], 2 / 3)
check("f1 = 4/(4+1+1)", ex["f1"], 4 / 6)
check("csi = 2/(2+1+1)", ex["csi"], 0.5)

print("\n-- exceedance: the threshold is strict, not inclusive --")
# A value exactly at the cutoff is not an exceedance, on both sides. CPCB
# publishes whole numbers, so the boundary is hit often and must not drift.
edge = metrics.exceedance([(250.0, 250.0)], 250.0)
check("exactly-at-threshold truth is a negative", edge["tn"], 1)
check("exactly-at-threshold pred raises no warning", edge["fp"], 0)

print("\n-- exceedance: empty denominators must be NaN, never 0.0 --")
quiet = metrics.exceedance([(10.0, 20.0), (30.0, 40.0)], 250.0)
check("no events, no warnings: n counted", quiet["n"], 2)
check("no events: events is 0", quiet["events"], 0)
check("no warnings issued -> precision NaN", quiet["precision"], NAN)
check("no events occurred -> recall NaN", quiet["recall"], NAN)
check("nothing to score -> f1 NaN", quiet["f1"], NAN)
check("nothing to score -> csi NaN", quiet["csi"], NAN)

print("\n-- exceedance: warnings issued and all wrong is 0.0, not NaN --")
# The distinction the NaN cases exist to protect: here precision really IS
# zero, and reporting NaN would hide a model crying wolf.
wolf = metrics.exceedance([(10.0, 900.0), (20.0, 800.0)], 250.0)
check("all false alarms -> precision 0.0", wolf["precision"], 0.0)
check("no events -> recall still NaN", wolf["recall"], NAN)
check("f1 0/(0+2+0) = 0.0", wolf["f1"], 0.0)
check("csi 0/(0+2+0) = 0.0", wolf["csi"], 0.0)

print("\n-- exceedance: every event missed --")
blind = metrics.exceedance([(900.0, 10.0), (800.0, 20.0)], 250.0)
check("all missed -> recall 0.0", blind["recall"], 0.0)
check("no warnings -> precision NaN", blind["precision"], NAN)
check("all missed -> csi 0.0", blind["csi"], 0.0)

print("\n-- exceedance: perfect --")
perfect = metrics.exceedance([(900.0, 900.0), (10.0, 10.0)], 250.0)
check("perfect recall", perfect["recall"], 1.0)
check("perfect precision", perfect["precision"], 1.0)
check("perfect csi", perfect["csi"], 1.0)

print("\n-- exceedance: empty input --")
check("empty n", metrics.exceedance([], 250.0)["n"], 0)
check("empty csi NaN", metrics.exceedance([], 250.0)["csi"], NAN)

print("\n-- exceedance_at: the truth cutoff and the warning cutoff are separate --")
# A model biased toward the middle: it answers 180 for an hour that is really
# 300. Read literally at 250 it warns nobody; read at its own scale it is
# perfectly informative. Both readings are of the SAME predictions.
biased_model = [(300.0, 180.0), (400.0, 190.0), (350.0, 160.0), (900.0, 210.0),
                (50.0, 40.0), (60.0, 55.0), (30.0, 20.0), (80.0, 70.0),
                (120.0, 95.0), (20.0, 15.0)]
raw = metrics.exceedance(biased_model, 250.0)
check("read literally at 250: every severe hour missed", raw["fn"], 4)
check("read literally at 250: recall 0.0", raw["recall"], 0.0)
check("read literally at 250: no warning issued, precision NaN",
      raw["precision"], NAN)

at120 = metrics.exceedance_at(biased_model, 250.0, 120.0)
check("warning at 120: all four caught", at120["tp"], 4)
check("warning at 120: recall 1.0", at120["recall"], 1.0)
check("the EVENT cutoff never moved — same four events", at120["events"], 4)
# repr, so the NaN in `precision` compares equal to itself.
check("exceedance(x, t) == exceedance_at(x, t, t)",
      repr(metrics.exceedance(biased_model, 250.0)),
      repr(metrics.exceedance_at(biased_model, 250.0, 250.0)))

print("\n-- best_threshold --")
t, csi = metrics.best_threshold(biased_model, 250.0)
check("finds a threshold that separates them", t <= 120.0, True)
check("and reports perfect CSI there", csi, 1.0)
check("the found threshold reproduces that CSI",
      metrics.exceedance_at(biased_model, 250.0, t)["csi"], 1.0)
# A model with no signal cannot be rescued by any threshold. The event rate
# here is 2 in 40, close to the real 2% — with a 50/50 fixture "always warn"
# scores CSI 0.50 on its own and the test would pass for the wrong reason.
noise = ([(300.0, 50.0), (400.0, 60.0)]
         + [(20.0 + i, 400.0 - i) for i in range(38)])
_, noise_csi = metrics.best_threshold(noise, 250.0)
check("a model with no signal gets a poor CSI, not a perfect one",
      noise_csi < 0.2, True)
check("empty input returns the event threshold unchanged",
      metrics.best_threshold([], 250.0)[0], 250.0)
# NaN must never win the argmax: an all-quiet window scores NaN everywhere.
quiet_pairs = [(10.0, 20.0), (30.0, 40.0)]
qt, qcsi = metrics.best_threshold(quiet_pairs, 250.0)
check("no events at all -> CSI never beats the -1 sentinel", qcsi != qcsi or qcsi <= 0, True)
# The bug this pins: with no events, every threshold scores CSI 0.0, so a
# floor below zero accepts the first (lowest) one tried and the model then
# warns on every hour of the test block.
check("no events at all -> falls back to the event threshold", qt, 250.0)
check("...and does NOT pick the lowest grid point", qt != 5.0, True)

print("\n-- residual quantiles --")
# residuals (truth - pred) are 0..10 inclusive: 11 values.
ramp = [(float(i), 0.0) for i in range(11)]
lo, hi = metrics.residual_quantiles(ramp, 0.0, 1.0)
check("0th quantile is the min", lo, 0.0)
check("100th quantile is the max", hi, 10.0)
mid_lo, mid_hi = metrics.residual_quantiles(ramp, 0.5, 0.5)
check("median of 0..10", mid_lo, 5.0)
# 0.05 * 10 = position 0.5 -> halfway between residual 0 and 1.
q05, q95 = metrics.residual_quantiles(ramp)
check("5th percentile interpolates", q05, 0.5)
check("95th percentile interpolates", q95, 9.5)
check("empty -> NaN", metrics.residual_quantiles([])[0], NAN)

print("\n-- residual quantiles: a biased model shifts the interval --")
# Every prediction is ~20 too low, so the interval must not straddle zero.
# Residuals are 19, 20, 21 — they have to differ, or min and max coincide and
# the test passes for the wrong reason.
biased = [(100.0, 80.0), (102.0, 81.0), (98.0, 79.0)]
blo, bhi = metrics.residual_quantiles(biased, 0.0, 1.0)
check("biased low -> interval entirely positive", blo > 0, True)
check("biased interval min", blo, 19.0)
check("biased interval max", bhi, 21.0)

print("\n-- coverage --")
check("interval covering everything", metrics.coverage(ramp, 0.0, 10.0), 1.0)
check("interval covering nothing", metrics.coverage(ramp, 100.0, 200.0), 0.0)
# residuals 0..10; [0,5] covers residuals 0,1,2,3,4,5 -> 6 of 11.
check("half-covering interval", metrics.coverage(ramp, 0.0, 5.0), 6 / 11)
check("coverage of empty is NaN", metrics.coverage([], 0.0, 1.0), NAN)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks PASS")
sys.exit(0)

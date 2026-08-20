"""The gate on the CROSS-VALIDATION split. No DB, no network, no ML library.

    python tests/test_splits.py

tests/test_features.py proves no FEATURE reads past issue time. This proves the
other half: that no fold trains on a label it should not have seen.

They are different leaks. A row's features can be impeccably backward-looking
while its *label* sits inside the test block — the label is data too, and a
model fitted on it has seen the answer. Slicing on issue time instead of target
time produces exactly that, silently, and it shows up as a suspiciously good
score rather than as an error.

Reads the real cache when it exists, because fold boundaries are calendar dates
and a synthetic fixture would only test the arithmetic against itself.
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))

import benchmark  # noqa: E402  — imports numpy/pandas only, no ML library
import features  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


print("-- fold boundaries are ordered and non-overlapping --")
for i, (train_end, test_start, test_end) in enumerate(benchmark.FOLDS, start=1):
    check(f"fold {i}: train_end < test_start", train_end < test_start, True)
    check(f"fold {i}: test_start <= test_end", test_start <= test_end, True)
for i in range(len(benchmark.FOLDS) - 1):
    this_end = benchmark.FOLDS[i][2]
    next_start = benchmark.FOLDS[i + 1][1]
    check(f"fold {i+1} test ends before fold {i+2} test begins",
          this_end < next_start, True)
    check(f"fold {i+2} trains on everything up to its own test block",
          benchmark.FOLDS[i + 1][0] >= benchmark.FOLDS[i][2], True)

print("\n-- the expanding window actually expands --")
ends = [f[0] for f in benchmark.FOLDS]
check("train boundaries are strictly increasing", ends == sorted(set(ends)), True)

if not os.path.exists(features.CACHE):
    print("\nno data/pm25_history.csv — skipping the data-dependent half.")
    print("Run: python scripts/baselines.py --refresh")
    sys.exit(1 if failures else 0)

print("\n-- no fold trains on a label from its own test block --")
wide = features.load_wide()
for horizon in benchmark.HORIZONS:
    frame = features.build(wide, horizon, None, spatial=False)
    target = pd.Series(frame.index.values + horizon, index=frame.index)
    for i, fold in enumerate(benchmark.FOLDS, start=1):
        train, inner, test = benchmark.fold_masks(frame, horizon, fold)
        boundary = benchmark._hour(fold[0], end_of_day=True)
        fitted = train | inner

        check(f"h={horizon} fold {i}: train and test share no row",
              int((fitted & test).sum()), 0)
        check(f"h={horizon} fold {i}: train and inner-validation are disjoint",
              int((train & inner).sum()), 0)
        # The tight version: labels reach the boundary and not one hour past it.
        # A loose "<=" would also pass if the split were off by a whole fold.
        check(f"h={horizon} fold {i}: latest training LABEL is the boundary",
              int(target[fitted].max()) - boundary, 0)
        check(f"h={horizon} fold {i}: every test label is inside the test block",
              bool(((target[test] >= benchmark._hour(fold[1]))
                    & (target[test] <= benchmark._hour(fold[2], True))).all()), True)
        check(f"h={horizon} fold {i}: all three masks are non-empty",
              bool(train.any() and inner.any() and test.any()), True)

print("\n-- the inner validation window is the END of training, not a sample --")
frame = features.build(wide, 24, None, spatial=False)
target = pd.Series(frame.index.values + 24, index=frame.index)
train, inner, _ = benchmark.fold_masks(frame, 24, benchmark.FOLDS[-1])
check("every inner-validation label is later than every training label",
      int(target[inner].min()) > int(target[train].max()), True)
check("the window is INNER_VALIDATION_DAYS wide",
      round((int(target[inner].max()) - int(target[inner].min())) / 24),
      benchmark.INNER_VALIDATION_DAYS)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks PASS")
sys.exit(0)

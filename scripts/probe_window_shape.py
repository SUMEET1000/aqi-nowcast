"""How many past days should the "usual pattern" block average over?

    python scripts/probe_window_shape.py              # the sweep, exit code is the verdict
    python scripts/probe_window_shape.py --self-test  # proves the check can fail
    python scripts/probe_window_shape.py --refresh    # re-pull the cache (one Neon wake)

Read-only. No model, no training, no Neon wake unless --refresh.

THE QUESTION. The window block will tell a person which part of today is
usually cleanest at their station. That shape has to be built from some number
of past days, and the two obvious choices are the ends of a range already
scored in Phase 3: seasonal persistence is N=1 (yesterday's curve, MAE 27.91 at
24h) and climatology is roughly N=30 (the month's average curve, MAE 32.63).
One day tracks the current regime and is noisy; one month is stable and ignores
that this week differs from three weeks ago. This sweeps N and measures the
thing the message actually claims.

WHY NOT MAE. MAE grades a predicted number. The block predicts no number — it
ranks four windows and names two of them. So the metric is whether the ranking
was right: did the shape name the window that really was cleanest, and the one
that really was worst. A shape can be badly wrong in µg/m³ and still rank
correctly, which is exactly what the message needs.

WHY PLAIN PERSISTENCE IS NOT IN THE SWEEP. It has no shape. "The air later
equals the air now" gives all four windows the same value, so it cannot rank
them at all. That is a property of the baseline, not a low score.

THE DAY RUNS 06:00 IST TO 05:59 IST, not midnight to midnight, so that "night"
is one contiguous block and so that the day boundary matches the 07:00 send. A
midnight boundary would split the night across two days and score the block
against a day the reader is not asking about.
"""

import argparse
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import env  # noqa: F401  — import-time UTF-8 console fix, needed for µg/m³
import baselines
from baselines import IST

# Named windows, in IST hours. These are the blocks a person plans around, not
# an even split: afternoon is short because that is the school-run and
# outdoor-work question, and night is long because nobody subdivides it.
WINDOWS = (
    ("morning",   (6, 7, 8, 9, 10)),
    ("afternoon", (11, 12, 13, 14, 15)),
    ("evening",   (16, 17, 18, 19, 20)),
    ("night",     (21, 22, 23, 0, 1, 2, 3, 4, 5)),
)
NAMES = tuple(name for name, _ in WINDOWS)
HOUR_TO_WINDOW = {h: name for name, hours in WINDOWS for h in hours}

# Candidate lookbacks, in days. 1 is seasonal persistence and 30 is roughly
# climatology, so the sweep spans the two baselines Phase 3 already scored.
SWEEP = (1, 2, 3, 5, 7, 10, 14, 21, 28)

# A window needs this many real readings before it gets a median. Below it the
# median is a coin flip wearing a statistic.
MIN_READINGS = 3

# Judgement, not measurement: how far above chance (25% for one of four) a hit
# rate must sit before the sweep is called useful. Replace it if the numbers
# come back near the floor.
MIN_LIFT = 0.10
CHANCE = 1.0 / len(WINDOWS)


def bucket(series: dict[int, dict[int, float]]
           ) -> dict[int, dict[object, dict[str, list[float]]]]:
    """{station: {day: {window: [values]}}}, days running 06:00-05:59 IST.

    EXACT ZEROS ARE DROPPED HERE and that is not tidying. 4,379 readings (2.3%)
    are exactly 0.0 and the EDA settled that they are dead sensors, not clean
    air — 58% sit in runs longer than 24h and 81% start in the hour after a
    reading above 20 µg/m³. Kept in, a dead sensor would be ranked as the
    cleanest window of the day, which is the one failure this block must never
    produce. features.DROP_EXACT_ZERO does the same thing for the model;
    baselines.load does not, so it has to happen here.
    """
    out: dict[int, dict[object, dict[str, list[float]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list)))
    for station, hours in series.items():
        for hour, value in hours.items():
            if value == 0.0:
                continue
            ist = datetime.fromtimestamp(hour * 3600, IST)
            day = (ist - timedelta(hours=6)).date()
            out[station][day][HOUR_TO_WINDOW[ist.hour]].append(value)
    return out


def medians(day_map: dict[str, list[float]]) -> dict[str, float] | None:
    """One median per window, or None unless all four windows qualify.

    All four or nothing: the block names the cleanest AND the worst window, and
    a ranking over three windows is not the ranking the message makes.
    """
    got = {}
    for name in NAMES:
        values = day_map.get(name, [])
        if len(values) < MIN_READINGS:
            return None
        got[name] = statistics.median(values)
    return got


def shape_over(days: dict[object, dict[str, list[float]]], window: list
               ) -> dict[str, float] | None:
    """The pattern from a set of past days: median per window, pooled.

    Pooled across days rather than averaging each day's median, so a day with
    one reading in a window cannot weigh as much as a day with five.
    """
    pooled: dict[str, list[float]] = defaultdict(list)
    for day in window:
        for name, values in days.get(day, {}).items():
            pooled[name].extend(values)
    return medians(pooled)


def sweep(buckets) -> dict[int, tuple[int, int, int]]:
    """{N: (days_scored, cleanest_hits, worst_hits)}.

    Every N is scored on the SAME station-days wherever it can be — a day only
    counts for an N when that N's lookback qualifies, so the n column has to be
    read beside the hit rate. Large N loses the early days of each station's
    history, which is why n falls as N rises rather than staying flat.
    """
    tally = {n: [0, 0, 0] for n in SWEEP}
    for station, days in buckets.items():
        for day in sorted(days):
            truth = medians(days[day])
            if truth is None:
                continue
            best_true = min(NAMES, key=lambda k: truth[k])
            worst_true = max(NAMES, key=lambda k: truth[k])
            for n in SWEEP:
                prior = [day - timedelta(days=i) for i in range(1, n + 1)]
                shape = shape_over(days, prior)
                if shape is None:
                    continue
                row = tally[n]
                row[0] += 1
                row[1] += min(NAMES, key=lambda k: shape[k]) == best_true
                row[2] += max(NAMES, key=lambda k: shape[k]) == worst_true
    return {n: tuple(v) for n, v in tally.items()}


def render(results: dict[int, tuple[int, int, int]]) -> str:
    lines = ["| days averaged | cleanest window right | worst window right | station-days |",
             "|---|---|---|---|"]
    for n in SWEEP:
        scored, clean, worst = results[n]
        if not scored:
            lines.append(f"| {n} | — | — | 0 |")
            continue
        note = " (= seasonal persistence)" if n == 1 else ""
        lines.append(f"| {n}{note} | {clean / scored:.1%} | "
                     f"{worst / scored:.1%} | {scored:,} |")
    lines.append(f"| chance | {CHANCE:.1%} | {CHANCE:.1%} | — |")
    return "\n".join(lines)


def decide(results: dict[int, tuple[int, int, int]]) -> tuple[int, str]:
    """(exit_code, verdict). Split from the measurement so every branch can be
    driven directly in the self-test — the same reason probe_cpcb_signal.py
    splits its decide()."""
    scored = {n: v for n, v in results.items() if v[0] > 0}
    if not scored:
        return 1, "NO VERDICT — no station-day had all four windows on both sides"
    def mean_hit(n):
        s, c, w = scored[n]
        return (c + w) / (2 * s)
    best = max(scored, key=mean_hit)
    lift = mean_hit(best) - CHANCE
    if lift < MIN_LIFT:
        return 1, (f"NO — the best lookback ({best}d) beats chance by only "
                   f"{lift:.1%}, under MIN_LIFT={MIN_LIFT:.0%}. The usual "
                   f"pattern does not predict today's ranking.")
    return 0, (f"YES — average over {best} days. Hit rate "
               f"{mean_hit(best):.1%} against chance {CHANCE:.1%} "
               f"(lift {lift:+.1%}).")


def self_test() -> int:
    """Proves the check discriminates. No file, no database, no network."""
    from datetime import date, timezone
    failures = []

    def synth(peak_window: str | None, days: int, seed: int = 0):
        """{station: {day: {window: [values]}}} built directly, no timestamps.

        A fixed peak every day must be found; noise must not be.
        """
        out = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
        state = seed
        for d in range(days):
            day = date(2026, 1, 1) + timedelta(days=d)
            for name in NAMES:
                for _ in range(MIN_READINGS):
                    if peak_window is None:
                        state = (state * 1103515245 + 12345) % 2147483648
                        out[1][day][name].append(20.0 + state % 200)
                    else:
                        out[1][day][name].append(
                            200.0 if name == peak_window else 20.0)
        return out

    # 1. A station whose evening is always worst must be called every time.
    fixed = sweep(synth("evening", 40))
    s, _c, w = fixed[7]
    if s == 0 or w / s < 0.99:
        failures.append(f"a fixed evening peak was missed ({w}/{s})")
    else:
        print(f"  PASS  fixed evening peak found on {w}/{s} days at N=7")
    code, _verdict = decide(fixed)
    if code != 0:
        failures.append("decide() refused a perfectly predictable station")
    else:
        print("  PASS  decide() returns 0 on a predictable shape")

    # 2. Pure noise must NOT clear MIN_LIFT. This is the check that the whole
    #    probe rests on: without it a high hit rate could be an artefact of the
    #    ranking arithmetic rather than a real pattern.
    noisy = sweep(synth(None, 200, seed=7))
    code, verdict = decide(noisy)
    if code == 0:
        failures.append(f"decide() accepted pure noise: {verdict}")
    else:
        print("  PASS  decide() refuses pure noise")

    # 3. An empty input reaches no verdict rather than dividing by zero.
    code, verdict = decide(sweep({}))
    if code != 1 or "NO VERDICT" not in verdict:
        failures.append("an empty input did not produce NO VERDICT")
    else:
        print("  PASS  no scoreable day reaches NO VERDICT")

    # 4. The IST window map, asserted rather than trusted. 01:30 UTC is the
    #    send time (07:00 IST), so it must land in the morning window. Flipping
    #    the offset to UTC turns this red.
    send = datetime.fromtimestamp(90 * 60, timezone.utc)  # 01:30 UTC
    ist_hour = send.astimezone(IST).hour
    if HOUR_TO_WINDOW[ist_hour] != "morning":
        failures.append(f"01:30 UTC maps to {HOUR_TO_WINDOW[ist_hour]}, "
                        f"not morning (IST hour {ist_hour})")
    else:
        print("  PASS  01:30 UTC (the send) lands in the morning window")

    # 5. Every hour of the clock belongs to exactly one window.
    if sorted(HOUR_TO_WINDOW) != list(range(24)):
        failures.append("the windows do not cover all 24 hours exactly once")
    else:
        print("  PASS  the four windows cover all 24 hours, no overlap")

    for f in failures:
        print(f"  FAIL  {f}", file=sys.stderr)
    return 1 if failures else 0


def by_spread(buckets, n: int, on: str = "shape",
              edges=(0, 10, 25, 50)) -> list[tuple]:
    """Hit rate split by how far apart the windows were, by `on`.

    A 41% hit rate on a four-way choice sounds poor until you ask what a miss
    costs. On a day whose four windows sit within a few µg/m³ of each other,
    naming the second-cleanest instead of the cleanest is not a wrong answer to
    a person deciding when to go out — the two are the same air.

    `on` decides WHICH spread does the splitting, and only one of the two can
    be acted on:

    - "shape" — the spread of the pattern we are about to show. Known at send
      time, so this is what MIN_SPREAD in the message can actually gate on.
    - "truth" — the spread the day really had. Not knowable at send time, so it
      diagnoses the metric (how much of the miss rate is ties) and must never
      be quoted as the accuracy of a shipped message.
    """
    if on not in ("shape", "truth"):
        raise ValueError(f"on must be 'shape' or 'truth', not {on!r}")
    bands = {e: [0, 0] for e in edges}
    for station, days in buckets.items():
        for day in sorted(days):
            truth = medians(days[day])
            if truth is None:
                continue
            shape = shape_over(days, [day - timedelta(days=i)
                                      for i in range(1, n + 1)])
            if shape is None:
                continue
            source = shape if on == "shape" else truth
            spread = max(source.values()) - min(source.values())
            edge = max(e for e in edges if spread >= e)
            bands[edge][0] += 1
            bands[edge][1] += (min(NAMES, key=lambda k: shape[k])
                               == min(NAMES, key=lambda k: truth[k]))
    return [(e, bands[e][0], bands[e][1]) for e in edges]


def render_spread(rows: list[tuple], n: int, on: str) -> str:
    which = ("the pattern we would SHOW (knowable at send time)" if on == "shape"
             else "what the day REALLY did (not knowable at send time)")
    lines = [f"Cleanest-window hit rate at N={n}, split by the spread of "
             f"{which}:", "",
             "| spread (µg/m³) | station-days | cleanest window right |",
             "|---|---|---|"]
    for edge, scored, hits in rows:
        label = f"{edge}+" if edge else f"under {rows[1][0]}"
        rate = f"{hits / scored:.1%}" if scored else "—"
        lines.append(f"| {label} | {scored:,} | {rate} |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh", action="store_true",
                    help="re-pull pm25_history into the cache (one Neon wake)")
    ap.add_argument("--self-test", action="store_true",
                    help="prove the check can fail; no file, no network")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    import os
    if args.refresh or not os.path.exists(baselines.CACHE):
        baselines.pull(baselines.CACHE)

    buckets = bucket(baselines.load(baselines.CACHE))
    results = sweep(buckets)
    code, verdict = decide(results)

    print(f"\nHow many past days should the window block average over?")
    print(f"{len(buckets)} stations, cache {baselines.CACHE}\n")
    print(render(results))
    print(f"\n{verdict}\n")

    # The lookback the sweep chose, then the number that decides whether this
    # is worth showing a person at all.
    scored = {n: v for n, v in results.items() if v[0] > 0}
    if scored:
        chosen = max(scored, key=lambda n: (scored[n][1] + scored[n][2])
                     / (2 * scored[n][0]))
        print(render_spread(by_spread(buckets, chosen, "shape"),
                            chosen, "shape"))
        print()
        print(render_spread(by_spread(buckets, chosen, "truth"),
                            chosen, "truth"))
        print()
    return code


if __name__ == "__main__":
    sys.exit(main())

"""Bootstrap statistics for leaderboard reporting (issue #161).

A pass rate over N tasks is itself a sample statistic: an agent that passes
24/40 could be a 60% agent or a 45% one having a good day. Every pass rate
the leaderboard and site report therefore carries a 95% bootstrap confidence
interval — resample the per-task outcomes with replacement 10,000 times, take
the mean of each resample, and read the 2.5th/97.5th percentiles of the
resampled rates.

Determinism is a product requirement, not an implementation detail: the same
results tree must always render the same intervals (README blocks and site
pages are committed artifacts), so the sampler is seeded from a fixed
constant and every function here is pure.
"""

from __future__ import annotations

import math
import random

#: Resample count for the percentile bootstrap (issue #161).
BOOTSTRAP_RESAMPLES = 10_000

#: Fixed sampler seed (issue #161). A leaderboard built twice from the same
#: results must be byte-identical, so the seed is a constant, never
#: caller-chosen randomness.
BOOTSTRAP_SEED = 20260101


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated percentile of an ascending list (q in [0, 1])."""
    if not sorted_values:
        raise ValueError("percentile of an empty list")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = q * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction


def bootstrap_pass_rate_ci(
    passed: list[bool],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    level: float = 0.95,
) -> tuple[float, float] | None:
    """Bootstrap CI for a pass rate, over per-task outcomes (issue #161).

    `passed` holds one outcome per graded task — one entry per run-record;
    with repeated runs (issue #162) each run contributes its own outcome.
    Returns `(low, high)` pass-rate bounds, or None for an empty list: no
    graded outcomes is a different fact from a 0% rate with a tight interval.
    A single outcome resamples to itself, so its interval is degenerate.

    The bounds are the `level` central percentiles of 10,000 resampled rates,
    drawn with `random.Random(seed)` in a fixed order — two calls with the
    same arguments always return the same pair.
    """
    if resamples < 1:
        raise ValueError("resamples must be >= 1")
    if not 0.0 < level < 1.0:
        raise ValueError("level must be strictly between 0 and 1")
    outcomes = [1.0 if item else 0.0 for item in passed]
    if not outcomes:
        return None
    total = len(outcomes)
    rng = random.Random(seed)
    rates = sorted(
        sum(rng.choices(outcomes, k=total)) / total for _ in range(resamples)
    )
    tail = (1.0 - level) / 2.0
    return _percentile(rates, tail), _percentile(rates, 1.0 - tail)


def intervals_overlap_heavily(
    point_a: float,
    ci_a: tuple[float, float],
    point_b: float,
    ci_b: tuple[float, float],
) -> bool:
    """True when each point estimate lies inside the other's interval.

    The "overlap heavily" test behind rank ties (issue #161): plain overlap of
    95% bounds is near-universal, but mutual containment of the point
    estimates means the data cannot even order the two agents — that is a tie
    worth marking. Inclusive at the bounds, so exactly equal rates tie.
    """
    low_a, high_a = ci_a
    low_b, high_b = ci_b
    return low_b <= point_a <= high_b and low_a <= point_b <= high_a

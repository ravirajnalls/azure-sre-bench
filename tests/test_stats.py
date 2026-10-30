"""Tests for the bootstrap statistics module (issue #161).

Determinism is the point: the same outcomes and seed must always produce the
same interval, because committed README blocks and site pages are rebuilt
from these numbers.
"""

from __future__ import annotations

import random

import pytest

from bench.stats import (
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    bootstrap_pass_rate_ci,
    intervals_overlap_heavily,
)


# --- bootstrap_pass_rate_ci -------------------------------------------------


def test_all_pass_is_a_degenerate_full_interval():
    # Every resample of all-pass outcomes is 1.0 — the interval is exact
    # regardless of seed or resample count.
    assert bootstrap_pass_rate_ci([True] * 5) == (1.0, 1.0)


def test_all_fail_is_a_degenerate_zero_interval():
    assert bootstrap_pass_rate_ci([False] * 4) == (0.0, 0.0)


def test_single_outcome_resamples_to_itself():
    assert bootstrap_pass_rate_ci([True]) == (1.0, 1.0)
    assert bootstrap_pass_rate_ci([False]) == (0.0, 0.0)


def test_empty_outcomes_have_no_interval():
    assert bootstrap_pass_rate_ci([]) is None


def test_same_seed_is_byte_identical():
    outcomes = [True, False, True, True, False, False, True, False]
    first = bootstrap_pass_rate_ci(outcomes, seed=1234)
    second = bootstrap_pass_rate_ci(outcomes, seed=1234)
    assert first == second


def test_defaults_match_the_documented_constants():
    outcomes = [True, False, True, True]
    assert bootstrap_pass_rate_ci(outcomes) == bootstrap_pass_rate_ci(
        outcomes, resamples=BOOTSTRAP_RESAMPLES, seed=BOOTSTRAP_SEED
    )


def test_golden_seeded_interval():
    # Regression pin for the seeded sampler: fixed seed, fixed resample
    # count, fixed population. If this fails, the seed, the draw order, or
    # the percentile math moved — update consciously, never silently.
    outcomes = [True] * 24 + [False] * 16
    assert bootstrap_pass_rate_ci(outcomes) == pytest.approx(
        (0.45, 0.75), abs=1e-9
    )


def test_interval_brackets_the_point_estimate():
    outcomes = [True, False, True, True, False, True, False, False, True, True]
    low, high = bootstrap_pass_rate_ci(outcomes)
    mean = sum(outcomes) / len(outcomes)
    assert 0.0 <= low <= mean <= high <= 1.0


def test_even_split_stays_in_plausible_bounds():
    # 30/30 with 10k resamples: SE = sqrt(0.25/60) ≈ 0.065, so a 95%
    # interval of roughly 0.5 ± 0.13. The loose [0.3, 0.7] guard catches
    # gross errors (wrong population size, off-by-one percentile) without
    # pinning statistical noise.
    low, high = bootstrap_pass_rate_ci([True] * 30 + [False] * 30)
    assert 0.3 < low <= 0.5 <= high < 0.7


def test_rejects_bad_resample_count_and_level():
    with pytest.raises(ValueError):
        bootstrap_pass_rate_ci([True], resamples=0)
    with pytest.raises(ValueError):
        bootstrap_pass_rate_ci([True], level=1.0)


# --- intervals_overlap_heavily ---------------------------------------------


def test_mutual_containment_is_a_heavy_overlap():
    assert intervals_overlap_heavily(0.5, (0.3, 0.7), 0.6, (0.4, 0.8))


def test_one_sided_containment_is_not_a_heavy_overlap():
    # B's point sits inside A's interval but A's point sits outside B's:
    # the data still orders them.
    assert not intervals_overlap_heavily(0.5, (0.3, 0.7), 0.75, (0.7, 0.9))


def test_disjoint_intervals_never_tie():
    assert not intervals_overlap_heavily(0.5, (0.3, 0.6), 0.8, (0.7, 0.9))


def test_bounds_are_inclusive_so_equal_rates_tie():
    assert intervals_overlap_heavily(0.5, (0.5, 0.7), 0.5, (0.4, 0.5))


def test_seed_only_affects_sampling_not_the_overlap_rule():
    rng = random.Random(7)
    outcomes = [rng.random() > 0.4 for _ in range(20)]
    ci = bootstrap_pass_rate_ci(outcomes)
    mean = sum(outcomes) / len(outcomes)
    assert intervals_overlap_heavily(mean, ci, mean, ci)

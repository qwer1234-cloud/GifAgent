"""W4 kill tests for quality_lab.calibration mutation survivors.

Targets survivors from the cosmic-ray run recorded in
``<sprint-root>/w4/mutation_scores/cosmic-ray/calibration.json``:

- ``calibration_curve``: empty-input guard (``or`` vs ``and``), clip lower
  bound with negative scores, bin ``upper`` edge indexing (``i + 1`` vs nine
  alternative operators).
- ``fit_monotonic_calibrator`` PAV loop: pool index arithmetic on
  ``pool_sums``/``pool_counts``/``pool_scores``/``del`` (``i + 1`` vs many
  alternatives), rate computation (``/`` vs ``**`` and ``//``), backtrack
  stepping (``i -= 1``, ``i > 0``).

Equivalent mutants (``not scores and not labels`` in the calibrator — zip
with an empty side yields the same empty result, ``count == 0`` vs ``<= 0``
on a non-negative sum, ``i < len-1`` vs ``!=``/``is not`` on the PAV cursor,
``i > 0`` vs ``!= 0``) are registered in w4-surviving.md.
"""

from __future__ import annotations

import pytest

from app.quality_lab.calibration import (
    calibration_curve,
    fit_monotonic_calibrator,
)


# ── calibration_curve ──────────────────────────────────────────────────────


def test_calibration_curve_with_empty_scores_returns_empty():
    assert calibration_curve([], [1, 0]) == []


def test_calibration_curve_clips_negative_scores_into_the_first_bin():
    result = calibration_curve([-0.5, 0.95], [1, 0], bins=10)
    assert sum(bin.count for bin in result) == 2
    assert result[0].count == 1
    assert result[0].mean_score == pytest.approx(-0.5)


def test_calibration_bin_edges_are_adjacent_half_open_intervals():
    result = calibration_curve([0.0, 0.3, 0.6, 0.9], [0, 1, 1, 0], bins=4)
    assert [b.lower for b in result] == [0.0, 0.25, 0.5, 0.75]
    assert [b.upper for b in result] == [0.25, 0.5, 0.75, 1.0]
    # bin 1 upper edge is bin_edges[2]; every off-by-one operator mutant
    # (i-1, i*1, i//1, i%1, i**1, i>>1, i|1, i&1, i^1, i+0) breaks this.
    assert result[1].upper == pytest.approx(0.5)
    assert result[1].count == 1
    assert result[1].positive_rate == pytest.approx(1.0)


# ── fit_monotonic_calibrator (PAV) ─────────────────────────────────────────


def test_pav_merges_violating_pools_into_a_monotone_step_function():
    calibrator = fit_monotonic_calibrator([0.1, 0.2, 0.3], [1, 0, 1])
    assert calibrator.thresholds == (0.2, 0.3)
    assert calibrator.values == pytest.approx((0.5, 1.0))


def test_pav_multi_violation_chain_with_backtracking():
    scores = [0.1, 0.12, 0.14, 0.2, 0.22]
    labels = [1, 1, 0, 1, 1]
    calibrator = fit_monotonic_calibrator(scores, labels)
    # After merging (0.12, 0.14) the pooled rate 2/3 violates against 1 and
    # triggers a backtrack merge; without the backtrack the first two pools
    # would stay split.
    assert calibrator.thresholds == pytest.approx((0.14, 0.2, 0.22))
    assert calibrator.values == pytest.approx((2.0 / 3.0, 1.0, 1.0))


def test_pav_tie_rates_do_not_merge_but_zero_rate_backtrack_does():
    scores = [0.1, 0.12, 0.2, 0.22]
    labels = [1, 0, 1, 0]
    calibrator = fit_monotonic_calibrator(scores, labels)
    # First merge yields rate 0.5 tied with the next pool's 0.5 (no merge on
    # a tie), then the 1.0 pool merges the 0.0 pool, and the backtracked
    # 0.5 vs 0.5 tie stops merging.
    assert calibrator.thresholds == pytest.approx((0.12, 0.22))
    assert calibrator.values == pytest.approx((0.5, 0.5))


def test_pav_empty_input_returns_empty_calibrator():
    calibrator = fit_monotonic_calibrator([], [])
    assert calibrator.thresholds == ()
    assert calibrator.values == ()


def test_pav_sorted_single_class_input():
    calibrator = fit_monotonic_calibrator([0.2, 0.1], [1, 1])
    assert calibrator.thresholds == pytest.approx((0.1, 0.2))
    assert calibrator.values == pytest.approx((1.0, 1.0))

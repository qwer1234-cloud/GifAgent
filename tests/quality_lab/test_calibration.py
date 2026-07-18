"""Tests for VLM score calibration (Phase 2 Task 4)."""
from __future__ import annotations

import random

import numpy as np
import pytest

from app.quality_lab.calibration import (
    CalibrationBin,
    MonotonicCalibrator,
    calibration_curve,
    fit_monotonic_calibrator,
)


# ===================================================================
# calibration_curve
# ===================================================================


class TestCalibrationCurve:
    """``calibration_curve`` — equal-width reliability bins."""

    def test_equal_width_bins(self) -> None:
        """Bins split [0, 1] into equal-width intervals."""
        scores = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95]
        labels = [0, 0, 0, 0, 1, 1, 0, 1, 1, 1]
        curve = calibration_curve(scores, labels, bins=5)
        assert len(curve) == 5

        # Bin 0: [0.0, 0.2) -> scores [0.1],   labels [0]
        assert curve[0].lower == pytest.approx(0.0, abs=1e-6)
        assert curve[0].upper == pytest.approx(0.2, abs=1e-6)
        assert curve[0].mean_score == pytest.approx(0.1, abs=1e-6)
        assert curve[0].positive_rate == pytest.approx(0.0, abs=1e-6)
        assert curve[0].count == 1

        # Bin 1: [0.2, 0.4) -> scores [0.2, 0.3], labels [0, 0]
        assert curve[1].lower == pytest.approx(0.2, abs=1e-6)
        assert curve[1].upper == pytest.approx(0.4, abs=1e-6)
        assert curve[1].mean_score == pytest.approx(0.25, abs=1e-6)
        assert curve[1].positive_rate == pytest.approx(0.0, abs=1e-6)
        assert curve[1].count == 2

        # Bin 2: [0.4, 0.6) -> scores [0.4, 0.5], labels [0, 1]
        assert curve[2].mean_score == pytest.approx(0.45, abs=1e-6)
        assert curve[2].positive_rate == pytest.approx(0.5, abs=1e-6)
        assert curve[2].count == 2

        # Bin 3: [0.6, 0.8) -> scores [0.6, 0.7], labels [1, 0]
        assert curve[3].mean_score == pytest.approx(0.65, abs=1e-6)
        assert curve[3].positive_rate == pytest.approx(0.5, abs=1e-6)
        assert curve[3].count == 2

        # Bin 4: [0.8, 1.0] -> scores [0.8, 0.9, 0.95], labels [1, 1, 1]
        assert curve[4].mean_score == pytest.approx(0.88333, abs=1e-4)
        assert curve[4].positive_rate == pytest.approx(1.0, abs=1e-6)
        assert curve[4].count == 3

    def test_empty_scores(self) -> None:
        """Empty input returns empty list."""
        curve = calibration_curve([], [], bins=5)
        assert curve == []

    def test_bin_count_default(self) -> None:
        """Default bin count is 10."""
        scores = np.linspace(0, 1, 100).tolist()
        labels = [1 if s > 0.5 else 0 for s in scores]
        curve = calibration_curve(scores, labels)
        assert len(curve) == 10

    def test_edge_scores_at_boundary(self) -> None:
        """Scores at bin edge (0.5) go into the right bin."""
        scores = [0.0, 0.25, 0.5, 0.75, 1.0]
        labels = [0, 0, 1, 1, 1]
        curve = calibration_curve(scores, labels, bins=2)
        assert len(curve) == 2

        # Bin 0: [0, 0.5) -> scores [0.0, 0.25], labels [0, 0]
        assert curve[0].count == 2
        assert curve[0].mean_score == pytest.approx(0.125, abs=1e-6)
        assert curve[0].positive_rate == pytest.approx(0.0, abs=1e-6)

        # Bin 1: [0.5, 1.0] -> scores [0.5, 0.75, 1.0], labels [1, 1, 1]
        assert curve[1].count == 3
        assert curve[1].mean_score == pytest.approx(0.75, abs=1e-6)
        assert curve[1].positive_rate == pytest.approx(1.0, abs=1e-6)

    def test_empty_bins_included(self) -> None:
        """Bins with no samples are included with count=0."""
        # 10 scores all in [0.8, 1.0], 5 bins
        scores = [0.81, 0.82, 0.83, 0.84, 0.85, 0.86, 0.87, 0.88, 0.89, 0.9]
        labels = [1] * 10
        curve = calibration_curve(scores, labels, bins=5)
        assert len(curve) == 5
        # Bins 0-3 should be empty
        for i in range(4):
            assert curve[i].count == 0
            assert curve[i].mean_score == 0.0
            assert curve[i].positive_rate == 0.0
        # Bin 4 has all data
        assert curve[4].count == 10
        assert curve[4].positive_rate == pytest.approx(1.0, abs=1e-6)

    def test_bin_membership_strict_left_boundary(self) -> None:
        """Scores of exactly 0.0 belong to bin [0.0, upper)."""
        scores = [0.0, 0.0, 0.0]
        labels = [0, 0, 1]
        curve = calibration_curve(scores, labels, bins=3)
        assert curve[0].count == 3
        assert curve[1].count == 0
        assert curve[2].count == 0

    def test_calibration_bin_dataclass(self) -> None:
        """CalibrationBin is a frozen dataclass with correct fields."""
        cb = CalibrationBin(
            lower=0.2, upper=0.4, mean_score=0.3, positive_rate=0.5, count=10
        )
        assert cb.lower == 0.2
        assert cb.upper == 0.4
        assert cb.mean_score == 0.3
        assert cb.positive_rate == 0.5
        assert cb.count == 10
        with pytest.raises(AttributeError):
            cb.count = 20  # frozen


# ===================================================================
# fit_monotonic_calibrator  (Pool Adjacent Violators)
# ===================================================================


class TestFitMonotonicCalibrator:
    """``fit_monotonic_calibrator`` — PAV isotonic regression."""

    def test_already_monotonic(self) -> None:
        """Non-decreasing scores with non-decreasing labels produce no pools."""
        scores = [0.1, 0.2, 0.3, 0.4, 0.5]
        labels = [0, 0, 1, 1, 1]
        calibrator = fit_monotonic_calibrator(scores, labels)
        # All pools already non-decreasing: no merges
        # Each point stays its own pool
        assert len(calibrator.thresholds) == 5
        assert calibrator.values == pytest.approx(
            (0.0, 0.0, 1.0, 1.0, 1.0), abs=1e-6
        )

    def test_pool_adjacent_violators(self) -> None:
        """Violating pair (score 0.3, label 1) and (score 0.4, label 0) merged."""
        scores = [0.1, 0.2, 0.3, 0.4, 0.5]
        labels = [0, 0, 1, 0, 1]
        calibrator = fit_monotonic_calibrator(scores, labels)
        # Sorted by score, pools:
        # (0.1, rate=0/1=0), (0.2, rate=0/1=0),
        # (0.3, rate=1/1=1) violates with (0.4, rate=0/1=0) -> merge
        #   merged: sum=1, count=2, rate=0.5
        # (0.5, rate=1/1=1)
        # Final: thresholds (0.1, 0.2, 0.4, 0.5), values (0, 0, 0.5, 1)
        assert len(calibrator.thresholds) == 4
        assert calibrator.values == pytest.approx(
            (0.0, 0.0, 0.5, 1.0), abs=1e-6
        )

    def test_cascading_merge(self) -> None:
        """After merge, check left neighbor for new violation."""
        scores = [0.1, 0.2, 0.3]
        labels = [1, 0, 0]
        calibrator = fit_monotonic_calibrator(scores, labels)
        # (0.1, rate=1) violates with (0.2, rate=0) -> merge
        #   merged: sum=1, count=2, rate=0.5
        # merged pool (0.2, rate=0.5) vs (0.3, rate=0) -> merge
        #   merged: sum=1, count=3, rate=1/3
        # Single pool: thresholds (0.3,), values (1/3,)
        assert len(calibrator.thresholds) == 1
        assert calibrator.values == pytest.approx((1.0 / 3.0,), abs=1e-6)

    def test_all_negative(self) -> None:
        """All labels 0 -> rates all 0.0."""
        scores = [0.1, 0.3, 0.5, 0.7, 0.9]
        labels = [0, 0, 0, 0, 0]
        calibrator = fit_monotonic_calibrator(scores, labels)
        assert all(v == 0.0 for v in calibrator.values)

    def test_all_positive(self) -> None:
        """All labels 1 -> rates all 1.0."""
        scores = [0.1, 0.3, 0.5, 0.7, 0.9]
        labels = [1, 1, 1, 1, 1]
        calibrator = fit_monotonic_calibrator(scores, labels)
        assert all(v == 1.0 for v in calibrator.values)

    def test_decreasing_scores_with_positive_trend(self) -> None:
        """Input order does not matter; sorting is internal."""
        scores = [0.9, 0.7, 0.5, 0.3, 0.1]
        labels = [1, 1, 0, 0, 0]
        calibrator = fit_monotonic_calibrator(scores, labels)
        # Sorted: (0.1, 0), (0.3, 0), (0.5, 0), (0.7, 1), (0.9, 1)
        # Already monotonic (0, 0, 0, 1, 1) -> no merges
        assert len(calibrator.thresholds) == 5
        assert calibrator.values == pytest.approx(
            (0.0, 0.0, 0.0, 1.0, 1.0), abs=1e-6
        )

    def test_empty_input(self) -> None:
        """Empty input returns empty thresholds and values."""
        calibrator = fit_monotonic_calibrator([], [])
        assert calibrator.thresholds == ()
        assert calibrator.values == ()

    def test_single_point(self) -> None:
        """Single point always works."""
        calibrator = fit_monotonic_calibrator([0.5], [1])
        assert calibrator.thresholds == (0.5,)
        assert calibrator.values == (1.0,)

    def test_monotonic_output_guaranteed(self) -> None:
        """Output values are always non-decreasing."""
        rng = random.Random(42)
        scores = [rng.random() for _ in range(100)]
        labels = [1 if s > 0.7 else 0 for s in scores]
        # Add noise to create violations
        for i in range(20):
            idx = rng.randint(0, 99)
            labels[idx] = 1 - labels[idx]

        calibrator = fit_monotonic_calibrator(scores, labels)
        for i in range(len(calibrator.values) - 1):
            assert calibrator.values[i] <= calibrator.values[i + 1] + 1e-10

    def test_monotonic_calibrator_dataclass(self) -> None:
        """MonotonicCalibrator is a frozen dataclass."""
        mc = MonotonicCalibrator(
            thresholds=(0.1, 0.5, 0.9), values=(0.0, 0.5, 1.0)
        )
        assert mc.thresholds == (0.1, 0.5, 0.9)
        assert mc.values == (0.0, 0.5, 1.0)
        with pytest.raises(AttributeError):
            mc.values = (0.0,)  # frozen

    def test_no_holdout_labels_accepted(self) -> None:
        """The fitting API accepts any binary labels; there is no holdout
        rejection in this function (holdout filtering is upstream)."""
        scores = [0.1, 0.2, 0.3, 0.4, 0.5]
        labels = [0, 0, 1, 1, 1]
        # No exception should be raised for any labels; the function
        # simply treats them as binary rates.
        calibrator = fit_monotonic_calibrator(scores, labels)
        assert calibrator is not None

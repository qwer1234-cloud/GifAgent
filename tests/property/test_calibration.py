"""Property tests for quality-lab PAV calibration (W5 Task 4).

Locked against ``app.quality_lab.calibration``:

- ``fit_monotonic_calibrator`` returns non-decreasing ``thresholds`` and
  ``values``; applying the documented right-closed step convention to the
  sorted training scores yields a non-decreasing calibrated sequence;
  empty input pins to empty tuples.
- ``calibration_curve`` covers ``[0, 1]`` with its bin edges and the bin
  counts sum to the sample count.  The implementation *clips* out-of-range
  scores into the first/last bin (code read: ``np.clip(np.floor(...)``),
  so nothing is dropped — pinned with a fixed example.
"""

from __future__ import annotations

import pytest
from hypothesis import given, seed, settings, strategies as st

from app.quality_lab.calibration import (
    MonotonicCalibrator,
    calibration_curve,
    fit_monotonic_calibrator,
)

SEED = 20260828

_SCORE_LABEL_PAIRS = st.lists(
    st.tuples(
        st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False),
        st.integers(0, 1),
    ),
    min_size=1,
    max_size=60,
)


def _calibrate(calibrator: MonotonicCalibrator, score: float) -> float:
    """Apply the documented ``MonotonicCalibrator`` step convention.

    Docstring contract of the frozen dataclass:
        score <= thresholds[0]                     -> values[0]
        thresholds[i-1] < score <= thresholds[i]   -> values[i]
        score > thresholds[-1]                     -> values[-1]
    """
    assert calibrator.thresholds
    if score <= calibrator.thresholds[0]:
        return calibrator.values[0]
    for i in range(1, len(calibrator.thresholds)):
        if calibrator.thresholds[i - 1] < score <= calibrator.thresholds[i]:
            return calibrator.values[i]
    return calibrator.values[-1]


@given(pairs=_SCORE_LABEL_PAIRS)
@settings(deadline=None)
@seed(SEED)
def test_pav_thresholds_and_values_are_non_decreasing(pairs):
    """PAV output is monotone: thresholds and values both non-decreasing."""
    calibrator = fit_monotonic_calibrator(
        [score for score, _ in pairs], [label for _, label in pairs]
    )

    assert calibrator.thresholds == tuple(sorted(calibrator.thresholds))
    assert calibrator.values == tuple(sorted(calibrator.values))
    assert len(calibrator.thresholds) == len(calibrator.values)
    assert all(0.0 <= value <= 1.0 for value in calibrator.values)


@given(pairs=_SCORE_LABEL_PAIRS)
@settings(deadline=None)
@seed(SEED)
def test_calibrated_training_scores_are_monotonic(pairs):
    """Right-closed step mapping keeps the training sequence monotone."""
    calibrator = fit_monotonic_calibrator(
        [score for score, _ in pairs], [label for _, label in pairs]
    )
    scores = sorted(score for score, _ in pairs)

    calibrated = [_calibrate(calibrator, score) for score in scores]

    assert calibrated == sorted(calibrated)


@given(
    pairs=_SCORE_LABEL_PAIRS,
    bins=st.integers(1, 20),
)
@settings(deadline=None)
@seed(SEED)
def test_calibration_curve_bins_cover_unit_interval_and_count_samples(
    pairs, bins
):
    """Bin edges span [0, 1] and every sample lands in exactly one bin."""
    scores = [score for score, _ in pairs]
    labels = [label for _, label in pairs]

    curve = calibration_curve(scores, labels, bins=bins)

    assert len(curve) == bins
    assert curve[0].lower == pytest.approx(0.0, abs=1e-9)
    assert curve[-1].upper == pytest.approx(1.0, abs=1e-9)
    assert all(0.0 <= bin_.lower < bin_.upper <= 1.0 for bin_ in curve)
    assert sum(bin_.count for bin_ in curve) == len(scores)
    assert all(bin_.count >= 0 for bin_ in curve)
    for bin_ in curve:
        if bin_.count:
            assert 0.0 <= bin_.positive_rate <= 1.0


def test_empty_inputs_are_pinned():
    """Empty training data freezes to empty calibrator / empty curve."""
    calibrator = fit_monotonic_calibrator([], [])
    assert calibrator.thresholds == ()
    assert calibrator.values == ()
    assert calibration_curve([], []) == []


def test_out_of_range_scores_are_clipped_not_dropped():
    """Code read pin: np.clip keeps out-of-range samples in edge bins."""
    curve = calibration_curve([-0.5, 0.5, 1.7], [0, 1, 1], bins=2)

    assert [bin_.count for bin_ in curve] == [1, 2]
    assert sum(bin_.count for bin_ in curve) == 3

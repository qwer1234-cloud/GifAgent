"""VLM score calibration for quality-lab experiments (Phase 2 Task 4).

Provides reliability-diagram binning and pool-adjacent-violators (PAV)
isotonic regression, both implemented with NumPy only (no scikit-learn).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationBin:
    """A single bin in a reliability / calibration curve.

    Attributes
    ----------
    lower
        Lower bound of the bin (inclusive for the first bin, exclusive
        for all others — following the convention of half-open intervals
        ``[lower, upper)`` with the last bin closed at 1.0).
    upper
        Upper bound of the bin.
    mean_score
        Mean predicted score of samples that fall into this bin.
    positive_rate
        Fraction of samples with label == 1 in this bin.
    count
        Number of samples assigned to this bin.
    """

    lower: float
    upper: float
    mean_score: float
    positive_rate: float
    count: int


@dataclass(frozen=True)
class MonotonicCalibrator:
    """A monotonic step-function mapping scores to calibrated probabilities.

    The calibrator returned by ``fit_monotonic_calibrator`` satisfies:

        score <= thresholds[0]          -> values[0]
        thresholds[i-1] < score <= thresholds[i]  -> values[i]   (i >= 1)
        score > thresholds[-1]          -> values[-1]

    Attributes
    ----------
    thresholds
        Sorted unique score boundaries (right edges of bins).
    values
        Calibrated probabilities (non-decreasing).
    """

    thresholds: tuple[float, ...]
    values: tuple[float, ...]


# ---------------------------------------------------------------------------
# calibration_curve
# ---------------------------------------------------------------------------


def calibration_curve(
    scores: Sequence[float],
    labels: Sequence[int],
    bins: int = 10,
) -> list[CalibrationBin]:
    """Compute an equal-width calibration (reliability) curve.

    The score range ``[0, 1]`` is split into *bins* equal-width
    intervals.  For each bin the mean predicted score and the positive
    rate (fraction of labels == 1) are computed.

    Parameters
    ----------
    scores
        Predicted scores (probabilities) in ``[0, 1]``.
    labels
        Binary ground-truth labels (0 or 1).
    bins
        Number of equal-width bins (default 10).

    Returns
    -------
    list[CalibrationBin]
        One entry per bin, including empty bins (where *count* = 0,
        *mean_score* = 0.0, *positive_rate* = 0.0).
    """
    if not scores or not labels:
        return []

    scores_arr = np.asarray(scores, dtype=float)
    labels_arr = np.asarray(labels, dtype=float)

    bin_edges = np.linspace(0.0, 1.0, bins + 1)
    # Use floor(score * bins) for robust bin assignment — avoids
    # floating-point edge sensitivity in np.digitize / np.linspace.
    bin_indices = np.clip(
        np.floor(scores_arr * bins).astype(np.intp), 0, bins - 1
    )

    result: list[CalibrationBin] = []
    for i in range(bins):
        mask = bin_indices == i
        count = int(np.sum(mask))
        if count == 0:
            result.append(
                CalibrationBin(
                    lower=float(bin_edges[i]),
                    upper=float(bin_edges[i + 1]),
                    mean_score=0.0,
                    positive_rate=0.0,
                    count=0,
                )
            )
        else:
            result.append(
                CalibrationBin(
                    lower=float(bin_edges[i]),
                    upper=float(bin_edges[i + 1]),
                    mean_score=float(np.mean(scores_arr[mask])),
                    positive_rate=float(np.mean(labels_arr[mask])),
                    count=count,
                )
            )

    return result


# ---------------------------------------------------------------------------
# fit_monotonic_calibrator  (Pool Adjacent Violators)
# ---------------------------------------------------------------------------


def fit_monotonic_calibrator(
    scores: Sequence[float],
    labels: Sequence[int],
) -> MonotonicCalibrator:
    """Fit an isotonic regression via the pool-adjacent-violators (PAV) algorithm.

    The input pairs ``(score, label)`` are sorted by score.  Starting
    from singleton pools, adjacent pools whose mean (positive rate)
    violates monotonicity (left > right) are repeatedly merged until
    the sequence of pool means is non-decreasing.

    Parameters
    ----------
    scores
        Predicted scores (any order).
    labels
        Binary ground-truth labels (0 or 1).

    Returns
    -------
    MonotonicCalibrator
        A frozen calibrator whose *thresholds* are the rightmost score
        of each pool and *values* are the pool's positive rate.
    """
    if not scores or not labels:
        return MonotonicCalibrator(thresholds=(), values=())

    # Sort by score
    sorted_pairs = sorted(zip(scores, labels), key=lambda x: x[0])

    # Each pool: total positive count, total count, rightmost score
    pool_sums: list[int] = []
    pool_counts: list[int] = []
    pool_scores: list[float] = []

    for score, label in sorted_pairs:
        pool_sums.append(label)
        pool_counts.append(1)
        pool_scores.append(score)

    # PAV: merge adjacent violators left->right, then backtrack
    i = 0
    while i < len(pool_sums) - 1:
        left_rate = pool_sums[i] / pool_counts[i]
        right_rate = pool_sums[i + 1] / pool_counts[i + 1]
        if left_rate > right_rate:
            # Merge pool i and i+1
            pool_sums[i] += pool_sums[i + 1]
            pool_counts[i] += pool_counts[i + 1]
            pool_scores[i] = pool_scores[i + 1]
            del pool_sums[i + 1]
            del pool_counts[i + 1]
            del pool_scores[i + 1]
            # Backtrack one step to check new violation
            if i > 0:
                i -= 1
        else:
            i += 1

    thresholds = tuple(pool_scores)
    values = tuple(s / c for s, c in zip(pool_sums, pool_counts))
    return MonotonicCalibrator(thresholds=thresholds, values=values)

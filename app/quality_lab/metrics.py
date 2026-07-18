"""NumPy-only quality metrics for VLM-generated GIFs (Phase 2 Task 4).

This module provides four metrics used in the experiment scorecard pipeline.
All implementations use only NumPy (no scikit-learn).
"""
from __future__ import annotations

from typing import Sequence

import numpy as np


# ---------------------------------------------------------------------------
# ndcg_at_k
# ---------------------------------------------------------------------------


def ndcg_at_k(relevances: Sequence[float], k: int) -> float:
    """Normalised Discounted Cumulative Gain at position *k*.

    Parameters
    ----------
    relevances
        Relevance scores in ranked order (higher = more relevant).
    k
        Position at which to compute NDCG.  When *k* exceeds
        ``len(relevances)`` the metric is computed over all available
        items.  When *k* <= 0 the result is 0.0.

    Returns
    -------
    float
        NDCG@k in ``[0, 1]`` (0.0 when IDCG is 0).
    """
    if k <= 0 or not relevances:
        return 0.0

    rel = np.asarray(relevances, dtype=float)
    actual_k = min(k, len(rel))

    # DCG: sum (2^rel - 1) / log2(position + 1) over first actual_k items
    gains = 2.0**rel[:actual_k] - 1.0
    positions = np.arange(1, actual_k + 1, dtype=float)
    dcg = float(np.sum(gains / np.log2(positions + 1.0)))

    # IDCG: top-actual_k gains from the ideal (descending) ordering of
    # ALL relevance scores — not just the first k items.
    ideal = np.sort(rel)[::-1][:actual_k]
    ideal_gains = 2.0**ideal - 1.0
    idcg = float(np.sum(ideal_gains / np.log2(positions + 1.0)))

    if idcg <= 0.0:
        return 0.0
    return dcg / idcg


# ---------------------------------------------------------------------------
# temporal_coverage
# ---------------------------------------------------------------------------


def temporal_coverage(
    intervals: Sequence[tuple[float, float]], duration: float
) -> float:
    """Fraction of *duration* covered by the union of *intervals*.

    Parameters
    ----------
    intervals
        Sequence of ``(start, end)`` tuples in seconds.  Intervals are
        merged when they overlap (any overlap including adjacency).
    duration
        Total time window in seconds.

    Returns
    -------
    float
        Coverage in ``[0, 1]``.  Returns 0.0 when *duration* is 0 or
        negative, or when *intervals* is empty.
    """
    if duration <= 0.0 or not intervals:
        return 0.0

    # Sort by start time
    sorted_iv = sorted(intervals, key=lambda x: x[0])

    # Merge overlapping intervals
    merged: list[tuple[float, float]] = []
    for start, end in sorted_iv:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            # Extend the last interval
            prev_start, prev_end = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end))

    covered = sum(end - start for start, end in merged)
    return min(covered / duration, 1.0)


# ---------------------------------------------------------------------------
# diversity_score
# ---------------------------------------------------------------------------


def diversity_score(vectors: np.ndarray) -> float:
    """Average pairwise cosine distance between *vectors*.

    Cosine distance is defined as ``1 - cosine_similarity``.  Pairs of
    vectors where at least one is all-zero are assigned distance 1.0.

    Parameters
    ----------
    vectors
        ``(N, D)`` array of float vectors.

    Returns
    -------
    float
        Average pairwise cosine distance.  Returns 0.0 when fewer than
        two vectors are provided.
    """
    n = vectors.shape[0]
    if n < 2:
        return 0.0

    # Compute all-pairs cosine distances
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    # Handle zero-norm vectors: set norm to 1 so dot/(norm_product) = 0
    norms_safe = np.where(norms == 0.0, 1.0, norms)
    normalized = vectors / norms_safe

    # Pairwise cosine similarity = dot product of normalized vectors
    cos_sim = normalized @ normalized.T  # (N, N)
    cos_sim = np.clip(cos_sim, -1.0, 1.0)

    # Extract upper-triangle (i < j) distances
    triu_indices = np.triu_indices(n, k=1)
    distances = 1.0 - cos_sim[triu_indices]

    return float(np.mean(distances))


# ---------------------------------------------------------------------------
# export_integrity
# ---------------------------------------------------------------------------


def export_integrity(attempted: int, succeeded: int) -> float:
    """Ratio of succeeded exports over attempted.

    Parameters
    ----------
    attempted
        Number of export attempts.
    succeeded
        Number of successful exports.

    Returns
    -------
    float
        ``succeeded / max(1, attempted)``.  Returns 1.0 when
        *attempted* is 0.
    """
    if attempted <= 0:
        return 1.0
    return succeeded / attempted

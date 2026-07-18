"""Tests for quality metrics (Phase 2 Task 4)."""
from __future__ import annotations

import numpy as np
import pytest

from app.quality_lab.metrics import (
    diversity_score,
    export_integrity,
    ndcg_at_k,
    temporal_coverage,
)


# ===================================================================
# ndcg_at_k
# ===================================================================


class TestNDCG:
    """``ndcg_at_k`` — Normalised Discounted Cumulative Gain @ k."""

    def test_perfect_ranking(self) -> None:
        """All relevant items at the top -> NDCG = 1.0."""
        relevances = [1.0, 1.0, 1.0]
        # DCG = (2^1-1)/log2(2) + (2^1-1)/log2(3) + (2^1-1)/log2(4)
        #     = 1/1 + 1/1.585 + 1/2 ≈ 2.1309
        # IDCG = same, NDCG = 1.0
        assert ndcg_at_k(relevances, 3) == pytest.approx(1.0, abs=1e-4)

    def test_imperfect_ranking(self) -> None:
        """Reversed relevance order yields NDCG < 1.0."""
        relevances = [1.0, 2.0, 3.0]
        # DCG = (2^1-1)/1 + (2^2-1)/log2(3) + (2^3-1)/log2(4)
        #     = 1 + 3/1.585 + 7/2 ≈ 6.392
        # IDCG = (2^3-1)/1 + (2^2-1)/log2(3) + (2^1-1)/log2(4)
        #      = 7 + 3/1.585 + 1/2 ≈ 9.392
        result = ndcg_at_k(relevances, 3)
        dcg = 1.0 + 3.0 / np.log2(3) + 7.0 / np.log2(4)
        idcg = 7.0 + 3.0 / np.log2(3) + 1.0 / np.log2(4)
        assert result == pytest.approx(dcg / idcg, abs=1e-4)

    def test_k_greater_than_length(self) -> None:
        """When k > len(relevances), compute over all available items."""
        relevances = [1.0, 0.0]
        result = ndcg_at_k(relevances, 5)
        # DCG = 1/1 + 0 = 1.0, IDCG = 1/1 + 0 = 1.0 -> 1.0
        assert result == pytest.approx(1.0, abs=1e-4)

    def test_k_less_than_length(self) -> None:
        """Only the first k items are considered."""
        relevances = [1.0, 0.0, 1.0]
        result = ndcg_at_k(relevances, 2)
        # DCG@2 = 1/1 + 0/log2(3) = 1.0
        # IDCG@2 = 1/1 + 1/log2(3) ≈ 1.6309
        expected = 1.0 / (1.0 + 1.0 / np.log2(3))
        assert result == pytest.approx(expected, abs=1e-4)

    def test_zero_k(self) -> None:
        """k=0 returns 0.0."""
        assert ndcg_at_k([1.0, 2.0, 3.0], 0) == 0.0

    def test_negative_k(self) -> None:
        """Negative k returns 0.0."""
        assert ndcg_at_k([1.0, 2.0, 3.0], -1) == 0.0

    def test_empty_relevances(self) -> None:
        """Empty list returns 0.0."""
        assert ndcg_at_k([], 5) == 0.0

    def test_all_zero_relevances(self) -> None:
        """All-zero relevances -> IDCG=0 -> returns 0.0."""
        assert ndcg_at_k([0.0, 0.0, 0.0], 3) == 0.0

    def test_single_item(self) -> None:
        """Single item always gives NDCG=1.0."""
        assert ndcg_at_k([5.0], 1) == pytest.approx(1.0, abs=1e-4)


# ===================================================================
# temporal_coverage
# ===================================================================


class TestTemporalCoverage:
    """``temporal_coverage`` — fraction of duration covered by interval union."""

    def test_non_overlapping(self) -> None:
        """Disjoint intervals sum their lengths."""
        intervals = [(0.0, 2.0), (3.0, 5.0)]
        # Covered = 2 + 2 = 4, duration = 10
        assert temporal_coverage(intervals, 10.0) == pytest.approx(0.4, abs=1e-6)

    def test_overlapping(self) -> None:
        """Overlapping intervals are merged (union)."""
        intervals = [(0.0, 5.0), (2.0, 7.0)]
        # Union = (0, 7), covered = 7
        assert temporal_coverage(intervals, 10.0) == pytest.approx(0.7, abs=1e-6)

    def test_fully_covered(self) -> None:
        """Single interval matching duration."""
        assert temporal_coverage([(0.0, 10.0)], 10.0) == pytest.approx(1.0, abs=1e-6)

    def test_empty_intervals(self) -> None:
        """No intervals returns 0.0."""
        assert temporal_coverage([], 10.0) == 0.0

    def test_zero_duration(self) -> None:
        """Zero duration returns 0.0."""
        assert temporal_coverage([(0.0, 5.0)], 0.0) == 0.0

    def test_negative_duration(self) -> None:
        """Negative duration returns 0.0."""
        assert temporal_coverage([(0.0, 5.0)], -1.0) == 0.0

    def test_capped_at_one(self) -> None:
        """Coverage never exceeds 1.0."""
        assert temporal_coverage([(0.0, 15.0)], 10.0) == pytest.approx(1.0, abs=1e-6)

    def test_nested_intervals(self) -> None:
        """A fully-contained interval does not double count."""
        intervals = [(1.0, 4.0), (2.0, 3.0)]
        assert temporal_coverage(intervals, 10.0) == pytest.approx(0.3, abs=1e-6)

    def test_adjacent_intervals(self) -> None:
        """Touching intervals (end == start) do not overlap."""
        intervals = [(0.0, 2.0), (2.0, 5.0)]
        assert temporal_coverage(intervals, 10.0) == pytest.approx(0.5, abs=1e-6)

    def test_unsorted_intervals(self) -> None:
        """Intervals need not be pre-sorted."""
        intervals = [(5.0, 8.0), (0.0, 3.0)]
        assert temporal_coverage(intervals, 10.0) == pytest.approx(0.6, abs=1e-6)


# ===================================================================
# diversity_score
# ===================================================================


class TestDiversityScore:
    """``diversity_score`` — average pairwise cosine distance."""

    def test_two_orthogonal_vectors(self) -> None:
        """Orthogonal vectors have distance 1.0."""
        vectors = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=float)
        # cos_sim = 0, dist = 1
        assert diversity_score(vectors) == pytest.approx(1.0, abs=1e-6)

    def test_two_identical_vectors(self) -> None:
        """Identical vectors have distance 0.0."""
        vectors = np.array([[1.0, 0.0], [1.0, 0.0]], dtype=float)
        assert diversity_score(vectors) == pytest.approx(0.0, abs=1e-6)

    def test_three_vectors(self) -> None:
        """Hand-computable 3-vector case."""
        vectors = np.array(
            [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]], dtype=float
        )
        result = diversity_score(vectors)
        # Pairs:
        # (0,1): cos_sim = 0,     dist = 1.0
        # (0,2): cos_sim = 1/√2,  dist = 1 - 1/√2
        # (1,2): cos_sim = 1/√2,  dist = 1 - 1/√2
        d = 1.0 - 1.0 / np.sqrt(2.0)
        expected = (1.0 + d + d) / 3.0
        assert result == pytest.approx(expected, abs=1e-6)

    def test_single_vector(self) -> None:
        """Single vector has no pairs -> 0.0."""
        vectors = np.array([[1.0, 0.0]], dtype=float)
        assert diversity_score(vectors) == 0.0

    def test_empty_vectors(self) -> None:
        """Empty array returns 0.0."""
        vectors = np.empty((0, 3), dtype=float)
        assert diversity_score(vectors) == 0.0

    def test_deterministic(self) -> None:
        """Same input -> same output (no sampling randomness)."""
        vectors = np.random.RandomState(42).rand(100, 50)
        r1 = diversity_score(vectors)
        r2 = diversity_score(vectors)
        assert r1 == pytest.approx(r2, abs=1e-10)

    def test_zero_vectors(self) -> None:
        """Zero vector has no direction -> cosine distance = 1."""
        vectors = np.array([[0.0, 0.0], [1.0, 0.0]], dtype=float)
        assert diversity_score(vectors) == pytest.approx(1.0, abs=1e-6)

    def test_opposite_vectors(self) -> None:
        """Opposite vectors have distance 2.0."""
        vectors = np.array([[1.0, 0.0], [-1.0, 0.0]], dtype=float)
        # cos_sim = -1, dist = 2
        assert diversity_score(vectors) == pytest.approx(2.0, abs=1e-6)


# ===================================================================
# export_integrity
# ===================================================================


class TestExportIntegrity:
    """``export_integrity`` — succeeded / max(1, attempted)."""

    def test_all_succeeded(self) -> None:
        assert export_integrity(10, 10) == pytest.approx(1.0, abs=1e-6)

    def test_partial_failures(self) -> None:
        assert export_integrity(10, 7) == pytest.approx(0.7, abs=1e-6)

    def test_all_failed(self) -> None:
        assert export_integrity(5, 0) == pytest.approx(0.0, abs=1e-6)

    def test_no_attempts(self) -> None:
        """When attempted=0, return 1.0."""
        assert export_integrity(0, 0) == pytest.approx(1.0, abs=1e-6)

    def test_no_attempts_with_zero_succeeded(self) -> None:
        """identical to test_no_attempts, explicit call."""
        assert export_integrity(0, 0) == 1.0

    def test_large_values(self) -> None:
        """Large integer values compute correctly."""
        assert export_integrity(1000, 997) == pytest.approx(0.997, abs=1e-6)

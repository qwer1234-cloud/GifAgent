"""W4 kill tests for app.services.vector_math (module has no other tests).

Pins the documented behaviour of the shared vector helpers, then kills the
cosmic-ray survivors recorded in
``<sprint-root>/w4/mutation_scores/cosmic-ray/vector_math.json``:

- ``l2_normalize``: unit-norm output for non-zero vectors, zero vector
  returned unchanged, float32 flattening.
- ``is_unit_vector``: zero vector is unit by definition, tolerance window.
- ``vector_to_blob`` / ``blob_to_vector``: L2-normalizing roundtrip and the
  ``embedding_dim`` mismatch error.
- ``max_cosine``: single-centroid, packed multi-centroid, empty blob and
  non-multiple blob behaviours.
- ``weighted_kmeans``: weighted k=1 centroid, k clamping, seed determinism,
  validation errors.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.services.vector_math import (
    blob_to_vector,
    is_unit_vector,
    l2_normalize,
    max_cosine,
    vector_to_blob,
    weighted_kmeans,
)


# ── l2_normalize ───────────────────────────────────────────────────────────


def test_l2_normalize_produces_unit_float32_vector():
    out = l2_normalize([3.0, 4.0])
    assert out.dtype == np.float32
    assert out.shape == (2,)
    assert out == pytest.approx([0.6, 0.8], abs=1e-6)
    assert float(np.linalg.norm(out)) == pytest.approx(1.0, abs=1e-6)


def test_l2_normalize_zero_vector_is_returned_unchanged():
    out = l2_normalize([0.0, 0.0, 0.0])
    assert np.all(out == 0.0)


def test_l2_normalize_flattens_and_handles_negatives():
    out = l2_normalize([[0.0, -2.0], [2.0, 0.0]])
    assert out.shape == (4,)
    assert out == pytest.approx([0.0, -0.70710678, 0.70710678, 0.0], abs=1e-6)


# ── is_unit_vector ─────────────────────────────────────────────────────────


def test_is_unit_vector_zero_vector_is_unit_by_definition():
    assert is_unit_vector([0.0, 0.0]) is True


def test_is_unit_vector_tolerance_window():
    assert is_unit_vector([0.6, 0.8]) is True
    assert is_unit_vector([3.0, 4.0]) is False
    slightly_off = np.array([1.0 + 1e-6, 0.0], dtype=np.float32)
    assert is_unit_vector(slightly_off) is True
    assert is_unit_vector([1.1, 0.0]) is False


# ── blob roundtrip ─────────────────────────────────────────────────────────


def test_blob_roundtrip_normalizes_before_serialization():
    blob = vector_to_blob([3.0, 4.0], embedding_dim=2)
    assert isinstance(blob, bytes)
    assert len(blob) == 8  # two float32
    assert blob_to_vector(blob) == pytest.approx([0.6, 0.8], abs=1e-6)


def test_vector_to_blob_rejects_dimension_mismatch():
    with pytest.raises(ValueError):
        vector_to_blob([1.0, 2.0, 3.0], embedding_dim=2)


# ── max_cosine ─────────────────────────────────────────────────────────────


def test_max_cosine_single_centroid():
    blob = vector_to_blob([1.0, 0.0], embedding_dim=2)
    assert max_cosine([1.0, 0.0], blob) == pytest.approx(1.0, abs=1e-6)
    assert max_cosine([0.0, 1.0], blob) == pytest.approx(0.0, abs=1e-6)


def test_max_cosine_packed_prototypes_returns_max():
    packed = (
        vector_to_blob([1.0, 0.0], embedding_dim=2)
        + vector_to_blob([0.0, 1.0], embedding_dim=2)
    )
    assert max_cosine([0.0, 1.0], packed) == pytest.approx(1.0, abs=1e-6)
    assert max_cosine([1.0, 0.0], packed) == pytest.approx(1.0, abs=1e-6)


def test_max_cosine_empty_blob_is_zero():
    assert max_cosine([1.0, 0.0], b"") == 0.0


def test_max_cosine_non_multiple_blob_uses_first_slice():
    blob = vector_to_blob([1.0, 0.0], embedding_dim=2) + b"\x00\x00\x00\x00"
    assert max_cosine([1.0, 0.0], blob) == pytest.approx(1.0, abs=1e-6)


# ── weighted_kmeans ────────────────────────────────────────────────────────


def test_weighted_kmeans_k_one_is_the_weighted_average():
    vectors = np.array([[0.0, 0.0], [2.0, 0.0]], dtype=np.float32)
    weights = np.array([1.0, 3.0], dtype=np.float32)
    out = weighted_kmeans(vectors, weights, 1)
    assert out.shape == (1, 2)
    assert out[0] == pytest.approx([1.5, 0.0], abs=1e-6)


def test_weighted_kmeans_clamps_k_to_sample_count():
    vectors = np.array([[0.0], [10.0]], dtype=np.float32)
    weights = np.array([1.0, 1.0], dtype=np.float32)
    out = weighted_kmeans(vectors, weights, 99)
    assert out.shape == (2, 1)


def test_weighted_kmeans_is_seed_deterministic():
    rng = np.random.default_rng(7)
    vectors = rng.standard_normal((12, 3)).astype(np.float32)
    weights = rng.random(12).astype(np.float32) + 0.1
    a = weighted_kmeans(vectors, weights, 3, seed=11)
    b = weighted_kmeans(vectors, weights, 3, seed=11)
    assert np.allclose(a, b)


def test_weighted_kmeans_validation_errors():
    vectors = np.array([[0.0], [1.0]], dtype=np.float32)
    with pytest.raises(ValueError):
        weighted_kmeans(np.zeros((0, 2), dtype=np.float32), np.zeros(0), 1)
    with pytest.raises(ValueError):
        weighted_kmeans(vectors, np.array([1.0, 2.0, 3.0], dtype=np.float32), 1)
    with pytest.raises(ValueError):
        weighted_kmeans(vectors, np.zeros(2, dtype=np.float32), 1)


# ── extended kills: normalisation and blob edges ───────────────────────────


def test_l2_normalize_sub_unit_vectors_are_still_scaled():
    # norm 0.5 <= 1: the `norm > 0` (not `> 1`) guard must divide.
    out = l2_normalize([0.3, 0.4])
    assert out == pytest.approx([0.6, 0.8], abs=1e-6)


def test_l2_normalize_nan_input_is_returned_unchanged():
    # norm is NaN: `norm > 0` is False, so the NaN vector is returned as-is
    # (an `!= 0` mutant would divide by NaN and poison every component).
    out = l2_normalize([np.nan, 1.0])
    assert np.isnan(out[0])
    assert out[1] == pytest.approx(1.0, abs=1e-6)


def test_is_unit_vector_tolerance_is_inclusive():
    # abs(norm - 1) == tol must count as unit (`<=`, not `<`).
    assert is_unit_vector([1.5, 0.0], tol=0.5) is True
    assert is_unit_vector([1.6, 0.0], tol=0.5) is False


def test_vector_to_blob_rejects_undersized_vectors():
    with pytest.raises(ValueError):
        vector_to_blob([1.0], embedding_dim=2)


def test_max_cosine_single_dimension_vectors():
    blob = vector_to_blob([5.0], embedding_dim=1)
    assert max_cosine([3.0], blob) == pytest.approx(1.0, abs=1e-6)


def test_max_cosine_empty_query_returns_zero():
    assert max_cosine([], vector_to_blob([1.0, 0.0], embedding_dim=2)) == 0.0


def test_max_cosine_packed_blob_non_multiple_uses_first_slice():
    # dim=5 query against an 8-float blob: 8 % 5 != 0 -> first-slice dot.
    blob = vector_to_blob([1.0, 0, 0, 0, 0], embedding_dim=5) + vector_to_blob(
        [0.0, 1, 0, 0, 0], embedding_dim=5
    )[:12]  # 8 floats total
    assert len(blob) == 32  # 8 * 4 bytes
    expected = float(np.dot(l2_normalize([1.0, 2, 0, 0, 0]), l2_normalize(np.frombuffer(blob[:20], dtype=np.float32))))
    assert max_cosine([1.0, 2, 0, 0, 0], blob) == pytest.approx(expected, abs=1e-6)


# ── weighted_kmeans: reference-algorithm comparison and guards ─────────────


def _reference_kmeans(vectors, weights, k, seed, n_iter=25):
    """Independent mirror of the documented weighted k-means semantics."""
    n, dim = vectors.shape
    kk = max(1, min(int(k), n))
    stacked = vectors.astype(np.float64)
    w = weights.astype(np.float64)
    if kk == 1:
        return np.average(stacked, axis=0, weights=w).astype(np.float32).reshape(1, dim)
    rng = np.random.default_rng(seed)
    centers = np.empty((kk, dim), dtype=np.float64)
    probs = w / w.sum()
    centers[0] = stacked[int(rng.choice(n, p=probs))]
    for c in range(1, kk):
        delta = stacked[:, None, :] - centers[None, :c, :]
        dists = np.min(np.sum(delta * delta, axis=2), axis=1)
        weighted = np.maximum(dists, 0.0) * w
        total = float(weighted.sum())
        if total <= 0:
            idx = int(rng.integers(0, n))
        else:
            idx = int(rng.choice(n, p=weighted / total))
        centers[c] = stacked[idx]
    for _ in range(n_iter):
        delta = stacked[:, None, :] - centers[None, :, :]
        labels = np.sum(delta * delta, axis=2).argmin(axis=1)
        new_centers = centers.copy()
        for c in range(kk):
            mask = labels == c
            if not np.any(mask):
                continue
            new_centers[c] = np.average(stacked[mask], axis=0, weights=w[mask])
        if np.allclose(new_centers, centers):
            break
        centers = new_centers
    return centers.astype(np.float32)


def test_weighted_kmeans_matches_reference_semantics():
    vectors = np.array([[0.0], [1.0], [10.0], [11.0], [30.0]], dtype=np.float32)
    weights = np.array([3.0, 1.0, 1.0, 3.0, 2.0], dtype=np.float32)
    out = weighted_kmeans(vectors, weights, 2, seed=7)
    expected = _reference_kmeans(vectors, weights, 2, seed=7)
    assert out.shape == expected.shape == (2, 1)
    assert np.allclose(out, expected)
    # pinned clustering for this fixture: {0,1,10,11} and {30}
    rows = {round(float(r[0]), 3) for r in out}
    assert rows == {5.5, 30.0}


def test_weighted_kmeans_identical_points_take_the_random_fallback():
    vectors = np.array([[5.0], [5.0]], dtype=np.float32)
    weights = np.array([1.0, 1.0], dtype=np.float32)
    out = weighted_kmeans(vectors, weights, 2, seed=3)
    assert out.shape == (2, 1)
    assert np.allclose(out, 5.0)


def test_weighted_kmeans_clamps_k_zero_to_one():
    vectors = np.array([[0.0], [2.0]], dtype=np.float32)
    weights = np.array([1.0, 1.0], dtype=np.float32)
    out = weighted_kmeans(vectors, weights, 0)
    assert out.shape == (1, 1)


def test_weighted_kmeans_error_messages_are_stable():
    vectors = np.array([[0.0], [1.0]], dtype=np.float32)
    with pytest.raises(ValueError, match="non-empty 2-D"):
        weighted_kmeans(np.array([1.0, 2.0]), np.array([1.0, 1.0]), 1)
    with pytest.raises(ValueError, match="1-D array"):
        weighted_kmeans(vectors, np.array([1.0, 2.0, 4.0]), 1)
    with pytest.raises(ValueError, match="must sum to a positive value"):
        weighted_kmeans(vectors, np.array([1.0, -2.0]), 1)
    with pytest.raises(ValueError, match="must sum to a positive value"):
        weighted_kmeans(vectors, np.zeros(2), 1)

"""Property tests for the taste-map SVD projection (W5 Task 6).

Locked against ``app.services.taste_map.project_taste_map``:

- output length equals ``n``; ``n == 0`` returns ``[]``; ``n == 1`` places
  the single point at the origin;
- every projected coordinate is finite;
- translation invariance: projecting ``X`` and ``X + c`` yields the same
  pairwise-distance matrix (SVD sign flips and rotations cannot change
  inter-point distances), ``atol=1e-4``.  Only ``n >= 3`` is tested.

The plan deliberately does NOT lock per-coordinate equality: SVD sign/rotation
ambiguity makes per-point comparison non-unique.  Degenerate point clouds
(two nearly tied trailing singular values) are filtered with ``assume`` so
the projection subspace itself is stable; this is generator narrowing, not a
product-code change.
"""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import assume, given, seed, settings, strategies as st

from app.services.taste_map import project_taste_map

SEED = 20260828


@st.composite
def point_clouds(draw, min_points: int, max_points: int):
    """A finite (n, d) cloud plus its ids and a translation vector."""
    n = draw(st.integers(min_points, max_points))
    d = draw(st.integers(2, 6))
    rows = draw(
        st.lists(
            st.lists(
                st.floats(-10.0, 10.0, allow_nan=False, allow_infinity=False),
                min_size=d,
                max_size=d,
            ),
            min_size=n,
            max_size=n,
        )
    )
    shift = draw(
        st.lists(
            st.floats(-10.0, 10.0, allow_nan=False, allow_infinity=False),
            min_size=d,
            max_size=d,
        )
    )
    return np.asarray(rows, dtype=np.float64), np.asarray(shift, dtype=np.float64)


def _pairwise_distances(points) -> np.ndarray:
    coords = np.asarray([[point.x, point.y] for point in points], dtype=np.float64)
    delta = coords[:, None, :] - coords[None, :, :]
    return np.sqrt(np.sum(delta * delta, axis=-1))


def _top_subspace_is_stable(vectors: np.ndarray) -> bool:
    """Reject clouds whose 2nd/3rd singular values are (near-)tied.

    When sigma_2 ~ sigma_3 the choice of projected 2-D subspace is not
    unique even between numerically identical inputs, so the distance-
    invariance property would be asserted against an ill-defined projection.
    """
    centred = vectors - vectors.mean(axis=0, keepdims=True)
    singular = np.linalg.svd(centred, compute_uv=False)
    if singular.size < 3:
        return True
    return bool(singular[1] - singular[2] > 1e-6 * max(float(singular[0]), 1.0))


@given(data=point_clouds(min_points=3, max_points=12))
@settings(deadline=None)
@seed(SEED)
def test_projection_shape_ids_and_finite_coords(data):
    vectors, _ = data
    ids = [f"cand-{i}" for i in range(vectors.shape[0])]

    points = project_taste_map(vectors, ids)

    assert len(points) == vectors.shape[0]
    assert [point.candidate_id for point in points] == ids
    for point in points:
        assert np.isfinite(point.x) and np.isfinite(point.y)


@given(data=point_clouds(min_points=3, max_points=12))
@settings(deadline=None)
@seed(SEED)
def test_pairwise_distances_are_translation_invariant(data):
    vectors, shift = data
    assume(_top_subspace_is_stable(vectors))
    ids = [f"cand-{i}" for i in range(vectors.shape[0])]

    base = project_taste_map(vectors, ids)
    shifted = project_taste_map(vectors + shift, ids)

    distances_base = _pairwise_distances(base)
    distances_shifted = _pairwise_distances(shifted)
    assert np.allclose(distances_base, distances_shifted, atol=1e-4, rtol=0.0)


def test_empty_cloud_returns_empty_list():
    assert project_taste_map(np.empty((0, 3), dtype=np.float64), []) == []


def test_single_point_is_pinned_to_origin():
    points = project_taste_map(np.array([[1.0, 2.0, 3.0]]), ["only"])

    assert len(points) == 1
    assert points[0].candidate_id == "only"
    assert points[0].x == 0.0
    assert points[0].y == 0.0


def test_ids_length_mismatch_raises_value_error():
    with pytest.raises(ValueError):
        project_taste_map(np.zeros((2, 3)), ["a"])
    with pytest.raises(ValueError):
        project_taste_map(np.zeros((2, 3)), ["a", "b", "c"])

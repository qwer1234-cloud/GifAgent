"""W4 kill tests for taste_map mutation survivors.

Targets survivors from the cosmic-ray run recorded in
``<sprint-root>/w4/mutation_scores/cosmic-ray/taste_map.json``:

- ``TastePoint`` immutability (``frozen=True``).
- ``candidate_ids`` length validation (``!=`` vs ``<`` / ``>``).
- empty-input degrade path (``n == 0``).
- sign stabilisation comparison (``< 0`` vs ``< 1`` / ``< -1``) — the raw
  PC coordinates of the fixture have magnitudes far below 1 and a negative
  max-|loading| on PC2, so both mutants flip/no-flip differently.
- ``x`` taken from column 0 (not column 1 / column -1).

Equivalent mutants (keyword-only marker, ``is not`` on small ints,
``n <= 1`` after the ``n == 0`` guard, ``candidate_ids[-1]`` on a
single-element list, ``keepdims`` broadcasting, ``full_matrices`` with the
``[:2]`` slice, ``Vt[:3]`` extra columns unread, unused ``seed``) are
registered in w4-surviving.md.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from app.services.taste_map import TastePoint, project_taste_map


def test_taste_point_is_frozen():
    point = TastePoint(candidate_id="a", x=0.0, y=0.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        point.x = 5.0


def test_candidate_ids_length_mismatch_raises_in_both_directions():
    vectors = np.zeros((2, 3), dtype=np.float32)
    with pytest.raises(ValueError):
        project_taste_map(vectors, ["only", "three", "ids"])  # len(ids) > n
    with pytest.raises(ValueError):
        project_taste_map(vectors, ["one"])  # len(ids) < n


def test_empty_inputs_return_empty_projection():
    assert project_taste_map(np.zeros((0, 3), dtype=np.float32), []) == []


def test_single_vector_is_pinned_to_origin():
    pts = project_taste_map(np.array([[0.7, 0.2, 0.1]], dtype=np.float32), ["solo"])
    assert len(pts) == 1
    assert pts[0].candidate_id == "solo"
    assert pts[0].x == 0.0 and pts[0].y == 0.0


def test_projection_coordinates_and_sign_stabilisation():
    vectors = np.array([[0.1, 0.0], [-0.05, 0.0], [0.0, 0.2]], dtype=np.float32)
    pts = project_taste_map(vectors, ["a", "b", "c"])
    expected = [("a", -0.0825, -0.0677), ("b", -0.0514, 0.0791), ("c", 0.1339, -0.0114)]
    for point, (cid, x, y) in zip(pts, expected):
        assert point.candidate_id == cid
        assert point.x == pytest.approx(x, abs=1e-4)
        assert point.y == pytest.approx(y, abs=1e-4)
    # PC2's raw max-|loading| entry is negative; stabilisation must have
    # flipped its sign so the reported y for "b" is positive.
    assert pts[1].y > 0.0

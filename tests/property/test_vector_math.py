"""Property tests for shared L2/vector-blob math (W5 Task 8, conditional).

Included because the canonical manifest pins ``vector_math_in_baseline: true``
(sprint_base_sha a6c9fb0a0dcbe5871efeaa846b6937df2efc7e9e).

Single-vector properties (``vector_to_blob`` L2-normalizes *before*
serializing):

- for finite, non-empty, non-zero vectors,
  ``blob_to_vector(vector_to_blob(v, embedding_dim=len(v)))`` equals
  ``l2_normalize(v)`` within ``atol=1e-6``;
- an already-normalized vector round-trips identically at the same tolerance;
- ``l2_normalize`` is idempotent;
- zero vectors and NaN/Inf inputs are pinned to today's behavior (zero vector
  returned unchanged; NaN preserved; Inf collapses to NaN/0) — behavior is
  pinned, semantics are not guessed.

Packed-prototype properties (which never go through ``vector_to_blob``):

- a ``k*dim`` float32 blob decodes via ``blob_to_vector`` and reshapes row-
  for-row identical to the prototypes;
- ``max_cosine(candidate, packed_blob)`` equals the maximum per-prototype
  cosine;
- ``vector_to_blob`` on a packed-length vector raises the embedding_dim
  mismatch ``ValueError`` (negative property);
- the non-multiple truncation path of ``max_cosine`` (first chunk) is pinned.
"""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import assume, given, seed, settings, strategies as st

from app.services.vector_math import (
    blob_to_vector,
    l2_normalize,
    max_cosine,
    vector_to_blob,
)

SEED = 20260828

FINITE_ENTRIES = st.floats(
    -10.0, 10.0, allow_nan=False, allow_infinity=False
)


def _as_float32(values: list[float]) -> np.ndarray:
    return np.asarray(values, dtype=np.float32)


def _assert_close(actual: np.ndarray, expected: np.ndarray) -> None:
    assert actual.shape == expected.shape
    assert np.allclose(actual, expected, atol=1e-6, rtol=0.0)


@st.composite
def non_zero_vectors(draw):
    values = draw(st.lists(FINITE_ENTRIES, min_size=1, max_size=8))
    vector = _as_float32(values)
    assume(float(np.linalg.norm(vector)) > 0.0)
    return vector


@st.composite
def packed_scenarios(draw):
    """k prototypes of dim d plus a matching-dimension candidate vector."""
    k = draw(st.integers(1, 5))
    dim = draw(st.integers(1, 8))
    rows = draw(
        st.lists(
            st.lists(FINITE_ENTRIES, min_size=dim, max_size=dim),
            min_size=k,
            max_size=k,
        )
    )
    candidate = draw(st.lists(FINITE_ENTRIES, min_size=dim, max_size=dim))
    return np.asarray(rows, dtype=np.float32), _as_float32(candidate)


# ── single-vector round trip ─────────────────────────────────────────────────


@given(vector=non_zero_vectors())
@settings(deadline=None)
@seed(SEED)
def test_roundtrip_equals_l2_normalize(vector):
    """Finite non-empty non-zero vectors survive blob round-trip normalized."""
    blob = vector_to_blob(vector, embedding_dim=vector.size)
    restored = blob_to_vector(blob)

    _assert_close(restored, l2_normalize(vector))


@given(vector=non_zero_vectors())
@settings(deadline=None)
@seed(SEED)
def test_normalized_vector_roundtrip_is_identity(vector):
    """An already-normalized vector round-trips to itself."""
    unit = l2_normalize(vector)

    restored = blob_to_vector(vector_to_blob(unit, embedding_dim=unit.size))

    _assert_close(restored, unit)


@given(vector=non_zero_vectors())
@settings(deadline=None)
@seed(SEED)
def test_l2_normalize_is_idempotent(vector):
    """``l2_normalize`` applied twice equals applying it once."""
    once = l2_normalize(vector)
    twice = l2_normalize(once)

    _assert_close(twice, once)


# ── pinned degenerate inputs (current behavior, no semantic guesses) ────────


def test_zero_vector_is_returned_unchanged_and_roundtrips_to_zero():
    zeros = np.zeros(4, dtype=np.float32)

    assert np.array_equal(l2_normalize(zeros), zeros)

    restored = blob_to_vector(vector_to_blob(zeros, embedding_dim=4))
    assert np.array_equal(restored, zeros)


def test_nan_entries_propagate_through_normalize_and_blob():
    """NaN input: l2_normalize returns it unchanged; the blob keeps NaN."""
    vector = _as_float32([1.0, np.nan])

    normalized = l2_normalize(vector)
    assert normalized[0] == np.float32(1.0)
    assert np.isnan(normalized[1])

    # vector_to_blob does not raise on NaN today; it serializes the
    # unchanged vector and NaN survives the round-trip.
    blob = vector_to_blob(vector, embedding_dim=2)
    restored = blob_to_vector(blob)
    assert restored[0] == np.float32(1.0)
    assert np.isnan(restored[1])


def test_inf_entries_collapse_to_nan_and_zero():
    """Inf input: norm is Inf, so division maps Inf->NaN and finite->0."""
    vector = _as_float32([np.inf, 1.0])

    # np.errstate only silences numpy's own division warning inside this
    # test; the pinned behavior below is the production code's output.
    with np.errstate(invalid="ignore"):
        normalized = l2_normalize(vector)
    assert np.isnan(normalized[0])
    assert normalized[1] == np.float32(0.0)


# ── packed prototypes (never through vector_to_blob) ─────────────────────────


@given(scenario=packed_scenarios())
@settings(deadline=None)
@seed(SEED)
def test_packed_blob_roundtrip_and_max_cosine(scenario):
    prototypes, candidate = scenario
    k, dim = prototypes.shape
    blob = prototypes.tobytes()

    decoded = blob_to_vector(blob).reshape(k, dim)
    assert np.array_equal(decoded, prototypes)

    expected = max(
        float(np.dot(l2_normalize(candidate), l2_normalize(row)))
        for row in decoded
    )
    result = max_cosine(candidate, blob)
    assert result == pytest.approx(expected, abs=1e-6)


@given(scenario=packed_scenarios())
@settings(deadline=None)
@seed(SEED)
def test_vector_to_blob_rejects_packed_length(scenario):
    """Negative property: packed k*dim vectors hit the mismatch guard."""
    prototypes, _ = scenario
    k, dim = prototypes.shape
    assume(k >= 2)

    with pytest.raises(ValueError, match="embedding_dim mismatch"):
        vector_to_blob(prototypes.reshape(-1), embedding_dim=dim)


def test_max_cosine_truncates_non_multiple_packed_blob():
    """Pin: a blob whose size is not a multiple of dim uses its first chunk."""
    blob = _as_float32([3.0, 4.0, 5.0]).tobytes()

    assert max_cosine(_as_float32([3.0, 4.0]), blob) == pytest.approx(
        1.0, abs=1e-6
    )
    assert max_cosine(_as_float32([1.0, 0.0]), blob) == pytest.approx(
        0.6, abs=1e-6
    )


def test_max_cosine_empty_blob_is_pinned_to_zero():
    assert max_cosine(_as_float32([1.0, 2.0]), b"") == 0.0

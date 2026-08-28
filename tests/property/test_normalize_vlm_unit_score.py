"""Property tests for ``normalize_vlm_unit_score`` (W5 Task 2).

The scorer maps VLM JSON numbers onto ``[0, 1]`` with a dual-track rule:
integer ``0-100`` is percent-scaled (``n / 100``), while legacy unit floats
pass through.  These properties freeze that mapping; nothing in the
implementation is modified.
"""

from __future__ import annotations

import math

from hypothesis import given, seed, settings, strategies as st

from app.services.export_ranking import normalize_vlm_unit_score

SEED = 20260828


@given(n=st.integers(0, 100))
@settings(deadline=None)
@seed(SEED)
def test_integer_scores_map_onto_n_over_100(n):
    """Integer track: every int 0-100 maps to exactly ``n / 100``."""
    result = normalize_vlm_unit_score(n)

    assert result is not None
    assert 0.0 <= result <= 1.0
    assert result == n / 100.0


@given(x=st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False))
@settings(deadline=None)
@seed(SEED)
def test_unit_floats_pass_through_unchanged(x):
    """Float track: unit floats in [0, 1] (including 1.0) pass through."""
    result = normalize_vlm_unit_score(x)

    assert result is not None
    assert 0.0 <= result <= 1.0
    assert result == x


@given(
    value=st.one_of(
        st.integers(-100, 1000),
        st.floats(-100.0, 1000.0, allow_nan=False, allow_infinity=False),
    )
)
@settings(deadline=None)
@seed(SEED)
def test_output_when_not_none_is_always_unit_and_finite(value):
    """Whenever a value is returned, it is finite and inside [0, 1]."""
    result = normalize_vlm_unit_score(value)

    if result is not None:
        assert math.isfinite(result)
        assert 0.0 <= result <= 1.0


@given(
    value=st.one_of(
        st.integers(max_value=-1),
        st.floats(
            -100.0, -1e-9, allow_nan=False, allow_infinity=False
        ),
    )
)
@settings(deadline=None)
@seed(SEED)
def test_negative_values_are_rejected(value):
    """Negative integers and negative floats never produce a score."""
    assert normalize_vlm_unit_score(value) is None


def test_rejected_inputs_are_pinned_to_none():
    """Pin today's rejections: bool, non-numbers, nan/inf, over-range."""
    assert normalize_vlm_unit_score(True) is None
    assert normalize_vlm_unit_score(False) is None
    assert normalize_vlm_unit_score(None) is None
    assert normalize_vlm_unit_score("60") is None
    assert normalize_vlm_unit_score(float("nan")) is None
    assert normalize_vlm_unit_score(float("inf")) is None
    assert normalize_vlm_unit_score(float("-inf")) is None
    assert normalize_vlm_unit_score(101) is None
    assert normalize_vlm_unit_score(1.1) is None
    assert normalize_vlm_unit_score(1.5) is None


def test_dual_track_boundary_is_pinned():
    """JSON ``1`` is 1/100 while JSON ``1.0`` stays the unit maximum."""
    assert normalize_vlm_unit_score(0) == 0.0
    assert normalize_vlm_unit_score(1) == 0.01
    assert normalize_vlm_unit_score(100) == 1.0
    assert normalize_vlm_unit_score(1.0) == 1.0
    assert normalize_vlm_unit_score(0.0) == 0.0
    # Whole floats in [2, 100] take the percent track as well.
    assert normalize_vlm_unit_score(2.0) == 0.02
    assert normalize_vlm_unit_score(100.0) == 1.0

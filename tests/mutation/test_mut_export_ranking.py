"""W4 kill tests for export_ranking mutation survivors.

Targets survivors from the cosmic-ray run recorded in
``<sprint-root>/w4/mutation_scores/cosmic-ray/export_ranking.json``:

- ``normalize_vlm_unit_score`` boundary mutants: legacy float 2.0/100.0
  acceptance, negative rejection, NaN handling, non-integral over-range
  rejection.
- ``sex_act_score`` default 0.0 mutants (non-dict payload / missing or
  invalid ``sex_act``).
- ``adult_priority_score`` / ``adult_export_score`` blend arithmetic and
  missing-worthiness default mutants.
- ``cinematic_score_from_assessment`` string-compare, identity-compare and
  ``continue``->``break`` mutants in the evidence loop.
- ``rank_clips_for_export`` exception-fallback mutant.

Equivalent mutants (annotation unions via ``from __future__ import
annotations``, ``*`` -> ``/`` keyword-marker, ``raw <= floor(raw)`` on
non-negative input, int-0 boundary covered by the ``raw <= 1.0`` fallback)
are registered in ``docs/reports/2026-08-token-sprint/w4-surviving.md``.
"""

from __future__ import annotations

import math

import pytest

from app.services.export_ranking import (
    adult_export_score,
    adult_priority_score,
    cinematic_score_from_assessment,
    normalize_vlm_unit_score,
    rank_clips_for_export,
    sex_act_score,
)


# ── normalize_vlm_unit_score boundaries ────────────────────────────────────


def test_normalize_rejects_non_integral_values_over_one():
    assert normalize_vlm_unit_score(2.5) is None
    assert normalize_vlm_unit_score(1.5) is None


def test_normalize_accepts_legacy_float_boundaries_two_and_hundred():
    # 2.0 and 100.0 as FLOATS take the legacy-integral branch; mutants
    # ``2.0 < raw`` / ``raw < 100.0`` / ``3.0 <=`` / ``<= 99.0`` break these.
    assert normalize_vlm_unit_score(2.0) == pytest.approx(0.02)
    assert normalize_vlm_unit_score(100.0) == pytest.approx(1.0)


def test_normalize_rejects_negative_scores():
    assert normalize_vlm_unit_score(-0.5) is None


def test_normalize_rejects_nan():
    assert normalize_vlm_unit_score(float("nan")) is None


# ── sex_act_score defaults ─────────────────────────────────────────────────


def test_sex_act_score_defaults_to_zero():
    assert sex_act_score(None) == 0.0
    assert sex_act_score("not-a-dict") == 0.0
    assert sex_act_score({}) == 0.0
    assert sex_act_score({"sex_act": "junk"}) == 0.0


# ── adult blend arithmetic and defaults ────────────────────────────────────


def test_adult_priority_score_blend_is_the_documented_weighted_sum():
    # 0.40*0.5 + 0.60*0.5 == 0.5; every arithmetic-operator mutant on the
    # blend line produces a value far outside this tolerance.
    score = adult_priority_score({"gif_worthiness": 0.5, "sex_act": 0.5})
    assert score == pytest.approx(0.5, abs=1e-9)


def test_adult_priority_score_missing_worthiness_counts_as_zero():
    assert adult_priority_score({"sex_act": 0.5}) == pytest.approx(0.3, abs=1e-9)


def test_adult_export_score_missing_worthiness_counts_as_zero():
    result = adult_export_score({"sex_act": 0.5})
    assert result["adult_score"] == pytest.approx(0.3, abs=1e-9)
    assert result["final_score"] == pytest.approx(0.3, abs=1e-9)


# ── cinematic evidence scan ────────────────────────────────────────────────


def test_cinematic_ignores_items_from_other_signal_families():
    # Non-matching families must be skipped regardless of sort order of the
    # family string; ``!=`` -> ``<`` / ``>`` mutants would read one of them.
    good_scores = {"cinematic_score": 0.7}
    assert (
        cinematic_score_from_assessment(
            {"evidence": [{"signal_family": "zzz", "scores": good_scores}]}
        )
        is None
    )
    assert (
        cinematic_score_from_assessment(
            {"evidence": [{"signal_family": "aaa", "scores": good_scores}]}
        )
        is None
    )


def test_cinematic_matches_signal_family_by_value_not_identity():
    # A runtime-built (non-interned) family string must still match.
    family = "".join(["cinematic", "_classifier"])
    assessment = {
        "evidence": [
            {"signal_family": family, "scores": {"cinematic_score": 0.7}}
        ]
    }
    assert cinematic_score_from_assessment(assessment) == pytest.approx(0.7)


def test_cinematic_skips_invalid_items_without_stopping_the_scan():
    good = {"signal_family": "cinematic_classifier", "scores": {"cinematic_score": 0.7}}
    cases = [
        # item not a Mapping at all
        {"evidence": ["junk", good], "current_quality": 0.9},
        # wrong signal family
        {
            "evidence": [{"signal_family": "other", "scores": {"cinematic_score": 0.1}}, good],
            "current_quality": 0.9,
        },
        # failed status
        {
            "evidence": [
                {
                    "signal_family": "cinematic_classifier",
                    "status": "FAILED",
                    "scores": {"cinematic_score": 0.1},
                },
                good,
            ],
            "current_quality": 0.9,
        },
        # scores not a Mapping
        {
            "evidence": [{"signal_family": "cinematic_classifier", "scores": "junk"}, good],
            "current_quality": 0.9,
        },
    ]
    for assessment in cases:
        # A ``continue`` -> ``break`` mutant would stop at the bad item and
        # fall back to current_quality (0.9) instead of reading the good one.
        assert cinematic_score_from_assessment(assessment) == pytest.approx(0.7)


# ── rank_clips_for_export exception fallback ───────────────────────────────


def test_rank_clips_for_export_falls_back_to_worthiness_when_scorer_raises():
    def exploding_scorer(clip):
        raise RuntimeError("scorer blew up")

    clips = [{"id": "x", "gif_worthiness": 0.5}]
    ranked = rank_clips_for_export(clips, exploding_scorer)
    assert len(ranked) == 1
    assert ranked[0]["final_score"] == pytest.approx(0.5)
    assert math.isfinite(ranked[0]["final_score"])

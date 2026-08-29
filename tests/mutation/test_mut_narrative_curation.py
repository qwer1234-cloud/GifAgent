"""W4 kill tests for narrative_curation mutation survivors.

Targets survivors from the cosmic-ray run recorded in
``<sprint-root>/w4/mutation_scores/cosmic-ray/narrative_curation.json``:

- the combined-score weights (``1.4``/``0.30``/``0.20``/``0.10`` and their
  operator variants): flip scenarios where the original winner's margin is
  smaller than the mutation's score shift;
- ``_norm`` min-max scaling (sub/add/div operator swaps, the ``1e-12``
  epsilon comparison, the all-equal 0.5 neutral);
- greedy tie-breaking (strict ``>`` keeps the first candidate);
- diversity bonus direction (new-video candidates earn the bonus);
- missing ``beat_scores`` key default (0.0);
- ``CurationCandidate`` / ``CuratedBeat`` immutability and field defaults.

Equivalent-ish mutants (weight scalings that cannot flip any ordering given
non-negative score ranges, ``-inf`` -> ``False`` guard, ``best_idx >= -1``,
default ``vector`` factory, all-equal ``0.5`` shift) are registered in
w4-surviving.md.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from app.services.narrative_curation import (
    CurationCandidate,
    CuratedBeat,
    curate_narrative,
)


def _cand(cid, video, bf, *, quality=None, preference=None, bf2=None):
    beats = {"opening": bf}
    if bf2 is not None:
        beats["development"] = bf2
    return CurationCandidate(
        candidate_id=cid,
        source_video=video,
        start_time=0.0,
        beat_scores=beats,
        quality=0.5 if quality is None else quality,
        preference=0.5 if preference is None else preference,
    )


def test_empty_pool_yields_missing_reasons_for_every_beat():
    result = curate_narrative([], beats=("opening", "ending"))
    assert [r.beat for r in result] == ["opening", "ending"]
    assert all(r.selected_candidate_id is None for r in result)
    assert all(r.missing_reason for r in result)


def test_exact_component_scores_for_the_winner():
    a = _cand("A", "v1", 1.0, quality=0.2, preference=0.4)
    b = _cand("B", "v2", 0.5, quality=1.0, preference=0.0)
    result = curate_narrative([a, b], beats=("opening",))
    assert result[0].selected_candidate_id == "A"
    assert result[0].component_scores == {
        "beat_fit": 1.0,
        "quality": 0.2,
        "preference": 0.4,
        "diversity_bonus": 0.1,
    }


def test_high_quality_beats_higher_beat_fit_via_quality_term():
    # Original: X wins through the 0.30 * norm_quality term. Flipping the
    # term's sign, scaling it to 0.30 ** norm_q, or dropping the multiply
    # hands the win to Y.
    x = _cand("X", "v1", 0.5, quality=1.0)
    y = _cand("Y", "v2", 0.55, quality=0.0)
    result = curate_narrative([x, y], beats=("opening",))
    assert result[0].selected_candidate_id == "X"


def test_low_quality_candidate_wins_only_via_beat_fit_margin():
    # Original: X wins through beat fit alone (quality term is zero). Making
    # the quality weight additive/multiplicative (0.30 + q, * q, q ** ...) or
    # negative flips the winner to Y.
    x = _cand("X", "v1", 1.0, quality=0.0)
    y = _cand("Y", "v2", 0.2, quality=1.0)
    result = curate_narrative([x, y], beats=("opening",))
    assert result[0].selected_candidate_id == "X"


def test_high_preference_beats_higher_beat_fit_via_preference_term():
    x = _cand("X", "v1", 0.5, preference=1.0)
    y = _cand("Y", "v2", 0.55, preference=0.0)
    result = curate_narrative([x, y], beats=("opening",))
    assert result[0].selected_candidate_id == "X"


def test_zero_preference_candidate_wins_via_beat_fit_margin():
    x = _cand("X", "v1", 1.0, preference=0.0)
    y = _cand("Y", "v2", 0.25, preference=1.0)
    result = curate_narrative([x, y], beats=("opening",))
    assert result[0].selected_candidate_id == "X"


def test_diversity_bonus_favors_new_video_source():
    # Same-video candidate has a slightly higher development beat fit, but
    # the new-video candidate wins through the diversity bonus (margin
    # 0.002 < 0.01 bonus contribution).
    first = _cand("F", "v1", 0.9)
    same_video = _cand("X", "v1", 0.9, bf2=0.65)
    new_video = _cand("Y", "v2", 0.6, bf2=0.63)
    result = curate_narrative([first, same_video, new_video], beats=("opening", "development"))
    assert result[0].selected_candidate_id == "F"
    assert result[1].selected_candidate_id == "Y"
    assert result[1].component_scores["diversity_bonus"] == 0.1


def test_same_video_candidate_wins_when_beat_fit_margin_exceeds_bonus():
    first = _cand("F", "v1", 0.9)
    same_video = _cand("X", "v1", 0.9, bf2=0.68)
    new_video = _cand("Y", "v2", 0.6, bf2=0.65)
    result = curate_narrative(
        [first, same_video, new_video], beats=("opening", "development")
    )
    assert result[0].selected_candidate_id == "F"
    # X has already-seen video (no bonus) but still edges out Y's bonus.
    assert result[1].selected_candidate_id == "X"
    assert result[1].component_scores["diversity_bonus"] == 0.0


def test_exact_tie_keeps_the_first_candidate():
    a = _cand("A", "v1", 0.6)
    b = _cand("B", "v2", 0.6)
    result = curate_narrative([a, b], beats=("opening",))
    assert result[0].selected_candidate_id == "A"


def test_beat_fit_defaults_to_zero_for_missing_beat_keys():
    has_key = _cand("X", "v1", 0.3)
    missing = CurationCandidate(
        candidate_id="Y", source_video="v2", start_time=0.0,
        beat_scores={}, quality=0.5, preference=0.5,
    )
    result = curate_narrative([has_key, missing], beats=("opening",))
    assert result[0].selected_candidate_id == "X"
    # A candidate with a negative beat-fit value must lose to the 0.0
    # default; a -1.0 default mutant would flip this.
    negative = _cand("Z", "v3", -0.5)
    result = curate_narrative([missing, negative], beats=("opening",))
    assert result[0].selected_candidate_id == "Y"


def test_min_max_spread_below_epsilon_is_neutral_half():
    # qualities differ by exactly 1e-12: the epsilon branch (spread < 1e-12)
    # does NOT trigger, so the tiny spread is scaled to a full 0/1 range and
    # the higher-quality candidate wins; an ``<=`` mutant would neutralise.
    x = CurationCandidate(
        candidate_id="X", source_video="v1", start_time=0.0,
        beat_scores={"opening": 0.5}, quality=1e-12, preference=0.5,
    )
    y = CurationCandidate(
        candidate_id="Y", source_video="v2", start_time=0.0,
        beat_scores={"opening": 0.6}, quality=0.0, preference=0.5,
    )
    result = curate_narrative([x, y], beats=("opening",))
    assert result[0].selected_candidate_id == "X"


def test_all_equal_scores_use_the_neutral_half():
    a = _cand("A", "v1", 0.6)
    b = _cand("B", "v2", 0.5)
    result = curate_narrative([a, b], beats=("opening",))
    assert result[0].selected_candidate_id == "A"
    assert result[0].component_scores["quality"] == 0.5
    assert result[0].component_scores["preference"] == 0.5


def test_no_usable_candidate_reports_missing_reason():
    a = _cand("A", "v1", 1.0)
    result = curate_narrative([a], beats=("opening", "development"))
    assert result[0].selected_candidate_id == "A"
    assert result[1].selected_candidate_id is None
    assert "development" in result[1].missing_reason


def test_dataclasses_are_frozen():
    candidate = CurationCandidate(candidate_id="a", source_video="v", start_time=0.0)
    beat = CuratedBeat(beat="opening", selected_candidate_id=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        candidate.candidate_id = "b"
    with pytest.raises(dataclasses.FrozenInstanceError):
        beat.beat = "ending"


def test_default_field_values():
    candidate = CurationCandidate(candidate_id="a", source_video="v", start_time=0.0)
    assert candidate.quality == 0.0
    assert candidate.preference == 0.0
    assert candidate.vector.shape == (0,)
    assert candidate.vector.dtype == np.float32
    assert candidate.beat_scores == {}

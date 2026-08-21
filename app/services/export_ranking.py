"""Preference-aware ranking helpers for adaptive GIF export."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
import math

ADULT_WORTHINESS_WEIGHT = 0.40
ADULT_SEX_ACT_WEIGHT = 0.60
DEFAULT_ADULT_BLEND_WEIGHT = 0.80
DEFAULT_CINEMATIC_BLEND_WEIGHT = 0.20


def _finite_unit_score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    if not math.isfinite(score) or score < 0.0 or score > 1.0:
        return None
    return score


def sex_act_score(payload: dict[str, Any] | None) -> float:
    """Return a clipped sex_act score, defaulting to 0.0 when absent."""
    if not isinstance(payload, dict):
        return 0.0
    parsed = _finite_unit_score(payload.get("sex_act"))
    return 0.0 if parsed is None else parsed


def adult_priority_score(payload: dict[str, Any]) -> float:
    """Blend GIF worthiness with sex-act intensity for adult ranking."""
    worth = _finite_unit_score(payload.get("gif_worthiness")) or 0.0
    return ADULT_WORTHINESS_WEIGHT * worth + ADULT_SEX_ACT_WEIGHT * sex_act_score(payload)


def cinematic_score_from_assessment(assessment: Mapping[str, Any] | None) -> float | None:
    """Read the cinematic expert's composite score, else color_balance."""
    if not isinstance(assessment, Mapping):
        return None
    evidence = assessment.get("evidence")
    if not isinstance(evidence, list):
        return None
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        if item.get("signal_family") != "cinematic_classifier":
            continue
        if item.get("status") not in {None, "AVAILABLE"}:
            continue
        scores = item.get("scores")
        if not isinstance(scores, Mapping):
            continue
        for key in ("cinematic_score", "color_balance"):
            parsed = _finite_unit_score(scores.get(key))
            if parsed is not None:
                return parsed
    return _finite_unit_score(assessment.get("current_quality"))


def adult_export_score(clip: dict[str, Any]) -> dict[str, Any]:
    """score_clip callback that prefers explicit sexual action."""
    frame = clip.get("best_frame") if isinstance(clip.get("best_frame"), dict) else clip
    sex = sex_act_score(frame)
    worth = _finite_unit_score(clip.get("gif_worthiness")) or 0.0
    adult = ADULT_WORTHINESS_WEIGHT * worth + ADULT_SEX_ACT_WEIGHT * sex
    return {
        "sex_act": sex,
        "adult_score": adult,
        "final_score": adult,
    }


def adult_moe_export_score(
    clip: dict[str, Any],
    *,
    adult_weight: float = DEFAULT_ADULT_BLEND_WEIGHT,
    cinematic_weight: float = DEFAULT_CINEMATIC_BLEND_WEIGHT,
) -> dict[str, Any]:
    """Blend adult sex-clip score with cinematic evidence for MoE selection."""
    adult_result = adult_export_score(clip)
    cinematic = cinematic_score_from_assessment(clip.get("quality_assessment"))
    if cinematic is None:
        cinematic = 0.5
    return {
        **adult_result,
        "cinematic_score": cinematic,
        "final_score": (
            float(adult_weight) * float(adult_result["adult_score"])
            + float(cinematic_weight) * cinematic
        ),
    }


def make_adult_moe_scorer(
    adult_weight: float = DEFAULT_ADULT_BLEND_WEIGHT,
    cinematic_weight: float = DEFAULT_CINEMATIC_BLEND_WEIGHT,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Bind ranking weights for ``rank_clips_for_export``."""

    def score_clip(clip: dict[str, Any]) -> dict[str, Any]:
        return adult_moe_export_score(
            clip,
            adult_weight=adult_weight,
            cinematic_weight=cinematic_weight,
        )

    return score_clip


def rank_clips_for_export(
    clips: list[dict[str, Any]],
    score_clip: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> list[dict[str, Any]]:
    """Score all candidates before sorting, falling back to VLM worthiness."""
    for clip in clips:
        base_score = float(clip["gif_worthiness"])
        try:
            result = score_clip(clip) or {}
        except Exception:
            result = {}
        final_score = result.get("final_score", base_score)
        if final_score is None:
            final_score = base_score
        clip.update(result)
        clip["final_score"] = float(final_score)
    return sorted(clips, key=lambda clip: clip["final_score"], reverse=True)

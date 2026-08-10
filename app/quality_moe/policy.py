"""Non-overridable quality decision guard."""

from __future__ import annotations

import math
from typing import Mapping

from app.quality_moe.config import QualityMoeConfig
from app.quality_moe.models import QualityAssessment, QualityDecision, RepairRecipe


_ACTION_COMPLETENESS_HARD_MINIMUM = 0.50


def hard_gate_reasons(clip: Mapping[str, object]) -> tuple[str, ...]:
    """Return deterministic eligibility failures from existing clip fields."""
    reasons: list[str] = []
    transition_action = clip.get("transition_action")
    if transition_action == "drop":
        reasons.append("transition_drop")
    elif transition_action == "unverified":
        reasons.append("transition_unverified")
    action_score = clip.get("action_completeness_score")
    if action_score is not None:
        try:
            parsed_action_score = float(action_score)
        except (TypeError, ValueError):
            reasons.append("action_completeness_invalid")
        else:
            if not math.isfinite(parsed_action_score):
                reasons.append("action_completeness_invalid")
            elif parsed_action_score < _ACTION_COMPLETENESS_HARD_MINIMUM:
                reasons.append("action_incomplete")
    for field, reason in (("media_decodable", "media_undecodable"), ("decode_ok", "media_undecodable")):
        if clip.get(field) is False:
            reasons.append(reason)
    return tuple(dict.fromkeys(reasons))


def enforce_decision(
    *,
    proposed: QualityDecision,
    confidence: float,
    negative_signal_families: tuple[str, ...] | list[str],
    hard_reasons: tuple[str, ...] | list[str],
    repair: RepairRecipe | None,
    config: QualityMoeConfig,
) -> QualityAssessment:
    """Apply non-overridable gates to a judge recommendation.

    ``report_only`` remains an execution-mode concern: a qualified rejection is
    recorded here while a later pipeline stage retains that candidate.
    """
    proposed = QualityDecision(proposed)
    normalized_reasons = tuple(dict.fromkeys(reason for reason in hard_reasons if reason))
    if normalized_reasons:
        return QualityAssessment(
            decision=QualityDecision.REJECT,
            confidence=_confidence(confidence),
            hard_reasons=normalized_reasons,
        )
    if proposed is QualityDecision.ABSTAIN:
        return QualityAssessment(decision=QualityDecision.ABSTAIN, confidence=_confidence(confidence))
    normalized_families = tuple(dict.fromkeys(
        family for family in negative_signal_families if isinstance(family, str) and family
    ))
    if proposed is QualityDecision.REJECT:
        if (
            _confidence(confidence) < config.soft_reject.min_judge_confidence
            or len(normalized_families) < config.soft_reject.min_independent_negative_families
        ):
            return QualityAssessment(
                decision=QualityDecision.REVIEW,
                confidence=_confidence(confidence),
                negative_signal_families=normalized_families,
            )
    if proposed is QualityDecision.KEEP_FOR_REPAIR and not _valid_repair(repair, config):
        return QualityAssessment(
            decision=QualityDecision.REVIEW,
            confidence=_confidence(confidence),
            negative_signal_families=normalized_families,
        )
    return QualityAssessment(
        decision=proposed,
        confidence=_confidence(confidence),
        negative_signal_families=normalized_families,
        repair=repair if proposed is QualityDecision.KEEP_FOR_REPAIR else None,
    )


def _confidence(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("confidence must be in [0, 1]")
    parsed = float(value)
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise ValueError("confidence must be in [0, 1]")
    return parsed


def _valid_repair(recipe: RepairRecipe | None, config: QualityMoeConfig) -> bool:
    if recipe is None or not config.repairability.enabled or not recipe.validated:
        return False
    try:
        recipe.validate()
    except ValueError:
        return False
    return (
        recipe.quality_gain >= config.repairability.min_quality_gain
        and recipe.confidence >= config.repairability.min_confidence
    )

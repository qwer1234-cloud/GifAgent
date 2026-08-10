"""Non-overridable quality decision guard."""

from __future__ import annotations

import math
from typing import Mapping, Sequence

from app.quality_moe.config import QualityMoeConfig
from app.quality_moe.models import (
    EvidenceStatus,
    ExpertEvidence,
    QualityAssessment,
    QualityDecision,
    RepairRecipe,
)


_ACTION_COMPLETENESS_HARD_MINIMUM = 0.50
_POLICY_VERSION = "quality-moe-policy-v1"
_CANONICAL_SIGNAL_FAMILIES = frozenset({
    "deterministic_temporal",
    "nr_vqa",
    "cinematic_classifier",
    "semantic_video_critic",
    "repair_delta",
})


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
    for field, reason in (
        ("media_decodable", "media_undecodable"),
        ("decode_ok", "media_undecodable"),
    ):
        if clip.get(field) is False:
            reasons.append(reason)
    return tuple(dict.fromkeys(reasons))


def enforce_decision(
    *,
    candidate_id: str,
    proposed: QualityDecision,
    confidence: float,
    evidence: Sequence[ExpertEvidence],
    hard_reasons: Sequence[str],
    repair: RepairRecipe | None,
    config: QualityMoeConfig,
    policy_version: str = _POLICY_VERSION,
) -> QualityAssessment:
    """Apply Core policy while retaining both recommendation and safe effect."""
    proposed = QualityDecision(proposed)
    parsed_confidence = _confidence(confidence)
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("candidate_id must be a non-empty string")
    if not isinstance(policy_version, str) or not policy_version:
        raise ValueError("policy_version must be a non-empty string")
    normalized_reasons = tuple(dict.fromkeys(
        reason for reason in hard_reasons if isinstance(reason, str) and reason
    ))
    if normalized_reasons:
        return _assessment(
            candidate_id=candidate_id,
            recommended=QualityDecision.REJECT,
            effective=QualityDecision.REJECT,
            confidence=parsed_confidence,
            config=config,
            policy_version=policy_version,
            hard_reasons=normalized_reasons,
        )
    if proposed is QualityDecision.ABSTAIN:
        return _assessment(
            candidate_id=candidate_id,
            recommended=QualityDecision.ABSTAIN,
            effective=QualityDecision.ABSTAIN,
            confidence=parsed_confidence,
            config=config,
            policy_version=policy_version,
        )
    negative_families = _negative_signal_families(evidence)
    recommended = proposed
    if proposed is QualityDecision.REJECT and (
        parsed_confidence < config.soft_reject.min_judge_confidence
        or len(negative_families)
        < config.soft_reject.min_independent_negative_families
    ):
        recommended = QualityDecision.REVIEW
    if proposed is QualityDecision.KEEP_FOR_REPAIR and not _valid_repair(
        repair, config, evidence
    ):
        recommended = QualityDecision.REVIEW
    effective = recommended
    if config.report_only and recommended is QualityDecision.REJECT:
        effective = QualityDecision.REVIEW
    return _assessment(
        candidate_id=candidate_id,
        recommended=recommended,
        effective=effective,
        confidence=parsed_confidence,
        config=config,
        policy_version=policy_version,
        negative_signal_families=negative_families,
        repair=repair if recommended is QualityDecision.KEEP_FOR_REPAIR else None,
    )


def _assessment(
    *,
    candidate_id: str,
    recommended: QualityDecision,
    effective: QualityDecision,
    confidence: float,
    config: QualityMoeConfig,
    policy_version: str,
    negative_signal_families: tuple[str, ...] = (),
    hard_reasons: tuple[str, ...] = (),
    repair: RepairRecipe | None = None,
) -> QualityAssessment:
    return QualityAssessment(
        candidate_id=candidate_id,
        evaluation_version=config.evaluation_version,
        config_hash=config.config_hash,
        policy_version=policy_version,
        recommended_decision=recommended,
        effective_decision=effective,
        confidence=confidence,
        negative_signal_families=negative_signal_families,
        hard_reasons=hard_reasons,
        repair=repair,
    )


def _negative_signal_families(
    evidence: Sequence[ExpertEvidence],
) -> tuple[str, ...]:
    families: list[str] = []
    seen_evidence: set[str] = set()
    for item in evidence:
        if not isinstance(item, ExpertEvidence):
            raise ValueError("evidence must contain ExpertEvidence values")
        identity = item.identity_hash
        if identity in seen_evidence:
            continue
        seen_evidence.add(identity)
        if (
            item.status is EvidenceStatus.AVAILABLE
            and item.signal_family in _CANONICAL_SIGNAL_FAMILIES
            and item.signal_family not in families
        ):
            families.append(item.signal_family)
    return tuple(families)


def _confidence(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("confidence must be in [0, 1]")
    parsed = float(value)
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise ValueError("confidence must be in [0, 1]")
    return parsed


def _valid_repair(
    recipe: RepairRecipe | None,
    config: QualityMoeConfig,
    evidence: Sequence[ExpertEvidence],
) -> bool:
    if recipe is None or not config.repairability.enabled:
        return False
    validation = recipe.validation
    if validation is None or validation.repair_delta_status is not EvidenceStatus.AVAILABLE:
        return False
    try:
        recipe.validate()
    except ValueError:
        return False
    if (
        validation.recipe_hash != recipe.recipe_hash
        or validation.config_hash != config.config_hash
        or not validation.source_input_hash
        or not validation.proxy_artifact_hash
    ):
        return False
    matching_delta = any(
        item.identity_hash == validation.repair_delta_evidence_id
        and item.signal_family == "repair_delta"
        and item.status is EvidenceStatus.AVAILABLE
        and item.input_hash == validation.source_input_hash
        and item.config_hash == validation.config_hash
        for item in evidence
    )
    return (
        matching_delta
        and recipe.quality_gain >= config.repairability.min_quality_gain
        and recipe.confidence >= config.repairability.min_confidence
    )

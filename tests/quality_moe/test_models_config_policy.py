import math

import pytest

from app.quality_moe.config import QualityMoeConfig
from app.quality_moe.models import (
    EvidenceStatus,
    EvidencePolarity,
    ExpertEvidence,
    QualityAssessment,
    QualityDecision,
    RepairValidation,
    RepairRecipe,
)
from app.quality_moe.policy import enforce_decision, hard_gate_reasons


def test_non_available_evidence_has_no_numeric_vote():
    evidence = ExpertEvidence(
        candidate_id="c1",
        evaluation_version="quality-moe-v1",
        expert_id="judge",
        expert_version="v1",
        signal_family="semantic_video_critic",
        status=EvidenceStatus.ABSTAINED,
        scores={"technical_integrity": 0.2},
    )

    assert evidence.available_scores() == {}


def test_available_evidence_is_immutable_and_json_safe():
    evidence = ExpertEvidence(
        candidate_id="c1",
        evaluation_version="quality-moe-v1",
        expert_id="technical",
        expert_version="v1",
        signal_family="nr_vqa",
        status=EvidenceStatus.AVAILABLE,
        scores={"technical_integrity": 0.7},
        findings=({"code": "blur", "severity": 0.4},),
    )

    with pytest.raises(TypeError):
        evidence.scores["technical_integrity"] = 0.1
    assert evidence.to_dict()["findings"] == [{"code": "blur", "severity": 0.4}]


def test_repair_recipe_copies_mutable_geometry_values():
    crop = [0.0, 0.0, 1.0, 1.0]
    recipe = RepairRecipe(recipe_id="repair-1", crop=crop)
    crop[2] = 0.5

    assert recipe.crop == (0.0, 0.0, 1.0, 1.0)


def test_assessment_copies_mutable_reason_codes():
    codes = ["underexposed_subject"]
    assessment = QualityAssessment(
        candidate_id="c1",
        evaluation_version="quality-moe-v1",
        config_hash="config-sha",
        policy_version="quality-moe-policy-v1",
        recommended_decision=QualityDecision.REVIEW,
        effective_decision=QualityDecision.REVIEW,
        confidence=0.6,
        reason_codes=codes,
    )
    codes.append("changed")

    assert assessment.reason_codes == ("underexposed_subject",)


def test_config_rejects_unsafe_repair_limits():
    with pytest.raises(ValueError, match="max_proxy_variants"):
        QualityMoeConfig.from_mapping(
            {"quality_moe": {"repairability": {"max_proxy_variants": 13}}}
        )


def test_config_rejects_soft_rejection_with_fewer_than_two_families():
    with pytest.raises(ValueError, match="min_independent_negative_families"):
        QualityMoeConfig.from_mapping(
            {"quality_moe": {"soft_reject": {"min_independent_negative_families": 1}}}
        )


def test_config_freezes_copied_canonical_mapping_without_environment_defaults():
    source = {
        "quality_moe": {
            "report_only": False,
            "soft_reject": {"min_judge_confidence": 0.9},
        }
    }

    config = QualityMoeConfig.from_mapping(source)
    source["quality_moe"]["soft_reject"]["min_judge_confidence"] = 0.1

    assert config.soft_reject.min_judge_confidence == 0.9
    assert config.to_dict()["repairability"]["max_proxy_variants"] == 12
    assert len(config.config_hash) == 64
    assert config.config_hash == QualityMoeConfig.from_mapping(
        {"quality_moe": {"soft_reject": {"min_judge_confidence": 0.9}, "report_only": False}}
    ).config_hash


def test_config_accepts_and_copies_spec_shaped_expert_and_judge_settings():
    source = {
        "quality_moe": {
            "experts": {"technical_aesthetic": {"enabled": True, "model_id": "bundled"}},
            "judge": {"model_id": "local-video", "temperature": 0, "schema_version": "quality-judge-v1"},
        }
    }

    config = QualityMoeConfig.from_mapping(source)
    source["quality_moe"]["judge"]["model_id"] = "changed"

    assert config.experts["technical_aesthetic"]["model_id"] == "bundled"
    assert config.judge["model_id"] == "local-video"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("photometric_mode", "per_frame_auto"),
        ("geometric_mode", "generative_outpainting"),
    ],
)
def test_config_rejects_non_v1_repair_modes(field, value):
    with pytest.raises(ValueError, match=field):
        QualityMoeConfig.from_mapping(
            {"quality_moe": {"repairability": {field: value}}}
        )


@pytest.mark.parametrize("bad_value", [math.nan, math.inf, -math.inf])
def test_config_rejects_non_finite_thresholds(bad_value):
    with pytest.raises(ValueError, match="min_confidence"):
        QualityMoeConfig.from_mapping(
            {"quality_moe": {"repairability": {"min_confidence": bad_value}}}
        )


def _evidence(
    signal_family: str,
    *,
    status: EvidenceStatus = EvidenceStatus.AVAILABLE,
    polarity: EvidencePolarity = EvidencePolarity.NEUTRAL,
    expert_id: str = "expert",
    candidate_id: str = "c1",
    evaluation_version: str = "quality-moe-v1",
    input_hash: str = "source-sha",
    config_hash: str = "config-sha",
    parent_input_hash: str | None = None,
) -> ExpertEvidence:
    return ExpertEvidence(
        candidate_id=candidate_id,
        evaluation_version=evaluation_version,
        expert_id=expert_id,
        expert_version="v1",
        signal_family=signal_family,
        status=status,
        scores={"technical_integrity": 0.4},
        input_hash=input_hash,
        config_hash=config_hash,
        parent_input_hash=parent_input_hash,
        polarity=polarity,
    )


def _policy_evidence(
    config: QualityMoeConfig,
    signal_family: str,
    **kwargs,
) -> ExpertEvidence:
    return _evidence(signal_family, config_hash=config.config_hash, **kwargs)


def _enforce(
    *,
    proposed: QualityDecision,
    config: QualityMoeConfig,
    evidence: tuple[ExpertEvidence, ...] = (),
    hard_reasons: tuple[str, ...] = (),
    repair: RepairRecipe | None = None,
    input_hash: str = "source-sha",
):
    return enforce_decision(
        candidate_id="c1",
        input_hash=input_hash,
        proposed=proposed,
        confidence=0.95,
        evidence=evidence,
        hard_reasons=hard_reasons,
        repair=repair,
        config=config,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("transition_action", "drop"),
        ("action_completeness_score", 0.2),
    ],
)
def test_hard_gate_cannot_be_overridden_by_keep(field, value):
    result = _enforce(
        proposed=QualityDecision.KEEP_AS_IS,
        hard_reasons=hard_gate_reasons({field: value}),
        config=QualityMoeConfig.defaults(),
    )

    assert result.recommended_decision is QualityDecision.REJECT
    assert result.effective_decision is QualityDecision.REJECT


def test_report_only_soft_rejection_is_effectively_review():
    config = QualityMoeConfig.defaults()
    result = _enforce(
        proposed=QualityDecision.REJECT,
        evidence=(
            _policy_evidence(config, "nr_vqa", polarity=EvidencePolarity.NEGATIVE),
            _policy_evidence(config, "semantic_video_critic", expert_id="judge", polarity=EvidencePolarity.NEGATIVE),
        ),
        config=config,
    )

    assert result.recommended_decision is QualityDecision.REJECT
    assert result.effective_decision is QualityDecision.REVIEW


def test_soft_rejection_counts_only_available_canonical_evidence_families():
    config = QualityMoeConfig.from_mapping({"quality_moe": {"report_only": False}})
    result = _enforce(
        proposed=QualityDecision.REJECT,
        evidence=(
            _policy_evidence(config, "nr_vqa", polarity=EvidencePolarity.NEGATIVE),
            _policy_evidence(config, "nr_vqa", polarity=EvidencePolarity.NEGATIVE),
            _policy_evidence(config, "semantic_video_critic", status=EvidenceStatus.UNAVAILABLE, polarity=EvidencePolarity.NEGATIVE),
            _policy_evidence(config, "unknown_family", expert_id="unknown", polarity=EvidencePolarity.NEGATIVE),
        ),
        config=config,
    )

    assert result.negative_signal_families == ("nr_vqa",)
    assert result.effective_decision is QualityDecision.REVIEW


def test_assessment_serializes_frozen_provenance_fields():
    assessment = QualityAssessment(
        candidate_id="c1",
        evaluation_version="quality-moe-v1",
        config_hash="config-sha",
        policy_version="quality-moe-policy-v1",
        recommended_decision=QualityDecision.REJECT,
        effective_decision=QualityDecision.REVIEW,
        confidence=0.95,
    )

    assert assessment.to_dict() == {
        "candidate_id": "c1",
        "evaluation_version": "quality-moe-v1",
        "config_hash": "config-sha",
        "policy_version": "quality-moe-policy-v1",
        "recommended_decision": "REJECT",
        "effective_decision": "REVIEW",
        "confidence": 0.95,
        "negative_signal_families": [],
        "hard_reasons": [],
        "repair": None,
        "reason_codes": [],
        "summary": "",
    }


def test_keep_for_repair_requires_bound_available_validation_evidence():
    config = QualityMoeConfig.from_mapping({"quality_moe": {"report_only": False}})
    unvalidated = RepairRecipe(
        recipe_id="repair-1", quality_gain=0.15, confidence=0.80
    )
    delta = _policy_evidence(
        config, "repair_delta", expert_id="repair-scorer",
        input_hash="proxy-sha", parent_input_hash="source-sha",
        polarity=EvidencePolarity.POSITIVE,
    )
    validated = RepairRecipe(
        recipe_id="repair-2",
        quality_gain=0.15,
        confidence=0.80,
        validation=RepairValidation(
            candidate_id="c1", evaluation_version="quality-moe-v1",
            source_input_hash="source-sha",
            proxy_artifact_hash="proxy-sha",
            recipe_hash=RepairRecipe(recipe_id="repair-2", quality_gain=0.15, confidence=0.80).recipe_hash,
            config_hash=config.config_hash,
            repair_delta_evidence_id=delta.identity_hash,
            repair_delta_status=EvidenceStatus.AVAILABLE,
        ),
    )

    assert _enforce(
        proposed=QualityDecision.KEEP_FOR_REPAIR,
        repair=unvalidated,
        config=config,
    ).effective_decision is QualityDecision.REVIEW
    assert _enforce(
        proposed=QualityDecision.KEEP_FOR_REPAIR,
        repair=validated,
        evidence=(delta,),
        config=config,
    ).effective_decision is QualityDecision.KEEP_FOR_REPAIR


def test_repair_validation_requires_matching_delta_source_and_config_hashes():
    config = QualityMoeConfig.from_mapping({"quality_moe": {"report_only": False}})
    delta = _policy_evidence(
        config, "repair_delta", expert_id="repair-scorer",
        input_hash="proxy-sha", parent_input_hash="source-sha",
        polarity=EvidencePolarity.POSITIVE,
    )
    recipe = RepairRecipe(
        recipe_id="repair-1",
        quality_gain=0.15,
        confidence=0.80,
        validation=RepairValidation(
            candidate_id="c1", evaluation_version="quality-moe-v1",
            source_input_hash="source-sha",
            proxy_artifact_hash="different-proxy-sha",
            recipe_hash=RepairRecipe(recipe_id="repair-1", quality_gain=0.15, confidence=0.80).recipe_hash,
            config_hash=config.config_hash,
            repair_delta_evidence_id=delta.identity_hash,
            repair_delta_status=EvidenceStatus.AVAILABLE,
        ),
    )

    assert _enforce(
        proposed=QualityDecision.KEEP_FOR_REPAIR,
        repair=recipe,
        evidence=(delta,),
        config=config,
    ).effective_decision is QualityDecision.REVIEW


def test_available_positive_evidence_never_counts_as_negative():
    config = QualityMoeConfig.from_mapping({"quality_moe": {"report_only": False}})

    result = _enforce(
        proposed=QualityDecision.REJECT,
        evidence=(
            _policy_evidence(config, "nr_vqa", polarity=EvidencePolarity.POSITIVE),
            _policy_evidence(config, "semantic_video_critic", polarity=EvidencePolarity.POSITIVE),
        ),
        config=config,
    )

    assert result.negative_signal_families == ()
    assert result.effective_decision is QualityDecision.REVIEW


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_id", "other-candidate"),
        ("evaluation_version", "quality-moe-v2"),
        ("config_hash", "other-config"),
        ("input_hash", "other-source"),
    ],
)
def test_cross_decision_context_evidence_does_not_count(field, value):
    config = QualityMoeConfig.from_mapping({"quality_moe": {"report_only": False}})
    matching = _policy_evidence(
        config, "nr_vqa", polarity=EvidencePolarity.NEGATIVE
    )
    foreign_kwargs = {field: value, "polarity": EvidencePolarity.NEGATIVE}
    if field != "config_hash":
        foreign_kwargs["config_hash"] = config.config_hash
    foreign = _evidence("semantic_video_critic", **foreign_kwargs)

    result = _enforce(
        proposed=QualityDecision.REJECT,
        evidence=(matching, foreign),
        config=config,
    )

    assert result.negative_signal_families == ("nr_vqa",)
    assert result.effective_decision is QualityDecision.REVIEW


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_id", "other-candidate"),
        ("evaluation_version", "quality-moe-v2"),
        ("config_hash", "other-config"),
    ],
)
def test_repair_delta_context_mismatches_cannot_keep_for_repair(field, value):
    config = QualityMoeConfig.from_mapping({"quality_moe": {"report_only": False}})
    delta_kwargs = {
        "expert_id": "repair-scorer",
        "input_hash": "proxy-sha",
        "parent_input_hash": "source-sha",
        "polarity": EvidencePolarity.POSITIVE,
        "config_hash": config.config_hash,
        field: value,
    }
    delta = _evidence("repair_delta", **delta_kwargs)
    recipe = RepairRecipe(
        recipe_id="repair-1", quality_gain=0.15, confidence=0.80,
        validation=RepairValidation(
            candidate_id="c1", evaluation_version="quality-moe-v1",
            source_input_hash="source-sha", proxy_artifact_hash="proxy-sha",
            recipe_hash=RepairRecipe(recipe_id="repair-1", quality_gain=0.15, confidence=0.80).recipe_hash,
            config_hash=config.config_hash,
            repair_delta_evidence_id=delta.identity_hash,
            repair_delta_status=EvidenceStatus.AVAILABLE,
        ),
    )

    assert _enforce(
        proposed=QualityDecision.KEEP_FOR_REPAIR,
        repair=recipe,
        evidence=(delta,),
        config=config,
    ).effective_decision is QualityDecision.REVIEW


def test_repair_delta_recipe_hash_mismatch_cannot_keep_for_repair():
    config = QualityMoeConfig.from_mapping({"quality_moe": {"report_only": False}})
    delta = _policy_evidence(
        config, "repair_delta", expert_id="repair-scorer",
        input_hash="proxy-sha", parent_input_hash="source-sha",
        polarity=EvidencePolarity.POSITIVE,
    )
    recipe = RepairRecipe(
        recipe_id="repair-1", quality_gain=0.15, confidence=0.80,
        validation=RepairValidation(
            candidate_id="c1", evaluation_version="quality-moe-v1",
            source_input_hash="source-sha", proxy_artifact_hash="proxy-sha",
            recipe_hash="different-recipe-hash", config_hash=config.config_hash,
            repair_delta_evidence_id=delta.identity_hash,
            repair_delta_status=EvidenceStatus.AVAILABLE,
        ),
    )

    assert _enforce(
        proposed=QualityDecision.KEEP_FOR_REPAIR,
        repair=recipe,
        evidence=(delta,),
        config=config,
    ).effective_decision is QualityDecision.REVIEW


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_id", "other-candidate"),
        ("evaluation_version", "quality-moe-v2"),
        ("config_hash", "other-config"),
    ],
)
def test_repair_validation_context_mismatch_cannot_keep_for_repair(field, value):
    config = QualityMoeConfig.from_mapping({"quality_moe": {"report_only": False}})
    delta = _policy_evidence(
        config, "repair_delta", expert_id="repair-scorer",
        input_hash="proxy-sha", parent_input_hash="source-sha",
        polarity=EvidencePolarity.POSITIVE,
    )
    validation_kwargs = {
        "candidate_id": "c1",
        "evaluation_version": "quality-moe-v1",
        "source_input_hash": "source-sha",
        "proxy_artifact_hash": "proxy-sha",
        "recipe_hash": RepairRecipe(recipe_id="repair-1", quality_gain=0.15, confidence=0.80).recipe_hash,
        "config_hash": config.config_hash,
        "repair_delta_evidence_id": delta.identity_hash,
        "repair_delta_status": EvidenceStatus.AVAILABLE,
        field: value,
    }
    recipe = RepairRecipe(
        recipe_id="repair-1", quality_gain=0.15, confidence=0.80,
        validation=RepairValidation(**validation_kwargs),
    )

    assert _enforce(
        proposed=QualityDecision.KEEP_FOR_REPAIR,
        repair=recipe,
        evidence=(delta,),
        config=config,
    ).effective_decision is QualityDecision.REVIEW


def test_repair_recipe_does_not_accept_a_caller_supplied_validated_boolean():
    with pytest.raises(TypeError, match="validated"):
        RepairRecipe(recipe_id="repair-1", validated=True)


def test_judge_refusal_is_abstain_without_negative_family():
    result = _enforce(
        proposed=QualityDecision.ABSTAIN,
        evidence=(_evidence("semantic_video_critic"),),
        config=QualityMoeConfig.defaults(),
    )

    assert result.recommended_decision is QualityDecision.ABSTAIN
    assert result.effective_decision is QualityDecision.ABSTAIN
    assert result.negative_signal_families == ()

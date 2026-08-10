import math

import pytest

from app.quality_moe.config import QualityMoeConfig
from app.quality_moe.models import (
    EvidenceStatus,
    ExpertEvidence,
    QualityAssessment,
    QualityDecision,
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
        decision=QualityDecision.REVIEW,
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


@pytest.mark.parametrize("bad_value", [math.nan, math.inf, -math.inf])
def test_config_rejects_non_finite_thresholds(bad_value):
    with pytest.raises(ValueError, match="min_confidence"):
        QualityMoeConfig.from_mapping(
            {"quality_moe": {"repairability": {"min_confidence": bad_value}}}
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("transition_action", "drop"),
        ("action_completeness_score", 0.2),
    ],
)
def test_hard_gate_cannot_be_overridden_by_keep(field, value):
    result = enforce_decision(
        proposed=QualityDecision.KEEP_AS_IS,
        confidence=0.99,
        negative_signal_families=(),
        hard_reasons=hard_gate_reasons({field: value}),
        repair=None,
        config=QualityMoeConfig.defaults(),
    )

    assert result.decision is QualityDecision.REJECT


def test_soft_reject_requires_two_independent_families():
    result = enforce_decision(
        proposed=QualityDecision.REJECT,
        confidence=0.95,
        negative_signal_families=("nr_vqa", "nr_vqa"),
        hard_reasons=(),
        repair=None,
        config=QualityMoeConfig.defaults(),
    )

    assert result.decision is QualityDecision.REVIEW


def test_report_only_records_qualified_rejection_without_overriding_hard_rejection():
    soft_result = enforce_decision(
        proposed=QualityDecision.REJECT,
        confidence=0.95,
        negative_signal_families=("nr_vqa", "semantic_video_critic"),
        hard_reasons=(),
        repair=None,
        config=QualityMoeConfig.defaults(),
    )
    hard_result = enforce_decision(
        proposed=QualityDecision.KEEP_AS_IS,
        confidence=0.99,
        negative_signal_families=(),
        hard_reasons=("transition_drop",),
        repair=None,
        config=QualityMoeConfig.defaults(),
    )

    assert soft_result.decision is QualityDecision.REJECT
    assert hard_result.decision is QualityDecision.REJECT


def test_keep_for_repair_requires_a_validated_recipe():
    unvalidated = RepairRecipe(recipe_id="repair-1", validated=False)
    validated = RepairRecipe(
        recipe_id="repair-2",
        quality_gain=0.15,
        confidence=0.80,
        validated=True,
    )
    config = QualityMoeConfig.from_mapping({"quality_moe": {"report_only": False}})

    assert enforce_decision(
        proposed=QualityDecision.KEEP_FOR_REPAIR,
        confidence=0.9,
        negative_signal_families=(),
        hard_reasons=(),
        repair=unvalidated,
        config=config,
    ).decision is QualityDecision.REVIEW
    assert enforce_decision(
        proposed=QualityDecision.KEEP_FOR_REPAIR,
        confidence=0.9,
        negative_signal_families=(),
        hard_reasons=(),
        repair=validated,
        config=config,
    ).decision is QualityDecision.KEEP_FOR_REPAIR


def test_judge_refusal_is_abstain_without_negative_family():
    result = enforce_decision(
        proposed=QualityDecision.ABSTAIN,
        confidence=0.0,
        negative_signal_families=("semantic_video_critic",),
        hard_reasons=(),
        repair=None,
        config=QualityMoeConfig.defaults(),
    )

    assert result.decision is QualityDecision.ABSTAIN
    assert result.negative_signal_families == ()

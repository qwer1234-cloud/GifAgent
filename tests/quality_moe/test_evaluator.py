from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path

import cv2
import numpy as np

from app.quality_moe.config import QualityMoeConfig
from app.quality_moe.judge import JudgeResult
from app.quality_moe.models import (
    EvidencePolarity,
    EvidenceStatus,
    ExpertEvidence,
    QualityDecision,
)
from app.quality_moe.repair import RepairSearchResult
from app.quality_moe.sampling import SampledClip


def _config(*, report_only: bool = False) -> QualityMoeConfig:
    return QualityMoeConfig.from_mapping({"quality_moe": {"report_only": report_only}})


def _sample(candidate_id: str = "c1", video_path: str = "unused.mp4") -> SampledClip:
    frames = tuple(np.full((18, 24, 3), value, dtype=np.uint8) for value in range(20, 80, 10))
    return SampledClip(candidate_id, video_path, 1.0, 2.0, tuple(1.0 + i / 5 for i in range(6)), frames)


@dataclass
class _CountingSampler:
    sample: SampledClip
    calls: int = 0

    def __call__(self, *_args, **_kwargs) -> SampledClip:
        self.calls += 1
        return self.sample


class _Expert:
    def __init__(self, config: QualityMoeConfig, family: str, expert_id: str, *, negative: bool = False) -> None:
        self.config, self.family, self.expert_id, self.negative = config, family, expert_id, negative

    def evaluate(self, sample: SampledClip) -> ExpertEvidence:
        return ExpertEvidence(
            candidate_id=sample.candidate_id, evaluation_version=self.config.evaluation_version,
            expert_id=self.expert_id, expert_version="test-v1", signal_family=self.family,
            status=EvidenceStatus.AVAILABLE, scores={"quality": 0.30 if self.negative else 0.92},
            input_hash=sample.input_hash, config_hash=self.config.config_hash,
            polarity=EvidencePolarity.NEGATIVE if self.negative else EvidencePolarity.NEUTRAL,
        )


def _judge_result(config: QualityMoeConfig, sample: SampledClip, decision: QualityDecision) -> JudgeResult:
    evidence = ExpertEvidence(
        candidate_id=sample.candidate_id, evaluation_version=config.evaluation_version,
        expert_id="judge", expert_version="test-v1", signal_family="semantic_video_critic",
        status=EvidenceStatus.ABSTAINED if decision is QualityDecision.ABSTAIN else EvidenceStatus.AVAILABLE, scores={"confidence": 0.96}, input_hash=sample.input_hash,
        config_hash=config.config_hash, polarity=EvidencePolarity.NEGATIVE if decision is QualityDecision.REJECT else EvidencePolarity.NEUTRAL,
        prompt_hash="prompt-hash",
    )
    return JudgeResult(decision, 0.96, {}, ("nr_vqa", "cinematic_classifier"), ("test_reject",), None,
                       "test judge", evidence, "prompt-hash", config.config_hash, "model-hash", 1, ("prompt-hash",))


class _Judge:
    def __init__(self, result: JudgeResult) -> None:
        self.result = result
        self.requests = []

    def judge(self, request):
        self.requests.append(request)
        assert Path(request.original_contact_sheet).is_file()
        assert Path(request.best_proxy_contact_sheet).is_file()
        assert request.original_contact_sheet != request.best_proxy_contact_sheet
        from app.quality_moe.judge import _prompt

        first = hashlib.sha256(_prompt(request, correction=False).encode("utf-8")).hexdigest()
        hashes = (first,) if self.result.attempts == 1 else (first, hashlib.sha256(_prompt(request, correction=True).encode("utf-8")).hexdigest())
        evidence = replace(self.result.evidence, prompt_hash=hashes[-1])
        return replace(self.result, evidence=evidence, prompt_hash=hashes[-1], attempt_prompt_hashes=hashes)


def _repair(sample: SampledClip, config: QualityMoeConfig, *, work_dir=None) -> RepairSearchResult:
    from app.quality_moe.models import RepairRecipe, RepairValidation

    proxy = SampledClip(sample.candidate_id, sample.video_path, sample.start_ts, sample.end_ts,
                        sample.timestamps, tuple(np.clip(frame + 10, 0, 255).astype(np.uint8) for frame in sample.frames))
    delta = ExpertEvidence(
        candidate_id=sample.candidate_id, evaluation_version=config.evaluation_version,
        expert_id="repair-delta", expert_version="test-v1", signal_family="repair_delta",
        status=EvidenceStatus.AVAILABLE, scores={"quality_gain": 0.2}, input_hash=proxy.input_hash,
        parent_input_hash=sample.input_hash, config_hash=config.config_hash, polarity=EvidencePolarity.POSITIVE,
    )
    recipe = RepairRecipe(recipe_id="proxy", quality_gain=0.2, confidence=0.9)
    recipe = replace(recipe, validation=RepairValidation(
        candidate_id=sample.candidate_id, evaluation_version=config.evaluation_version,
        source_input_hash=sample.input_hash, proxy_artifact_hash=proxy.input_hash,
        recipe_hash=recipe.recipe_hash, config_hash=config.config_hash,
        repair_delta_evidence_id=delta.identity_hash, repair_delta_status=EvidenceStatus.AVAILABLE,
    ))
    source = _Expert(config, "nr_vqa", "repair-source").evaluate(sample)
    return RepairSearchResult((), recipe, delta, source, source, source, best_proxy=proxy)


def _no_proxy_repair(sample: SampledClip, config: QualityMoeConfig, *, work_dir=None) -> RepairSearchResult:
    source = _Expert(config, "nr_vqa", "repair-source").evaluate(sample)
    return RepairSearchResult((), None, None, source, source, source)


def _candidate(tmp_path, candidate_id: str = "c1") -> dict[str, object]:
    video = tmp_path / f"{candidate_id}.mp4"
    video.write_bytes(b"whole-source-video")
    return {"candidate_id": candidate_id, "video_path": str(video), "start_ts": 1.0, "end_ts": 2.0}


def test_report_only_keeps_rejected_candidate_but_records_recommendation(tmp_path):
    from app.quality_moe.evaluator import evaluate_candidates

    config = _config(report_only=True)
    candidate = _candidate(tmp_path)
    sample = _sample(video_path=str(candidate["video_path"]))
    batch = evaluate_candidates(
        [candidate], config=config, work_dir=tmp_path, sampler=_CountingSampler(sample),
        experts=(_Expert(config, "nr_vqa", "technical", negative=True), _Expert(config, "cinematic_classifier", "cinematic", negative=True), _Expert(config, "deterministic_temporal", "temporal")),
        repair_search=_repair, judge=_Judge(_judge_result(config, sample, QualityDecision.REJECT)),
    )

    assert len(batch.effective_clips) == 1
    assert batch.assessments[0].decision is QualityDecision.REJECT
    assert batch.effective_clips[0]["quality_assessment"]["decision"] == "REJECT"


def test_active_routing_keeps_only_effective_keep_and_sends_review_to_humans(tmp_path):
    from app.quality_moe.evaluator import evaluate_candidates

    config = _config(report_only=False)
    candidate = _candidate(tmp_path)
    sample = _sample(video_path=str(candidate["video_path"]))
    batch = evaluate_candidates(
        [candidate], config=config, work_dir=tmp_path, sampler=_CountingSampler(sample),
        experts=(_Expert(config, "nr_vqa", "technical", negative=True), _Expert(config, "cinematic_classifier", "cinematic", negative=True), _Expert(config, "deterministic_temporal", "temporal")),
        repair_search=_no_proxy_repair,
    )

    assert batch.effective_clips == ()
    assert batch.human_review_clips[0]["candidate_id"] == "c1"
    assert batch.assessments[0].effective_decision is QualityDecision.REVIEW


def test_hard_gate_short_circuits_sampling_and_judge(tmp_path):
    from app.quality_moe.evaluator import evaluate_candidate

    config, sampler = _config(), _CountingSampler(_sample())
    assessment = evaluate_candidate(
        {**_candidate(tmp_path), "transition_action": "drop"}, config=config, work_dir=tmp_path, sampler=sampler,
        judge=object(),
    )

    assert sampler.calls == 0
    assert assessment.decision is QualityDecision.REJECT
    assert assessment.hard_reasons == ("transition_drop",)


def test_judge_unavailable_is_structured_abstention(tmp_path):
    from app.quality_moe.evaluator import evaluate_candidate

    config = _config()
    candidate = _candidate(tmp_path)
    sample = _sample(video_path=str(candidate["video_path"]))
    unavailable = _judge_result(config, sample, QualityDecision.ABSTAIN)
    unavailable = replace(unavailable, evidence=replace(unavailable.evidence, status=EvidenceStatus.UNAVAILABLE))
    assessment = evaluate_candidate(
        candidate, config=config, work_dir=tmp_path, sampler=_CountingSampler(sample),
        experts=(_Expert(config, "nr_vqa", "technical", negative=True), _Expert(config, "cinematic_classifier", "cinematic", negative=True), _Expert(config, "deterministic_temporal", "temporal")),
        repair_search=_repair, judge=_Judge(unavailable),
    )

    assert assessment.decision is QualityDecision.ABSTAIN
    assert assessment.evidence[-1].status is EvidenceStatus.UNAVAILABLE


def test_evidence_sorting_and_provenance_hashes_are_stable(tmp_path):
    from app.quality_moe.evaluator import evaluate_candidate

    config = _config()
    candidate = _candidate(tmp_path)
    sample = _sample(video_path=str(candidate["video_path"]))
    experts = (_Expert(config, "nr_vqa", "z"), _Expert(config, "cinematic_classifier", "a"), _Expert(config, "deterministic_temporal", "m"))
    first = evaluate_candidate(candidate, config=config, work_dir=tmp_path, sampler=_CountingSampler(sample), experts=experts)
    second = evaluate_candidate(candidate, config=config, work_dir=tmp_path, sampler=_CountingSampler(sample), experts=tuple(reversed(experts)))

    assert [(item.signal_family, item.expert_id) for item in first.evidence] == sorted((item.signal_family, item.expert_id) for item in first.evidence)
    assert first.provenance["evaluation_hash"] == second.provenance["evaluation_hash"]
    assert first.provenance["source_file_sha256"] == second.provenance["source_file_sha256"]
    assert list(first.provenance["frame_timestamps"]) == list(sample.timestamps)


def test_one_bad_candidate_does_not_stop_the_batch(tmp_path):
    from app.quality_moe.evaluator import evaluate_candidates

    config = _config(report_only=True)
    good = _candidate(tmp_path, "good")
    sample = _sample("good", str(good["video_path"]))
    batch = evaluate_candidates(
        [{"candidate_id": "bad", "video_path": str(tmp_path / "missing.mp4"), "start_ts": 1.0, "end_ts": 2.0}, good],
        config=config, work_dir=tmp_path, sampler=_CountingSampler(sample),
        experts=(_Expert(config, "nr_vqa", "technical"), _Expert(config, "cinematic_classifier", "cinematic"), _Expert(config, "deterministic_temporal", "temporal")),
    )

    assert len(batch.assessments) == 2
    assert batch.assessments[0].decision is QualityDecision.ABSTAIN
    assert batch.assessments[1].candidate_id == "good"


def test_default_sampler_unavailable_cannot_keep_and_routes_to_manual_review(tmp_path):
    from app.quality_moe.evaluator import evaluate_candidate

    assessment = evaluate_candidate(_candidate(tmp_path), config=_config(), work_dir=tmp_path)

    assert assessment.decision is QualityDecision.ABSTAIN
    assert assessment.effective_decision is QualityDecision.ABSTAIN
    assert assessment.provenance["failure"]["code"] == "sampling_unavailable"


def test_sampler_wrong_source_or_bounds_is_abstained_before_experts(tmp_path):
    from app.quality_moe.evaluator import evaluate_candidate

    candidate = _candidate(tmp_path)
    wrong = _sample(video_path=str(tmp_path / "other.mp4"))
    assessment = evaluate_candidate(candidate, config=_config(), work_dir=tmp_path, sampler=_CountingSampler(wrong))

    assert assessment.decision is QualityDecision.ABSTAIN
    assert assessment.provenance["failure"]["code"] == "sampling_context_mismatch"


def test_sampler_wrong_bounds_is_abstained_before_experts(tmp_path):
    from app.quality_moe.evaluator import evaluate_candidate

    candidate = _candidate(tmp_path)
    base = _sample(video_path=str(candidate["video_path"]))
    wrong = SampledClip(base.candidate_id, base.video_path, 1.1, 2.1, tuple(1.1 + i / 5 for i in range(6)), base.frames)
    assessment = evaluate_candidate(candidate, config=_config(), work_dir=tmp_path, sampler=_CountingSampler(wrong))

    assert assessment.decision is QualityDecision.ABSTAIN
    assert assessment.provenance["failure"]["code"] == "sampling_context_mismatch"


def test_missing_or_duplicate_complementary_expert_families_route_to_review(tmp_path):
    from app.quality_moe.evaluator import evaluate_candidate

    config = _config()
    candidate = _candidate(tmp_path)
    sample = _sample(video_path=str(candidate["video_path"]))
    assessment = evaluate_candidate(
        candidate, config=config, work_dir=tmp_path, sampler=_CountingSampler(sample),
        experts=(_Expert(config, "nr_vqa", "one"), _Expert(config, "nr_vqa", "two"), _Expert(config, "deterministic_temporal", "three")),
    )

    assert assessment.decision is QualityDecision.REVIEW
    assert assessment.effective_decision is QualityDecision.REVIEW
    assert "expert_coverage_invalid" in assessment.provenance["failure"]["code"]


def test_stale_judge_context_is_invalid_and_its_rejection_is_not_adopted(tmp_path):
    from app.quality_moe.evaluator import evaluate_candidate

    config = _config()
    candidate = _candidate(tmp_path)
    sample = _sample(video_path=str(candidate["video_path"]))
    stale = _judge_result(config, sample, QualityDecision.REJECT)
    stale_evidence = replace(stale.evidence, candidate_id="stale")
    stale = replace(stale, evidence=stale_evidence)
    assessment = evaluate_candidate(
        candidate, config=config, work_dir=tmp_path, sampler=_CountingSampler(sample),
        experts=(_Expert(config, "nr_vqa", "technical", negative=True), _Expert(config, "cinematic_classifier", "cinematic", negative=True), _Expert(config, "deterministic_temporal", "temporal")),
        repair_search=_repair, judge=_Judge(stale),
    )

    assert assessment.decision is QualityDecision.REVIEW
    assert any(item.status is EvidenceStatus.INVALID for item in assessment.evidence)


def test_stage_latencies_are_recorded_but_do_not_change_evaluation_hash(tmp_path):
    from app.quality_moe.evaluator import evaluate_candidate

    config = _config()
    candidate = _candidate(tmp_path)
    sample = _sample(video_path=str(candidate["video_path"]))
    ticks = iter(range(0, 100_000_000, 2_000_000))
    first = evaluate_candidate(candidate, config=config, work_dir=tmp_path, sampler=_CountingSampler(sample), clock=lambda: next(ticks))
    ticks = iter(range(100_000_000, 200_000_000, 3_000_000))
    second = evaluate_candidate(candidate, config=config, work_dir=tmp_path, sampler=_CountingSampler(sample), clock=lambda: next(ticks))

    assert first.provenance["latency_ms"]["sampler"] > 0
    assert set(first.provenance["latency_ms"]) == {"sampler", "experts", "repair", "judge", "total"}
    assert first.provenance["evaluation_hash"] == second.provenance["evaluation_hash"]


def test_repair_and_judge_stage_latencies_are_nonzero(tmp_path):
    from app.quality_moe.evaluator import evaluate_candidate

    config = _config()
    candidate = _candidate(tmp_path)
    sample = _sample(video_path=str(candidate["video_path"]))
    ticks = iter(range(0, 200_000_000, 2_000_000))
    assessment = evaluate_candidate(
        candidate, config=config, work_dir=tmp_path, sampler=_CountingSampler(sample), clock=lambda: next(ticks),
        experts=(_Expert(config, "nr_vqa", "technical", negative=True), _Expert(config, "cinematic_classifier", "cinematic", negative=True), _Expert(config, "deterministic_temporal", "temporal")),
        repair_search=_repair, judge=_Judge(_judge_result(config, sample, QualityDecision.REJECT)),
    )

    assert assessment.provenance["latency_ms"]["repair"] > 0
    assert assessment.provenance["latency_ms"]["judge"] > 0
    assert assessment.provenance["judge"]["model_hash"] == "model-hash"

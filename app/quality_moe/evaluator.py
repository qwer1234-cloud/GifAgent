"""Deterministic candidate-level orchestration for the quality MoE.

The evaluator owns no scoring model.  It samples once, coordinates bounded
experts, records provenance, and lets the policy module make the final,
non-overridable routing decision.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

import cv2
import numpy as np

from app.quality_moe.config import QualityMoeConfig
from app.quality_moe.experts import CinematicExpert, TechnicalAestheticExpert, TemporalExpert
from app.quality_moe.judge import JudgeRequest, JudgeResult
from app.quality_moe.models import EvidencePolarity, EvidenceStatus, ExpertEvidence, QualityAssessment, QualityDecision
from app.quality_moe.policy import enforce_decision, hard_gate_reasons
from app.quality_moe.repair import RepairSearchResult, search_repairs
from app.quality_moe.sampling import SampledClip, sample_clip_frames


class _Expert(Protocol):
    def evaluate(self, sampled_clip: SampledClip) -> ExpertEvidence: ...


class _Judge(Protocol):
    def judge(self, request: JudgeRequest) -> JudgeResult: ...


@dataclass(frozen=True)
class QualityBatchResult:
    assessments: tuple[QualityAssessment, ...]
    effective_clips: tuple[dict[str, object], ...]
    human_review_clips: tuple[dict[str, object], ...]


def evaluate_candidate(
    candidate: Mapping[str, object],
    *,
    config: QualityMoeConfig,
    work_dir: str | Path,
    sampler: Callable[..., SampledClip] = sample_clip_frames,
    experts: Sequence[_Expert] | None = None,
    judge: _Judge | Callable[[JudgeRequest], JudgeResult] | None = None,
    repair_search: Callable[..., RepairSearchResult] = search_repairs,
) -> QualityAssessment:
    """Evaluate one exact source interval without permitting a model bypass.

    Injected dependencies make the boundary testable; their outputs are still
    checked against the frozen candidate/config/input context before policy
    enforcement.
    """
    candidate_id, video_path, start_ts, end_ts = _candidate_fields(candidate)
    source_hash = _file_hash(video_path)
    fallback_input = _hash_json({"candidate_id": candidate_id, "source_file_sha256": source_hash, "start_ts": start_ts, "end_ts": end_ts})
    hard_reasons = hard_gate_reasons(candidate)
    if hard_reasons:
        assessment = enforce_decision(
            candidate_id=candidate_id, input_hash=fallback_input, proposed=QualityDecision.KEEP_AS_IS,
            confidence=1.0, evidence=(), hard_reasons=hard_reasons, repair=None, config=config,
        )
        return _annotate(assessment, input_hash=fallback_input, evidence=(), provenance=_provenance(
            candidate_id, video_path, source_hash, start_ts, end_ts, (), config, (), None, None,
        ))

    try:
        sampled = sampler(video_path, start_ts, end_ts, candidate_id)
    except Exception as error:  # A bad candidate is data, never a batch failure.
        return _abstained(candidate_id, video_path, source_hash, start_ts, end_ts, fallback_input, config, "sampling_exception", str(error))
    if not isinstance(sampled, SampledClip) or sampled.candidate_id != candidate_id:
        return _abstained(candidate_id, video_path, source_hash, start_ts, end_ts, fallback_input, config, "sampling_invalid", "Sampler returned an invalid candidate sample.")

    input_hash = sampled.input_hash
    expert_set = tuple(experts) if experts is not None else (
        TechnicalAestheticExpert(config), CinematicExpert(config), TemporalExpert(config),
    )
    if len(expert_set) != 3:
        raise ValueError("exactly three low-cost experts are required")
    collected = _collect_evidence(expert_set, sampled, config)
    evidence = _contextualize(collected, sampled, config)
    repair_result: RepairSearchResult | None = None
    if _needs_repair(evidence):
        try:
            repair_result = repair_search(sampled, config, work_dir=work_dir)
        except Exception as error:
            repair_result = None
            evidence = _sorted_evidence((*evidence, _unavailable(sampled, config, "repair_search", "repair_search_exception", str(error))))
        if repair_result is not None:
            if repair_result.repair_delta is not None:
                evidence = _contextualize((*evidence, repair_result.repair_delta), sampled, config)
            for failure in repair_result.render_failures:
                evidence = _contextualize((*evidence, _unavailable(sampled, config, "repair_proxy", failure.error_code, failure.summary)), sampled, config)

    judge_result: JudgeResult | None = None
    proposed, confidence = QualityDecision.KEEP_AS_IS, _expert_confidence(evidence)
    repair = repair_result.best_recipe if repair_result else None
    best_proxy = repair_result.best_proxy if repair_result else None
    if _needs_repair(evidence):
        if best_proxy is not None and repair is not None and judge is not None:
            try:
                request = _judge_request(work_dir, sampled, best_proxy, evidence, repair.recipe_id)
                judge_result = _call_judge(judge, request)
                if not isinstance(judge_result, JudgeResult):
                    raise ValueError("Judge returned an invalid result.")
                evidence = _contextualize((*evidence, judge_result.evidence), sampled, config)
                proposed, confidence = judge_result.decision, judge_result.confidence
            except Exception as error:
                evidence = _sorted_evidence((*evidence, _unavailable(sampled, config, "semantic_video_critic", "judge_unavailable", str(error))))
                proposed, confidence = QualityDecision.ABSTAIN, 0.0
        else:
            proposed, confidence = QualityDecision.REVIEW, _expert_confidence(evidence)

    policy_evidence = tuple(item for item in evidence if _matches_context(item, sampled, config))
    assessment = enforce_decision(
        candidate_id=candidate_id, input_hash=input_hash, proposed=proposed, confidence=confidence,
        evidence=policy_evidence, hard_reasons=(), repair=repair, config=config,
    )
    reason_codes = judge_result.reason_codes if judge_result else ()
    summary = judge_result.summary if judge_result else "Deterministic expert evidence was evaluated under frozen quality policy."
    provenance = _provenance(candidate_id, video_path, source_hash, start_ts, end_ts, sampled.timestamps, config, evidence, repair_result, judge_result)
    return _annotate(assessment, input_hash=input_hash, evidence=evidence, provenance=provenance, reason_codes=reason_codes, summary=summary)


def evaluate_candidates(
    candidates: Sequence[Mapping[str, object]], **kwargs: object,
) -> QualityBatchResult:
    """Evaluate every candidate independently and apply report/active routing."""
    config = kwargs.get("config")
    if not isinstance(config, QualityMoeConfig):
        raise ValueError("config must be a QualityMoeConfig")
    assessments: list[QualityAssessment] = []
    effective: list[dict[str, object]] = []
    human: list[dict[str, object]] = []
    for candidate in candidates:
        try:
            assessment = evaluate_candidate(candidate, **kwargs)  # type: ignore[arg-type]
        except Exception as error:
            candidate_id = str(candidate.get("candidate_id", candidate.get("id", "invalid"))) if isinstance(candidate, Mapping) else "invalid"
            assessment = _abstained(candidate_id, "", "unavailable", 0.0, 1.0, _hash_json({"candidate_id": candidate_id}), config, "candidate_exception", str(error))
        assessments.append(assessment)
        clip = dict(candidate) if isinstance(candidate, Mapping) else {"candidate_id": assessment.candidate_id}
        clip["quality_assessment"] = assessment.to_dict()
        if (config.report_only and not assessment.hard_reasons) or assessment.effective_decision in {QualityDecision.KEEP_AS_IS, QualityDecision.KEEP_FOR_REPAIR}:
            effective.append(clip)
        if assessment.effective_decision in {QualityDecision.REVIEW, QualityDecision.ABSTAIN}:
            human.append(clip)
    return QualityBatchResult(tuple(assessments), tuple(effective), tuple(human))


def _candidate_fields(candidate: Mapping[str, object]) -> tuple[str, str, float, float]:
    if not isinstance(candidate, Mapping):
        raise ValueError("candidate must be a mapping")
    candidate_id = candidate.get("candidate_id", candidate.get("id"))
    video_path = candidate.get("video_path", candidate.get("source_video", candidate.get("source_path")))
    start_ts, end_ts = candidate.get("start_ts", candidate.get("start")), candidate.get("end_ts", candidate.get("end"))
    if not isinstance(candidate_id, str) or not candidate_id or not isinstance(video_path, str) or not video_path:
        raise ValueError("candidate needs candidate_id and video_path")
    try:
        start, end = float(start_ts), float(end_ts)
    except (TypeError, ValueError) as error:
        raise ValueError("candidate needs finite start_ts and end_ts") from error
    if not 0.0 <= start < end:
        raise ValueError("candidate interval must satisfy 0 <= start_ts < end_ts")
    return candidate_id, video_path, start, end


def _collect_evidence(experts: Sequence[_Expert], sampled: SampledClip, config: QualityMoeConfig) -> tuple[ExpertEvidence, ...]:
    def run(expert: _Expert) -> ExpertEvidence:
        try:
            item = expert.evaluate(sampled)
            if not isinstance(item, ExpertEvidence):
                raise ValueError("expert returned invalid evidence")
            return item
        except Exception as error:
            return _unavailable(sampled, config, getattr(expert, "signal_family", "unknown"), "expert_exception", str(error), expert_id=getattr(expert, "expert_id", "unknown"))
    with ThreadPoolExecutor(max_workers=3) as pool:
        return tuple(pool.map(run, experts))


def _needs_repair(evidence: Sequence[ExpertEvidence]) -> bool:
    available = [item for item in evidence if item.status is EvidenceStatus.AVAILABLE]
    scores = [value for item in available for value in item.scores.values()]
    polarities = {item.polarity for item in available}
    return bool(available) and (bool({EvidencePolarity.NEGATIVE, EvidencePolarity.POSITIVE} <= polarities) or any(item.polarity is EvidencePolarity.NEGATIVE for item in available) or (bool(scores) and min(scores) < 0.65))


def _judge_request(work_dir: str | Path, original: SampledClip, proxy: SampledClip, evidence: Sequence[ExpertEvidence], recipe_id: str) -> JudgeRequest:
    directory = Path(work_dir) / "quality_moe" / original.input_hash
    original_path = _contact_sheet(directory, "original", original.frames)
    proxy_path = _contact_sheet(directory, "best-proxy", proxy.frames)
    return JudgeRequest(original.candidate_id, original.input_hash, original_path, proxy_path, tuple(evidence), (recipe_id,))


def _contact_sheet(directory: Path, name: str, frames: Sequence[np.ndarray]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if not frames:
        raise ValueError("contact sheet needs frames")
    height = min(frame.shape[0] for frame in frames)
    tiles = [frame if frame.shape[0] == height else cv2.resize(frame, (round(frame.shape[1] * height / frame.shape[0]), height)) for frame in frames]
    target = directory / f"{name}.png"
    ok, encoded = cv2.imencode(".png", cv2.hconcat(tiles))
    if not ok:
        raise OSError("contact sheet encoding failed")
    content = encoded.tobytes()
    if not target.exists():
        target.write_bytes(content)
    elif target.read_bytes() != content:
        raise FileExistsError(f"non-repeatable contact sheet: {target}")
    return target


def _call_judge(judge: _Judge | Callable[[JudgeRequest], JudgeResult], request: JudgeRequest) -> JudgeResult:
    method = getattr(judge, "judge", None)
    return method(request) if callable(method) else judge(request)  # type: ignore[operator]


def _matches_context(item: ExpertEvidence, sampled: SampledClip, config: QualityMoeConfig) -> bool:
    if item.candidate_id != sampled.candidate_id or item.evaluation_version != config.evaluation_version or item.config_hash != config.config_hash:
        return False
    return item.input_hash == sampled.input_hash or (item.signal_family == "repair_delta" and item.parent_input_hash == sampled.input_hash)


def _contextualize(evidence: Sequence[ExpertEvidence], sampled: SampledClip, config: QualityMoeConfig) -> tuple[ExpertEvidence, ...]:
    """Do not let an injected or failed component poison the policy context."""
    checked: list[ExpertEvidence] = []
    for item in evidence:
        if _matches_context(item, sampled, config):
            checked.append(item)
        else:
            checked.append(ExpertEvidence(
                candidate_id=sampled.candidate_id, evaluation_version=config.evaluation_version,
                expert_id=item.expert_id, expert_version=item.expert_version,
                signal_family=item.signal_family, status=EvidenceStatus.INVALID,
                findings=({"code": "context_mismatch"},),
                summary="Evidence did not match the frozen candidate evaluation context.",
                input_hash=sampled.input_hash, config_hash=config.config_hash,
            ))
    return _sorted_evidence(checked)


def _unavailable(sampled: SampledClip, config: QualityMoeConfig, family: str, code: str, summary: str, *, expert_id: str | None = None) -> ExpertEvidence:
    return ExpertEvidence(sampled.candidate_id, config.evaluation_version, expert_id or family, "evaluator-v1", family, EvidenceStatus.UNAVAILABLE, findings=({"code": code},), summary=summary[:2000], input_hash=sampled.input_hash, config_hash=config.config_hash)


def _sorted_evidence(evidence: Sequence[ExpertEvidence]) -> tuple[ExpertEvidence, ...]:
    return tuple(sorted(evidence, key=lambda item: (item.signal_family, item.expert_id)))


def _expert_confidence(evidence: Sequence[ExpertEvidence]) -> float:
    values = [value for item in evidence if item.status is EvidenceStatus.AVAILABLE for value in item.scores.values()]
    return max(0.0, min(1.0, sum(values) / len(values))) if values else 0.0


def _file_hash(path: str) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return "unavailable"
    return digest.hexdigest()


def _hash_json(value: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _provenance(candidate_id: str, video_path: str, source_hash: str, start_ts: float, end_ts: float, timestamps: Sequence[float], config: QualityMoeConfig, evidence: Sequence[ExpertEvidence], repair: RepairSearchResult | None, judge: JudgeResult | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "candidate_id": candidate_id, "source_video": video_path, "source_file_sha256": source_hash,
        "boundaries": {"start_ts": start_ts, "end_ts": end_ts}, "frame_timestamps": list(timestamps),
        "evaluation_version": config.evaluation_version, "config_hash": config.config_hash,
        "evidence": [item.to_dict() for item in _sorted_evidence(evidence)],
        "repair": {
            "recipe": repair.best_recipe.to_dict() if repair and repair.best_recipe else None,
            "render_failures": [{"recipe_id": item.recipe_id, "code": item.error_code} for item in repair.render_failures] if repair else [],
            "proxy_lineage": {
                "source_input_hash": next((item.input_hash for item in evidence if item.signal_family != "repair_delta"), None),
                "proxy_input_hash": repair.best_proxy.input_hash,
            } if repair and repair.best_proxy and evidence else None,
        },
        "judge": {"model_hash": judge.model_hash, "prompt_hash": judge.prompt_hash, "attempt_prompt_hashes": list(judge.attempt_prompt_hashes), "attempts": judge.attempts} if judge else None,
    }
    payload["evaluation_hash"] = _hash_json(payload)
    return payload


def _annotate(assessment: QualityAssessment, *, input_hash: str, evidence: Sequence[ExpertEvidence], provenance: Mapping[str, object], reason_codes: Sequence[str] = (), summary: str = "") -> QualityAssessment:
    return replace(assessment, input_hash=input_hash, evidence=_sorted_evidence(evidence), provenance=provenance, reason_codes=tuple(reason_codes), summary=summary)


def _abstained(candidate_id: str, video_path: str, source_hash: str, start_ts: float, end_ts: float, input_hash: str, config: QualityMoeConfig, code: str, summary: str) -> QualityAssessment:
    assessment = enforce_decision(candidate_id=candidate_id, input_hash=input_hash, proposed=QualityDecision.ABSTAIN, confidence=0.0, evidence=(), hard_reasons=(), repair=None, config=config)
    provenance = _provenance(candidate_id, video_path, source_hash, start_ts, end_ts, (), config, (), None, None)
    provenance["failure"] = {"code": code}
    provenance["evaluation_hash"] = _hash_json(provenance)
    return _annotate(assessment, input_hash=input_hash, evidence=(), provenance=provenance, summary=summary)

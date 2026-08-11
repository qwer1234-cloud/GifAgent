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
import os
from pathlib import Path
import time
from typing import Callable, Mapping, Protocol, Sequence

import cv2
import numpy as np

from app.quality_moe.config import QualityMoeConfig
from app.quality_moe.experts import CinematicExpert, TechnicalAestheticExpert, TemporalExpert
from app.quality_moe.judge import JudgeRequest, JudgeResult, _prompt
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


@dataclass(frozen=True)
class _SourceIdentity:
    normalized_path: str
    source_path: str
    sha256: str | None


def evaluate_candidate(
    candidate: Mapping[str, object],
    *,
    config: QualityMoeConfig,
    work_dir: str | Path,
    sampler: Callable[..., SampledClip] = sample_clip_frames,
    experts: Sequence[_Expert] | None = None,
    judge: _Judge | Callable[[JudgeRequest], JudgeResult] | None = None,
    repair_search: Callable[..., RepairSearchResult] = search_repairs,
    clock: Callable[[], int] = time.monotonic_ns,
    source_identity: _SourceIdentity | None = None,
    verify_source_after_sampling: bool = True,
) -> QualityAssessment:
    """Evaluate one exact source interval without permitting a model bypass.

    Injected dependencies make the boundary testable; their outputs are still
    checked against the frozen candidate/config/input context before policy
    enforcement.
    """
    candidate_id, video_path, start_ts, end_ts = _candidate_fields(candidate)
    hard_reasons = hard_gate_reasons(candidate)
    if hard_reasons:
        # A deterministic hard gate is authoritative and must not require
        # media eligibility, sampling, model calls, or filesystem reads.
        fallback_input = _hash_json({"candidate_id": candidate_id, "source_file_sha256": "not_checked", "start_ts": start_ts, "end_ts": end_ts})
        assessment = enforce_decision(
            candidate_id=candidate_id, input_hash=fallback_input, proposed=QualityDecision.KEEP_AS_IS,
            confidence=1.0, evidence=(), hard_reasons=hard_reasons, repair=None, config=config,
        )
        return _annotate(assessment, input_hash=fallback_input, evidence=(), provenance=_provenance(
            candidate_id, video_path, "not_checked", start_ts, end_ts, (), config, (), None, None, _latencies(total=0),
        ))

    if (
        source_identity is not None
        and source_identity.normalized_path != _normal_path(video_path)
    ):
        raise ValueError("source identity does not match candidate video_path")
    source_hash = (
        source_identity.sha256
        if source_identity is not None
        else _source_hash(video_path)
    )
    if source_hash is None:
        unavailable_input = _hash_json({"candidate_id": candidate_id, "source_file_sha256": "unavailable", "start_ts": start_ts, "end_ts": end_ts})
        return _abstained(candidate_id, video_path, "unavailable", start_ts, end_ts, unavailable_input, config, "source_unavailable", "Source media is missing, unreadable, or cannot be fully hashed.")
    fallback_input = _hash_json({"candidate_id": candidate_id, "source_file_sha256": source_hash, "start_ts": start_ts, "end_ts": end_ts})

    total_started = clock()
    sampler_started = clock()
    try:
        sampled = sampler(video_path, start_ts, end_ts, candidate_id)
    except Exception as error:  # A bad candidate is data, never a batch failure.
        return _abstained(candidate_id, video_path, source_hash, start_ts, end_ts, fallback_input, config, "sampling_exception", str(error), _latencies(sampler=_elapsed_ms(sampler_started, clock), total=_elapsed_ms(total_started, clock)))
    sampler_latency = _elapsed_ms(sampler_started, clock)
    current_source_hash = (
        _source_hash(video_path) if verify_source_after_sampling else source_hash
    )
    if current_source_hash is None or current_source_hash != source_hash:
        current_input = _hash_json({"candidate_id": candidate_id, "source_file_sha256": current_source_hash or "unavailable", "start_ts": start_ts, "end_ts": end_ts})
        return _abstained(candidate_id, video_path, current_source_hash or "unavailable", start_ts, end_ts, current_input, config, "source_hash_changed", "Source media changed while sampling.", _latencies(sampler=sampler_latency, total=_elapsed_ms(total_started, clock)))
    sampling_failure = _validate_sampled(sampled, candidate_id, video_path, start_ts, end_ts, source_hash, candidate)
    if sampling_failure is not None:
        return _abstained(candidate_id, video_path, source_hash, start_ts, end_ts, fallback_input, config, sampling_failure, "Sampler output did not prove the requested source interval.", _latencies(sampler=sampler_latency, total=_elapsed_ms(total_started, clock)))

    input_hash = sampled.input_hash
    expert_set = tuple(experts) if experts is not None else (
        TechnicalAestheticExpert(config), CinematicExpert(config), TemporalExpert(config),
    )
    if len(expert_set) != 3:
        raise ValueError("exactly three low-cost experts are required")
    collected, expert_latency = _collect_evidence(expert_set, sampled, config, clock)
    evidence = _contextualize(collected, sampled, config)
    coverage_failure = _coverage_failure(evidence)
    if coverage_failure is not None:
        return _review(
            candidate_id, video_path, source_hash, start_ts, end_ts, sampled, config, evidence,
            coverage_failure, _latencies(sampler=sampler_latency, experts=expert_latency, total=_elapsed_ms(total_started, clock)),
        )
    repair_result: RepairSearchResult | None = None
    repair_latency = 0
    if _needs_repair(evidence):
        repair_started = clock()
        try:
            repair_result = repair_search(sampled, config, work_dir=work_dir)
        except Exception as error:
            repair_result = None
            evidence = _sorted_evidence((*evidence, _unavailable(sampled, config, "repair_search", "repair_search_exception", str(error))))
        repair_latency = _elapsed_ms(repair_started, clock)
        if repair_result is not None:
            if repair_result.repair_delta is not None:
                evidence = _contextualize((*evidence, repair_result.repair_delta), sampled, config)
            for failure in repair_result.render_failures:
                evidence = _contextualize((*evidence, _unavailable(sampled, config, "repair_proxy", failure.error_code, failure.summary)), sampled, config)

    judge_result: JudgeResult | None = None
    proposed, confidence = QualityDecision.KEEP_AS_IS, _expert_confidence(evidence)
    repair = repair_result.best_recipe if repair_result else None
    best_proxy = repair_result.best_proxy if repair_result else None
    latency = _latencies(sampler=sampler_latency, experts=expert_latency, repair=repair_latency)
    if _needs_repair(evidence):
        if best_proxy is not None and repair is not None and judge is not None:
            repair_failure = _validate_repair_context(repair_result, sampled, config)
            if repair_failure is not None:
                evidence = _sorted_evidence((*evidence, _invalid(sampled, config, "repair_delta", repair_failure)))
                proposed, confidence = QualityDecision.REVIEW, _expert_confidence(evidence)
            else:
                judge_started = clock()
                try:
                    request = _judge_request(work_dir, sampled, best_proxy, evidence, repair.recipe_id)
                    candidate_result = _call_judge(judge, request)
                    judge_latency = _elapsed_ms(judge_started, clock)
                    latency["judge"] = judge_latency
                    judge_failure = _validate_judge_result(candidate_result, request, sampled, config, repair_result)
                    if judge_failure is not None:
                        evidence = _sorted_evidence((*evidence, _invalid(sampled, config, "semantic_video_critic", judge_failure)))
                        proposed, confidence = QualityDecision.REVIEW, _expert_confidence(evidence)
                    else:
                        judge_result = replace(candidate_result, evidence=replace(candidate_result.evidence, latency_ms=judge_latency))
                        evidence = _contextualize((*evidence, judge_result.evidence), sampled, config)
                        proposed, confidence = judge_result.decision, judge_result.confidence
                except Exception as error:
                    latency["judge"] = _elapsed_ms(judge_started, clock)
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
    latency["total"] = _elapsed_ms(total_started, clock)
    provenance = _provenance(candidate_id, video_path, source_hash, start_ts, end_ts, sampled.timestamps, config, evidence, repair_result, judge_result, latency)
    return _annotate(assessment, input_hash=input_hash, evidence=evidence, provenance=provenance, reason_codes=reason_codes, summary=summary)


def evaluate_candidates(
    candidates: Sequence[Mapping[str, object]], **kwargs: object,
) -> QualityBatchResult:
    """Evaluate every candidate independently and apply report/active routing."""
    config = kwargs.get("config")
    if not isinstance(config, QualityMoeConfig):
        raise ValueError("config must be a QualityMoeConfig")
    identities: dict[str, _SourceIdentity] = {}
    candidate_identities: list[_SourceIdentity | None] = []
    for candidate in candidates:
        identity: _SourceIdentity | None = None
        if isinstance(candidate, Mapping):
            try:
                _candidate_id, video_path, _start, _end = _candidate_fields(candidate)
                if not hard_gate_reasons(candidate):
                    normalized_path = _normal_path(video_path)
                    identity = identities.get(normalized_path)
                    if identity is None:
                        identity = _SourceIdentity(
                            normalized_path=normalized_path,
                            source_path=video_path,
                            sha256=_source_hash(video_path),
                        )
                        identities[normalized_path] = identity
            except ValueError:
                identity = None
        candidate_identities.append(identity)

    assessments: list[QualityAssessment] = []
    effective: list[dict[str, object]] = []
    human: list[dict[str, object]] = []
    for candidate, identity in zip(candidates, candidate_identities):
        try:
            candidate_kwargs = dict(kwargs)
            if identity is not None:
                candidate_kwargs["source_identity"] = identity
                candidate_kwargs["verify_source_after_sampling"] = False
            assessment = evaluate_candidate(candidate, **candidate_kwargs)  # type: ignore[arg-type]
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

    for identity in identities.values():
        if identity.sha256 is None:
            continue
        current_hash = _source_hash(identity.source_path)
        if current_hash != identity.sha256:
            raise ValueError(
                f"source changed during quality evaluation: {identity.source_path}"
            )
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


def _validate_sampled(sampled: object, candidate_id: str, video_path: str, start_ts: float, end_ts: float, source_hash: str, candidate: Mapping[str, object]) -> str | None:
    if not isinstance(sampled, SampledClip):
        return "sampling_invalid"
    if sampled.status is not EvidenceStatus.AVAILABLE or not sampled.frames:
        return "sampling_unavailable"
    if sampled.candidate_id != candidate_id or _normal_path(sampled.video_path) != _normal_path(video_path):
        return "sampling_context_mismatch"
    if abs(sampled.start_ts - start_ts) > 1e-6 or abs(sampled.end_ts - end_ts) > 1e-6:
        return "sampling_context_mismatch"
    if any(timestamp < start_ts - 1e-6 or timestamp > end_ts + 1e-6 for timestamp in sampled.timestamps):
        return "sampling_context_mismatch"
    declared_hash = candidate.get("source_file_sha256")
    if declared_hash is not None and (not isinstance(declared_hash, str) or declared_hash != source_hash):
        return "source_hash_mismatch"
    declared_input_hash = candidate.get("input_hash")
    if declared_input_hash is not None and (not isinstance(declared_input_hash, str) or declared_input_hash != sampled.input_hash):
        return "input_hash_mismatch"
    return None


def _normal_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(str(Path(value).resolve(strict=False))))


def _coverage_failure(evidence: Sequence[ExpertEvidence]) -> str | None:
    expected = {"nr_vqa", "deterministic_temporal", "cinematic_classifier"}
    available = [item for item in evidence if item.status is EvidenceStatus.AVAILABLE]
    families = [item.signal_family for item in available]
    return None if len(available) == 3 and set(families) == expected and len(set(families)) == 3 else "expert_coverage_invalid"


def _validate_repair_context(result: RepairSearchResult | None, sampled: SampledClip, config: QualityMoeConfig) -> str | None:
    if result is None or result.best_recipe is None or result.best_proxy is None or result.repair_delta is None:
        return "repair_proxy_missing"
    recipe, proxy, delta, validation = result.best_recipe, result.best_proxy, result.repair_delta, result.best_recipe.validation
    if proxy.status is not EvidenceStatus.AVAILABLE or not proxy.frames:
        return "repair_proxy_unavailable"
    if proxy.candidate_id != sampled.candidate_id or _normal_path(proxy.video_path) != _normal_path(sampled.video_path):
        return "repair_proxy_context_mismatch"
    if abs(proxy.start_ts - sampled.start_ts) > 1e-6 or abs(proxy.end_ts - sampled.end_ts) > 1e-6:
        return "repair_proxy_context_mismatch"
    if len(proxy.frames) != len(sampled.frames) or len(proxy.timestamps) != len(sampled.timestamps):
        return "repair_proxy_shape_mismatch"
    if any(abs(actual - expected) > 1e-6 for actual, expected in zip(proxy.timestamps, sampled.timestamps)):
        return "repair_proxy_timestamp_mismatch"
    if any(actual.shape != expected.shape for actual, expected in zip(proxy.frames, sampled.frames)):
        return "repair_proxy_shape_mismatch"
    if validation is None or validation.candidate_id != sampled.candidate_id or validation.evaluation_version != config.evaluation_version or validation.config_hash != config.config_hash:
        return "repair_validation_context_mismatch"
    if validation.source_input_hash != sampled.input_hash or validation.proxy_artifact_hash != proxy.input_hash or validation.recipe_hash != recipe.recipe_hash:
        return "repair_validation_hash_mismatch"
    if delta.identity_hash != validation.repair_delta_evidence_id or delta.input_hash != proxy.input_hash or delta.parent_input_hash != sampled.input_hash or not _matches_context(delta, sampled, config):
        return "repair_delta_context_mismatch"
    return None


def _validate_judge_result(result: object, request: JudgeRequest, sampled: SampledClip, config: QualityMoeConfig, repair_result: RepairSearchResult) -> str | None:
    if not isinstance(result, JudgeResult):
        return "judge_result_invalid"
    if result.config_hash != config.config_hash or result.evidence.config_hash != config.config_hash or result.evidence.candidate_id != sampled.candidate_id or result.evidence.evaluation_version != config.evaluation_version or result.evidence.input_hash != sampled.input_hash:
        return "judge_context_mismatch"
    expected_attempts = (_sha256_text(_prompt(request, correction=False)),)
    if result.attempts == 2:
        expected_attempts += (_sha256_text(_prompt(request, correction=True)),)
    if result.evidence.prompt_hash != result.prompt_hash or result.attempt_prompt_hashes != expected_attempts or result.prompt_hash != expected_attempts[-1]:
        return "judge_prompt_context_mismatch"
    model_id = config.judge.get("model_id")
    if isinstance(model_id, str) and model_id and result.model_hash != hashlib.sha256(model_id.encode("utf-8")).hexdigest():
        return "judge_model_context_mismatch"
    if result.decision is QualityDecision.KEEP_FOR_REPAIR and result.selected_recipe_id != repair_result.best_recipe.recipe_id:
        return "judge_recipe_mismatch"
    if result.selected_recipe_id is not None and result.selected_recipe_id not in request.allowed_recipe_ids:
        return "judge_recipe_mismatch"
    return None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _latencies(**values: int) -> dict[str, int]:
    result = {"sampler": 0, "experts": 0, "repair": 0, "judge": 0, "total": 0}
    result.update({name: max(0, int(value)) for name, value in values.items()})
    return result


def _elapsed_ms(started: int, clock: Callable[[], int]) -> int:
    return max(0, (clock() - started) // 1_000_000)


def _collect_evidence(experts: Sequence[_Expert], sampled: SampledClip, config: QualityMoeConfig, clock: Callable[[], int]) -> tuple[tuple[ExpertEvidence, ...], int]:
    def run(expert: _Expert) -> ExpertEvidence:
        started = clock()
        try:
            item = expert.evaluate(sampled)
            if not isinstance(item, ExpertEvidence):
                raise ValueError("expert returned invalid evidence")
            return replace(item, latency_ms=_elapsed_ms(started, clock))
        except Exception as error:
            return replace(_unavailable(sampled, config, getattr(expert, "signal_family", "unknown"), "expert_exception", str(error), expert_id=getattr(expert, "expert_id", "unknown")), latency_ms=_elapsed_ms(started, clock))
    started = clock()
    with ThreadPoolExecutor(max_workers=3) as pool:
        return tuple(pool.map(run, experts)), _elapsed_ms(started, clock)


def _needs_repair(evidence: Sequence[ExpertEvidence]) -> bool:
    available = [item for item in evidence if item.status is EvidenceStatus.AVAILABLE]
    scores = [value for item in available for value in item.scores.values()]
    polarities = {item.polarity for item in available}
    return bool(available) and (bool({EvidencePolarity.NEGATIVE, EvidencePolarity.POSITIVE} <= polarities) or any(item.polarity is EvidencePolarity.NEGATIVE for item in available) or (bool(scores) and min(scores) < 0.65))


def _judge_request(work_dir: str | Path, original: SampledClip, proxy: SampledClip, evidence: Sequence[ExpertEvidence], recipe_id: str) -> JudgeRequest:
    directory = Path(work_dir) / "quality_moe" / original.input_hash
    original_path = _contact_sheet(directory, "original", original.frames)
    proxy_path = _contact_sheet(directory, "best-proxy", proxy.frames)
    # Prompt construction is a deterministic projection; persisted evidence
    # retains measured latency separately for audit.
    prompt_evidence = tuple(replace(item, latency_ms=0) for item in _sorted_evidence(evidence))
    return JudgeRequest(original.candidate_id, original.input_hash, original_path, proxy_path, prompt_evidence, (recipe_id,))


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


def _invalid(sampled: SampledClip, config: QualityMoeConfig, family: str, code: str) -> ExpertEvidence:
    return ExpertEvidence(sampled.candidate_id, config.evaluation_version, family, "evaluator-v1", family, EvidenceStatus.INVALID, findings=({"code": code},), summary="Component output did not match the frozen evaluation context.", input_hash=sampled.input_hash, config_hash=config.config_hash)


def _sorted_evidence(evidence: Sequence[ExpertEvidence]) -> tuple[ExpertEvidence, ...]:
    return tuple(sorted(evidence, key=lambda item: (item.signal_family, item.expert_id)))


def _expert_confidence(evidence: Sequence[ExpertEvidence]) -> float:
    values = [value for item in evidence if item.status is EvidenceStatus.AVAILABLE for value in item.scores.values()]
    return max(0.0, min(1.0, sum(values) / len(values))) if values else 0.0


def _source_hash(path: str) -> str | None:
    digest = hashlib.sha256()
    try:
        source_path = Path(path)
        if not source_path.is_file():
            return None
        with source_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    value = digest.hexdigest()
    return value if len(value) == 64 and all(character in "0123456789abcdef" for character in value) else None


def _hash_json(value: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _provenance(candidate_id: str, video_path: str, source_hash: str, start_ts: float, end_ts: float, timestamps: Sequence[float], config: QualityMoeConfig, evidence: Sequence[ExpertEvidence], repair: RepairSearchResult | None, judge: JudgeResult | None, latency: Mapping[str, int]) -> dict[str, object]:
    payload: dict[str, object] = {
        "candidate_id": candidate_id, "source_video": video_path, "source_file_sha256": source_hash,
        "boundaries": {"start_ts": start_ts, "end_ts": end_ts}, "frame_timestamps": list(timestamps),
        "evaluation_version": config.evaluation_version, "config_hash": config.config_hash,
        "latency_ms": dict(latency),
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
    payload["evaluation_hash"] = _hash_json(_without_dynamic_values(payload))
    return payload


def _annotate(assessment: QualityAssessment, *, input_hash: str, evidence: Sequence[ExpertEvidence], provenance: Mapping[str, object], reason_codes: Sequence[str] = (), summary: str = "") -> QualityAssessment:
    return replace(assessment, input_hash=input_hash, evidence=_sorted_evidence(evidence), provenance=provenance, reason_codes=tuple(reason_codes), summary=summary)


def _review(candidate_id: str, video_path: str, source_hash: str, start_ts: float, end_ts: float, sampled: SampledClip, config: QualityMoeConfig, evidence: Sequence[ExpertEvidence], code: str, latency: Mapping[str, int]) -> QualityAssessment:
    assessment = enforce_decision(candidate_id=candidate_id, input_hash=sampled.input_hash, proposed=QualityDecision.REVIEW, confidence=_expert_confidence(evidence), evidence=tuple(item for item in evidence if _matches_context(item, sampled, config)), hard_reasons=(), repair=None, config=config)
    provenance = _provenance(candidate_id, video_path, source_hash, start_ts, end_ts, sampled.timestamps, config, evidence, None, None, latency)
    provenance["failure"] = {"code": code}
    provenance["evaluation_hash"] = _hash_json(_without_dynamic_values(provenance))
    return _annotate(assessment, input_hash=sampled.input_hash, evidence=evidence, provenance=provenance, summary="Complementary quality evidence was incomplete and needs human review.")


def _abstained(candidate_id: str, video_path: str, source_hash: str, start_ts: float, end_ts: float, input_hash: str, config: QualityMoeConfig, code: str, summary: str, latency: Mapping[str, int] | None = None) -> QualityAssessment:
    assessment = enforce_decision(candidate_id=candidate_id, input_hash=input_hash, proposed=QualityDecision.ABSTAIN, confidence=0.0, evidence=(), hard_reasons=(), repair=None, config=config)
    provenance = _provenance(candidate_id, video_path, source_hash, start_ts, end_ts, (), config, (), None, None, latency or _latencies(total=0))
    provenance["failure"] = {"code": code}
    provenance["evaluation_hash"] = _hash_json(_without_dynamic_values(provenance))
    return _annotate(assessment, input_hash=input_hash, evidence=(), provenance=provenance, summary=summary)


def _without_dynamic_values(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _without_dynamic_values(item) for key, item in value.items() if key not in {"latency_ms", "evaluation_hash"}}
    if isinstance(value, (tuple, list)):
        return [_without_dynamic_values(item) for item in value]
    return value

"""Quality MoE boundary glue between the pipeline and app.quality_moe."""
from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import httpx

from app.pipeline.vlm_runtime import _is_stable_http_url, _resolve_vlm_runtime
from app.quality_moe.config import QualityMoeConfig
from app.quality_moe.evaluator import QualityBatchResult, evaluate_candidates
from app.quality_moe.judge import OllamaQualityJudge
from app.quality_moe.models import (
    EvidenceStatus,
    QualityAssessment,
    QualityDecision,
    RepairRecipe,
    RepairValidation,
)


def _resolve_quality_runtime_snapshot(
    config_data: dict,
    *,
    auto_resolver=None,
) -> dict:
    """Resolve runtime URLs once at the Direct-mode startup boundary."""
    from app.quality_moe.config import freeze_quality_runtime_config

    if auto_resolver is None:
        return freeze_quality_runtime_config(config_data)

    def ready(runtime_config):
        from types import SimpleNamespace

        return SimpleNamespace(
            base_url=auto_resolver(
                _resolve_vlm_runtime(config_data), config_data
            )
        )

    return freeze_quality_runtime_config(config_data, ready_resolver=ready)


def _quality_config_from_pipeline_cfg(cfg: dict) -> QualityMoeConfig:
    raw = cfg.get("quality_moe")
    if raw is None:
        # Compatibility for direct unit/legacy callers which construct the
        # historical flat cfg by hand instead of going through extract_config.
        quality_config = QualityMoeConfig.from_mapping(
            {"quality_moe": {"enabled": False, "report_only": True}}
        )
        cfg["quality_moe"] = quality_config.to_dict()
        cfg["quality_moe_config_hash"] = quality_config.config_hash
        return quality_config
    quality_config = QualityMoeConfig.from_mapping({"quality_moe": raw})
    expected_hash = cfg.get("quality_moe_config_hash")
    if expected_hash is None:
        cfg["quality_moe_config_hash"] = quality_config.config_hash
    elif quality_config.config_hash != expected_hash:
        raise ValueError("quality_moe config hash does not match the frozen snapshot")
    judge_base_url = str(quality_config.judge.get("base_url", "") or "").strip()
    if judge_base_url and not _is_stable_http_url(judge_base_url):
        live = str(cfg.get("_live_vlm_base_url") or "").strip()
        if not live.startswith(("http://", "https://")):
            raise ValueError(
                "quality_moe judge base_url must be a frozen absolute URL"
            )
        judge = dict(quality_config.judge)
        judge["base_url"] = live.rstrip("/")
        quality_config = replace(quality_config, judge=judge)
        print(f"  [quality] judge base_url={judge['base_url']}", flush=True)
    return quality_config


def _quality_moe_summary(
    cfg: dict,
    assessments: list[dict],
    *,
    input_count: int,
    effective_count: int,
    human_review_count: int,
    assessed_candidates: list[dict] | None = None,
) -> dict:
    quality_config = _quality_config_from_pipeline_cfg(cfg)
    quality = quality_config.to_dict()
    decision_counts: dict[str, int] = {}
    for assessment in assessments:
        decision = str(assessment["effective_decision"])
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
    candidate_ledger = json.loads(json.dumps(assessed_candidates or []))
    return {
        "enabled": bool(quality["enabled"]),
        "report_only": bool(quality["report_only"]),
        "evaluation_version": str(quality["evaluation_version"]),
        "config_hash": quality_config.config_hash,
        "policy_snapshot": {
            "report_only": bool(quality["report_only"]),
            "min_judge_confidence": quality_config.soft_reject.min_judge_confidence,
            "min_independent_negative_families": (
                quality_config.soft_reject.min_independent_negative_families
            ),
            "policy_version": "quality-moe-policy-v1",
        },
        "input_count": input_count,
        "assessed_count": len(assessments),
        "effective_count": effective_count,
        "human_review_count": human_review_count,
        "decision_counts": decision_counts,
        "top_assessments": [
            {
                "candidate_id": assessment["candidate_id"],
                "effective_decision": assessment["effective_decision"],
                "confidence": assessment["confidence"],
            }
            for assessment in assessments[:10]
        ],
        "assessments": assessments,
        "assessed_candidates": candidate_ledger,
        "assessed_candidates_digest": _quality_evidence_hash(candidate_ledger),
        "candidate_ledger": {"mode": "embedded"},
    }


def _quality_evidence_hash(evidence: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


_QUALITY_HARD_GATE_INPUT_FIELDS = (
    "transition_action",
    "action_completeness_score",
    "media_decodable",
    "decode_ok",
)


def _quality_hard_gate_context(candidate: dict | None) -> dict:
    candidate = candidate or {}
    return {
        field: candidate[field]
        for field in _QUALITY_HARD_GATE_INPUT_FIELDS
        if field in candidate
    }


def _quality_candidate_ledger(
    candidates: list[dict], assessments: list[dict] | None = None,
) -> list[dict]:
    assessments_by_id = {
        str(item.get("candidate_id")): item
        for item in assessments or []
        if isinstance(item, dict)
    }
    ledger: list[dict] = []
    for candidate in candidates:
        candidate_id = str(candidate["candidate_id"])
        entry = {
            "candidate_id": str(candidate["candidate_id"]),
            "hard_gate_context": _quality_hard_gate_context(candidate),
            "hard_gate_context_hash": _quality_evidence_hash(
                _quality_hard_gate_context(candidate)
            ),
        }
        assessment = assessments_by_id.get(candidate_id)
        provenance = assessment.get("provenance") if assessment else None
        provenance = provenance if isinstance(provenance, dict) else {}
        source_sha = provenance.get("source_file_sha256")
        if isinstance(source_sha, str) and re.fullmatch(r"[0-9a-f]{64}", source_sha):
            source_path = os.path.abspath(str(candidate["video_path"]))
            try:
                source_stat = os.stat(source_path)
            except OSError as exc:
                raise ValueError(
                    f"quality source became unavailable: {source_path}"
                ) from exc
            entry["source_identity"] = {
                "video_path": source_path,
                "source_file_sha256": source_sha,
                "size_bytes": source_stat.st_size,
                "mtime_ns": source_stat.st_mtime_ns,
            }
        ledger.append(entry)
    return ledger


def _enrich_quality_assessment(
    assessment: dict,
    *,
    candidate: dict | None = None,
) -> dict:
    enriched = json.loads(json.dumps(assessment))
    hard_gate_context = _quality_hard_gate_context(candidate)
    enriched["hard_gate_context"] = hard_gate_context
    enriched["hard_gate_context_hash"] = _quality_evidence_hash(hard_gate_context)
    evidence = enriched.get("evidence", [])
    enriched["evidence_hashes"] = [
        _quality_evidence_hash(item) for item in evidence if isinstance(item, dict)
    ]
    repair = enriched.get("repair")
    selected_recipe_id = None
    if (
        enriched.get("effective_decision") == QualityDecision.KEEP_FOR_REPAIR.value
        and isinstance(repair, dict)
    ):
        selected_recipe_id = repair.get("recipe_id")
    enriched["selected_recipe_id"] = selected_recipe_id

    available_scores = [
        float(score)
        for item in evidence
        if isinstance(item, dict)
        and item.get("status") == EvidenceStatus.AVAILABLE.value
        and item.get("signal_family") != "repair_delta"
        for score in (item.get("scores") or {}).values()
        if isinstance(score, (int, float)) and not isinstance(score, bool)
    ]
    repaired_scores = [
        float(score)
        for item in evidence
        if isinstance(item, dict)
        and item.get("status") == EvidenceStatus.AVAILABLE.value
        and item.get("signal_family") == "repair_delta"
        for score in (item.get("scores") or {}).values()
        if isinstance(score, (int, float)) and not isinstance(score, bool)
    ]
    current_quality = (
        sum(available_scores) / len(available_scores) if available_scores else None
    )
    quality_gain = repair.get("quality_gain") if isinstance(repair, dict) else None
    enriched["current_quality"] = current_quality
    if repaired_scores:
        enriched["recoverable_quality"] = sum(repaired_scores) / len(repaired_scores)
    elif (
        current_quality is not None
        and isinstance(quality_gain, (int, float))
        and not isinstance(quality_gain, bool)
    ):
        enriched["recoverable_quality"] = min(
            1.0, current_quality + float(quality_gain)
        )
    else:
        enriched["recoverable_quality"] = current_quality
    return enriched


def _evaluate_quality_pipeline_candidates(
    candidates: list[dict],
    *,
    video_path: str,
    cfg: dict,
    work_dir: str | Path,
) -> tuple[list[dict], dict]:
    """Run the one shared quality boundary for direct and staged candidates."""
    quality_config = _quality_config_from_pipeline_cfg(cfg)

    normalized: list[dict] = []
    candidate_ids: list[str] = []
    for candidate in candidates:
        clip = deepcopy(candidate)
        candidate_id = clip.get("candidate_id") or clip.get("clip_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError("quality candidate_id must be a non-empty string")
        if candidate_id in candidate_ids:
            raise ValueError(f"quality candidate_id must be unique: {candidate_id}")
        candidate_ids.append(candidate_id)
        clip["candidate_id"] = candidate_id
        clip.setdefault("clip_id", candidate_id)
        clip["video_path"] = video_path
        normalized.append(clip)

    if not quality_config.enabled:
        return normalized, _quality_moe_summary(
            cfg, [], input_count=len(normalized), effective_count=len(normalized),
            human_review_count=0, assessed_candidates=[],
        )

    judge = (
        OllamaQualityJudge(quality_config, httpx.HTTPTransport())
        if quality_config.judge.get("model_id")
        else None
    )
    batch = evaluate_candidates(
        normalized,
        config=quality_config,
        work_dir=work_dir,
        judge=judge,
    )
    if not isinstance(batch, QualityBatchResult):
        raise ValueError("quality evaluator returned an invalid batch")
    if any(
        not isinstance(assessment, QualityAssessment)
        for assessment in batch.assessments
    ):
        raise ValueError("quality batch assessments must be QualityAssessment values")
    assessment_ids = [assessment.candidate_id for assessment in batch.assessments]
    if assessment_ids != candidate_ids:
        raise ValueError(
            "quality batch assessments must map one-to-one in input order"
        )

    def routed_ids(values, *, route_name: str) -> list[str]:
        ids: list[str] = []
        for value in values:
            if not isinstance(value, dict):
                raise ValueError(f"quality batch {route_name} routing is invalid")
            candidate_id = value.get("candidate_id")
            if (
                not isinstance(candidate_id, str)
                or candidate_id not in candidate_ids
                or candidate_id in ids
            ):
                raise ValueError(f"quality batch {route_name} routing is invalid")
            ids.append(candidate_id)
        return ids

    effective_ids = routed_ids(
        batch.effective_clips, route_name="effective_clips"
    )
    human_ids = routed_ids(
        batch.human_review_clips, route_name="human_review_clips"
    )
    keep_decisions = {
        QualityDecision.KEEP_AS_IS, QualityDecision.KEEP_FOR_REPAIR,
    }
    expected_effective_ids = [
        assessment.candidate_id
        for assessment in batch.assessments
        if (
            quality_config.report_only and not assessment.hard_reasons
        ) or assessment.effective_decision in keep_decisions
    ]
    expected_human_ids = [
        assessment.candidate_id
        for assessment in batch.assessments
        if assessment.effective_decision in {
            QualityDecision.REVIEW, QualityDecision.ABSTAIN,
        }
    ]
    if (
        effective_ids != expected_effective_ids
        or human_ids != expected_human_ids
    ):
        raise ValueError("quality batch routing does not match assessments")

    candidates_by_id = {
        str(candidate["candidate_id"]): candidate for candidate in normalized
    }
    assessments = [
        _enrich_quality_assessment(
            assessment.to_dict(),
            candidate=candidates_by_id.get(assessment.candidate_id),
        )
        for assessment in batch.assessments
    ]
    assessed_candidates = _quality_candidate_ledger(normalized, assessments)
    by_candidate_id = {
        assessment["candidate_id"]: assessment for assessment in assessments
    }
    routed: list[dict] = []
    route_ids = candidate_ids if quality_config.report_only else effective_ids
    for candidate_id in route_ids:
        clip = deepcopy(candidates_by_id[candidate_id])
        clip["quality_assessment"] = by_candidate_id[candidate_id]
        routed.append(clip)
    summary = _quality_moe_summary(
        cfg,
        assessments,
        input_count=len(normalized),
        effective_count=len(routed),
        human_review_count=len(human_ids),
        assessed_candidates=assessed_candidates,
    )
    return routed, summary


def _validated_repair_recipe(
    assessment: object,
    *,
    candidate_id: str,
    config_hash: str,
) -> RepairRecipe | None:
    """Return the bound recipe only for an effective validated repair KEEP."""
    if not isinstance(assessment, dict):
        return None
    if assessment.get("effective_decision") != QualityDecision.KEEP_FOR_REPAIR.value:
        return None
    if (
        assessment.get("candidate_id") != candidate_id
        or assessment.get("config_hash") != config_hash
    ):
        return None
    repair_data = assessment.get("repair")
    if not isinstance(repair_data, dict):
        return None
    if assessment.get("selected_recipe_id") != repair_data.get("recipe_id"):
        return None
    validation_data = repair_data.get("validation")
    if not isinstance(validation_data, dict):
        return None
    try:
        validation = RepairValidation(
            candidate_id=validation_data["candidate_id"],
            evaluation_version=validation_data["evaluation_version"],
            source_input_hash=validation_data["source_input_hash"],
            proxy_artifact_hash=validation_data["proxy_artifact_hash"],
            recipe_hash=validation_data["recipe_hash"],
            config_hash=validation_data["config_hash"],
            repair_delta_evidence_id=validation_data["repair_delta_evidence_id"],
            repair_delta_status=validation_data["repair_delta_status"],
        )
        recipe = RepairRecipe(
            recipe_id=repair_data["recipe_id"],
            exposure_ev=repair_data.get("exposure_ev", 0.0),
            gamma=repair_data.get("gamma", 1.0),
            contrast=repair_data.get("contrast", 0.0),
            shadows=repair_data.get("shadows", 0.0),
            highlights=repair_data.get("highlights", 0.0),
            white_balance=repair_data.get("white_balance", (1.0, 1.0, 1.0)),
            crop=repair_data.get("crop", (0.0, 0.0, 1.0, 1.0)),
            zoom=repair_data.get("zoom", 1.0),
            rotation_degrees=repair_data.get("rotation_degrees", 0.0),
            perspective_corner_movement=repair_data.get("perspective_corner_movement", 0.0),
            quality_gain=repair_data.get("quality_gain", 0.0),
            confidence=repair_data.get("confidence", 0.0),
            validation=validation,
        ).validate()
    except (KeyError, TypeError, ValueError):
        return None
    matching_delta = any(
        _quality_evidence_hash(item) == validation.repair_delta_evidence_id
        and item.get("signal_family") == "repair_delta"
        and item.get("status") == EvidenceStatus.AVAILABLE.value
        and item.get("polarity") == "POSITIVE"
        and item.get("candidate_id") == candidate_id
        and item.get("evaluation_version") == assessment.get("evaluation_version")
        and item.get("config_hash") == config_hash
        and item.get("input_hash") == validation.proxy_artifact_hash
        and item.get("parent_input_hash") == validation.source_input_hash
        for item in assessment.get("evidence", [])
        if isinstance(item, dict)
    )
    if (
        validation.candidate_id != candidate_id
        or validation.evaluation_version != assessment.get("evaluation_version")
        or validation.config_hash != config_hash
        or validation.source_input_hash != assessment.get("input_hash")
        or validation.recipe_hash != recipe.recipe_hash
        or validation.repair_delta_status is not EvidenceStatus.AVAILABLE
        or not matching_delta
    ):
        return None
    return recipe


def _export_repair_recipe(
    assessment: object,
    *,
    candidate_id: str,
    quality_config: QualityMoeConfig,
) -> RepairRecipe | None:
    """Resolve the applied recipe under the frozen report/active policy."""
    if quality_config.report_only:
        return None
    return _validated_repair_recipe(
        assessment,
        candidate_id=candidate_id,
        config_hash=quality_config.config_hash,
    )


_QUALITY_LINEAGE_FIELDS = (
    "quality_decision",
    "current_quality",
    "recoverable_quality",
    "repair_applied",
    "recommended_recipe_id",
    "recommended_recipe",
    "applied_recipe_id",
    "applied_recipe",
    "evidence_hashes",
    "config_hash",
    "parent_source",
)


_QUALITY_SOURCE_HASH_CACHE: dict[tuple[object, ...], str] = {}


def _stable_source_sha256(video_path: str) -> str:
    """Hash a stable source once per process/stat identity."""
    absolute_path = os.path.abspath(video_path)
    try:
        before = os.stat(absolute_path)
    except OSError as exc:
        raise ValueError(f"quality source is unavailable: {absolute_path}") from exc
    stat_identity = (
        os.path.normcase(absolute_path),
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        getattr(before, "st_ino", 0),
    )
    cached = _QUALITY_SOURCE_HASH_CACHE.get(stat_identity)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    try:
        with open(absolute_path, "rb") as source_file:
            for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                digest.update(chunk)
        after = os.stat(absolute_path)
    except OSError as exc:
        raise ValueError(f"quality source is unavailable: {absolute_path}") from exc
    after_identity = (
        os.path.normcase(absolute_path),
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        getattr(after, "st_ino", 0),
    )
    if after_identity != stat_identity:
        raise ValueError("quality source changed while its identity was verified")
    value = digest.hexdigest()
    _QUALITY_SOURCE_HASH_CACHE[stat_identity] = value
    return value


def _quality_source_sha256(assessment: object) -> str | None:
    if not isinstance(assessment, dict):
        return None
    provenance = assessment.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    expected = provenance.get("source_file_sha256")
    if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        return None
    return expected


def _assert_quality_source_unchanged(
    video_path: str, assessment: object,
) -> str | None:
    if not isinstance(assessment, dict):
        return None
    expected = _quality_source_sha256(assessment)
    if expected is None:
        # Hard-gated / unavailable assessments use sentinels like
        # "not_checked" and must not abort GIF export.
        return None
    actual = _stable_source_sha256(video_path)
    if actual != expected:
        raise ValueError("quality source changed after assessment")
    return expected


def _quality_export_lineage(
    assessment: object,
    *,
    candidate_id: str,
    video_path: str,
    start_ts: float,
    end_ts: float,
    config_hash: str,
    repair_applied: bool,
) -> dict:
    if not isinstance(assessment, dict):
        return {}
    provenance = assessment.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    source_file_sha256 = provenance.get("source_file_sha256")
    if not isinstance(source_file_sha256, str):
        source_file_sha256 = None
    recommended_recipe = assessment.get("repair")
    recommended_recipe_id = assessment.get("selected_recipe_id")
    applied_recipe = recommended_recipe if repair_applied else None
    applied_recipe_id = recommended_recipe_id if repair_applied else None
    return {
        "quality_decision": assessment.get("effective_decision"),
        "current_quality": assessment.get("current_quality"),
        "recoverable_quality": assessment.get("recoverable_quality"),
        "repair_applied": repair_applied,
        "recommended_recipe_id": recommended_recipe_id,
        "recommended_recipe": recommended_recipe,
        "applied_recipe_id": applied_recipe_id,
        "applied_recipe": applied_recipe,
        "evidence_hashes": list(assessment.get("evidence_hashes", [])),
        "config_hash": config_hash,
        "parent_source": {
            "candidate_id": candidate_id,
            "input_hash": assessment.get("input_hash"),
            "source_file_sha256": source_file_sha256,
            "video_path": os.path.abspath(video_path),
            "start_ts": start_ts,
            "end_ts": end_ts,
        },
    }

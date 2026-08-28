"""Manifest v2 payload validators: action clips, quality, gif lineage."""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path


_ACTION_CLIP_REQUIRED_FIELDS = (
    "action_boundary_mode",
    "action_boundary_confidence",
    "action_vlm_verified",
    "action_analysis_version",
    "guarded_export_window",
    "start_ts",
    "end_ts",
)
_ACTION_NULLABLE_NUMERIC_FIELDS = (
    "action_start_ts",
    "action_peak_ts",
    "action_end_ts",
    "action_completeness_score",
    "action_boundary_confidence",
    "loop_quality_score",
)
_QUALITY_DECISIONS = frozenset(
    {"KEEP_AS_IS", "KEEP_FOR_REPAIR", "REVIEW", "REJECT", "ABSTAIN"}
)
_QUALITY_EVIDENCE_STATUSES = frozenset(
    {"AVAILABLE", "UNAVAILABLE", "ABSTAINED", "INVALID"}
)
_QUALITY_EVIDENCE_POLARITIES = frozenset(
    {"POSITIVE", "NEGATIVE", "NEUTRAL"}
)
_QUALITY_HARD_REASONS = frozenset({
    "transition_drop",
    "transition_unverified",
    "action_completeness_invalid",
    "action_incomplete",
    "media_undecodable",
})
_QUALITY_HARD_GATE_INPUT_FIELDS = frozenset({
    "transition_action",
    "action_completeness_score",
    "media_decodable",
    "decode_ok",
})
# Hard-gated / unreadable source assessments skip hashing the media file.
# gif_clip writes these sentinels into parent_source; materialize must accept them.
_QUALITY_SOURCE_SHA_SENTINELS = frozenset({"not_checked", "unavailable"})


def _require_v2_field(
    value: dict, field: str, *, context: str
) -> object:
    if field not in value:
        raise ValueError(f"{context} missing required field: {field}")
    return value[field]


def _finite_number(
    value: object, field: str, *, context: str, nullable: bool = False
) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context} {field} must be a finite number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{context} {field} must be a finite number")
    return parsed


def _validate_action_clip_v2(clip: object, *, context: str) -> None:
    if not isinstance(clip, dict):
        raise ValueError(f"{context} must be an object")
    for field in _ACTION_CLIP_REQUIRED_FIELDS:
        _require_v2_field(clip, field, context=context)

    mode = clip["action_boundary_mode"]
    if not isinstance(mode, str) or not mode.strip():
        raise ValueError(
            f"{context} action_boundary_mode must be a non-empty string"
        )
    start_ts = _finite_number(clip["start_ts"], "start_ts", context=context)
    end_ts = _finite_number(clip["end_ts"], "end_ts", context=context)
    duration = float(end_ts) - float(start_ts)
    if duration < 2.0 - 1e-9 or duration > 20.0 + 1e-9:
        raise ValueError(
            f"{context} duration {duration:.6f}s is outside [2.0, 20.0]"
        )
    if not isinstance(clip["action_vlm_verified"], bool):
        raise ValueError(f"{context} action_vlm_verified must be a boolean")
    version = clip["action_analysis_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise ValueError(
            f"{context} action_analysis_version must be a positive integer"
        )
    if clip["guarded_export_window"] is not True:
        raise ValueError(f"{context} guarded_export_window must be true")

    for field in _ACTION_NULLABLE_NUMERIC_FIELDS:
        if field in clip:
            _finite_number(
                clip[field], field, context=context, nullable=True
            )
    split_index_present = "action_split_index" in clip
    split_count_present = "action_split_count" in clip
    if split_index_present != split_count_present:
        raise ValueError(
            f"{context} action_split_index and action_split_count "
            "must be provided together"
        )
    if split_index_present:
        split_index = clip["action_split_index"]
        split_count = clip["action_split_count"]
        if (
            isinstance(split_index, bool)
            or not isinstance(split_index, int)
            or isinstance(split_count, bool)
            or not isinstance(split_count, int)
            or split_index <= 0
            or split_count <= 0
            or split_index > split_count
        ):
            raise ValueError(
                f"{context} action split indexes must be positive integers "
                "with action_split_index <= action_split_count"
            )
    if "transition_risk" in clip:
        _finite_number(
            clip["transition_risk"],
            "transition_risk",
            context=context,
            nullable=True,
        )


def _validate_action_guard_v2(value: object) -> None:
    context = "rank_dedup_manifest action_guard"
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    for field in (
        "action_config_hash",
        "action_analysis_version",
        "input",
        "output",
        "cv_ms",
        "vlm_ms",
        "total_ms",
    ):
        _require_v2_field(value, field, context=context)
    action_hash = value["action_config_hash"]
    if not isinstance(action_hash, str) or not action_hash.strip():
        raise ValueError(
            f"{context} action_config_hash must be a non-empty string"
        )
    version = value["action_analysis_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise ValueError(
            f"{context} action_analysis_version must be a positive integer"
        )
    for field in ("input", "output"):
        count = value[field]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"{context} {field} must be a non-negative integer")
    for field in ("cv_ms", "vlm_ms", "total_ms"):
        elapsed = _finite_number(value[field], field, context=context)
        if float(elapsed) < 0.0:
            raise ValueError(f"{context} {field} must be non-negative")


def _quality_hash(value: object, field: str, *, context: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{context} {field} must be 64 lowercase hexadecimal characters")
    return value


def _quality_source_file_sha256(value: object, field: str, *, context: str) -> str:
    if isinstance(value, str) and value in _QUALITY_SOURCE_SHA_SENTINELS:
        return value
    return _quality_hash(value, field, context=context)


def _validate_quality_repair(value: object, *, assessment: dict, context: str):
    from app.quality_moe.models import RepairRecipe, RepairValidation

    if not isinstance(value, dict):
        raise ValueError(f"{context} repair must be an object")
    validation_data = value.get("validation")
    if not isinstance(validation_data, dict):
        raise ValueError(f"{context} repair validation must be an object")
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
            recipe_id=value["recipe_id"],
            exposure_ev=value.get("exposure_ev", 0.0),
            gamma=value.get("gamma", 1.0),
            contrast=value.get("contrast", 0.0),
            shadows=value.get("shadows", 0.0),
            highlights=value.get("highlights", 0.0),
            white_balance=value.get("white_balance", (1.0, 1.0, 1.0)),
            crop=value.get("crop", (0.0, 0.0, 1.0, 1.0)),
            zoom=value.get("zoom", 1.0),
            rotation_degrees=value.get("rotation_degrees", 0.0),
            perspective_corner_movement=value.get("perspective_corner_movement", 0.0),
            quality_gain=value.get("quality_gain", 0.0),
            confidence=value.get("confidence", 0.0),
            validation=validation,
        ).validate()
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{context} repair is invalid: {exc}") from exc
    _quality_hash(value.get("recipe_hash"), "repair.recipe_hash", context=context)
    for field in (
        "source_input_hash", "proxy_artifact_hash", "recipe_hash",
        "config_hash", "repair_delta_evidence_id",
    ):
        _quality_hash(validation_data.get(field), f"repair.validation.{field}", context=context)
    if value["recipe_hash"] != recipe.recipe_hash or validation.recipe_hash != recipe.recipe_hash:
        raise ValueError(f"{context} repair recipe_hash does not match the recipe")
    matching_delta = any(
        hashlib.sha256(
            json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest() == validation.repair_delta_evidence_id
        and item.get("signal_family") == "repair_delta"
        and item.get("status") == "AVAILABLE"
        and item.get("polarity") == "POSITIVE"
        and item.get("candidate_id") == assessment["candidate_id"]
        and item.get("evaluation_version") == assessment["evaluation_version"]
        and item.get("config_hash") == assessment["config_hash"]
        and item.get("input_hash") == validation.proxy_artifact_hash
        and item.get("parent_input_hash") == validation.source_input_hash
        for item in assessment.get("evidence", [])
        if isinstance(item, dict)
    )
    if (
        validation.candidate_id != assessment["candidate_id"]
        or validation.evaluation_version != assessment["evaluation_version"]
        or validation.config_hash != assessment["config_hash"]
        or validation.source_input_hash != assessment.get("input_hash")
        or validation.repair_delta_status.value != "AVAILABLE"
        or not matching_delta
    ):
        raise ValueError(f"{context} repair validation does not match assessment context")
    return recipe


def _validate_quality_assessment(value: object, *, context: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    for field in (
        "candidate_id",
        "evaluation_version",
        "config_hash",
        "policy_version",
        "recommended_decision",
        "effective_decision",
        "confidence",
    ):
        _require_v2_field(value, field, context=context)
    for field in ("recommended_decision", "effective_decision"):
        if value[field] not in _QUALITY_DECISIONS:
            raise ValueError(f"{context} {field} has an unknown quality decision")
    confidence = _finite_number(value["confidence"], "confidence", context=context)
    if not 0.0 <= float(confidence) <= 1.0:
        raise ValueError(f"{context} confidence must be in [0, 1]")
    _quality_hash(value["config_hash"], "config_hash", context=context)
    if "input_hash" in value:
        _quality_hash(value["input_hash"], "input_hash", context=context)
    if "decision" in value and value["decision"] != value["recommended_decision"]:
        raise ValueError(f"{context} decision must match recommended_decision")
    hard_reasons = value.get("hard_reasons", [])
    if not isinstance(hard_reasons, list) or any(
        reason not in _QUALITY_HARD_REASONS for reason in hard_reasons
    ):
        raise ValueError(f"{context} hard_reasons contains an unknown hard gate")
    hard_gate_context = value.get("hard_gate_context")
    if not isinstance(hard_gate_context, dict) or (
        set(hard_gate_context) - _QUALITY_HARD_GATE_INPUT_FIELDS
    ):
        raise ValueError(f"{context} hard_gate_context is invalid")
    expected_context_hash = hashlib.sha256(
        json.dumps(
            hard_gate_context,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    if value.get("hard_gate_context_hash") != expected_context_hash:
        raise ValueError(f"{context} hard_gate_context_hash does not match context")
    evidence = value.get("evidence", [])
    if not isinstance(evidence, list):
        raise ValueError(f"{context} evidence must be an array")
    for index, item in enumerate(evidence):
        evidence_context = f"{context} evidence[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{evidence_context} must be an object")
        if item.get("status") not in _QUALITY_EVIDENCE_STATUSES:
            raise ValueError(f"{evidence_context} status is unknown")
        if item.get("polarity") not in _QUALITY_EVIDENCE_POLARITIES:
            raise ValueError(f"{evidence_context} polarity is unknown")
        scores = item.get("scores", {})
        if not isinstance(scores, dict):
            raise ValueError(f"{evidence_context} scores must be an object")
        for score_name, score in scores.items():
            parsed = _finite_number(
                score, str(score_name), context=evidence_context
            )
            if not 0.0 <= float(parsed) <= 1.0:
                raise ValueError(
                    f"{evidence_context} {score_name} must be in [0, 1]"
                )
        for field in ("input_hash", "config_hash"):
            _quality_hash(item.get(field), field, context=evidence_context)
        for field in ("parent_input_hash", "prompt_hash"):
            if item.get(field) is not None:
                _quality_hash(item[field], field, context=evidence_context)
        for field in ("candidate_id", "evaluation_version", "config_hash"):
            if item.get(field) != value.get(field):
                raise ValueError(f"{evidence_context} {field} does not match assessment")
    evidence_hashes = value.get("evidence_hashes", [])
    if not isinstance(evidence_hashes, list):
        raise ValueError(f"{context} evidence_hashes must be an array")
    for evidence_hash in evidence_hashes:
        _quality_hash(evidence_hash, "evidence_hashes", context=context)
    actual_evidence_hashes = [
        hashlib.sha256(
            json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        for item in evidence
    ]
    if evidence_hashes != actual_evidence_hashes:
        raise ValueError(f"{context} evidence_hashes do not match evidence")
    for field in ("current_quality", "recoverable_quality"):
        if field in value and value[field] is not None:
            score = _finite_number(value[field], field, context=context)
            if not 0.0 <= float(score) <= 1.0:
                raise ValueError(f"{context} {field} must be in [0, 1]")
    selected_recipe_id = value.get("selected_recipe_id")
    if value["effective_decision"] == "KEEP_FOR_REPAIR":
        if not isinstance(selected_recipe_id, str) or not selected_recipe_id:
            raise ValueError(f"{context} selected_recipe_id is required")
        _validate_quality_repair(value.get("repair"), assessment=value, context=context)
        if value["repair"].get("recipe_id") != selected_recipe_id:
            raise ValueError(f"{context} selected_recipe_id does not match repair")
    elif selected_recipe_id is not None:
        raise ValueError(f"{context} selected_recipe_id is only valid for KEEP_FOR_REPAIR")


def _candidate_ledger_digest(candidates: list[dict]) -> str:
    return hashlib.sha256(
        json.dumps(
            candidates,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _validate_assessed_candidates(value: object, *, context: str) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be an array")
    candidate_ids: list[str] = []
    for index, candidate in enumerate(value):
        item_context = f"{context}[{index}]"
        if not isinstance(candidate, dict):
            raise ValueError(f"{item_context} must be an object")
        candidate_id = candidate.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError(f"{item_context} candidate_id must be non-empty")
        hard_context = candidate.get("hard_gate_context")
        if not isinstance(hard_context, dict) or (
            set(hard_context) - _QUALITY_HARD_GATE_INPUT_FIELDS
        ):
            raise ValueError(f"{item_context} hard_gate_context is invalid")
        context_hash = hashlib.sha256(
            json.dumps(
                hard_context,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        if candidate.get("hard_gate_context_hash") != context_hash:
            raise ValueError(
                f"{item_context} hard_gate_context_hash does not match context"
            )
        source_identity = candidate.get("source_identity")
        if source_identity is not None:
            if not isinstance(source_identity, dict):
                raise ValueError(f"{item_context} source_identity must be an object")
            required_source_fields = {
                "video_path", "source_file_sha256", "size_bytes", "mtime_ns",
            }
            if set(source_identity) != required_source_fields:
                raise ValueError(
                    f"{item_context} source_identity fields are invalid"
                )
            source_path = source_identity["video_path"]
            if not isinstance(source_path, str) or not source_path or not Path(source_path).is_absolute():
                raise ValueError(
                    f"{item_context} source_identity video_path must be absolute"
                )
            _quality_hash(
                source_identity["source_file_sha256"],
                "source_identity.source_file_sha256",
                context=item_context,
            )
            for field in ("size_bytes", "mtime_ns"):
                field_value = source_identity[field]
                if (
                    isinstance(field_value, bool)
                    or not isinstance(field_value, int)
                    or field_value < 0
                ):
                    raise ValueError(
                        f"{item_context} source_identity {field} must be non-negative"
                    )
        candidate_ids.append(candidate_id)
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError(f"{context} has duplicate candidate_id values")
    return value


def _artifact_lineage_projection(value: object, *, context: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    fields = (
        "artifact_id", "stage_id", "artifact_kind", "sha256", "size_bytes",
    )
    for field in fields:
        _require_v2_field(value, field, context=context)
    for field in ("artifact_id", "stage_id", "artifact_kind"):
        if not isinstance(value[field], str) or not value[field]:
            raise ValueError(f"{context} {field} must be non-empty")
    _quality_hash(value["sha256"], "sha256", context=context)
    if (
        isinstance(value["size_bytes"], bool)
        or not isinstance(value["size_bytes"], int)
        or value["size_bytes"] < 0
    ):
        raise ValueError(f"{context} size_bytes must be non-negative")
    return {field: value[field] for field in fields}


def _validate_quality_candidate_ledger(
    summary: dict,
    *,
    candidate_ledger_bytes: bytes | None,
    candidate_ledger_ref: dict | None,
    upstream_artifact_ref: dict | None,
    require_external: bool,
) -> list[dict]:
    context = "rank_dedup_manifest quality_moe candidate ledger"
    embedded = _validate_assessed_candidates(
        summary.get("assessed_candidates"), context=f"{context} candidates"
    )
    if summary.get("assessed_candidates_digest") != _candidate_ledger_digest(embedded):
        raise ValueError(f"{context} digest does not match candidates")
    metadata = summary.get("candidate_ledger")
    if not isinstance(metadata, dict) or metadata.get("mode") not in {
        "embedded", "external",
    }:
        raise ValueError(f"{context} mode must be embedded or external")
    if metadata["mode"] == "embedded":
        if require_external:
            raise ValueError(
                f"{context} must use an external candidate ledger"
            )
        return embedded

    if (
        candidate_ledger_bytes is None
        or candidate_ledger_ref is None
        or upstream_artifact_ref is None
    ):
        raise ValueError(f"{context} external lineage inputs are required")
    ledger_projection = _artifact_lineage_projection(
        candidate_ledger_ref, context=f"{context} artifact"
    )
    metadata_projection = _artifact_lineage_projection(
        metadata, context=f"{context} metadata"
    )
    if metadata_projection != ledger_projection:
        raise ValueError(f"{context} metadata does not match DB artifact")
    if hashlib.sha256(candidate_ledger_bytes).hexdigest() != ledger_projection["sha256"]:
        raise ValueError(f"{context} SHA-256 mismatch")
    if len(candidate_ledger_bytes) != ledger_projection["size_bytes"]:
        raise ValueError(f"{context} size mismatch")
    try:
        ledger = json.loads(candidate_ledger_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{context} JSON is invalid") from exc
    if not isinstance(ledger, dict) or ledger.get("schema_version") != 1:
        raise ValueError(f"{context} schema_version must be 1")
    if ledger.get("stage") != "rank_input":
        raise ValueError(f"{context} stage must be rank_input")
    candidates = _validate_assessed_candidates(
        ledger.get("assessed_candidates"), context=f"{context} sidecar candidates"
    )
    if ledger.get("assessed_candidates_digest") != _candidate_ledger_digest(candidates):
        raise ValueError(f"{context} sidecar digest does not match candidates")
    if candidates != embedded:
        raise ValueError(f"{context} sidecar candidates do not match manifest")
    upstream_projection = _artifact_lineage_projection(
        upstream_artifact_ref, context=f"{context} upstream DB artifact"
    )
    if upstream_projection["artifact_kind"] != "synthesize_manifest":
        raise ValueError(f"{context} upstream artifact kind is invalid")
    if _artifact_lineage_projection(
        ledger.get("upstream_artifact"), context=f"{context} sidecar upstream"
    ) != upstream_projection:
        raise ValueError(f"{context} sidecar upstream lineage does not match DB")
    if _artifact_lineage_projection(
        metadata.get("upstream_artifact"), context=f"{context} metadata upstream"
    ) != upstream_projection:
        raise ValueError(f"{context} manifest upstream lineage does not match DB")
    return candidates


def _validate_quality_summary(
    value: object,
    *,
    clips: list[dict],
    authoritative_candidates: list[dict] | None = None,
) -> None:
    context = "rank_dedup_manifest quality_moe"
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    for field in (
        "enabled", "report_only", "evaluation_version", "config_hash",
        "policy_snapshot",
        "input_count", "assessed_count", "effective_count",
        "human_review_count", "decision_counts", "top_assessments", "assessments",
        "assessed_candidates", "assessed_candidates_digest", "candidate_ledger",
    ):
        _require_v2_field(value, field, context=context)
    for field in ("enabled", "report_only"):
        if not isinstance(value[field], bool):
            raise ValueError(f"{context} {field} must be a boolean")
    for field in ("input_count", "assessed_count", "effective_count", "human_review_count"):
        count = value[field]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"{context} {field} must be a non-negative integer")
    _quality_hash(value["config_hash"], "config_hash", context=context)
    policy_snapshot = value["policy_snapshot"]
    if not isinstance(policy_snapshot, dict):
        raise ValueError(f"{context} policy_snapshot must be an object")
    for field in (
        "report_only", "min_judge_confidence",
        "min_independent_negative_families", "policy_version",
    ):
        _require_v2_field(policy_snapshot, field, context=f"{context} policy_snapshot")
    if policy_snapshot["report_only"] is not value["report_only"]:
        raise ValueError(f"{context} policy_snapshot report_only does not match summary")
    min_confidence = _finite_number(
        policy_snapshot["min_judge_confidence"],
        "min_judge_confidence",
        context=f"{context} policy_snapshot",
    )
    if not 0.8 <= float(min_confidence) <= 1.0:
        raise ValueError(f"{context} policy_snapshot min_judge_confidence must be in [0.8, 1]")
    min_families = policy_snapshot["min_independent_negative_families"]
    if (
        isinstance(min_families, bool)
        or not isinstance(min_families, int)
        or not 2 <= min_families <= 5
    ):
        raise ValueError(
            f"{context} policy_snapshot min_independent_negative_families must be in [2, 5]"
        )
    if not isinstance(policy_snapshot["policy_version"], str) or not policy_snapshot["policy_version"]:
        raise ValueError(f"{context} policy_snapshot policy_version must be non-empty")
    assessments = value["assessments"]
    if not isinstance(assessments, list):
        raise ValueError(f"{context} assessments must be an array")
    if value["assessed_count"] != len(assessments):
        raise ValueError(f"{context} assessed_count must equal len(assessments)")
    if value["enabled"] and value["input_count"] != value["assessed_count"]:
        raise ValueError(f"{context} input_count must equal assessed_count when enabled")
    ledger_candidates = authoritative_candidates
    if ledger_candidates is None:
        ledger_candidates = _validate_assessed_candidates(
            value["assessed_candidates"], context=f"{context} assessed_candidates"
        )
    assessment_ids = [item.get("candidate_id") for item in assessments]
    ledger_ids = [item["candidate_id"] for item in ledger_candidates]
    if assessment_ids != ledger_ids:
        raise ValueError(f"{context} assessments do not map one-to-one to candidate ledger")
    if not isinstance(value["decision_counts"], dict):
        raise ValueError(f"{context} decision_counts must be an object")
    if any(decision not in _QUALITY_DECISIONS for decision in value["decision_counts"]):
        raise ValueError(f"{context} decision_counts has an unknown quality decision")
    if not isinstance(value["top_assessments"], list):
        raise ValueError(f"{context} top_assessments must be an array")
    for index, assessment in enumerate(assessments):
        _validate_quality_assessment(
            assessment, context=f"{context} assessments[{index}]"
        )
        if assessment["config_hash"] != value["config_hash"]:
            raise ValueError(f"{context} assessment config_hash does not match summary")
        if assessment["evaluation_version"] != value["evaluation_version"]:
            raise ValueError(f"{context} assessment evaluation_version does not match summary")
        if assessment["policy_version"] != policy_snapshot["policy_version"]:
            raise ValueError(f"{context} assessment policy_version does not match snapshot")
    if value["enabled"]:
        from app.quality_moe.config import (
            QualityMoeConfig,
            SoftRejectConfig,
        )
        from app.quality_moe.models import (
            EvidencePolarity,
            EvidenceStatus,
            ExpertEvidence,
            QualityDecision,
        )
        from app.quality_moe.evaluator import _coverage_failure
        from app.quality_moe.policy import enforce_decision, hard_gate_reasons

        defaults = QualityMoeConfig.defaults()
        frozen_policy_config = QualityMoeConfig(
            enabled=True,
            report_only=value["report_only"],
            evaluation_version=value["evaluation_version"],
            soft_reject=SoftRejectConfig(
                min_judge_confidence=float(min_confidence),
                min_independent_negative_families=min_families,
            ),
            repairability=defaults.repairability,
            experts=defaults.experts,
            judge=defaults.judge,
            config_hash=value["config_hash"],
        )
        clips_by_id = {clip["clip_id"]: clip for clip in clips}
        ledger_by_id = {
            candidate["candidate_id"]: candidate for candidate in ledger_candidates
        }
        for index, assessment in enumerate(assessments):
            evidence = tuple(
                ExpertEvidence(
                    candidate_id=item["candidate_id"],
                    evaluation_version=item["evaluation_version"],
                    expert_id=item.get("expert_id", ""),
                    expert_version=item.get("expert_version", ""),
                    signal_family=item.get("signal_family", ""),
                    status=EvidenceStatus(item["status"]),
                    scores=item.get("scores", {}),
                    findings=tuple(item.get("findings", [])),
                    summary=item.get("summary", ""),
                    input_hash=item["input_hash"],
                    config_hash=item["config_hash"],
                    parent_input_hash=item.get("parent_input_hash"),
                    polarity=EvidencePolarity(item["polarity"]),
                    prompt_hash=item.get("prompt_hash"),
                    latency_ms=item.get("latency_ms", 0),
                )
                for item in assessment.get("evidence", [])
            )
            repair = None
            if assessment["recommended_decision"] == "KEEP_FOR_REPAIR":
                repair = _validate_quality_repair(
                    assessment.get("repair"),
                    assessment=assessment,
                    context=f"{context} assessments[{index}]",
                )
            clip = clips_by_id.get(assessment["candidate_id"])
            serialized_hard_reasons = tuple(assessment.get("hard_reasons", []))
            ledger_candidate = ledger_by_id[assessment["candidate_id"]]
            hard_gate_context = ledger_candidate["hard_gate_context"]
            if (
                assessment["hard_gate_context"] != hard_gate_context
                or assessment["hard_gate_context_hash"]
                != ledger_candidate["hard_gate_context_hash"]
            ):
                raise ValueError(
                    f"{context} assessments[{index}] does not match candidate ledger"
                )
            if clip is not None:
                clip_context = {
                    field: clip[field]
                    for field in _QUALITY_HARD_GATE_INPUT_FIELDS
                    if field in clip
                }
                if clip_context != hard_gate_context:
                    raise ValueError(
                        f"{context} assessments[{index}] hard-gate context is not immutable"
                    )
            hard_reasons = hard_gate_reasons(hard_gate_context)
            if serialized_hard_reasons != hard_reasons:
                raise ValueError(
                    f"{context} assessments[{index}] hard-gate context does not match hard_reasons"
                )
            source_identity = ledger_candidate.get("source_identity")
            if clip is not None and not hard_reasons and source_identity is None:
                raise ValueError(
                    f"{context} assessments[{index}] lacks immutable source identity"
                )
            if source_identity is not None:
                provenance = assessment.get("provenance")
                if not isinstance(provenance, dict):
                    raise ValueError(
                        f"{context} assessments[{index}] provenance must be an object"
                    )
                if (
                    provenance.get("source_file_sha256")
                    != source_identity["source_file_sha256"]
                ):
                    raise ValueError(
                        f"{context} assessments[{index}] source hash does not match ledger"
                    )
                provenance_path = provenance.get("source_video")
                if (
                    not isinstance(provenance_path, str)
                    or str(Path(provenance_path).resolve(strict=False)).casefold()
                    != str(Path(source_identity["video_path"]).resolve(strict=False)).casefold()
                ):
                    raise ValueError(
                        f"{context} assessments[{index}] source path does not match ledger"
                    )
            core_expert_families = {
                "nr_vqa", "deterministic_temporal", "cinematic_classifier",
            }
            current_evidence = tuple(
                item for item in evidence
                if item.candidate_id == assessment["candidate_id"]
                and item.evaluation_version == value["evaluation_version"]
                and item.config_hash == value["config_hash"]
                and item.input_hash == assessment.get("input_hash")
                and item.signal_family in core_expert_families
            )
            coverage_failure = _coverage_failure(current_evidence)
            if (
                not hard_reasons
                and coverage_failure is not None
                and assessment["recommended_decision"] not in {"REVIEW", "ABSTAIN"}
            ):
                raise ValueError(
                    f"{context} assessments[{index}] failed expert coverage replay"
                )
            recomputed = enforce_decision(
                candidate_id=assessment["candidate_id"],
                input_hash=assessment.get("input_hash", ""),
                proposed=QualityDecision(assessment["recommended_decision"]),
                confidence=assessment["confidence"],
                evidence=evidence,
                hard_reasons=hard_reasons,
                repair=repair,
                config=frozen_policy_config,
                policy_version=policy_snapshot["policy_version"],
            )
            if (
                assessment["recommended_decision"]
                != recomputed.recommended_decision.value
                or assessment["effective_decision"]
                != recomputed.effective_decision.value
                or tuple(assessment.get("negative_signal_families", []))
                != recomputed.negative_signal_families
            ):
                raise ValueError(
                    f"{context} assessments[{index}] failed policy recomputation"
                )
    actual_counts: dict[str, int] = {}
    for assessment in assessments:
        decision = assessment["effective_decision"]
        actual_counts[decision] = actual_counts.get(decision, 0) + 1
    if value["decision_counts"] != actual_counts:
        raise ValueError(f"{context} decision_counts do not match assessments")
    expected_top_assessments = [
        {
            "candidate_id": assessment["candidate_id"],
            "effective_decision": assessment["effective_decision"],
            "confidence": assessment["confidence"],
        }
        for assessment in assessments[:10]
    ]
    if value["top_assessments"] != expected_top_assessments:
        raise ValueError(f"{context} top_assessments do not match assessments")
    if value["enabled"] and value["report_only"] and value["effective_count"] != value["assessed_count"]:
        raise ValueError(
            f"{context} report_only must not drop candidates via quality routing"
        )
    if len(clips) > value["effective_count"]:
        raise ValueError(
            f"{context} exported clip count cannot exceed effective_count"
        )
    assessment_ids = [assessment["candidate_id"] for assessment in assessments]
    clip_ids = [clip["clip_id"] for clip in clips]
    if len(clip_ids) != len(set(clip_ids)):
        raise ValueError(f"{context} exported clip_ids must be unique")
    if value["enabled"] and value["report_only"]:
        assessed_set = set(assessment_ids)
        if any(clip_id not in assessed_set for clip_id in clip_ids):
            raise ValueError(
                f"{context} report_only may truncate assessed clips but cannot export unassessed ids"
            )
    if value["enabled"] and not value["report_only"]:
        keep_ids = {
            assessment["candidate_id"] for assessment in assessments
            if assessment["effective_decision"] in {"KEEP_AS_IS", "KEEP_FOR_REPAIR"}
        }
        if any(clip_id not in keep_ids for clip_id in clip_ids):
            raise ValueError(
                f"{context} active routing may fan out only effective KEEP clips"
            )
    by_id = {assessment["candidate_id"]: assessment for assessment in assessments}
    for clip in clips:
        if value["enabled"] and clip.get("quality_assessment") != by_id.get(clip["clip_id"]):
            raise ValueError(f"{context} per-clip quality_assessment is not immutable")


def _validate_gif_export_v2(value: dict) -> None:
    context = "gif_clip_manifest"
    for field in (
        "sha256",
        "duration_s",
        "size_bytes",
        "status",
    ):
        _require_v2_field(value, field, context=context)

    clip_id = value.get("clip_id")
    if not isinstance(clip_id, str) or not clip_id.strip():
        raise ValueError(f"{context} clip_id must be a non-empty string")
    gif_path = value.get("gif_path")
    if not isinstance(gif_path, str) or not gif_path.strip():
        raise ValueError(f"{context} gif_path must be a non-empty string")
    sha256 = value["sha256"]
    if (
        not isinstance(sha256, str)
        or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None
    ):
        raise ValueError(f"{context} sha256 must be 64 hexadecimal characters")
    duration_s = _finite_number(
        value["duration_s"], "duration_s", context=context
    )
    start_ts = _finite_number(value["start_ts"], "start_ts", context=context)
    end_ts = _finite_number(value["end_ts"], "end_ts", context=context)
    expected_duration = float(end_ts) - float(start_ts)
    if not math.isclose(
        float(duration_s), expected_duration, rel_tol=1e-9, abs_tol=1e-6
    ):
        raise ValueError(
            f"{context} duration_s must equal end_ts - start_ts"
        )
    size_bytes = value["size_bytes"]
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
    ):
        raise ValueError(f"{context} size_bytes must be a non-negative integer")
    status = value["status"]
    if (
        not isinstance(status, str)
        or status not in {"succeeded", "failed"}
    ):
        raise ValueError(
            f"{context} status must be 'succeeded' or 'failed'"
        )


def _validate_gif_quality_lineage(value: dict) -> None:
    context = "gif_clip_manifest quality lineage"
    assessment = value.get("quality_assessment")
    if not isinstance(assessment, dict):
        return
    _validate_quality_assessment(assessment, context=f"{context} assessment")
    for field in (
        "quality_decision", "current_quality", "recoverable_quality",
        "repair_applied", "recommended_recipe_id", "recommended_recipe",
        "applied_recipe_id", "applied_recipe", "evidence_hashes",
        "config_hash", "parent_source",
    ):
        _require_v2_field(value, field, context=context)
    if value["quality_decision"] not in _QUALITY_DECISIONS:
        raise ValueError(f"{context} quality_decision is unknown")
    if value["quality_decision"] != assessment["effective_decision"]:
        raise ValueError(f"{context} quality_decision does not match assessment")
    _quality_hash(value["config_hash"], "config_hash", context=context)
    if value["config_hash"] != assessment["config_hash"]:
        raise ValueError(f"{context} config_hash does not match assessment")
    for field in ("current_quality", "recoverable_quality"):
        if value[field] is not None:
            score = _finite_number(value[field], field, context=context)
            if not 0.0 <= float(score) <= 1.0:
                raise ValueError(f"{context} {field} must be in [0, 1]")
        if value[field] != assessment.get(field):
            raise ValueError(f"{context} {field} does not match assessment")
    evidence_hashes = value["evidence_hashes"]
    if not isinstance(evidence_hashes, list):
        raise ValueError(f"{context} evidence_hashes must be an array")
    for evidence_hash in evidence_hashes:
        _quality_hash(evidence_hash, "evidence_hashes", context=context)
    if evidence_hashes != assessment.get("evidence_hashes", []):
        raise ValueError(f"{context} evidence_hashes do not match assessment")
    if not isinstance(value["repair_applied"], bool):
        raise ValueError(f"{context} repair_applied must be a boolean")
    if value["recommended_recipe_id"] != assessment.get("selected_recipe_id"):
        raise ValueError(f"{context} recommended_recipe_id does not match assessment")
    if value["recommended_recipe"] != assessment.get("repair"):
        raise ValueError(f"{context} recommended_recipe does not match assessment")
    if value["repair_applied"]:
        if assessment["effective_decision"] != "KEEP_FOR_REPAIR":
            raise ValueError(f"{context} applied repair requires KEEP_FOR_REPAIR")
        if value["applied_recipe_id"] != value["recommended_recipe_id"]:
            raise ValueError(f"{context} applied_recipe_id does not match recommendation")
        if value["applied_recipe"] != value["recommended_recipe"]:
            raise ValueError(f"{context} applied_recipe does not match recommendation")
    elif value["applied_recipe_id"] is not None or value["applied_recipe"] is not None:
        raise ValueError(f"{context} unapplied repair must not select a recipe")
    parent = value["parent_source"]
    if not isinstance(parent, dict):
        raise ValueError(f"{context} parent_source must be an object")
    for field in (
        "candidate_id", "input_hash", "source_file_sha256", "video_path",
        "start_ts", "end_ts",
    ):
        _require_v2_field(parent, field, context=f"{context} parent_source")
    if parent["candidate_id"] != value["clip_id"] or parent["candidate_id"] != assessment["candidate_id"]:
        raise ValueError(f"{context} parent_source candidate_id does not match clip")
    _quality_hash(
        parent["input_hash"], "input_hash", context=f"{context} parent_source"
    )
    _quality_source_file_sha256(
        parent["source_file_sha256"],
        "source_file_sha256",
        context=f"{context} parent_source",
    )
    if parent["input_hash"] != assessment.get("input_hash"):
        raise ValueError(f"{context} parent_source input_hash does not match assessment")
    if not isinstance(parent["video_path"], str) or not parent["video_path"]:
        raise ValueError(f"{context} parent_source video_path must be non-empty")
    for field in ("start_ts", "end_ts"):
        timestamp = _finite_number(parent[field], field, context=f"{context} parent_source")
        if not math.isclose(float(timestamp), float(value[field]), abs_tol=1e-6):
            raise ValueError(f"{context} parent_source {field} does not match GIF interval")


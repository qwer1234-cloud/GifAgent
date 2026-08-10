"""P1-2: Manifest validation tests.

Verify that ``validate_manifest_json`` catches all error types:
missing fields, wrong stage, wrong clip_id, unsupported version,
empty JSON, wrong encoding, and manifest/GIF SHA mismatch.

Also verify that ``_read_upstream_manifest`` in the stage script
wires through to the shared validator.
"""

from __future__ import annotations

import json
import hashlib
import os
import sys
from copy import deepcopy
from pathlib import Path

import pytest


def _quality_evidence_hash(evidence: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            evidence,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _hard_gate_context_hash(context: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            context,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _assessed_candidates_digest(candidates: list[dict]) -> str:
    return hashlib.sha256(
        json.dumps(
            candidates,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _valid_quality_rank_manifest() -> dict:
    hard_gate_context = {"transition_action": "keep"}
    evidence = []
    for expert_id, signal_family in (
        ("technical", "nr_vqa"),
        ("temporal", "deterministic_temporal"),
        ("cinematic", "cinematic_classifier"),
    ):
        evidence.append({
            "candidate_id": "clip-1",
            "evaluation_version": "quality-moe-v1",
            "expert_id": expert_id,
            "expert_version": "v1",
            "signal_family": signal_family,
            "status": "AVAILABLE",
            "scores": {"technical_integrity": 0.81},
            "findings": [],
            "summary": "clean",
            "input_hash": "c" * 64,
            "config_hash": "b" * 64,
            "parent_input_hash": None,
            "polarity": "POSITIVE",
            "prompt_hash": None,
            "latency_ms": 1,
        })
    assessment = {
        "candidate_id": "clip-1",
        "evaluation_version": "quality-moe-v1",
        "config_hash": "b" * 64,
        "policy_version": "quality-moe-policy-v1",
        "recommended_decision": "KEEP_AS_IS",
        "effective_decision": "KEEP_AS_IS",
        "decision": "KEEP_AS_IS",
        "confidence": 0.92,
        "negative_signal_families": [],
        "hard_reasons": [],
        "hard_gate_context": hard_gate_context,
        "hard_gate_context_hash": _hard_gate_context_hash(hard_gate_context),
        "repair": None,
        "reason_codes": [],
        "summary": "clean",
        "input_hash": "c" * 64,
        "evidence": evidence,
        "provenance": {"source_file_sha256": "d" * 64},
        "evidence_hashes": [],
        "selected_recipe_id": None,
        "current_quality": 0.81,
        "recoverable_quality": 0.81,
    }
    assessment["evidence_hashes"] = [
        _quality_evidence_hash(item) for item in assessment["evidence"]
    ]
    assessed_candidates = [{
        "candidate_id": "clip-1",
        "hard_gate_context": deepcopy(hard_gate_context),
        "hard_gate_context_hash": _hard_gate_context_hash(hard_gate_context),
    }]
    return {
        "schema_version": 2,
        "stage": "rank_dedup",
        "clip_count": 1,
        "clips": [{
            "clip_id": "clip-1",
            "start_ts": 2.0,
            "end_ts": 8.0,
            "action_boundary_mode": "cv",
            "action_boundary_confidence": 0.8,
            "action_vlm_verified": False,
            "action_analysis_version": 1,
            "guarded_export_window": True,
            "transition_action": "keep",
            "quality_assessment": deepcopy(assessment),
        }],
        "action_guard": {
            "action_config_hash": "a" * 64,
            "action_analysis_version": 1,
            "input": 1,
            "output": 1,
            "cv_ms": 0.0,
            "vlm_ms": 0.0,
            "total_ms": 0.0,
        },
        "quality_moe": {
            "enabled": True,
            "report_only": True,
            "evaluation_version": "quality-moe-v1",
            "config_hash": "b" * 64,
            "policy_snapshot": {
                "report_only": True,
                "min_judge_confidence": 0.8,
                "min_independent_negative_families": 2,
                "policy_version": "quality-moe-policy-v1",
            },
            "input_count": 1,
            "assessed_count": 1,
            "effective_count": 1,
            "human_review_count": 0,
            "decision_counts": {"KEEP_AS_IS": 1},
            "top_assessments": [{
                "candidate_id": "clip-1",
                "effective_decision": "KEEP_AS_IS",
                "confidence": 0.92,
            }],
            "assessments": [deepcopy(assessment)],
            "assessed_candidates": assessed_candidates,
            "assessed_candidates_digest": _assessed_candidates_digest(
                assessed_candidates
            ),
            "candidate_ledger": {"mode": "embedded"},
        },
    }


def _external_quality_ledger_fixture(manifest: dict) -> tuple[bytes, dict, dict]:
    upstream_ref = {
        "artifact_id": "synthesize-artifact",
        "stage_id": "synthesize-stage",
        "artifact_kind": "synthesize_manifest",
        "sha256": "a" * 64,
        "size_bytes": 123,
    }
    quality = manifest["quality_moe"]
    ledger = {
        "schema_version": 1,
        "stage": "rank_input",
        "upstream_artifact": upstream_ref,
        "assessed_candidates": quality["assessed_candidates"],
        "assessed_candidates_digest": quality["assessed_candidates_digest"],
    }
    ledger_bytes = json.dumps(
        ledger, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    ledger_ref = {
        "artifact_id": "candidate-ledger-artifact",
        "stage_id": "rank-stage",
        "artifact_kind": "rank_candidate_ledger",
        "sha256": hashlib.sha256(ledger_bytes).hexdigest(),
        "size_bytes": len(ledger_bytes),
    }
    quality["candidate_ledger"] = {
        "mode": "external",
        **ledger_ref,
        "upstream_artifact": upstream_ref,
    }
    return ledger_bytes, ledger_ref, upstream_ref


def _valid_quality_repair_rank_manifest() -> dict:
    from app.quality_moe.models import EvidenceStatus, RepairRecipe, RepairValidation

    manifest = _valid_quality_rank_manifest()
    assessment = manifest["quality_moe"]["assessments"][0]
    delta = deepcopy(assessment["evidence"][0])
    delta.update({
        "signal_family": "repair_delta",
        "input_hash": "2" * 64,
        "parent_input_hash": assessment["input_hash"],
    })
    delta_hash = _quality_evidence_hash(delta)
    base = RepairRecipe(
        recipe_id="repair-1", exposure_ev=0.25,
        quality_gain=0.2, confidence=0.9,
    )
    recipe = RepairRecipe(
        recipe_id="repair-1", exposure_ev=0.25,
        quality_gain=0.2, confidence=0.9,
        validation=RepairValidation(
            candidate_id="clip-1",
            evaluation_version="quality-moe-v1",
            source_input_hash=assessment["input_hash"],
            proxy_artifact_hash=delta["input_hash"],
            recipe_hash=base.recipe_hash,
            config_hash=assessment["config_hash"],
            repair_delta_evidence_id=delta_hash,
            repair_delta_status=EvidenceStatus.AVAILABLE,
        ),
    )
    assessment.update({
        "recommended_decision": "KEEP_FOR_REPAIR",
        "effective_decision": "KEEP_FOR_REPAIR",
        "decision": "KEEP_FOR_REPAIR",
        "repair": recipe.to_dict(),
        "evidence": [*assessment["evidence"], delta],
        "evidence_hashes": [*assessment["evidence_hashes"], delta_hash],
        "selected_recipe_id": "repair-1",
    })
    manifest["clips"][0]["quality_assessment"] = deepcopy(assessment)
    manifest["quality_moe"]["decision_counts"] = {"KEEP_FOR_REPAIR": 1}
    manifest["quality_moe"]["top_assessments"][0]["effective_decision"] = (
        "KEEP_FOR_REPAIR"
    )
    return manifest


# ---------------------------------------------------------------------------
# Unit tests for validate_manifest_json
# ---------------------------------------------------------------------------


class TestManifestValidation:
    """Test the shared validator function directly."""

    def test_valid_discover_manifest(self):
        """Valid discover manifest passes validation."""
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 1,
            "stage": "discover",
            "duration_s": 123.4,
        }).encode("utf-8")

        result = validate_manifest_json(data, "discover_manifest")
        assert result["schema_version"] == 1
        assert result["stage"] == "discover"
        assert result["duration_s"] == 123.4

    def test_missing_required_field(self):
        """Missing required field raises ValueError."""
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 1,
            "stage": "discover",
            # missing duration_s
        }).encode("utf-8")

        with pytest.raises(ValueError, match="missing required field"):
            validate_manifest_json(data, "discover_manifest")

    def test_wrong_stage_name(self):
        """Wrong stage name raises ValueError."""
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 1,
            "stage": "vlm",  # should be discover
            "duration_s": 100,
        }).encode("utf-8")

        with pytest.raises(ValueError, match="stage mismatch"):
            validate_manifest_json(
                data, "discover_manifest", expected_stage="discover",
            )

    def test_wrong_clip_id(self):
        """Wrong clip_id raises ValueError."""
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 1,
            "stage": "gif_clip",
            "clip_id": "wrong-clip",
            "gif_path": "/tmp/test.gif",
        }).encode("utf-8")

        with pytest.raises(ValueError, match="clip_id mismatch"):
            validate_manifest_json(
                data, "gif_clip_manifest",
                expected_stage="gif_clip",
                expected_clip_id="correct-clip",
            )

    def test_empty_json(self):
        """Empty bytes raise ValueError."""
        from app.task_engine.artifacts import validate_manifest_json

        with pytest.raises(ValueError, match="Empty manifest"):
            validate_manifest_json(b"", "discover_manifest")

    def test_invalid_json(self):
        """Invalid JSON raises ValueError."""
        from app.task_engine.artifacts import validate_manifest_json

        with pytest.raises(ValueError, match="Invalid JSON"):
            validate_manifest_json(b"not json at all", "discover_manifest")

    def test_unknown_artifact_kind(self):
        """Unknown artifact_kind raises ValueError."""
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({"schema_version": 1}).encode("utf-8")

        with pytest.raises(ValueError, match="Unknown artifact_kind"):
            validate_manifest_json(data, "nonexistent_kind")

    def test_wrong_encoding(self):
        """Non-UTF-8 bytes raise ValueError."""
        from app.task_engine.artifacts import validate_manifest_json

        # Valid JSON but encoded in UTF-16 (wrong encoding)
        data = json.dumps({"schema_version": 1, "stage": "discover", "duration_s": 100})
        encoded = data.encode("utf-16")

        with pytest.raises(ValueError, match="Invalid JSON"):
            validate_manifest_json(encoded, "discover_manifest")

    def test_rank_dedup_clip_count_mismatch(self):
        """rank_dedup_manifest with clip_count != len(clips) raises ValueError."""
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 1,
            "stage": "rank_dedup",
            "clip_count": 999,  # wrong
            "clips": [{"clip_id": "c1"}, {"clip_id": "c2"}],
        }).encode("utf-8")

        with pytest.raises(ValueError, match="clip_count"):
            validate_manifest_json(data, "rank_dedup_manifest")

    def test_rank_dedup_duplicate_clip_ids(self):
        """rank_dedup_manifest with duplicate clip_ids raises ValueError."""
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 1,
            "stage": "rank_dedup",
            "clip_count": 3,
            "clips": [
                {"clip_id": "c1"},
                {"clip_id": "c1"},  # duplicate
                {"clip_id": "c2"},
            ],
        }).encode("utf-8")

        with pytest.raises(ValueError, match="duplicate clip_ids"):
            validate_manifest_json(data, "rank_dedup_manifest")

    def test_rank_dedup_empty_clip_id(self):
        """rank_dedup_manifest with empty clip_id raises ValueError."""
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 1,
            "stage": "rank_dedup",
            "clip_count": 2,
            "clips": [
                {"clip_id": "c1"},
                {"clip_id": ""},  # empty
            ],
        }).encode("utf-8")

        with pytest.raises(ValueError, match="empty clip_id"):
            validate_manifest_json(data, "rank_dedup_manifest")

    def test_valid_sample_manifest(self):
        """Valid sample manifest passes validation."""
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 1,
            "stage": "sample",
            "frame_count": 10,
            "timestamps": [1, 2, 3],
            "frame_paths": ["/tmp/f1.jpg", "/tmp/f2.jpg", "/tmp/f3.jpg"],
        }).encode("utf-8")

        result = validate_manifest_json(data, "sample_manifest")
        assert result["frame_count"] == 10

    def test_rank_manifest_v2_requires_action_metadata(self):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = {
            "schema_version": 2,
            "stage": "rank_dedup",
            "clip_count": 1,
            "clips": [{
                "clip_id": "clip-1",
                "start_ts": 2.0,
                "end_ts": 8.0,
            }],
            "action_guard": {},
        }

        with pytest.raises(ValueError, match="action_boundary_mode"):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"),
                "rank_dedup_manifest",
            )

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("start_ts", float("nan")),
            ("end_ts", float("inf")),
            ("action_boundary_confidence", float("-inf")),
        ],
    )
    def test_rank_manifest_v2_rejects_nonfinite_action_fields(
        self, field, value
    ):
        from app.task_engine.artifacts import validate_manifest_json

        clip = {
            "clip_id": "clip-1",
            "start_ts": 2.0,
            "end_ts": 8.0,
            "action_boundary_mode": "cv",
            "action_boundary_confidence": 0.8,
            "action_vlm_verified": False,
            "action_analysis_version": 1,
            "guarded_export_window": True,
        }
        clip[field] = value
        manifest = {
            "schema_version": 2,
            "stage": "rank_dedup",
            "clip_count": 1,
            "clips": [clip],
            "action_guard": {
                "action_config_hash": "a" * 64,
                "action_analysis_version": 1,
                "input": 1,
                "output": 1,
                "cv_ms": 0.0,
                "vlm_ms": 0.0,
                "total_ms": 0.0,
            },
        }
        with pytest.raises(ValueError, match=field):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"),
                "rank_dedup_manifest",
            )

    @pytest.mark.parametrize("duration", [1.999, 20.001])
    def test_rank_manifest_v2_enforces_action_duration(self, duration):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = {
            "schema_version": 2,
            "stage": "rank_dedup",
            "clip_count": 1,
            "clips": [{
                "clip_id": "clip-1",
                "start_ts": 2.0,
                "end_ts": 2.0 + duration,
                "action_boundary_mode": "cv",
                "action_boundary_confidence": None,
                "action_vlm_verified": False,
                "action_analysis_version": 1,
                "guarded_export_window": True,
            }],
            "action_guard": {
                "action_config_hash": "a" * 64,
                "action_analysis_version": 1,
                "input": 1,
                "output": 1,
                "cv_ms": 0.0,
                "vlm_ms": 0.0,
                "total_ms": 0.0,
            },
        }
        with pytest.raises(ValueError, match="duration"):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"),
                "rank_dedup_manifest",
            )

    def test_schema_v1_rank_and_gif_manifests_remain_valid(self):
        from app.task_engine.artifacts import validate_manifest_json

        rank = {
            "schema_version": 1,
            "stage": "rank_dedup",
            "clip_count": 1,
            "clips": [{"clip_id": "legacy"}],
        }
        gif = {
            "schema_version": 1,
            "stage": "gif_clip",
            "clip_id": "legacy",
            "gif_path": "legacy.gif",
        }
        assert validate_manifest_json(
            json.dumps(rank).encode("utf-8"), "rank_dedup_manifest"
        )["schema_version"] == 1
        assert validate_manifest_json(
            json.dumps(gif).encode("utf-8"), "gif_clip_manifest"
        )["schema_version"] == 1

    def test_gif_manifest_v2_requires_action_metadata(self):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = {
            "schema_version": 2,
            "stage": "gif_clip",
            "clip_id": "clip-1",
            "gif_path": "clip-1.gif",
            "start_ts": 2.0,
            "end_ts": 8.0,
        }
        with pytest.raises(ValueError, match="action_boundary_mode"):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"),
                "gif_clip_manifest",
            )

    @pytest.mark.parametrize(
        ("field", "value", "match"),
        [
            ("clips", [7], "must be an object"),
            ("clip_count", True, "non-negative integer"),
        ],
    )
    def test_rank_manifest_v2_rejects_malformed_container_types(
        self, field, value, match
    ):
        from app.task_engine.artifacts import validate_manifest_json

        clip = {
            "clip_id": "clip-1",
            "start_ts": 2.0,
            "end_ts": 8.0,
            "action_boundary_mode": "cv",
            "action_boundary_confidence": 0.8,
            "action_vlm_verified": False,
            "action_analysis_version": 1,
            "guarded_export_window": True,
        }
        manifest = {
            "schema_version": 2,
            "stage": "rank_dedup",
            "clip_count": 1,
            "clips": [clip],
            "action_guard": {
                "action_config_hash": "a" * 64,
                "action_analysis_version": 1,
                "input": 1,
                "output": 1,
                "cv_ms": 0.0,
                "vlm_ms": 0.0,
                "total_ms": 0.0,
            },
        }
        manifest[field] = value
        with pytest.raises(ValueError, match=match):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"),
                "rank_dedup_manifest",
            )

    @pytest.mark.parametrize("clip_id", [True, 7, "   "])
    def test_rank_manifest_v2_requires_string_clip_id(self, clip_id):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = {
            "schema_version": 2,
            "stage": "rank_dedup",
            "clip_count": 1,
            "clips": [{
                "clip_id": clip_id,
                "start_ts": 2.0,
                "end_ts": 8.0,
                "action_boundary_mode": "cv",
                "action_boundary_confidence": 0.8,
                "action_vlm_verified": False,
                "action_analysis_version": 1,
                "guarded_export_window": True,
            }],
            "action_guard": {
                "action_config_hash": "a" * 64,
                "action_analysis_version": 1,
                "input": 1,
                "output": 1,
                "cv_ms": 0.0,
                "vlm_ms": 0.0,
                "total_ms": 0.0,
            },
        }
        with pytest.raises(ValueError, match="clip_id"):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"),
                "rank_dedup_manifest",
            )

    @pytest.mark.parametrize(
        ("field", "value", "match"),
        [
            ("gif_path", True, "gif_path"),
            ("sha256", None, "sha256"),
            ("duration_s", None, "duration_s"),
            ("size_bytes", None, "size_bytes"),
            ("status", None, "status"),
        ],
    )
    def test_gif_manifest_v2_requires_valid_export_metadata(
        self, field, value, match
    ):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = {
            "schema_version": 2,
            "stage": "gif_clip",
            "clip_id": "clip-1",
            "gif_path": "clip-1.gif",
            "sha256": "a" * 64,
            "duration_s": 6.0,
            "size_bytes": 123,
            "status": "succeeded",
            "start_ts": 2.0,
            "end_ts": 8.0,
            "action_boundary_mode": "cv",
            "action_boundary_confidence": 0.8,
            "action_vlm_verified": False,
            "action_analysis_version": 1,
            "guarded_export_window": True,
        }
        if value is None:
            manifest.pop(field)
        else:
            manifest[field] = value
        with pytest.raises(ValueError, match=match):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"),
                "gif_clip_manifest",
            )

    @pytest.mark.parametrize(
        ("field", "value", "match"),
        [
            ("sha256", "xyz", "sha256"),
            ("duration_s", 5.0, "duration_s"),
            ("size_bytes", True, "size_bytes"),
            ("size_bytes", -1, "size_bytes"),
            ("status", 7, "status"),
        ],
    )
    def test_gif_manifest_v2_rejects_invalid_export_metadata(
        self, field, value, match
    ):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = {
            "schema_version": 2,
            "stage": "gif_clip",
            "clip_id": "clip-1",
            "gif_path": "clip-1.gif",
            "sha256": "a" * 64,
            "duration_s": 6.0,
            "size_bytes": 123,
            "status": "succeeded",
            "start_ts": 2.0,
            "end_ts": 8.0,
            "action_boundary_mode": "cv",
            "action_boundary_confidence": 0.8,
            "action_vlm_verified": False,
            "action_analysis_version": 1,
            "guarded_export_window": True,
        }
        manifest[field] = value
        with pytest.raises(ValueError, match=match):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"),
                "gif_clip_manifest",
            )

    def test_rank_manifest_accepts_quality_evidence_and_rejects_unknown_decision(self):
        from app.task_engine.artifacts import validate_manifest_json

        assessment = deepcopy(
            _valid_quality_rank_manifest()["quality_moe"]["assessments"][0]
        )
        manifest = {
            "schema_version": 2,
            "stage": "rank_dedup",
            "clip_count": 1,
            "clips": [{
                "clip_id": "clip-1",
                "start_ts": 2.0,
                "end_ts": 8.0,
                "action_boundary_mode": "cv",
                "action_boundary_confidence": 0.8,
                "action_vlm_verified": False,
                "action_analysis_version": 1,
                "guarded_export_window": True,
                "transition_action": "keep",
                "quality_assessment": assessment,
            }],
            "action_guard": {
                "action_config_hash": "a" * 64,
                "action_analysis_version": 1,
                "input": 1,
                "output": 1,
                "cv_ms": 0.0,
                "vlm_ms": 0.0,
                "total_ms": 0.0,
            },
            "quality_moe": {
                "enabled": True,
                "report_only": True,
                "evaluation_version": "quality-moe-v1",
                "config_hash": "b" * 64,
                "policy_snapshot": {
                    "report_only": True,
                    "min_judge_confidence": 0.8,
                    "min_independent_negative_families": 2,
                    "policy_version": "quality-moe-policy-v1",
                },
                "input_count": 1,
                "assessed_count": 1,
                "effective_count": 1,
                "human_review_count": 0,
                "decision_counts": {"KEEP_AS_IS": 1},
                "top_assessments": [{
                    "candidate_id": "clip-1",
                    "effective_decision": "KEEP_AS_IS",
                    "confidence": 0.92,
                }],
                "assessments": [deepcopy(assessment)],
                "assessed_candidates": deepcopy(
                    _valid_quality_rank_manifest()["quality_moe"][
                        "assessed_candidates"
                    ]
                ),
                "assessed_candidates_digest": _valid_quality_rank_manifest()[
                    "quality_moe"
                ]["assessed_candidates_digest"],
                "candidate_ledger": {"mode": "embedded"},
            },
        }

        validated = validate_manifest_json(
            json.dumps(manifest).encode("utf-8"), "rank_dedup_manifest"
        )
        assert validated["clips"][0]["quality_assessment"]["confidence"] == 0.92

        manifest["clips"][0]["quality_assessment"]["effective_decision"] = "MAYBE"
        with pytest.raises(ValueError, match="effective_decision"):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"), "rank_dedup_manifest"
            )

    def test_zero_clip_rank_manifest_keeps_zero_count_quality_summary(self):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = {
            "schema_version": 2,
            "stage": "rank_dedup",
            "clip_count": 0,
            "clips": [],
            "action_guard": {
                "action_config_hash": "a" * 64,
                "action_analysis_version": 1,
                "input": 0,
                "output": 0,
                "cv_ms": 0.0,
                "vlm_ms": 0.0,
                "total_ms": 0.0,
            },
            "quality_moe": {
                "enabled": True,
                "report_only": True,
                "evaluation_version": "quality-moe-v1",
                "config_hash": "b" * 64,
                "policy_snapshot": {
                    "report_only": True,
                    "min_judge_confidence": 0.8,
                    "min_independent_negative_families": 2,
                    "policy_version": "quality-moe-policy-v1",
                },
                "input_count": 0,
                "assessed_count": 0,
                "effective_count": 0,
                "human_review_count": 0,
                "decision_counts": {},
                "top_assessments": [],
                "assessments": [],
                "assessed_candidates": [],
                "assessed_candidates_digest": _assessed_candidates_digest([]),
                "candidate_ledger": {"mode": "embedded"},
            },
        }

        validated = validate_manifest_json(
            json.dumps(manifest).encode("utf-8"), "rank_dedup_manifest"
        )
        assert validated["quality_moe"]["assessed_count"] == 0

    @pytest.mark.parametrize(
        ("mutation", "match"),
        [
            (lambda manifest: manifest["clips"][0]["quality_assessment"].__setitem__("confidence", 1.1), "confidence"),
            (lambda manifest: manifest["clips"][0]["quality_assessment"]["evidence"][0]["scores"].__setitem__("technical_integrity", -0.1), "technical_integrity"),
            (lambda manifest: manifest["clips"][0]["quality_assessment"].__setitem__("config_hash", "not-a-hash"), "config_hash"),
            (lambda manifest: manifest["clips"][0]["quality_assessment"]["evidence"][0].__setitem__("status", "MISSING"), "status"),
        ],
    )
    def test_rank_manifest_strictly_validates_quality_evidence(self, mutation, match):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = _valid_quality_rank_manifest()
        mutation(manifest)

        with pytest.raises(ValueError, match=match):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"), "rank_dedup_manifest"
            )

    def test_rank_manifest_rejects_evidence_hash_not_bound_to_evidence(self):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = _valid_quality_rank_manifest()
        manifest["quality_moe"]["assessments"][0]["evidence_hashes"] = ["e" * 64]
        manifest["clips"][0]["quality_assessment"] = deepcopy(
            manifest["quality_moe"]["assessments"][0]
        )

        with pytest.raises(ValueError, match="evidence_hashes do not match"):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"), "rank_dedup_manifest"
            )

    def test_rank_manifest_rejects_top_summary_not_bound_to_assessments(self):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = _valid_quality_rank_manifest()
        manifest["quality_moe"]["top_assessments"][0]["confidence"] = 0.1

        with pytest.raises(ValueError, match="top_assessments do not match"):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"), "rank_dedup_manifest"
            )

    def test_report_only_rank_manifest_cannot_drop_or_reorder_assessed_clips(self):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = _valid_quality_rank_manifest()
        manifest["quality_moe"]["input_count"] = 2
        manifest["quality_moe"]["assessed_count"] = 2
        second = {
            **deepcopy(manifest["quality_moe"]["assessments"][0]),
            "candidate_id": "clip-2",
        }
        for item in second["evidence"]:
            item["candidate_id"] = "clip-2"
        second["evidence_hashes"] = [
            _quality_evidence_hash(item) for item in second["evidence"]
        ]
        manifest["quality_moe"]["assessments"].append(second)
        manifest["quality_moe"]["decision_counts"] = {"KEEP_AS_IS": 2}
        manifest["quality_moe"]["top_assessments"].append({
            "candidate_id": "clip-2",
            "effective_decision": "KEEP_AS_IS",
            "confidence": 0.92,
        })
        second_candidate = deepcopy(
            manifest["quality_moe"]["assessed_candidates"][0]
        )
        second_candidate["candidate_id"] = "clip-2"
        manifest["quality_moe"]["assessed_candidates"].append(second_candidate)
        manifest["quality_moe"]["assessed_candidates_digest"] = (
            _assessed_candidates_digest(
                manifest["quality_moe"]["assessed_candidates"]
            )
        )

        with pytest.raises(ValueError, match="report_only"):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"), "rank_dedup_manifest"
            )

    def test_active_rank_manifest_rejects_forged_soft_reject_without_evidence(self):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = _valid_quality_rank_manifest()
        assessment = manifest["quality_moe"]["assessments"][0]
        assessment.update({
            "recommended_decision": "REJECT",
            "effective_decision": "REJECT",
            "decision": "REJECT",
            "confidence": 0.95,
            "evidence": [],
            "evidence_hashes": [],
            "current_quality": None,
            "recoverable_quality": None,
        })
        manifest["quality_moe"]["report_only"] = False
        manifest["quality_moe"]["policy_snapshot"]["report_only"] = False
        manifest["quality_moe"]["effective_count"] = 0
        manifest["quality_moe"]["decision_counts"] = {"REJECT": 1}
        manifest["quality_moe"]["top_assessments"] = [{
            "candidate_id": "clip-1",
            "effective_decision": "REJECT",
            "confidence": 0.95,
        }]
        manifest["clips"] = []
        manifest["clip_count"] = 0

        with pytest.raises(ValueError, match="expert coverage replay"):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"), "rank_dedup_manifest"
            )

    def test_active_rank_manifest_rejects_invented_hard_gate_reason(self):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = _valid_quality_rank_manifest()
        assessment = manifest["quality_moe"]["assessments"][0]
        assessment.update({
            "recommended_decision": "REJECT",
            "effective_decision": "REJECT",
            "decision": "REJECT",
            "confidence": 1.0,
            "hard_reasons": ["invented_gate"],
        })
        manifest["quality_moe"]["report_only"] = False
        manifest["quality_moe"]["policy_snapshot"]["report_only"] = False
        manifest["quality_moe"]["effective_count"] = 0
        manifest["quality_moe"]["decision_counts"] = {"REJECT": 1}
        manifest["quality_moe"]["top_assessments"] = [{
            "candidate_id": "clip-1",
            "effective_decision": "REJECT",
            "confidence": 1.0,
        }]
        manifest["clips"] = []
        manifest["clip_count"] = 0

        with pytest.raises(ValueError, match="hard_reasons"):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"), "rank_dedup_manifest"
            )

    def test_active_rank_manifest_recomputes_hard_reasons_from_frozen_context(self):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = _valid_quality_rank_manifest()
        assessment = manifest["quality_moe"]["assessments"][0]
        assessment.update({
            "recommended_decision": "REJECT",
            "effective_decision": "REJECT",
            "decision": "REJECT",
            "confidence": 1.0,
            "hard_reasons": ["transition_drop"],
        })
        manifest["quality_moe"]["report_only"] = False
        manifest["quality_moe"]["policy_snapshot"]["report_only"] = False
        manifest["quality_moe"]["effective_count"] = 0
        manifest["quality_moe"]["decision_counts"] = {"REJECT": 1}
        manifest["quality_moe"]["top_assessments"] = [{
            "candidate_id": "clip-1",
            "effective_decision": "REJECT",
            "confidence": 1.0,
        }]
        manifest["clips"] = []
        manifest["clip_count"] = 0

        with pytest.raises(ValueError, match="hard-gate context"):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"), "rank_dedup_manifest"
            )

    def test_rank_manifest_rejects_assessment_context_forged_against_candidate_ledger(self):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = _valid_quality_rank_manifest()
        assessment = manifest["quality_moe"]["assessments"][0]
        forged_context = {"transition_action": "drop"}
        assessment.update({
            "recommended_decision": "REJECT",
            "effective_decision": "REJECT",
            "decision": "REJECT",
            "confidence": 1.0,
            "hard_reasons": ["transition_drop"],
            "hard_gate_context": forged_context,
            "hard_gate_context_hash": _hard_gate_context_hash(forged_context),
        })
        manifest["quality_moe"]["report_only"] = False
        manifest["quality_moe"]["policy_snapshot"]["report_only"] = False
        manifest["quality_moe"]["effective_count"] = 0
        manifest["quality_moe"]["decision_counts"] = {"REJECT": 1}
        manifest["quality_moe"]["top_assessments"] = [{
            "candidate_id": "clip-1",
            "effective_decision": "REJECT",
            "confidence": 1.0,
        }]
        manifest["clips"] = []
        manifest["clip_count"] = 0

        with pytest.raises(ValueError, match="candidate ledger"):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"), "rank_dedup_manifest"
            )

    def test_staged_quality_context_rejects_embedded_candidate_ledger(self):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = _valid_quality_rank_manifest()

        with pytest.raises(ValueError, match="external candidate ledger"):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"),
                "rank_dedup_manifest",
                require_external_quality_ledger=True,
            )

    def test_staged_quality_context_rejects_missing_candidate_ledger_input(self):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = _valid_quality_rank_manifest()
        _ledger_bytes, _ledger_ref, upstream_ref = (
            _external_quality_ledger_fixture(manifest)
        )

        with pytest.raises(ValueError, match="external lineage inputs"):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"),
                "rank_dedup_manifest",
                upstream_artifact_ref=upstream_ref,
                require_external_quality_ledger=True,
            )

    def test_staged_quality_context_rejects_missing_synthesize_lineage(self):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = _valid_quality_rank_manifest()
        ledger_bytes, ledger_ref, _upstream_ref = (
            _external_quality_ledger_fixture(manifest)
        )

        with pytest.raises(ValueError, match="external lineage inputs"):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"),
                "rank_dedup_manifest",
                candidate_ledger_bytes=ledger_bytes,
                candidate_ledger_ref=ledger_ref,
                require_external_quality_ledger=True,
            )

    def test_direct_quality_context_still_accepts_embedded_candidate_ledger(self):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = _valid_quality_rank_manifest()

        validated = validate_manifest_json(
            json.dumps(manifest).encode("utf-8"), "rank_dedup_manifest"
        )

        assert validated["quality_moe"]["candidate_ledger"] == {
            "mode": "embedded"
        }

    def test_staged_rank_rejects_synchronized_ledger_forgery_against_db_sha(
        self, tmp_path: Path,
    ):
        from scripts import test_video_adaptive as adaptive

        manifest = _valid_quality_rank_manifest()
        assessment = manifest["quality_moe"]["assessments"][0]
        forged_context = {"transition_action": "drop"}
        assessment.update({
            "recommended_decision": "REJECT",
            "effective_decision": "REJECT",
            "decision": "REJECT",
            "confidence": 1.0,
            "hard_reasons": ["transition_drop"],
            "hard_gate_context": forged_context,
            "hard_gate_context_hash": _hard_gate_context_hash(forged_context),
        })
        forged_candidates = [{
            "candidate_id": "clip-1",
            "hard_gate_context": forged_context,
            "hard_gate_context_hash": _hard_gate_context_hash(forged_context),
        }]
        quality = manifest["quality_moe"]
        quality.update({
            "report_only": False,
            "effective_count": 0,
            "decision_counts": {"REJECT": 1},
            "top_assessments": [{
                "candidate_id": "clip-1",
                "effective_decision": "REJECT",
                "confidence": 1.0,
            }],
            "assessed_candidates": forged_candidates,
            "assessed_candidates_digest": _assessed_candidates_digest(
                forged_candidates
            ),
        })
        quality["policy_snapshot"]["report_only"] = False
        manifest["clips"] = []
        manifest["clip_count"] = 0

        source_path = tmp_path / "synthesize_manifest.json"
        source_path.write_text(json.dumps({
            "schema_version": 1,
            "stage": "synthesize",
            "clips": [],
        }), encoding="utf-8")
        source_raw = source_path.read_bytes()
        source_ref = {
            "artifact_id": "source-artifact",
            "stage_id": "synthesize-stage",
            "artifact_kind": "synthesize_manifest",
            "path": str(source_path),
            "sha256": hashlib.sha256(source_raw).hexdigest(),
            "size_bytes": len(source_raw),
        }
        authentic_candidates = [{
            "candidate_id": "clip-1",
            "hard_gate_context": {"transition_action": "keep"},
            "hard_gate_context_hash": _hard_gate_context_hash(
                {"transition_action": "keep"}
            ),
        }]
        authentic_ledger = {
            "schema_version": 1,
            "stage": "rank_input",
            "upstream_artifact": {
                key: source_ref[key]
                for key in (
                    "artifact_id", "stage_id", "artifact_kind", "sha256",
                    "size_bytes",
                )
            },
            "assessed_candidates": authentic_candidates,
            "assessed_candidates_digest": _assessed_candidates_digest(
                authentic_candidates
            ),
        }
        authentic_raw = json.dumps(
            authentic_ledger, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        forged_ledger = deepcopy(authentic_ledger)
        forged_ledger["assessed_candidates"] = forged_candidates
        forged_ledger["assessed_candidates_digest"] = (
            _assessed_candidates_digest(forged_candidates)
        )
        ledger_path = tmp_path / "rank_candidate_ledger.json"
        ledger_path.write_text(
            json.dumps(forged_ledger, ensure_ascii=False), encoding="utf-8"
        )
        ledger_ref = {
            "artifact_id": "ledger-artifact",
            "stage_id": "rank-stage",
            "artifact_kind": "rank_candidate_ledger",
            "path": str(ledger_path),
            "sha256": hashlib.sha256(authentic_raw).hexdigest(),
            "size_bytes": len(authentic_raw),
        }
        quality["candidate_ledger"] = {
            "mode": "external",
            **{
                key: ledger_ref[key]
                for key in (
                    "artifact_id", "stage_id", "artifact_kind", "sha256",
                    "size_bytes",
                )
            },
            "upstream_artifact": authentic_ledger["upstream_artifact"],
        }
        rank_path = tmp_path / "rank_dedup_manifest.json"
        rank_path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(ValueError, match="SHA-256 mismatch"):
            adaptive._read_upstream_manifest(
                {
                    "rank_dedup_manifest": [{"path": str(rank_path)}],
                    "rank_candidate_ledger": [ledger_ref],
                    "synthesize_manifest": [source_ref],
                },
                "rank_dedup_manifest",
                "gif_clip",
            )

    def test_staged_rank_rejects_manifest_content_changed_after_db_hash(
        self, tmp_path: Path,
    ):
        from scripts import test_video_adaptive as adaptive

        manifest = _valid_quality_rank_manifest()
        rank_path = tmp_path / "rank_dedup_manifest.json"
        rank_path.write_text(json.dumps(manifest), encoding="utf-8")
        authentic_raw = rank_path.read_bytes()
        manifest["attacker_note"] = "content changed after registration"
        rank_path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(ValueError, match="rank_dedup_manifest SHA-256 mismatch"):
            adaptive._read_upstream_manifest(
                {"rank_dedup_manifest": [{
                    "path": str(rank_path),
                    "sha256": hashlib.sha256(authentic_raw).hexdigest(),
                    "size_bytes": len(authentic_raw),
                }]},
                "rank_dedup_manifest",
                "gif_clip",
            )

    def test_rank_manifest_rejects_zero_evidence_keep_that_bypasses_coverage(self):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = _valid_quality_rank_manifest()
        assessment = manifest["quality_moe"]["assessments"][0]
        assessment["evidence"] = []
        assessment["evidence_hashes"] = []
        manifest["clips"][0]["quality_assessment"] = deepcopy(assessment)

        with pytest.raises(ValueError, match="expert coverage"):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"), "rank_dedup_manifest"
            )

    def test_rank_manifest_replays_core_coverage_without_counting_judge_evidence(self):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = _valid_quality_rank_manifest()
        assessment = manifest["quality_moe"]["assessments"][0]
        judge_evidence = deepcopy(assessment["evidence"][0])
        judge_evidence.update({
            "expert_id": "semantic-judge",
            "signal_family": "semantic_judge",
        })
        assessment["evidence"].append(judge_evidence)
        assessment["evidence_hashes"].append(_quality_evidence_hash(judge_evidence))
        manifest["clips"][0]["quality_assessment"] = deepcopy(assessment)

        validated = validate_manifest_json(
            json.dumps(manifest).encode("utf-8"), "rank_dedup_manifest"
        )
        assert len(validated["quality_moe"]["assessments"][0]["evidence"]) == 4

    def test_rank_manifest_rejects_repair_outside_safe_recipe_bounds(self):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = _valid_quality_rank_manifest()
        assessment = manifest["clips"][0]["quality_assessment"]
        assessment["recommended_decision"] = "KEEP_FOR_REPAIR"
        assessment["effective_decision"] = "KEEP_FOR_REPAIR"
        assessment["decision"] = "KEEP_FOR_REPAIR"
        assessment["selected_recipe_id"] = "repair-1"
        assessment["repair"] = {
            "recipe_id": "repair-1",
            "exposure_ev": 0.0,
            "gamma": 1.0,
            "contrast": 0.0,
            "shadows": 0.0,
            "highlights": 0.0,
            "white_balance": [1.0, 1.0, 1.0],
            "crop": [0.0, 0.0, 0.5, 0.5],
            "zoom": 1.0,
            "rotation_degrees": 0.0,
            "perspective_corner_movement": 0.0,
            "quality_gain": 0.2,
            "confidence": 0.9,
            "recipe_hash": "f" * 64,
            "validation": {
                "candidate_id": "clip-1",
                "evaluation_version": "quality-moe-v1",
                "source_input_hash": "c" * 64,
                "proxy_artifact_hash": "1" * 64,
                "recipe_hash": "f" * 64,
                "config_hash": "b" * 64,
                "repair_delta_evidence_id": "e" * 64,
                "repair_delta_status": "AVAILABLE",
            },
            "validated": True,
        }

        with pytest.raises(ValueError, match="crop"):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"), "rank_dedup_manifest"
            )

    def test_rank_manifest_rejects_repair_validation_bound_to_non_delta_evidence(self):
        from app.task_engine.artifacts import validate_manifest_json

        manifest = _valid_quality_repair_rank_manifest()
        assessment = manifest["quality_moe"]["assessments"][0]
        assessment["evidence"][-1]["signal_family"] = "nr_vqa"
        forged_hash = _quality_evidence_hash(assessment["evidence"][-1])
        assessment["evidence_hashes"][-1] = forged_hash
        assessment["repair"]["validation"]["repair_delta_evidence_id"] = forged_hash
        manifest["clips"][0]["quality_assessment"] = deepcopy(assessment)

        with pytest.raises(ValueError, match="repair validation"):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"), "rank_dedup_manifest"
            )

    @pytest.mark.parametrize(
        ("field", "value", "match"),
        [
            ("quality_decision", "MAYBE", "quality_decision"),
            ("config_hash", "bad", "config_hash"),
            ("evidence_hashes", ["bad"], "evidence_hashes"),
        ],
    )
    def test_gif_manifest_strictly_validates_quality_lineage(self, field, value, match):
        from app.task_engine.artifacts import validate_manifest_json

        assessment = _valid_quality_rank_manifest()["clips"][0]["quality_assessment"]
        manifest = {
            "schema_version": 2,
            "stage": "gif_clip",
            "clip_id": "clip-1",
            "gif_path": "clip-1.gif",
            "sha256": "a" * 64,
            "duration_s": 6.0,
            "size_bytes": 123,
            "status": "succeeded",
            "start_ts": 2.0,
            "end_ts": 8.0,
            "action_boundary_mode": "cv",
            "action_boundary_confidence": 0.8,
            "action_vlm_verified": False,
            "action_analysis_version": 1,
            "guarded_export_window": True,
            "quality_assessment": assessment,
            "quality_decision": "KEEP_AS_IS",
            "current_quality": 0.81,
            "recoverable_quality": 0.81,
            "repair_applied": False,
            "recommended_recipe_id": None,
            "recommended_recipe": None,
            "applied_recipe_id": None,
            "applied_recipe": None,
            "evidence_hashes": ["e" * 64],
            "config_hash": "b" * 64,
            "parent_source": {
                "candidate_id": "clip-1",
                "input_hash": "c" * 64,
                "source_file_sha256": "d" * 64,
                "video_path": "source.mp4",
                "start_ts": 2.0,
                "end_ts": 8.0,
            },
        }
        manifest[field] = value

        with pytest.raises(ValueError, match=match):
            validate_manifest_json(
                json.dumps(manifest).encode("utf-8"), "gif_clip_manifest"
            )

    def test_gif_manifest_preserves_repair_recommendation_without_applying_pixels(self):
        from app.task_engine.artifacts import validate_manifest_json

        assessment = _valid_quality_repair_rank_manifest()["quality_moe"][
            "assessments"
        ][0]
        manifest = {
            "schema_version": 2,
            "stage": "gif_clip",
            "clip_id": "clip-1",
            "gif_path": "clip-1.gif",
            "sha256": "a" * 64,
            "duration_s": 6.0,
            "size_bytes": 123,
            "status": "succeeded",
            "start_ts": 2.0,
            "end_ts": 8.0,
            "action_boundary_mode": "cv",
            "action_boundary_confidence": 0.8,
            "action_vlm_verified": False,
            "action_analysis_version": 1,
            "guarded_export_window": True,
            "quality_assessment": assessment,
            "quality_decision": "KEEP_FOR_REPAIR",
            "current_quality": assessment["current_quality"],
            "recoverable_quality": assessment["recoverable_quality"],
            "repair_applied": False,
            "recommended_recipe_id": assessment["selected_recipe_id"],
            "recommended_recipe": assessment["repair"],
            "applied_recipe_id": None,
            "applied_recipe": None,
            "evidence_hashes": assessment["evidence_hashes"],
            "config_hash": assessment["config_hash"],
            "parent_source": {
                "candidate_id": "clip-1",
                "input_hash": assessment["input_hash"],
                "source_file_sha256": "d" * 64,
                "video_path": "source.mp4",
                "start_ts": 2.0,
                "end_ts": 8.0,
            },
        }

        validated = validate_manifest_json(
            json.dumps(manifest).encode("utf-8"), "gif_clip_manifest"
        )
        assert validated["repair_applied"] is False
        assert validated["recommended_recipe_id"] == "repair-1"
        assert validated["applied_recipe_id"] is None
        assert validated["quality_assessment"]["selected_recipe_id"] == "repair-1"


class TestManifestSchemaVersion:
    """P1-2: ``schema_version`` must be a positive integer in the supported
    set.  Booleans, strings, zero, negatives and unknown future versions
    must be rejected with a message listing supported versions."""

    def test_manifest_rejects_schema_version_zero(self):
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 0, "stage": "discover", "duration_s": 10,
        }).encode("utf-8")
        with pytest.raises(ValueError, match="schema_version"):
            validate_manifest_json(data, "discover_manifest")

    def test_manifest_rejects_future_schema_version(self):
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 999, "stage": "discover", "duration_s": 10,
        }).encode("utf-8")
        with pytest.raises(ValueError, match="unsupported"):
            validate_manifest_json(data, "discover_manifest")

    def test_manifest_rejects_schema_version_bool(self):
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": True, "stage": "discover", "duration_s": 10,
        }).encode("utf-8")
        with pytest.raises(ValueError, match="integer"):
            validate_manifest_json(data, "discover_manifest")

    def test_manifest_rejects_schema_version_string(self):
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": "1", "stage": "discover", "duration_s": 10,
        }).encode("utf-8")
        with pytest.raises(ValueError, match="integer"):
            validate_manifest_json(data, "discover_manifest")

    def test_manifest_rejects_negative_schema_version(self):
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": -1, "stage": "discover", "duration_s": 10,
        }).encode("utf-8")
        with pytest.raises(ValueError, match="schema_version"):
            validate_manifest_json(data, "discover_manifest")

    def test_manifest_error_message_lists_supported_versions(self):
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 2, "stage": "discover", "duration_s": 10,
        }).encode("utf-8")
        with pytest.raises(ValueError) as excinfo:
            validate_manifest_json(data, "discover_manifest")
        msg = str(excinfo.value)
        assert "discover_manifest" in msg
        assert "supported" in msg.lower()

    def test_materialize_envelope_rejects_unknown_version(self):
        from app.task_engine.artifacts import validate_materialize_envelope

        envelope = {
            "schema_version": 999, "stage": "materialize",
            "artifacts": {"gif_file": [], "gif_clip_manifest": []},
            "stage_statuses": [],
        }
        with pytest.raises(ValueError, match="unsupported"):
            validate_materialize_envelope(envelope)

    def test_materialize_envelope_rejects_non_integer_version(self):
        from app.task_engine.artifacts import validate_materialize_envelope

        envelope = {
            "schema_version": "1", "stage": "materialize",
            "artifacts": {"gif_file": [], "gif_clip_manifest": []},
            "stage_statuses": [],
        }
        with pytest.raises(ValueError, match="integer"):
            validate_materialize_envelope(envelope)


# ---------------------------------------------------------------------------
# Integration tests for _read_upstream_manifest wiring
# ---------------------------------------------------------------------------


class TestReadUpstreamManifestWiring:
    """Verify _read_upstream_manifest passes through to validate_manifest_json."""

    def test_staged_rank_reader_requires_external_quality_ledger(
        self, tmp_path: Path,
    ):
        from scripts import test_video_adaptive as adaptive

        rank_path = tmp_path / "rank_dedup_manifest.json"
        rank_path.write_text(
            json.dumps(_valid_quality_rank_manifest()), encoding="utf-8"
        )

        with pytest.raises(ValueError, match="external candidate ledger"):
            adaptive._read_upstream_manifest(
                {"rank_dedup_manifest": [{"path": str(rank_path)}]},
                "rank_dedup_manifest",
                "gif_clip",
            )

    def test_staged_rank_reader_rejects_embedded_ledger_with_all_sidecars(
        self, tmp_path: Path,
    ):
        from scripts import test_video_adaptive as adaptive

        manifest = _valid_quality_rank_manifest()
        sidecar_manifest = _valid_quality_rank_manifest()
        ledger_bytes, ledger_ref, upstream_ref = (
            _external_quality_ledger_fixture(sidecar_manifest)
        )
        rank_path = tmp_path / "rank_dedup_manifest.json"
        rank_path.write_text(json.dumps(manifest), encoding="utf-8")
        ledger_path = tmp_path / "rank_candidate_ledger.json"
        ledger_path.write_bytes(ledger_bytes)

        with pytest.raises(ValueError, match="external candidate ledger"):
            adaptive._read_upstream_manifest(
                {
                    "rank_dedup_manifest": [{"path": str(rank_path)}],
                    "rank_candidate_ledger": [{
                        **ledger_ref, "path": str(ledger_path),
                    }],
                    "synthesize_manifest": [upstream_ref],
                },
                "rank_dedup_manifest",
                "gif_clip",
            )

    def test_read_upstream_manifest_valid(self, tmp_path: Path):
        """_read_upstream_manifest validates and returns data for a valid manifest."""
        import json as _json

        # Import the script-level function.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "test_video_adaptive",
            os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "test_video_adaptive.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        # Create a valid discover manifest.
        manifest_path = tmp_path / "discover_manifest.json"
        manifest_path.write_text(_json.dumps({
            "schema_version": 1,
            "stage": "discover",
            "duration_s": 120.0,
        }))

        inputs = {
            "discover_manifest": [{
                "artifact_id": "art-1",
                "path": str(manifest_path),
                "clip_id": None,
            }],
        }

        result = mod._read_upstream_manifest(inputs, "discover_manifest", "sample")
        assert result["schema_version"] == 1
        assert result["duration_s"] == 120.0

    def test_read_upstream_manifest_missing_field_raises(self, tmp_path: Path):
        """_read_upstream_manifest raises ValueError for missing required field."""
        import json as _json
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "test_video_adaptive",
            os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "test_video_adaptive.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        # Missing duration_s
        manifest_path = tmp_path / "discover_manifest.json"
        manifest_path.write_text(_json.dumps({
            "schema_version": 1,
            "stage": "discover",
        }))

        inputs = {
            "discover_manifest": [{
                "artifact_id": "art-1",
                "path": str(manifest_path),
                "clip_id": None,
            }],
        }

        with pytest.raises(ValueError, match="missing required field"):
            mod._read_upstream_manifest(inputs, "discover_manifest", "sample")

    def test_read_upstream_manifest_wrong_stage_raises(self, tmp_path: Path):
        """_read_upstream_manifest raises ValueError for wrong stage."""
        import json as _json
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "test_video_adaptive",
            os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "test_video_adaptive.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        # Stage is "vlm" but expected "discover"
        manifest_path = tmp_path / "discover_manifest.json"
        manifest_path.write_text(_json.dumps({
            "schema_version": 1,
            "stage": "vlm",
            "duration_s": 100,
        }))

        inputs = {
            "discover_manifest": [{
                "artifact_id": "art-1",
                "path": str(manifest_path),
                "clip_id": None,
            }],
        }

        with pytest.raises(ValueError, match="stage mismatch"):
            mod._read_upstream_manifest(inputs, "discover_manifest", "sample")

    def test_read_upstream_manifest_empty_file_raises(self, tmp_path: Path):
        """_read_upstream_manifest raises ValueError for empty file."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "test_video_adaptive",
            os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "test_video_adaptive.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        manifest_path = tmp_path / "empty.json"
        manifest_path.write_text("")

        inputs = {
            "discover_manifest": [{
                "artifact_id": "art-1",
                "path": str(manifest_path),
                "clip_id": None,
            }],
        }

        with pytest.raises(ValueError, match="Empty manifest"):
            mod._read_upstream_manifest(inputs, "discover_manifest", "sample")

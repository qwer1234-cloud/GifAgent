from __future__ import annotations

import json
import hashlib
from types import SimpleNamespace

from app.quality_moe.evaluator import QualityBatchResult
from app.quality_moe.models import (
    EvidencePolarity,
    EvidenceStatus,
    ExpertEvidence,
    QualityAssessment,
    QualityDecision,
    RepairRecipe,
    RepairValidation,
)
from scripts import test_video_adaptive as adaptive


def _assessment(candidate_id: str, config_hash: str, decision: QualityDecision):
    return QualityAssessment(
        candidate_id=candidate_id,
        evaluation_version="quality-moe-v1",
        config_hash=config_hash,
        policy_version="quality-moe-policy-v1",
        recommended_decision=decision,
        effective_decision=decision,
        confidence=0.91,
        input_hash=hashlib.sha256(candidate_id.encode("utf-8")).hexdigest(),
    )


def test_zero_clip_staged_rank_manifest_records_quality_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(
        adaptive,
        "_read_upstream_manifest",
        lambda *_args, **_kwargs: {"clips": [], "scored_frames": []},
    )
    cfg = adaptive.extract_config({"adaptive": {}})

    result = adaptive._stage_rank_dedup(
        "source.mp4",
        str(tmp_path / "exports"),
        str(tmp_path),
        cfg,
        {},
        config_data={"adaptive": {}},
    )

    manifest_path = result["_artifacts"][0]["path"]
    manifest = json.loads(open(manifest_path, encoding="utf-8").read())
    assert manifest["quality_moe"] == {
        "enabled": True,
        "report_only": True,
        "evaluation_version": "quality-moe-v1",
        "config_hash": cfg["quality_moe_config_hash"],
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
    }


def test_report_only_quality_evaluation_preserves_candidate_order(tmp_path, monkeypatch):
    cfg = adaptive.extract_config({"quality_moe": {"report_only": True}})
    candidates = [
        {"clip_id": "clip-b", "start_ts": 8.0, "end_ts": 12.0},
        {"clip_id": "clip-a", "start_ts": 2.0, "end_ts": 6.0},
    ]
    calls = []

    def fake_evaluate(values, **kwargs):
        calls.append((list(values), kwargs))
        assessments = tuple(
            _assessment(item["candidate_id"], cfg["quality_moe_config_hash"], QualityDecision.REVIEW)
            for item in values
        )
        effective = tuple(
            {**item, "quality_assessment": assessment.to_dict()}
            for item, assessment in zip(values, assessments)
        )
        return QualityBatchResult(assessments, effective, effective)

    monkeypatch.setattr(adaptive, "evaluate_candidates", fake_evaluate)

    routed, summary = adaptive._evaluate_quality_pipeline_candidates(
        candidates,
        video_path="source.mp4",
        cfg=cfg,
        work_dir=tmp_path,
    )

    assert [clip["clip_id"] for clip in routed] == ["clip-b", "clip-a"]
    assert [clip["candidate_id"] for clip in calls[0][0]] == ["clip-b", "clip-a"]
    assert calls[0][1]["config"].config_hash == cfg["quality_moe_config_hash"]
    assert summary["input_count"] == summary["effective_count"] == 2
    assert [item["effective_decision"] for item in summary["assessments"]] == [
        "REVIEW", "REVIEW"
    ]


def test_active_quality_evaluation_exports_only_keep_but_persists_review(tmp_path, monkeypatch):
    cfg = adaptive.extract_config({"quality_moe": {"report_only": False}})
    candidates = [
        {"clip_id": "keep", "start_ts": 1.0, "end_ts": 5.0},
        {"clip_id": "review", "start_ts": 6.0, "end_ts": 10.0},
    ]

    def fake_evaluate(values, **_kwargs):
        keep = _assessment("keep", cfg["quality_moe_config_hash"], QualityDecision.KEEP_AS_IS)
        review = _assessment("review", cfg["quality_moe_config_hash"], QualityDecision.REVIEW)
        return QualityBatchResult(
            (keep, review),
            ({**values[0], "quality_assessment": keep.to_dict()},),
            ({**values[1], "quality_assessment": review.to_dict()},),
        )

    monkeypatch.setattr(adaptive, "evaluate_candidates", fake_evaluate)

    routed, summary = adaptive._evaluate_quality_pipeline_candidates(
        candidates,
        video_path="source.mp4",
        cfg=cfg,
        work_dir=tmp_path,
    )

    assert [clip["clip_id"] for clip in routed] == ["keep"]
    assert summary["effective_count"] == 1
    assert summary["human_review_count"] == 1
    assert [item["candidate_id"] for item in summary["assessments"]] == [
        "keep", "review"
    ]


def test_report_only_preserves_hard_rejected_candidate_for_existing_export(
    tmp_path, monkeypatch,
):
    cfg = adaptive.extract_config({"quality_moe": {"report_only": True}})
    candidate = {"clip_id": "hard", "start_ts": 1.0, "end_ts": 5.0}
    rejected = QualityAssessment(
        candidate_id="hard",
        evaluation_version="quality-moe-v1",
        config_hash=cfg["quality_moe_config_hash"],
        policy_version="quality-moe-policy-v1",
        recommended_decision=QualityDecision.REJECT,
        effective_decision=QualityDecision.REJECT,
        confidence=1.0,
        hard_reasons=("transition_drop",),
    )
    monkeypatch.setattr(
        adaptive,
        "evaluate_candidates",
        lambda values, **_kwargs: QualityBatchResult((rejected,), (), ()),
    )

    routed, summary = adaptive._evaluate_quality_pipeline_candidates(
        [candidate], video_path="source.mp4", cfg=cfg, work_dir=tmp_path
    )

    assert [clip["clip_id"] for clip in routed] == ["hard"]
    assert routed[0]["quality_assessment"]["effective_decision"] == "REJECT"
    assert summary["effective_count"] == 1


def test_stage_gif_report_only_never_applies_recommended_repair_to_pixels(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-remains-unchanged")
    clip_id = "repair-clip"
    assessment = _assessment(
        clip_id,
        adaptive.extract_config({})["quality_moe_config_hash"],
        QualityDecision.KEEP_FOR_REPAIR,
    ).to_dict()
    assessment.update({
        "input_hash": "1" * 64,
        "evidence_hashes": ["2" * 64],
        "selected_recipe_id": "repair-1",
        "current_quality": 0.55,
        "recoverable_quality": 0.78,
        "repair": {"recipe_id": "repair-1", "recipe_hash": "3" * 64},
        "provenance": {"source_file_sha256": "4" * 64},
    })
    rank_manifest = {
        "schema_version": 2,
        "clips": [{
            "clip_id": clip_id,
            "rank": 1,
            "start_ts": 1.0,
            "end_ts": 5.0,
            "guarded_export_window": True,
            "gif_worthiness": 0.8,
            "frame_count": 1,
            "quality_assessment": assessment,
        }],
    }
    monkeypatch.setattr(adaptive, "_read_upstream_manifest", lambda *_a, **_k: rank_manifest)
    monkeypatch.setattr(
        adaptive.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(stdout="20.0", returncode=0),
    )
    monkeypatch.setattr(
        adaptive,
        "_validated_repair_recipe",
        lambda *_a, **_k: object(),
    )
    filter_calls = []

    def fake_filter(recipe, *, fps, max_width):
        filter_calls.append((recipe, fps, max_width))
        repair = ",eq=brightness=0.1" if recipe is not None else ""
        return f"fps=12{repair},scale=320:-1:flags=lanczos"

    monkeypatch.setattr(adaptive, "build_ffmpeg_filter", fake_filter)
    commands = {}

    def fake_export(*, palette_command, gif_command, palette_path, output_path):
        commands["palette"] = palette_command
        commands["gif"] = gif_command
        open(output_path, "wb").write(b"gif-bytes")
        return SimpleNamespace(success=True, size_bytes=9, error=None)

    monkeypatch.setattr(adaptive, "run_gif_export_attempt", fake_export)
    cfg = adaptive.extract_config({
        "adaptive": {"gif_fps": 12, "gif_max_width": 320},
        "quality_moe": {"report_only": True},
    })
    export_dir = tmp_path / "exports"
    frames_dir = tmp_path / "frames"
    export_dir.mkdir()
    frames_dir.mkdir()

    result = adaptive._stage_gif_clip(
        str(source), str(frames_dir), str(export_dir), str(tmp_path), cfg,
        clip_id=clip_id, inputs={},
    )

    assert len(filter_calls) == 1
    assert filter_calls[0][0] is None
    assert commands["palette"][commands["palette"].index("-vf") + 1] == (
        "fps=12,scale=320:-1:flags=lanczos,palettegen"
    )
    assert commands["gif"][commands["gif"].index("-lavfi") + 1] == (
        "fps=12,scale=320:-1:flags=lanczos[x];[x][1:v]paletteuse"
    )
    assert source.read_bytes() == b"source-remains-unchanged"
    manifest = json.loads(
        open(result["_artifacts"][1]["path"], encoding="utf-8").read()
    )
    assert manifest["quality_decision"] == "KEEP_FOR_REPAIR"
    assert manifest["current_quality"] == 0.55
    assert manifest["recoverable_quality"] == 0.78
    assert manifest["repair_applied"] is False
    assert manifest["selected_recipe_id"] is None
    assert manifest["selected_recipe"] is None
    assert manifest["evidence_hashes"] == ["2" * 64]
    assert manifest["config_hash"] == cfg["quality_moe_config_hash"]
    assert manifest["parent_source"] == {
        "candidate_id": clip_id,
        "input_hash": "1" * 64,
        "source_file_sha256": "4" * 64,
        "video_path": str(source.resolve()),
        "start_ts": 1.0,
        "end_ts": 5.0,
    }


def test_repair_filter_selection_requires_bound_recipe_validation():
    cfg = adaptive.extract_config({"quality_moe": {"report_only": False}})
    config_hash = cfg["quality_moe_config_hash"]
    delta = ExpertEvidence(
        candidate_id="clip-1",
        evaluation_version="quality-moe-v1",
        expert_id="repair-scorer",
        expert_version="v1",
        signal_family="repair_delta",
        status=EvidenceStatus.AVAILABLE,
        scores={"technical_integrity": 0.8},
        input_hash="2" * 64,
        parent_input_hash="1" * 64,
        config_hash=config_hash,
        polarity=EvidencePolarity.POSITIVE,
    )
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
            source_input_hash="1" * 64,
            proxy_artifact_hash="2" * 64,
            recipe_hash=base.recipe_hash,
            config_hash=config_hash,
            repair_delta_evidence_id=delta.identity_hash,
            repair_delta_status=EvidenceStatus.AVAILABLE,
        ),
    )
    assessment = QualityAssessment(
        candidate_id="clip-1",
        evaluation_version="quality-moe-v1",
        config_hash=config_hash,
        policy_version="quality-moe-policy-v1",
        recommended_decision=QualityDecision.KEEP_FOR_REPAIR,
        effective_decision=QualityDecision.KEEP_FOR_REPAIR,
        confidence=0.9,
        repair=recipe,
        input_hash="1" * 64,
        evidence=(delta,),
    )
    payload = adaptive._enrich_quality_assessment(assessment.to_dict())

    selected = adaptive._validated_repair_recipe(
        payload, candidate_id="clip-1", config_hash=config_hash
    )
    assert selected is not None
    assert selected.recipe_id == "repair-1"

    payload["selected_recipe_id"] = "invented"
    assert adaptive._validated_repair_recipe(
        payload, candidate_id="clip-1", config_hash=config_hash
    ) is None
    payload["selected_recipe_id"] = "repair-1"
    payload["config_hash"] = "f" * 64
    assert adaptive._validated_repair_recipe(
        payload, candidate_id="clip-1", config_hash=config_hash
    ) is None

    forged = adaptive._enrich_quality_assessment(assessment.to_dict())
    forged["evidence"][0]["signal_family"] = "nr_vqa"
    forged_hash = adaptive._quality_evidence_hash(forged["evidence"][0])
    forged["evidence_hashes"] = [forged_hash]
    forged["repair"]["validation"]["repair_delta_evidence_id"] = forged_hash
    assert adaptive._validated_repair_recipe(
        forged, candidate_id="clip-1", config_hash=config_hash
    ) is None


def test_staged_rank_writes_valid_report_only_assessments_after_dedup(
    tmp_path, monkeypatch,
):
    clips = [
        {"start_ts": 2.0, "end_ts": 6.0, "gif_worthiness": 0.7},
        {"start_ts": 8.0, "end_ts": 12.0, "gif_worthiness": 0.9},
    ]
    monkeypatch.setattr(
        adaptive,
        "_read_upstream_manifest",
        lambda *_a, **_k: {"clips": clips, "scored_frames": []},
    )
    monkeypatch.setattr(
        adaptive.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(stdout="20.0", returncode=0, stderr=""),
    )

    def fake_materialize(*, clip, **_kwargs):
        clean = {
            **clip,
            "action_boundary_mode": "cv",
            "action_boundary_confidence": 0.8,
            "action_vlm_verified": False,
            "action_analysis_version": 1,
            "guarded_export_window": True,
        }
        return SimpleNamespace(
            clips=(clean,), transition_metrics={}, action_metrics={},
        )

    monkeypatch.setattr(adaptive, "materialize_action_candidates", fake_materialize)
    seen = []

    def fake_evaluate(values, **kwargs):
        seen.extend(values)
        assessments = tuple(
            _assessment(
                value["candidate_id"], kwargs["config"].config_hash,
                QualityDecision.KEEP_AS_IS,
            )
            for value in values
        )
        effective = tuple(
            {**value, "quality_assessment": assessment.to_dict()}
            for value, assessment in zip(values, assessments)
        )
        return QualityBatchResult(assessments, effective, ())

    monkeypatch.setattr(adaptive, "evaluate_candidates", fake_evaluate)
    cfg = adaptive.extract_config({
        "adaptive": {
            "embedding_dedup_enabled": False,
            "temporal_dedup_enabled": False,
            "output_ratio": 1.0,
            "max_output": 0,
        },
        "quality_moe": {"report_only": True},
    })

    result = adaptive._stage_rank_dedup(
        "source.mp4", str(tmp_path / "exports"), str(tmp_path), cfg, {},
        config_data={"quality_moe": {"report_only": True}},
    )
    manifest = json.loads(open(result["_artifacts"][0]["path"], encoding="utf-8").read())

    assert [clip["gif_worthiness"] for clip in seen] == [0.9, 0.7]
    assert [clip["gif_worthiness"] for clip in manifest["clips"]] == [0.9, 0.7]
    assert manifest["quality_moe"]["assessed_count"] == 2
    from app.task_engine.artifacts import validate_manifest_json
    validate_manifest_json(
        json.dumps(manifest).encode("utf-8"), "rank_dedup_manifest"
    )


def test_direct_config_uses_one_loaded_snapshot_not_global_get(monkeypatch):
    snapshot = {
        "adaptive": {"gif_fps": 11},
        "preference_memory": {"enabled": True},
        "quality_moe": {"enabled": True, "report_only": False},
    }
    monkeypatch.setattr(
        adaptive, "get", lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("global config must not be reread")
        ),
        raising=False,
    )

    cfg = adaptive._extract_direct_snapshot_config(snapshot)

    assert cfg["gif_fps"] == 11
    assert cfg["preference_memory_enabled"] is True
    assert cfg["quality_moe"]["report_only"] is False

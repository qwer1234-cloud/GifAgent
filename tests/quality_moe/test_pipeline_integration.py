from __future__ import annotations

import json
import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    input_hash = hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()
    evidence = tuple(
        ExpertEvidence(
            candidate_id=candidate_id,
            evaluation_version="quality-moe-v1",
            expert_id=expert_id,
            expert_version="v1",
            signal_family=signal_family,
            status=EvidenceStatus.AVAILABLE,
            scores={"technical_integrity": 0.8},
            input_hash=input_hash,
            config_hash=config_hash,
            polarity=EvidencePolarity.POSITIVE,
        )
        for expert_id, signal_family in (
            ("technical", "nr_vqa"),
            ("temporal", "deterministic_temporal"),
            ("cinematic", "cinematic_classifier"),
        )
    )
    return QualityAssessment(
        candidate_id=candidate_id,
        evaluation_version="quality-moe-v1",
        config_hash=config_hash,
        policy_version="quality-moe-policy-v1",
        recommended_decision=decision,
        effective_decision=decision,
        confidence=0.91,
        input_hash=input_hash,
        evidence=evidence,
    )


def _valid_repair_assessment(candidate_id: str, config_hash: str) -> dict:
    delta = ExpertEvidence(
        candidate_id=candidate_id,
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
            candidate_id=candidate_id,
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
        candidate_id=candidate_id,
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
    payload = adaptive._enrich_quality_assessment(
        assessment.to_dict(), candidate={"transition_action": "keep"}
    )
    payload.update({
        "current_quality": 0.55,
        "recoverable_quality": 0.78,
        "provenance": {"source_file_sha256": "4" * 64},
    })
    return payload


def _synthesize_lineage_ref() -> dict:
    return {
        "artifact_id": "synth-artifact",
        "stage_id": "synth-stage",
        "artifact_kind": "synthesize_manifest",
        "sha256": "a" * 64,
        "size_bytes": 123,
    }


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
        {"synthesize_manifest": [_synthesize_lineage_ref()]},
        config_data={"adaptive": {}},
    )

    manifest_path = result["_artifacts"][0]["path"]
    manifest = json.loads(open(manifest_path, encoding="utf-8").read())
    summary = manifest["quality_moe"]
    assert summary["input_count"] == summary["assessed_count"] == 0
    assert summary["effective_count"] == summary["human_review_count"] == 0
    assert summary["assessments"] == summary["assessed_candidates"] == []
    assert summary["candidate_ledger"]["mode"] == "external"


def test_staged_rank_writes_external_candidate_ledger_bound_to_input_artifact(
    tmp_path,
):
    synth_path = tmp_path / "synthesize_manifest.json"
    synth_path.write_text(json.dumps({
        "schema_version": 1,
        "stage": "synthesize",
        "clips": [],
    }), encoding="utf-8")
    synth_raw = synth_path.read_bytes()
    synth_ref = {
        "artifact_id": "synth-artifact",
        "stage_id": "synth-stage",
        "artifact_kind": "synthesize_manifest",
        "path": str(synth_path),
        "sha256": hashlib.sha256(synth_raw).hexdigest(),
        "size_bytes": len(synth_raw),
    }
    cfg = adaptive.extract_config({"quality_moe": {"report_only": True}})

    result = adaptive._stage_rank_dedup(
        "source.mp4", str(tmp_path / "exports"), str(tmp_path), cfg,
        {"synthesize_manifest": [synth_ref]},
        {"_stage_id": "rank-stage"},
    )

    artifacts = {item["artifact_kind"]: item for item in result["_artifacts"]}
    assert set(artifacts) == {"rank_dedup_manifest", "rank_candidate_ledger"}
    manifest = json.loads(open(
        artifacts["rank_dedup_manifest"]["path"], encoding="utf-8"
    ).read())
    ledger = json.loads(open(
        artifacts["rank_candidate_ledger"]["path"], encoding="utf-8"
    ).read())
    assert manifest["quality_moe"]["candidate_ledger"]["mode"] == "external"
    assert ledger["upstream_artifact"] == {
        key: synth_ref[key]
        for key in (
            "artifact_id", "stage_id", "artifact_kind", "sha256", "size_bytes",
        )
    }
    assert ledger["assessed_candidates"] == []


def test_direct_quality_summary_keeps_independent_assessed_candidate_ledger(
    tmp_path, monkeypatch,
):
    cfg = adaptive.extract_config({"quality_moe": {"report_only": True}})
    candidate = {
        "candidate_id": "clip-1",
        "clip_id": "clip-1",
        "start_ts": 1.0,
        "end_ts": 2.0,
        "transition_action": "keep",
    }
    assessment = _assessment(
        "clip-1", cfg["quality_moe_config_hash"], QualityDecision.KEEP_AS_IS
    )
    monkeypatch.setattr(
        adaptive,
        "evaluate_candidates",
        lambda *_a, **_k: QualityBatchResult(
            (assessment,), ({**candidate},), (),
        ),
    )

    _routed, summary = adaptive._evaluate_quality_pipeline_candidates(
        [candidate], video_path="source.mp4", cfg=cfg, work_dir=tmp_path,
    )

    assert summary["candidate_ledger"] == {"mode": "embedded"}
    assert summary["assessed_candidates"] == [{
        "candidate_id": "clip-1",
        "hard_gate_context": {"transition_action": "keep"},
        "hard_gate_context_hash": hashlib.sha256(
            b'{"transition_action":"keep"}'
        ).hexdigest(),
    }]


@pytest.mark.parametrize(
    "candidates",
    [
        [{"start_ts": 1.0, "end_ts": 2.0}],
        [
            {"candidate_id": "same", "start_ts": 1.0, "end_ts": 2.0},
            {"candidate_id": "same", "start_ts": 3.0, "end_ts": 4.0},
        ],
    ],
    ids=["missing", "duplicate"],
)
def test_shared_quality_boundary_rejects_missing_or_duplicate_candidate_ids(
    candidates, tmp_path, monkeypatch,
):
    cfg = adaptive.extract_config({"quality_moe": {"report_only": False}})
    monkeypatch.setattr(
        adaptive,
        "evaluate_candidates",
        lambda *_a, **_k: pytest.fail("invalid IDs reached the evaluator"),
    )

    with pytest.raises(ValueError, match="candidate_id"):
        adaptive._evaluate_quality_pipeline_candidates(
            candidates, video_path="source.mp4", cfg=cfg, work_dir=tmp_path,
        )


@pytest.mark.parametrize(
    "batch_factory",
    [
        lambda keep, review, values: QualityBatchResult(
            (keep,), (values[0],), (values[1],)
        ),
        lambda keep, review, values: QualityBatchResult(
            (review, keep), (values[0],), (values[1],)
        ),
    ],
    ids=["missing-assessment", "reordered-assessments"],
)
def test_shared_quality_boundary_requires_assessments_one_to_one_in_input_order(
    batch_factory, tmp_path, monkeypatch,
):
    cfg = adaptive.extract_config({"quality_moe": {"report_only": False}})
    candidates = [
        {"candidate_id": "keep", "start_ts": 1.0, "end_ts": 2.0},
        {"candidate_id": "review", "start_ts": 3.0, "end_ts": 4.0},
    ]

    def fake_evaluate(values, **_kwargs):
        keep = _assessment(
            "keep", cfg["quality_moe_config_hash"], QualityDecision.KEEP_AS_IS
        )
        review = _assessment(
            "review", cfg["quality_moe_config_hash"], QualityDecision.REVIEW
        )
        return batch_factory(keep, review, values)

    monkeypatch.setattr(adaptive, "evaluate_candidates", fake_evaluate)

    with pytest.raises(ValueError, match="assessments"):
        adaptive._evaluate_quality_pipeline_candidates(
            candidates, video_path="source.mp4", cfg=cfg, work_dir=tmp_path,
        )


@pytest.mark.parametrize(
    "effective_ids,human_ids",
    [
        (["foreign"], ["review"]),
        (["keep"], ["foreign"]),
        (["keep", "keep"], ["review"]),
        (["keep"], ["review", "review"]),
        (["review"], ["review"]),
        (["keep"], ["keep"]),
    ],
    ids=[
        "foreign-effective", "foreign-human", "duplicate-effective",
        "duplicate-human", "wrong-effective-route", "wrong-human-route",
    ],
)
def test_shared_quality_boundary_rejects_invalid_batch_routing(
    effective_ids, human_ids, tmp_path, monkeypatch,
):
    cfg = adaptive.extract_config({"quality_moe": {"report_only": False}})
    candidates = [
        {"candidate_id": "keep", "start_ts": 1.0, "end_ts": 2.0},
        {"candidate_id": "review", "start_ts": 3.0, "end_ts": 4.0},
    ]

    def fake_evaluate(values, **_kwargs):
        assessments = (
            _assessment(
                "keep", cfg["quality_moe_config_hash"],
                QualityDecision.KEEP_AS_IS,
            ),
            _assessment(
                "review", cfg["quality_moe_config_hash"],
                QualityDecision.REVIEW,
            ),
        )
        by_id = {item["candidate_id"]: item for item in values}
        by_id["foreign"] = {"candidate_id": "foreign", "start_ts": 9.0}
        return QualityBatchResult(
            assessments,
            tuple(by_id[item_id] for item_id in effective_ids),
            tuple(by_id[item_id] for item_id in human_ids),
        )

    monkeypatch.setattr(adaptive, "evaluate_candidates", fake_evaluate)

    with pytest.raises(ValueError, match="routing"):
        adaptive._evaluate_quality_pipeline_candidates(
            candidates, video_path="source.mp4", cfg=cfg, work_dir=tmp_path,
        )


def test_shared_quality_boundary_rebuilds_routed_payload_from_original_input(
    tmp_path, monkeypatch,
):
    cfg = adaptive.extract_config({"quality_moe": {"report_only": False}})
    candidate = {
        "candidate_id": "keep",
        "clip_id": "keep",
        "start_ts": 1.0,
        "end_ts": 2.0,
        "nested": {"source": "original"},
    }
    assessment = _assessment(
        "keep", cfg["quality_moe_config_hash"], QualityDecision.KEEP_AS_IS
    )
    replacement = {
        **candidate,
        "start_ts": 99.0,
        "nested": {"source": "batch-replacement"},
    }
    monkeypatch.setattr(
        adaptive,
        "evaluate_candidates",
        lambda *_a, **_k: QualityBatchResult(
            (assessment,), (replacement,), (),
        ),
    )

    routed, _summary = adaptive._evaluate_quality_pipeline_candidates(
        [candidate], video_path="source.mp4", cfg=cfg, work_dir=tmp_path,
    )

    assert routed[0]["start_ts"] == 1.0
    assert routed[0]["nested"] == {"source": "original"}


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
    cfg = adaptive.extract_config({
        "adaptive": {"gif_fps": 12, "gif_max_width": 320},
        "quality_moe": {"report_only": True},
    })
    assessment = _valid_repair_assessment(
        clip_id, cfg["quality_moe_config_hash"]
    )
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    assessment["provenance"]["source_file_sha256"] = source_sha256
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
            "transition_action": "keep",
            "action_boundary_mode": "cv",
            "action_boundary_confidence": 0.8,
            "action_vlm_verified": False,
            "action_analysis_version": 1,
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
    assert manifest["recommended_recipe_id"] == "repair-1"
    assert manifest["recommended_recipe"] == assessment["repair"]
    assert manifest["applied_recipe_id"] is None
    assert manifest["applied_recipe"] is None
    assert manifest["evidence_hashes"] == assessment["evidence_hashes"]
    assert manifest["config_hash"] == cfg["quality_moe_config_hash"]
    assert manifest["parent_source"] == {
        "candidate_id": clip_id,
        "input_hash": "1" * 64,
        "source_file_sha256": source_sha256,
        "video_path": str(source.resolve()),
        "start_ts": 1.0,
        "end_ts": 5.0,
    }

    materialize_work = tmp_path / "materialize"
    materialize_work.mkdir()
    gif_artifact = dict(result["_artifacts"][0])
    manifest_artifact = dict(result["_artifacts"][1])
    gif_artifact["sha256"] = hashlib.sha256(
        Path(gif_artifact["path"]).read_bytes()
    ).hexdigest()
    manifest_artifact["sha256"] = hashlib.sha256(
        Path(manifest_artifact["path"]).read_bytes()
    ).hexdigest()
    materialized = adaptive._stage_materialize(
        str(source), str(export_dir), str(materialize_work), cfg,
        inputs={
            "schema_version": 1,
            "stage": "materialize",
            "artifacts": {
                "gif_file": [gif_artifact],
                "gif_clip_manifest": [manifest_artifact],
            },
            "stage_statuses": [],
        },
        config_data={"export_base_dir": str(tmp_path / "formal")},
    )
    result_path = next(
        item["path"] for item in materialized["_artifacts"]
        if item["artifact_kind"] == "result"
    )
    published = json.loads(open(result_path, encoding="utf-8").read())[
        "succeeded"
    ][0]
    assert published["recommended_recipe_id"] == "repair-1"
    assert published["recommended_recipe"] == assessment["repair"]
    assert published["applied_recipe_id"] is None
    assert published["applied_recipe"] is None
    assert published["repair_applied"] is False


def test_stage_gif_rejects_source_changed_after_quality_rank(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"ranked-source")
    clip_id = "changed-source"
    cfg = adaptive.extract_config({"quality_moe": {"report_only": True}})
    assessment = _valid_repair_assessment(
        clip_id, cfg["quality_moe_config_hash"]
    )
    assessment["provenance"]["source_file_sha256"] = hashlib.sha256(
        source.read_bytes()
    ).hexdigest()
    rank_manifest = {
        "schema_version": 2,
        "clips": [
            {
                "clip_id": clip_id,
                "rank": 1,
                "start_ts": 1.0,
                "end_ts": 5.0,
                "guarded_export_window": True,
                "gif_worthiness": 0.8,
                "frame_count": 1,
                "quality_assessment": assessment,
            }
        ],
    }
    monkeypatch.setattr(
        adaptive, "_read_upstream_manifest", lambda *_args, **_kwargs: rank_manifest
    )
    export_called = False

    def forbidden_export(**_kwargs):
        nonlocal export_called
        export_called = True
        raise AssertionError("FFmpeg export must not run for a changed source")

    monkeypatch.setattr(adaptive, "run_gif_export_attempt", forbidden_export)
    source.write_bytes(b"changed-after-rank")

    with pytest.raises(ValueError, match="source.*changed"):
        adaptive._stage_gif_clip(
            str(source),
            str(tmp_path),
            str(tmp_path),
            str(tmp_path),
            cfg,
            clip_id=clip_id,
            inputs={},
        )
    assert export_called is False


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


def test_staged_rank_rebuilds_valid_active_assessments_after_dedup(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
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
            replace(
                _assessment(
                    value["candidate_id"], kwargs["config"].config_hash,
                    QualityDecision.KEEP_AS_IS,
                ),
                provenance={
                    "source_file_sha256": source_sha,
                    "source_video": str(source),
                },
            )
            for value in values
        )
        effective = tuple(
            {
                **value,
                "gif_worthiness": -1.0,
                "quality_assessment": assessment.to_dict(),
            }
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
        "quality_moe": {"report_only": False},
    })

    result = adaptive._stage_rank_dedup(
        str(source), str(tmp_path / "exports"), str(tmp_path), cfg,
        {"synthesize_manifest": [_synthesize_lineage_ref()]},
        config_data={"quality_moe": {"report_only": False}},
    )
    manifest = json.loads(open(result["_artifacts"][0]["path"], encoding="utf-8").read())

    assert [clip["gif_worthiness"] for clip in seen] == [0.7, 0.9]
    assert [clip["gif_worthiness"] for clip in manifest["clips"]] == [0.9, 0.7]
    assert manifest["quality_moe"]["assessed_count"] == 2
    artifacts = {item["artifact_kind"]: item for item in result["_artifacts"]}
    ledger_ref = {
        **manifest["quality_moe"]["candidate_ledger"],
        "path": artifacts["rank_candidate_ledger"]["path"],
    }
    from app.task_engine.artifacts import validate_manifest_json
    validated = validate_manifest_json(
        Path(artifacts["rank_dedup_manifest"]["path"]).read_bytes(),
        "rank_dedup_manifest",
        candidate_ledger_bytes=Path(
            artifacts["rank_candidate_ledger"]["path"]
        ).read_bytes(),
        candidate_ledger_ref=ledger_ref,
        upstream_artifact_ref=_synthesize_lineage_ref(),
    )
    assert validated["quality_moe"]["assessed_count"] == 2


def test_staged_rank_evaluates_quality_before_output_truncation(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    clips = [
        {"start_ts": 2.0, "end_ts": 6.0, "gif_worthiness": 0.7},
        {"start_ts": 8.0, "end_ts": 12.0, "gif_worthiness": 0.9},
        {"start_ts": 14.0, "end_ts": 18.0, "gif_worthiness": 0.8},
    ]
    monkeypatch.setattr(
        adaptive,
        "_read_upstream_manifest",
        lambda *_a, **_k: {"clips": clips, "scored_frames": []},
    )
    monkeypatch.setattr(
        adaptive.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(stdout="30.0", returncode=0, stderr=""),
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
            replace(
                _assessment(
                    value["candidate_id"], kwargs["config"].config_hash,
                    QualityDecision.KEEP_AS_IS,
                ),
                provenance={
                    "source_file_sha256": source_sha,
                    "source_video": str(source),
                },
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
            "max_output": 1,
        },
        "quality_moe": {"report_only": True},
    })

    result = adaptive._stage_rank_dedup(
        str(source), str(tmp_path / "exports"), str(tmp_path), cfg,
        {"synthesize_manifest": [_synthesize_lineage_ref()]},
        config_data={"quality_moe": {"report_only": True}},
    )
    manifest = json.loads(open(result["_artifacts"][0]["path"], encoding="utf-8").read())

    assert [clip["gif_worthiness"] for clip in seen] == [0.7, 0.9, 0.8]
    assert manifest["quality_moe"]["input_count"] == 3
    assert manifest["quality_moe"]["assessed_count"] == 3
    assert manifest["quality_moe"]["effective_count"] == 3
    assert [clip["gif_worthiness"] for clip in manifest["clips"]] == [0.9]
    assert manifest["clip_count"] == 1

    artifacts = {item["artifact_kind"]: item for item in result["_artifacts"]}
    ledger_ref = {
        **manifest["quality_moe"]["candidate_ledger"],
        "path": artifacts["rank_candidate_ledger"]["path"],
    }
    from app.task_engine.artifacts import validate_manifest_json
    validated = validate_manifest_json(
        Path(artifacts["rank_dedup_manifest"]["path"]).read_bytes(),
        "rank_dedup_manifest",
        candidate_ledger_bytes=Path(
            artifacts["rank_candidate_ledger"]["path"]
        ).read_bytes(),
        candidate_ledger_ref=ledger_ref,
        upstream_artifact_ref=_synthesize_lineage_ref(),
    )
    assert validated["quality_moe"]["assessed_count"] == 3
    assert validated["clip_count"] == 1


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


def test_stage_mode_only_reads_job_frozen_quality_endpoint(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "vlm": {
            "provider": "ollama", "model": "llava:13b",
            "base_url": "http://job-frozen.example:11434",
        },
        "quality_moe": {
            "judge": {
                "model_id": "llava:13b",
                "base_url": "http://job-frozen.example:11434",
            },
        },
    }), encoding="utf-8")
    monkeypatch.setattr(
        adaptive,
        "_resolve_quality_runtime_snapshot",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("stage must not resolve an already frozen endpoint")
        ),
    )
    monkeypatch.setattr(adaptive, "init_db", lambda: None)
    seen = []

    def fake_stage(stage, **kwargs):
        seen.append(kwargs["config_data"]["quality_moe"]["judge"]["base_url"])
        return {"output_key": stage, "_artifacts": []}

    monkeypatch.setattr(adaptive, "_run_stage", fake_stage)

    adaptive.run_stage_mode(
        stage="discover",
        video_path=str(tmp_path / "source.mp4"),
        work_dir=str(tmp_path / "work"),
        result_path=str(tmp_path / "result.json"),
        config_path=str(config_path),
    )

    assert seen == ["http://job-frozen.example:11434"]


def test_quality_stage_rejects_unfrozen_endpoint_sentinel_before_judge_http():
    cfg = adaptive.extract_config({
        "quality_moe": {
            "enabled": True,
            "judge": {"model_id": "llava:13b", "base_url": "inherit_vlm"},
        },
    })

    import pytest
    with pytest.raises(ValueError, match="frozen absolute URL"):
        adaptive._quality_config_from_pipeline_cfg(cfg)


def test_direct_pipeline_binds_vlm_runtime_url_for_inherit_vlm_judge():
    """Direct mode must expose the live VLM URL the same way stage mode does."""
    cfg = adaptive.extract_config({
        "quality_moe": {
            "enabled": True,
            "judge": {"model_id": "llava:13b", "base_url": "inherit_vlm"},
        },
    })
    runtime = adaptive.VlmRuntimeConfig(
        provider="ollama",
        model="llava:13b",
        base_url="http://172.27.227.98:11434/",
        manage_lifecycle=False,
        launch_mode="none",
        retry_delay_s=0.0,
    )
    live = str(runtime.base_url).strip()
    if live.startswith(("http://", "https://")):
        cfg["_live_vlm_base_url"] = live.rstrip("/")

    quality = adaptive._quality_config_from_pipeline_cfg(cfg)
    assert quality.judge["base_url"] == "http://172.27.227.98:11434"


def test_quality_stage_materializes_inherit_vlm_from_live_vlm_url():
    cfg = adaptive.extract_config({
        "quality_moe": {
            "enabled": True,
            "judge": {"model_id": "llava:13b", "base_url": "inherit_vlm"},
        },
    })
    frozen_hash = cfg["quality_moe_config_hash"]
    cfg["_live_vlm_base_url"] = "http://172.27.227.98:11434/"

    quality = adaptive._quality_config_from_pipeline_cfg(cfg)

    assert quality.judge["base_url"] == "http://172.27.227.98:11434"
    assert quality.config_hash == frozen_hash
    assert cfg["quality_moe"]["judge"]["base_url"] == "inherit_vlm"


def test_stage_mode_attaches_live_url_for_inherit_vlm(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "vlm": {
            "provider": "ollama",
            "model": "llava:13b",
            "base_url": "auto",
            "launch_mode": "wsl",
            "wsl_distro": "Ubuntu-20.04",
            "manage_lifecycle": True,
        },
        "quality_moe": {
            "enabled": True,
            "judge": {"model_id": "llava:13b", "base_url": "inherit_vlm"},
        },
    }), encoding="utf-8")
    monkeypatch.setattr(
        adaptive,
        "_materialize_vlm_runtime",
        lambda runtime, _snapshot=None: replace(runtime, base_url="http://live-ollama.example:11434"),
    )
    monkeypatch.setattr(adaptive, "init_db", lambda: None)
    seen = []

    def fake_stage(stage, **kwargs):
        quality = adaptive._quality_config_from_pipeline_cfg(kwargs["cfg"])
        seen.append({
            "live": kwargs["cfg"].get("_live_vlm_base_url"),
            "judge": quality.judge["base_url"],
            "frozen": kwargs["config_data"]["quality_moe"]["judge"]["base_url"],
        })
        return {"output_key": stage, "_artifacts": []}

    monkeypatch.setattr(adaptive, "_run_stage", fake_stage)

    adaptive.run_stage_mode(
        stage="rank_dedup",
        video_path=str(tmp_path / "source.mp4"),
        work_dir=str(tmp_path / "work"),
        result_path=str(tmp_path / "result.json"),
        config_path=str(config_path),
    )

    assert seen == [{
        "live": "http://live-ollama.example:11434",
        "judge": "http://live-ollama.example:11434",
        "frozen": "inherit_vlm",
    }]

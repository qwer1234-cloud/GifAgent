"""Integration tests: candidate capture + source features in the shared
rank_dedup path (Task 3 of the 2026-09-11 learning-closure remediation)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app.pipeline.stages import rank_dedup as rank_dedup_stage
from app.services.curve_source_features import (
    CURVE_SOURCE_FEATURE_NAMES,
    DEFAULT_SOURCE_EXTRACTOR_SHA256,
    CurveFeatureResult,
)

EXTRACTOR = DEFAULT_SOURCE_EXTRACTOR_SHA256


def _synthetic_video(tmp_path: Path) -> Path:
    """Deterministic tiny MP4 (test pattern); never user media."""
    video = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=7:size=64x64:rate=8",
            "-pix_fmt", "yuv420p", str(video),
        ],
        check=True,
        capture_output=True,
    )
    return video


def _clips() -> list[dict]:
    return [
        {
            "start_ts": 0.5,
            "end_ts": 3.0,
            "gif_worthiness": 0.80,
            "best_frame": {"caption": "kitchen dance", "timestamp": 1.5},
            "frame_count": 3,
        },
        {
            "start_ts": 3.5,
            "end_ts": 6.0,
            "gif_worthiness": 0.60,
            "best_frame": {"caption": "hallway walk", "timestamp": 4.5},
            "frame_count": 3,
        },
    ]


class FakeCurveRuntime:
    """Deterministic stand-in for the out-of-process feature worker."""

    def __init__(self, unavailable_keys: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []
        self.unavailable_keys = set(unavailable_keys)

    def evaluate_video(self, source_path, source_sha256, windows, timeout_s):
        self.calls.append(
            (str(source_path), source_sha256, tuple(w.preexport_key for w in windows))
        )
        results = []
        for window in windows:
            if window.preexport_key in self.unavailable_keys:
                results.append(
                    CurveFeatureResult(
                        preexport_key=window.preexport_key,
                        status="UNAVAILABLE",
                        feature_version="curve-source-v1",
                        extractor_sha256=EXTRACTOR,
                        features=None,
                        actual_timestamps=(),
                        input_hash="i" * 64,
                        missing_reasons=("pose_coverage_low",),
                        wall_clock_ms=1.0,
                    )
                )
                continue
            features = {
                name: round(0.5 + index / 100.0, 6)
                for index, name in enumerate(CURVE_SOURCE_FEATURE_NAMES)
            }
            mid = (window.start_us + window.end_us) / 2.0 / 1e6
            results.append(
                CurveFeatureResult(
                    preexport_key=window.preexport_key,
                    status="AVAILABLE",
                    feature_version="curve-source-v1",
                    extractor_sha256=EXTRACTOR,
                    features=features,
                    actual_timestamps=(
                        window.start_us / 1e6,
                        mid,
                        window.end_us / 1e6,
                    ),
                    input_hash="i" * 64,
                    missing_reasons=(),
                    wall_clock_ms=1.0,
                )
            )
        return tuple(results)


def _capture_cfg(video: Path, tmp_path: Path, runtime, **overrides) -> dict:
    cfg = {
        "embed_sim_threshold": 0.94,
        "embed_dedup_enabled": False,
        "temporal_dedup_enabled": False,
        "temporal_dedup_min_gap_s": 12,
        "embed_dedup_max_gap_s": 0,
        "output_ratio": 1.0,
        "max_output": 2,
        "quality_ranking_adult_weight": 0.80,
        "quality_ranking_cinematic_weight": 0.20,
        "worthiness_threshold": 0.5,
        "min_brightness": 0,
        "transition_guard_enabled": False,
        "transition_min_duration_s": 2.0,
        "transition_boundary_margin_s": 0.25,
        "transition_scan_fps": 8,
        "transition_scan_width": 320,
        "transition_motion_compensation": True,
        "transition_hard_threshold": 0.65,
        "transition_soft_threshold": 0.40,
        "transition_soft_run_frames": 3,
        "transition_rescore_split_segments": True,
        "action_guard_enabled": False,
        "action_vlm_verify_enabled": False,
        "action_analysis_version": 1,
        "action_analysis_window_s": 30.0,
        "action_preferred_min_duration_s": 4.0,
        "action_preferred_max_duration_s": 12.0,
        "action_scan_fps": 4.0,
        "action_boundary_confidence_threshold": 0.65,
        "action_loop_adjust_s": 0.75,
        "action_vlm_min_worthiness": 0.60,
        "action_fallback_mode": "fixed_window",
        "action_config_hash": "fixture-action-hash",
        "boundary_snap_enabled": False,
        "quality_moe": {"enabled": False, "report_only": True},
        "preference_memory_enabled": False,
        "score_prompt_mode": "default",
        "vlm_temperature": 0.1,
        "vlm_top_p": 0.9,
        "vlm_top_k": 20,
        # capture wiring under test
        "candidate_capture_enabled": True,
        "candidate_capture_version": "capture-v1",
        "candidate_capture_evidence_root": str(tmp_path / "evidence"),
        "candidate_capture_db_path": str(tmp_path / "capture.db"),
        "curve_preference_enabled": True,
        "curve_preference_mode": "shadow",
        "curve_source_runtime": runtime,
    }
    cfg.update(overrides)
    return cfg


def _run_rank_stage(video: Path, tmp_path: Path, cfg: dict) -> tuple[dict, Path]:
    work = tmp_path / "work"
    export = tmp_path / "export"
    work.mkdir(parents=True, exist_ok=True)
    export.mkdir(parents=True, exist_ok=True)

    synth = {
        "schema_version": 1,
        "stage": "synthesize",
        "clips": _clips(),
        "scored_frames": [],
    }
    synth_path = work / "synthesize_manifest.json"
    synth_path.write_text(json.dumps(synth), encoding="utf-8")
    raw = synth_path.read_bytes()
    synth_ref = {
        "path": str(synth_path),
        "artifact_kind": "synthesize_manifest",
        "stage_id": "test-stage",
        "artifact_id": "art-synth-0001",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }

    def fake_quality(candidates, *, video_path, cfg, work_dir):
        from app.pipeline.quality_bridge import _quality_moe_summary

        return candidates, _quality_moe_summary(
            cfg,
            [],
            input_count=len(candidates),
            effective_count=len(candidates),
            human_review_count=0,
        )

    def fake_frame_scorer(**kwargs):
        payload = {
            "timestamp": kwargs.get("timestamp"),
            "path": kwargs.get("frame_path"),
            "caption": "synthetic",
            "emotional_core": "joy",
            "gif_worthiness": 0.9,
            "aesthetic_notes": [],
            "reason": "clean",
        }
        return payload, None

    original_quality = rank_dedup_stage._evaluate_quality_pipeline_candidates
    original_scorer = rank_dedup_stage._score_vlm_frame
    rank_dedup_stage._evaluate_quality_pipeline_candidates = fake_quality
    rank_dedup_stage._score_vlm_frame = fake_frame_scorer
    try:
        result = rank_dedup_stage._stage_rank_dedup(
            str(video),
            str(export),
            str(work),
            cfg,
            {"synthesize_manifest": [synth_ref]},
            {},
        )
    finally:
        rank_dedup_stage._evaluate_quality_pipeline_candidates = original_quality
        rank_dedup_stage._score_vlm_frame = original_scorer

    manifest_path = Path(result["_artifacts"][0]["path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest, work


def _capture_rows(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT record_id, run_id, preexport_key, capture_version, "
            "capture_status, payload_path, payload_sha256 "
            "FROM candidate_analysis_records"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def test_capture_and_features_persisted_in_shared_rank_path(tmp_path):
    runtime = FakeCurveRuntime()
    video = _synthetic_video(tmp_path)
    cfg = _capture_cfg(video, tmp_path, runtime)

    manifest, _work = _run_rank_stage(video, tmp_path, cfg)

    assert manifest.get("candidate_capture", {}).get("enabled") is True
    rows = _capture_rows(cfg["candidate_capture_db_path"])
    assert len(rows) == len(manifest["clips"]) >= 1
    keys = {row["preexport_key"] for row in rows}
    assert keys == {clip["preexport_key"] for clip in manifest["clips"]}
    for row in rows:
        assert row["capture_status"] == "CAPTURED"
        payload = json.loads(Path(row["payload_path"]).read_text(encoding="utf-8"))
        assert hashlib.sha256(
            Path(row["payload_path"]).read_bytes()
        ).hexdigest() == row["payload_sha256"]
        feature = payload["source_feature"]
        assert feature["status"] == "AVAILABLE"
        assert set(feature["features"]) == set(CURVE_SOURCE_FEATURE_NAMES)
        assert payload["source_video_sha256"] == _video_sha(video)

    # Every output clip carries its analysis record link.
    for clip in manifest["clips"]:
        assert clip.get("analysis_record_id")
        assert clip.get("source_feature_status") == "AVAILABLE"

    # One batched runtime call for the whole video.
    assert len(runtime.calls) == 1
    assert len(runtime.calls[0][2]) == len(manifest["clips"])

    # Selection events appended after ranking/output cap.
    conn = sqlite3.connect(cfg["candidate_capture_db_path"])
    try:
        events = conn.execute(
            "SELECT event_kind, COUNT(*) FROM candidate_analysis_events "
            "GROUP BY event_kind"
        ).fetchall()
    finally:
        conn.close()
    kinds = dict(events)
    assert kinds.get("scored", 0) == len(rows)
    assert kinds.get("selected", 0) + kinds.get("not_selected", 0) >= len(rows)


def _video_sha(video: Path) -> str:
    return hashlib.sha256(video.read_bytes()).hexdigest()


def test_disabled_capture_makes_no_db_or_runtime_calls(tmp_path):
    runtime = FakeCurveRuntime()
    video = _synthetic_video(tmp_path)
    cfg = _capture_cfg(
        video,
        tmp_path,
        runtime,
        candidate_capture_enabled=False,
        curve_preference_mode="off",
        curve_preference_enabled=False,
    )

    manifest, _work = _run_rank_stage(video, tmp_path, cfg)

    assert "candidate_capture" not in manifest
    assert runtime.calls == []
    assert not Path(cfg["candidate_capture_db_path"]).exists()
    assert not (tmp_path / "evidence").exists()
    for clip in manifest["clips"]:
        assert "analysis_record_id" not in clip
        # Disabled capture keeps the legacy 16-char identity behavior.
        assert "candidate_id" in clip
        assert "preexport_key" not in clip


def _scenario_dir(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """Each scenario gets its own video copy so run ids and evidence roots
    never collide (capture evidence is identity-immutable)."""
    root = tmp_path / name
    root.mkdir()
    video = _synthetic_video(root)
    return root, video


def test_shadow_feature_failure_keeps_formal_output(tmp_path):
    probe_root, probe_video = _scenario_dir(tmp_path, "probe")
    cfg_probe = _capture_cfg(probe_video, probe_root, FakeCurveRuntime())
    manifest_probe, _w = _run_rank_stage(probe_video, probe_root, cfg_probe)
    unavailable_key = sorted(c["preexport_key"] for c in manifest_probe["clips"])[0]

    main_root, main_video = _scenario_dir(tmp_path, "main")
    runtime = FakeCurveRuntime(unavailable_keys=(unavailable_key,))
    cfg = _capture_cfg(main_video, main_root, runtime)
    manifest, _work = _run_rank_stage(main_video, main_root, cfg)

    # Formal output unchanged: every probe clip still present.
    assert sorted(c["preexport_key"] for c in manifest["clips"]) == sorted(
        c["preexport_key"] for c in manifest_probe["clips"]
    )
    rows = {
        r["preexport_key"]: r
        for r in _capture_rows(cfg["candidate_capture_db_path"])
    }
    failed_payload = json.loads(
        Path(rows[unavailable_key]["payload_path"]).read_text(encoding="utf-8")
    )
    assert failed_payload["source_feature"]["status"] == "UNAVAILABLE"
    assert (
        "pose_coverage_low"
        in failed_payload["source_feature"]["missing_reasons"]
    )
    by_key = {c["preexport_key"]: c for c in manifest["clips"]}
    assert by_key[unavailable_key]["source_feature_status"] == "UNAVAILABLE"
    assert by_key[unavailable_key]["capture_status"] == "CAPTURED"


def test_active_mode_requires_complete_capture(tmp_path, monkeypatch):
    probe_root, probe_video = _scenario_dir(tmp_path, "probe")
    cfg_probe = _capture_cfg(
        probe_video, probe_root, FakeCurveRuntime(), curve_preference_mode="active"
    )
    manifest_probe, _w = _run_rank_stage(probe_video, probe_root, cfg_probe)
    victim_key = sorted(c["preexport_key"] for c in manifest_probe["clips"])[0]

    from app.services.candidate_capture import persist_capture as real_persist

    original = real_persist

    def failing_persist(conn, payload, evidence_root):
        if (
            payload.get("preexport_key") == victim_key
            and payload.get("capture_status") == "CAPTURED"
        ):
            raise RuntimeError("simulated capture db failure")
        return original(conn, payload, evidence_root)

    monkeypatch.setattr(
        "app.services.candidate_capture.persist_capture", failing_persist
    )

    main_root, main_video = _scenario_dir(tmp_path, "main")
    runtime = FakeCurveRuntime()
    cfg = _capture_cfg(
        main_video, main_root, runtime, curve_preference_mode="active"
    )
    manifest, _work = _run_rank_stage(main_video, main_root, cfg)

    out_keys = [c["preexport_key"] for c in manifest["clips"]]
    assert victim_key not in out_keys
    assert len(out_keys) == len(manifest_probe["clips"]) - 1


def test_publication_link_uses_actual_candidate_and_gif_hash(tmp_path):
    from app.services.candidate_capture import persist_capture
    from app.services.library_ingest import ingest_published_clips
    from app.services.preference_schema import apply_preference_schema

    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)

    gif_file = tmp_path / "formal.gif"
    gif_file.write_bytes(b"GIF89a-publication-test")
    gif_sha = hashlib.sha256(gif_file.read_bytes()).hexdigest()

    payload = {
        "run_id": "run-pub",
        "preexport_key": "a" * 64,
        "capture_version": "capture-v1",
        "source_video_sha256": "b" * 64,
        "start_us": 1_000_000,
        "end_us": 4_000_000,
        "anchor_us": 2_500_000,
        "baseline_score": 0.8,
        "capture_status": "CAPTURED",
    }
    persisted = persist_capture(conn, payload, tmp_path)

    clips = [
        {
            "clip_id": "clip_pub_001",
            "start_ts": 1.0,
            "end_ts": 4.0,
            "formal_path": str(gif_file),
            "gif_worthiness": 0.8,
            "sha256": gif_sha,
            "analysis_record_id": persisted.record_id,
        }
    ]
    result = ingest_published_clips(
        conn, video_path=str(tmp_path / "video.mp4"), clips=clips, run_id="run-pub"
    )
    assert result["ingested"] == 1
    assert result["needs_repair"] == 0

    link = conn.execute(
        "SELECT candidate_id, gif_sha256 FROM candidate_analysis_publications "
        "WHERE record_id = ?",
        (persisted.record_id,),
    ).fetchone()
    assert link is not None
    actual_candidate_id = conn.execute(
        "SELECT candidate_id FROM candidate_gifs WHERE source_run_candidate_id = 'clip_pub_001'"
    ).fetchone()[0]
    assert link[0] == actual_candidate_id
    assert link[1] == gif_sha


def test_publication_link_failure_marks_needs_repair(tmp_path, monkeypatch):
    from app.services import library_ingest as ingest_module
    from app.services.candidate_capture import persist_capture
    from app.services.library_ingest import ingest_published_clips
    from app.services.preference_schema import apply_preference_schema

    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)

    gif_file = tmp_path / "formal.gif"
    gif_file.write_bytes(b"GIF89a-publication-test")

    payload = {
        "run_id": "run-pub-fail",
        "preexport_key": "a" * 64,
        "capture_version": "capture-v1",
        "source_video_sha256": "b" * 64,
        "start_us": 1_000_000,
        "end_us": 4_000_000,
        "anchor_us": 2_500_000,
        "baseline_score": 0.8,
        "capture_status": "CAPTURED",
    }
    persisted = persist_capture(conn, payload, tmp_path)

    def broken_link(*_args, **_kwargs):
        raise RuntimeError("simulated link failure")

    monkeypatch.setattr(ingest_module, "link_publication", broken_link)

    clips = [
        {
            "clip_id": "clip_pub_002",
            "start_ts": 1.0,
            "end_ts": 4.0,
            "formal_path": str(gif_file),
            "gif_worthiness": 0.8,
            "sha256": hashlib.sha256(gif_file.read_bytes()).hexdigest(),
            "analysis_record_id": persisted.record_id,
        }
    ]
    result = ingest_published_clips(
        conn, video_path=str(tmp_path / "video.mp4"), clips=clips, run_id="run-pub-fail"
    )
    assert result["ingested"] == 1
    assert result["needs_repair"] == 1

    status = conn.execute(
        "SELECT capture_status FROM candidate_analysis_records WHERE record_id = ?",
        (persisted.record_id,),
    ).fetchone()[0]
    assert status == "NEEDS_REPAIR"
    event = conn.execute(
        "SELECT COUNT(*) FROM candidate_analysis_events WHERE record_id = ? "
        "AND event_kind = 'publication_failed'",
        (persisted.record_id,),
    ).fetchone()[0]
    assert event == 1


def test_disabled_and_shadow_formal_order_are_identical(tmp_path):
    """Task 8 Step 2: shadow capture changes neither the formal selection
    order nor the scores; evidence is added on the side."""
    outputs = {}
    for scenario in ("disabled", "shadow"):
        root = tmp_path / scenario
        root.mkdir()
        video = _synthetic_video(root)
        runtime = FakeCurveRuntime()
        cfg = _capture_cfg(
            video, root, runtime,
            candidate_capture_enabled=(scenario == "shadow"),
            curve_preference_mode="shadow" if scenario == "shadow" else "off",
        )
        manifest, _work = _run_rank_stage(video, root, cfg)
        # Identity schemes differ by design (legacy hash vs preexport
        # prefix); the business outcome -- window, rank, score -- must not.
        outputs[scenario] = [
            (
                clip.get("start_ts"),
                clip.get("end_ts"),
                clip.get("rank"),
                clip.get("final_score"),
            )
            for clip in manifest["clips"]
        ]
    assert outputs["disabled"] == outputs["shadow"]


def test_disabled_run_is_field_identical_to_legacy_shape(tmp_path):
    """Task 8 Step 1: a disabled run carries no capture/curve diagnostic
    fields on its business clips."""
    root = tmp_path / "disabled"
    root.mkdir()
    video = _synthetic_video(root)
    cfg = _capture_cfg(
        video, root, FakeCurveRuntime(), candidate_capture_enabled=False
    )
    manifest, _work = _run_rank_stage(video, root, cfg)
    for clip in manifest["clips"]:
        for forbidden in (
            "analysis_record_id",
            "capture_status",
            "source_feature_status",
            "source_features",
            "curve_route",
        ):
            assert forbidden not in clip


def test_tampered_capture_payload_fails_closed_at_ingest(tmp_path):
    """P1 remediation: a capture JSON modified on disk must not contribute
    features to the training ledger -- the hash check rolls back the
    publication link and marks the record NEEDS_REPAIR."""
    import sqlite3 as _sqlite3

    from app.services.candidate_capture import persist_capture
    from app.services.library_ingest import ingest_published_clips
    from app.services.preference_schema import apply_preference_schema
    from app.services.curve_source_features import (
        CURVE_SOURCE_FEATURE_NAMES,
        CurveWindow,
        window_input_hash,
    )

    conn = _sqlite3.connect(":memory:")
    apply_preference_schema(conn)

    gif_file = tmp_path / "formal.gif"
    gif_file.write_bytes(b"GIF89a-tamper-test")
    gif_sha = hashlib.sha256(gif_file.read_bytes()).hexdigest()

    payload = {
        "run_id": "run-tamper",
        "preexport_key": "a" * 64,
        "capture_version": "capture-v1",
        "source_video_sha256": "b" * 64,
        "start_us": 1_000_000,
        "end_us": 4_000_000,
        "anchor_us": 2_500_000,
        "baseline_score": 0.8,
        "capture_status": "CAPTURED",
        "source_feature": {
            "status": "AVAILABLE",
            "feature_version": "curve-source-v1",
            "extractor_sha256": "e" * 64,
            "features": {n: 0.5 for n in CURVE_SOURCE_FEATURE_NAMES},
            "actual_timestamps": [1.0, 2.0, 3.0],
            "input_hash": window_input_hash(
                "b" * 64,
                CurveWindow(
                    preexport_key="a" * 64,
                    start_us=1_000_000,
                    end_us=4_000_000,
                ),
                extractor_sha256="e" * 64,
            ),
            "missing_reasons": [],
            "wall_clock_ms": 1.0,
        },
    }
    persisted = persist_capture(conn, payload, tmp_path / "evidence")

    # Tamper with the payload on disk AFTER capture.
    payload_file = Path(persisted.payload_path)
    tampered = json.loads(payload_file.read_text(encoding="utf-8"))
    tampered["source_feature"]["features"]["extension_peak"] = 99.0
    payload_file.write_text(
        json.dumps(tampered, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    clips = [
        {
            "clip_id": "clip_tamper",
            "start_ts": 1.0,
            "end_ts": 4.0,
            "formal_path": str(gif_file),
            "gif_worthiness": 0.8,
            "sha256": gif_sha,
            "analysis_record_id": persisted.record_id,
        }
    ]
    result = ingest_published_clips(
        conn, video_path=str(tmp_path / "v.mp4"), clips=clips, run_id="run-tamper"
    )
    assert result["needs_repair"] == 1

    # Nothing from the tampered payload entered the ledger.
    features = conn.execute("SELECT COUNT(*) FROM curve_features").fetchone()[0]
    assert features == 0
    links = conn.execute(
        "SELECT COUNT(*) FROM candidate_analysis_publications WHERE record_id = ?",
        (persisted.record_id,),
    ).fetchone()[0]
    assert links == 0
    status = conn.execute(
        "SELECT capture_status FROM candidate_analysis_records WHERE record_id = ?",
        (persisted.record_id,),
    ).fetchone()[0]
    assert status == "NEEDS_REPAIR"


def test_features_mounted_before_ranking_in_real_rank_dedup_path(tmp_path, monkeypatch):
    """P1 remediation: in the REAL _stage_rank_dedup path the source features
    are mounted BEFORE the shared scoring, so a DRAFT shadow model actually
    produces non-baseline nominal/effective scores and the capture freezes
    them -- not a hand-seeded fixture."""
    import base64 as _b64

    import numpy as _np

    import numpy as _np

    from app.pipeline import ranking as pipeline_ranking
    from app.pipeline.stages import rank_dedup as rank_dedup_stage
    from app.services.curve_preference import canonical_json_sha256
    from app.services.curve_source_features import CURVE_SOURCE_FEATURE_NAMES
    from app.services.preference_schema import apply_preference_schema

    root = tmp_path / "shadow-real"
    root.mkdir()
    video = _synthetic_video(root)

    # A library DB with a published profile and a DRAFT source-domain model.
    lib = sqlite3.connect(":memory:")
    lib.row_factory = sqlite3.Row
    apply_preference_schema(lib)
    lib.execute(
        "INSERT INTO preference_profile_current (slot, profile_version) "
        "VALUES ('current', 'profile_v1')"
    )
    centroid = _np.asarray([1.0] + [0.0] * 767, dtype=_np.float32).tobytes()
    lib.execute(
        "INSERT INTO preference_profiles (profile_version, scope, scenario_key, "
        "like_count, dislike_count, neutral_count, confidence, liked_centroid_blob) "
        "VALUES ('profile_v1', 'global', NULL, 3, 1, 0, 0.9, ?)",
        (centroid,),
    )
    manifest = {"feature_version": "curve-source-v1", "sources": [], "rows": []}
    manifest_sha = canonical_json_sha256(manifest)
    coefficients = {name: 0.1 for name in CURVE_SOURCE_FEATURE_NAMES}
    lib.execute(
        """
        INSERT INTO curve_preference_models (
            model_version, feature_version, extractor_sha256, status,
            coefficients_json, intercept, feature_mean_json, feature_std_json,
            feature_names_json, l2_lambda, training_event_watermark,
            training_source_set_sha256, holdout_row_count, metrics_json
        ) VALUES ('draft-real-1', 'curve-source-v1', ?, 'DRAFT', ?, 0.0, ?, ?, ?, 1.0,
            'wm', 'ts', 40, '{}')
        """,
        (
            "e" * 64,
            json.dumps(coefficients),
            json.dumps({name: 0.5 for name in CURVE_SOURCE_FEATURE_NAMES}),
            json.dumps({name: 0.2 for name in CURVE_SOURCE_FEATURE_NAMES}),
            json.dumps(list(CURVE_SOURCE_FEATURE_NAMES)),
        ),
    )
    lib.execute(
        "UPDATE curve_preference_models SET training_manifest_json = ?, "
        "holdout_manifest_json = ?, training_manifest_sha256 = ?, "
        "holdout_manifest_sha256 = ? WHERE model_version = 'draft-real-1'",
        (
            json.dumps(manifest, sort_keys=True),
            json.dumps(manifest, sort_keys=True),
            manifest_sha,
            manifest_sha,
        ),
    )
    lib.commit()

    monkeypatch.setattr(pipeline_ranking, "get_connection", lambda: lib)
    monkeypatch.setattr(
        pipeline_ranking,
        "compute_text_embedding",
        lambda text: [1.0 if "dance" in text else 0.2] + [0.0] * 767,
    )

    runtime = FakeCurveRuntime()
    cfg = _capture_cfg(
        video, root, runtime,
        preference_memory_enabled=True,
        curve_shadow_model_version="draft-real-1",
        max_output=2,
    )
    original_scorer = rank_dedup_stage._score_vlm_frame
    rank_dedup_stage._score_vlm_frame = lambda **kwargs: (
        {
            "timestamp": kwargs.get("timestamp"),
            "path": kwargs.get("frame_path"),
            "caption": "kitchen dance",
            "emotional_core": "joy",
            "gif_worthiness": 0.9,
            "aesthetic_notes": [],
            "reason": "clean",
        },
        None,
    )
    try:
        manifest_out, _work = _run_rank_stage(video, root, cfg)
    finally:
        rank_dedup_stage._score_vlm_frame = original_scorer

    # The runtime must have received the REAL source file hash (review P1:
    # feature extraction bound to the actual video bytes).
    real_sha = hashlib.sha256(video.read_bytes()).hexdigest()
    assert runtime.calls
    for call_source_path, call_sha, _keys in runtime.calls:
        assert call_sha == real_sha
    for clip in manifest_out["clips"]:
        assert clip.get("source_video_sha256") == real_sha

    # The shadow route ran with real mounted features on the ranked clips.
    shadow_clips = [c for c in manifest_out["clips"] if c.get("curve_route")]
    assert shadow_clips, "no clip was curve-scored in the real rank path"
    for clip in shadow_clips:
        assert clip["curve_route"] == "SHADOW"
        assert clip["source_feature_status"] == "AVAILABLE"
        assert clip["curve_nominal_score"] != clip["curve_baseline_score"]
        # Production final score stays at the baseline in shadow.
        assert clip["final_score"] == clip["curve_baseline_score"]

    # The frozen capture payload carries the same three-way numbers.
    rows = _capture_rows(cfg["candidate_capture_db_path"])
    assert rows
    for row in rows:
        payload = json.loads(Path(row["payload_path"]).read_text(encoding="utf-8"))
        if payload.get("curve_route") == "SHADOW":
            assert payload["nominal_score"] is not None
            assert payload["nominal_score"] != payload["baseline_score"]
            identity = payload["preference_identity"]
            assert identity["vector_sha256"]
            # The frozen vector is the exact scoring input (checksum holds).
            decoded = _b64.b64decode(identity["vector_float32_b64"], validate=True)
            assert hashlib.sha256(decoded).hexdigest() == identity["vector_sha256"]


def test_profile_centroid_consumes_retrieval_vector_only_once(tmp_path):
    """P1 remediation: a candidate carrying BOTH the frozen preference
    vector and the retrieval clip vector contributes exactly ONE vector to
    profile centroids (the 'clip' row) -- the frozen scoring evidence is
    never double-counted into the preference profile."""
    import hashlib as _hashlib
    import numpy as _np
    import sqlite3 as _sqlite3

    from app.services.preference_memory import PreferenceMemoryService
    from app.services.preference_profile_snapshot import _consumed_vector_records
    from app.services.preference_schema import apply_preference_schema

    conn = _sqlite3.connect(":memory:")
    conn.row_factory = _sqlite3.Row
    apply_preference_schema(conn)

    cid = "cand-dual"
    clip_blob = _np.asarray([1.0] + [0.0] * 767, dtype=_np.float32).tobytes()
    preference_blob = _np.asarray([0.6, 0.8] + [0.0] * 766, dtype=_np.float32).tobytes()
    for vector_type, blob in (("clip", clip_blob), ("preference", preference_blob)):
        conn.execute(
            "INSERT INTO candidate_vectors (candidate_id, vector_type, "
            "embedding_model, embedding_dim, vector_blob, normalized, source_text_hash) "
            "VALUES (?, ?, 'nomic-embed-text:latest', 768, ?, 1, ?)",
            (cid, vector_type, blob, f"hash-{vector_type}"),
        )
    conn.commit()

    service = PreferenceMemoryService(conn)
    dual_centroid = service._compute_centroid(
        [cid],
        embedding_model="nomic-embed-text:latest",
        embedding_dim=768,
    )

    # Reference: the centroid with ONLY the retrieval vector present.
    conn.execute(
        "DELETE FROM candidate_vectors WHERE candidate_id = ? AND vector_type = 'preference'",
        (cid,),
    )
    conn.commit()
    clip_only_centroid = service._compute_centroid(
        [cid],
        embedding_model="nomic-embed-text:latest",
        embedding_dim=768,
    )
    assert dual_centroid == clip_only_centroid

    # The qualification snapshot freezes exactly one consumed vector record
    # for the dual-vector candidate.
    records = _consumed_vector_records(
        conn,
        target_ids=[cid],
        embedding_model="nomic-embed-text:latest",
        embedding_dim=768,
    )
    assert len(records) == 1
    assert records[0]["vector_blob_sha256"] == _hashlib.sha256(clip_blob).hexdigest()
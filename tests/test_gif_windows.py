import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.gif_windows import build_export_window
from scripts import test_video_adaptive


def _synthesize_artifact_ref(path: str) -> dict:
    candidate = Path(path)
    raw = candidate.read_bytes() if candidate.is_file() else b"standalone-fixture"
    return {
        "artifact_id": "standalone-synthesize",
        "stage_id": "standalone-synthesize-stage",
        "artifact_kind": "synthesize_manifest",
        "path": path,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }


def _validate_standalone_rank(work_dir: Path) -> dict:
    from app.task_engine.artifacts import validate_manifest_json

    manifest_path = work_dir / "rank_dedup_manifest.json"
    raw = manifest_path.read_bytes()
    preview = json.loads(raw)
    ledger_path = work_dir / "rank_candidate_ledger_manifest.json"
    return validate_manifest_json(
        raw,
        "rank_dedup_manifest",
        candidate_ledger_bytes=ledger_path.read_bytes(),
        candidate_ledger_ref={
            **preview["quality_moe"]["candidate_ledger"],
            "path": str(ledger_path),
        },
        upstream_artifact_ref=preview["quality_moe"]["candidate_ledger"][
            "upstream_artifact"
        ],
    )


def test_single_frame_window_is_centered_and_capped():
    """A top-scoring single needs the full configured duration, never more."""
    window = build_export_window(
        clip={"frame_count": 1, "best_frame_ts": 10.0, "gif_worthiness": 1.0},
        total_duration_s=30.0,
        min_duration_s=1.5,
        max_duration_s=5.0,
    )

    assert window.duration_s == 5.0
    assert window.start_s == pytest.approx(8.0)
    assert window.end_s == pytest.approx(13.0)


def test_single_frame_uses_its_own_cap():
    """One scored frame is thin evidence, so it gets a tighter ceiling."""
    clip = {"frame_count": 1, "gif_worthiness": 1.0, "best_frame_ts": 60.0}

    window = build_export_window(
        clip,
        total_duration_s=600.0,
        min_duration_s=2.0,
        max_duration_s=20.0,
        single_frame_max_duration_s=5.0,
    )

    assert window.duration_s == pytest.approx(5.0)


def test_multi_frame_ignores_single_frame_cap():
    """Merged runs keep their evidence-backed span-plus-three seconds."""
    clip = {
        "frame_count": 4,
        "start_ts": 30.0,
        "end_ts": 42.0,
        "best_frame_ts": 36.0,
    }

    window = build_export_window(
        clip,
        total_duration_s=600.0,
        min_duration_s=2.0,
        max_duration_s=20.0,
        single_frame_max_duration_s=5.0,
    )

    assert window.duration_s == pytest.approx(15.0)


def test_omitting_the_cap_preserves_legacy_behavior():
    clip = {"frame_count": 1, "gif_worthiness": 1.0, "best_frame_ts": 60.0}

    window = build_export_window(
        clip, total_duration_s=600.0, min_duration_s=2.0, max_duration_s=20.0
    )

    assert window.duration_s == pytest.approx(20.0)


def test_single_frame_cap_is_clamped_into_the_configured_bounds():
    """A cap below min_duration must not invert the interpolation range."""
    clip = {"frame_count": 1, "gif_worthiness": 0.0, "best_frame_ts": 60.0}

    window = build_export_window(
        clip,
        total_duration_s=600.0,
        min_duration_s=4.0,
        max_duration_s=20.0,
        single_frame_max_duration_s=1.0,
    )

    assert window.duration_s == pytest.approx(4.0)


def test_single_frame_cap_never_exceeds_max_duration():
    clip = {"frame_count": 1, "gif_worthiness": 1.0, "best_frame_ts": 60.0}

    window = build_export_window(
        clip,
        total_duration_s=600.0,
        min_duration_s=2.0,
        max_duration_s=8.0,
        single_frame_max_duration_s=50.0,
    )

    assert window.duration_s == pytest.approx(8.0)


def test_direct_clip_shape_uses_nested_best_frame_timestamp():
    """Legacy direct clips anchor the 40/60 window on their nested best frame."""
    window = build_export_window(
        clip={
            "frame_count": 1,
            "gif_worthiness": 1.0,
            "best_frame": {"timestamp": 10.0},
        },
        total_duration_s=30.0,
        min_duration_s=1.5,
        max_duration_s=5.0,
    )

    assert window.start_s == pytest.approx(8.0)
    assert window.end_s == pytest.approx(13.0)


def test_multi_frame_window_never_exceeds_max_duration():
    """A long merged run cannot bypass the configured export duration cap."""
    window = build_export_window(
        clip={
            "frame_count": 12,
            "start_ts": 10.0,
            "end_ts": 40.0,
            "best_frame_ts": 20.0,
            "gif_worthiness": 0.8,
        },
        total_duration_s=60.0,
        min_duration_s=2.0,
        max_duration_s=5.0,
    )

    assert window.duration_s == 5.0
    assert window.start_s == pytest.approx(18.0)
    assert window.end_s == pytest.approx(23.0)


def test_window_clamps_at_the_end_of_a_short_video():
    """Boundary clamping retains the requested duration when the video permits it."""
    window = build_export_window(
        clip={"frame_count": 1, "best_frame_ts": 29.5, "gif_worthiness": 1.0},
        total_duration_s=30.0,
        min_duration_s=1.5,
        max_duration_s=5.0,
    )

    assert window.start_s == 25.0
    assert window.end_s == 30.0
    assert window.duration_s == 5.0


def test_staged_export_uses_the_bounded_shared_window(tmp_path, monkeypatch):
    """The staged ffmpeg boundary receives and records the capped window."""
    target_clip = {
        "clip_id": "long-clip",
        "start_ts": 10.0,
        "end_ts": 40.0,
        "best_frame_ts": 20.0,
        "frame_count": 12,
        "gif_worthiness": 0.8,
        "rank": 1,
    }
    captured_attempts = []
    monkeypatch.setattr(
        test_video_adaptive,
        "_read_upstream_manifest",
        lambda *_args: {"clips": [target_clip]},
    )
    monkeypatch.setattr(
        test_video_adaptive.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="60.0\n"),
    )

    def fake_export_attempt(**kwargs):
        captured_attempts.append(kwargs)
        Path(kwargs["output_path"]).write_bytes(b"GIF89a")
        return SimpleNamespace(success=True, size_bytes=6, error=None)

    monkeypatch.setattr(
        test_video_adaptive, "run_gif_export_attempt", fake_export_attempt
    )
    frames_dir = tmp_path / "frames"
    export_dir = tmp_path / "exports"
    work_dir = tmp_path / "work"
    for directory in (frames_dir, export_dir, work_dir):
        directory.mkdir()

    test_video_adaptive._stage_gif_clip(
        video_path=str(tmp_path / "source.mp4"),
        frames_dir=str(frames_dir),
        export_dir=str(export_dir),
        work_dir=str(work_dir),
        cfg={"gif_fps": 24, "gif_max_width": 720, "min_duration": 2.0, "max_duration": 5.0},
        clip_id="long-clip",
        inputs={"rank_dedup_manifest": [{"path": "ignored"}]},
    )

    assert float(captured_attempts[0]["palette_command"][5]) == 5.0
    manifest = json.loads((work_dir / "gif_clip_long-clip_manifest.json").read_text())
    assert manifest["start_ts"] == 18.0
    assert manifest["end_ts"] == 23.0


def test_staged_export_preserves_guarded_split_window(tmp_path, monkeypatch):
    """A guarded split segment must not be recentered across its boundary."""
    target_clip = {
        "clip_id": "guarded-split", "start_ts": 11.0, "end_ts": 13.0,
        "best_frame_ts": 20.0, "frame_count": 12, "gif_worthiness": 0.8,
        "rank": 1, "guarded_export_window": True,
    }
    captured_attempts = []
    monkeypatch.setattr(
        test_video_adaptive, "_read_upstream_manifest",
        lambda *_args: {"clips": [target_clip]},
    )
    monkeypatch.setattr(
        test_video_adaptive.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="60.0\n"),
    )

    def fake_export_attempt(**kwargs):
        captured_attempts.append(kwargs)
        Path(kwargs["output_path"]).write_bytes(b"GIF89a")
        return SimpleNamespace(success=True, size_bytes=6, error=None)

    monkeypatch.setattr(test_video_adaptive, "run_gif_export_attempt", fake_export_attempt)
    frames_dir = tmp_path / "frames"
    export_dir = tmp_path / "exports"
    work_dir = tmp_path / "work"
    for directory in (frames_dir, export_dir, work_dir):
        directory.mkdir()

    test_video_adaptive._stage_gif_clip(
        video_path=str(tmp_path / "source.mp4"), frames_dir=str(frames_dir),
        export_dir=str(export_dir), work_dir=str(work_dir),
        cfg={"gif_fps": 24, "gif_max_width": 720, "min_duration": 2.0, "max_duration": 5.0},
        clip_id="guarded-split", inputs={"rank_dedup_manifest": [{"path": "ignored"}]},
    )

    assert float(captured_attempts[0]["palette_command"][3]) == 11.0
    assert float(captured_attempts[0]["palette_command"][5]) == 2.0
    manifest = json.loads((work_dir / "gif_clip_guarded-split_manifest.json").read_text())
    assert manifest["start_ts"] == 11.0
    assert manifest["end_ts"] == 13.0


def test_direct_action_export_uses_exact_guarded_window_capped_at_twenty_seconds(
    tmp_path, monkeypatch
):
    """Direct FFmpeg must receive the exact safe action window and hard ceiling."""
    from tests.test_adaptive_direct_action import _action_clip, _materialization
    from tests.test_adaptive_direct_transition import _run_direct_pipeline_fixture

    action_clip = _action_clip(2.0, 22.0)
    monkeypatch.setattr(
        test_video_adaptive,
        "materialize_action_candidates",
        lambda **_kwargs: _materialization((action_clip,)),
        raising=False,
    )
    result = _run_direct_pipeline_fixture(
        tmp_path,
        monkeypatch,
        max_output=1,
        cfg_overrides={"action_guard_enabled": True, "max_duration": 20.0},
        total_duration_s=40.0,
    )

    captured_attempts = result["_fixture_export_attempts"]
    assert float(captured_attempts[0]["palette_command"][3]) == 2.0
    assert float(captured_attempts[0]["palette_command"][5]) == 20.0
    assert result["top_clips"][0]["start_ts"] == 2.0
    assert result["top_clips"][0]["end_ts"] == 22.0
    assert result["top_clips"][0]["duration"] <= 20.0


def test_direct_and_staged_action_splits_match_before_ranking(
    tmp_path, monkeypatch
):
    """Both execution modes consume identical shared materialized children."""
    from tests.test_adaptive_direct_action import _action_clip, _materialization
    from tests.test_adaptive_direct_transition import (
        _cfg,
        _run_direct_pipeline_fixture,
    )

    action_children = (
        _action_clip(2.0, 7.0, split_index=1, split_count=2),
        _action_clip(8.0, 14.0, split_index=2, split_count=2),
    )
    seen_caches = []

    def fake_materialize(**kwargs):
        seen_caches.append(kwargs["evidence_cache"])
        return _materialization(action_children, split=1)

    monkeypatch.setattr(
        test_video_adaptive,
        "materialize_action_candidates",
        fake_materialize,
    )
    direct = _run_direct_pipeline_fixture(
        tmp_path,
        monkeypatch,
        max_output=2,
        cfg_overrides={"action_guard_enabled": True},
    )

    staged_work = tmp_path / "staged-work"
    staged_export = tmp_path / "staged-export"
    staged_work.mkdir()
    staged_export.mkdir()
    synth = {
        "schema_version": 1,
        "stage": "synthesize",
        "clips": [{
            "start_ts": 2.0,
            "end_ts": 14.0,
            "best_frame_ts": 7.0,
            "frame_count": 2,
            "gif_worthiness": 0.9,
        }],
        "scored_frames": [],
    }
    synth_path = staged_work / "synthesize_manifest.json"
    synth_path.write_text(json.dumps(synth), encoding="utf-8")
    cfg = _cfg()
    cfg.update(
        action_guard_enabled=True,
        action_config_hash="fixture-action-hash",
        max_output=2,
    )
    test_video_adaptive._stage_rank_dedup(
        str(tmp_path / "source.mp4"),
        str(staged_export),
        str(staged_work),
        cfg,
        {"synthesize_manifest": [_synthesize_artifact_ref(str(synth_path))]},
        {
            "vlm": {
                "provider": "ollama",
                "model": "fixture",
                "base_url": "http://fixture.invalid",
            }
        },
    )
    staged = json.loads(
        (staged_work / "rank_dedup_manifest.json").read_text(encoding="utf-8")
    )
    assert staged["action_guard"]["action_config_hash"] == (
        test_video_adaptive._freeze_stage_action_config(cfg)[1]
    )

    fields = (
        "start_ts",
        "end_ts",
        "action_split_index",
        "action_split_count",
    )
    direct_windows = sorted(
        tuple(clip[field] for field in fields) for clip in direct["top_clips"]
    )
    staged_windows = sorted(
        tuple(clip[field] for field in fields) for clip in staged["clips"]
    )
    assert staged_windows == direct_windows
    assert len({clip["clip_id"] for clip in staged["clips"]}) == 2
    assert len(seen_caches) == 2
    assert seen_caches[0] is not seen_caches[1]


def test_empty_staged_rank_derives_canonical_action_hash(
    tmp_path, monkeypatch
):
    """Legacy callers without a supplied hash still emit self-validating v2."""
    from app.services.action_config import freeze_action_config
    from app.task_engine.artifacts import validate_manifest_json
    from tests.test_adaptive_direct_transition import _cfg

    cfg = _cfg()
    cfg["max_duration"] = 20.0
    cfg.pop("action_config_hash")
    monkeypatch.setattr(
        test_video_adaptive,
        "_read_upstream_manifest",
        lambda *_args: {"clips": [], "scored_frames": []},
    )
    test_video_adaptive._stage_rank_dedup(
        str(tmp_path / "source.mp4"),
        str(tmp_path / "exports"),
        str(tmp_path),
        cfg,
        {"synthesize_manifest": [_synthesize_artifact_ref("ignored")]},
        None,
    )

    manifest_path = tmp_path / "rank_dedup_manifest.json"
    manifest = _validate_standalone_rank(tmp_path)
    assert manifest["action_guard"]["action_config_hash"] == (
        freeze_action_config(cfg)[1]
    )


@pytest.mark.parametrize(
    "legacy_overrides",
    [
        {"action_guard_enabled": "false"},
        {"action_guard_enabled": False, "action_analysis_version": 2},
    ],
)
def test_nonempty_legacy_staged_rank_uses_normalized_disabled_action_config(
    tmp_path, monkeypatch, legacy_overrides
):
    """Post-processing must use repaired values, never truthy raw strings."""
    from app.services.action_pipeline import ActionMaterialization
    from app.task_engine.artifacts import validate_manifest_json
    from tests.test_adaptive_direct_transition import _cfg

    cfg = _cfg()
    cfg.update(legacy_overrides)
    cfg["embed_dedup_enabled"] = False
    cfg["temporal_dedup_enabled"] = False
    cfg.pop("action_config_hash")
    legacy_clip = {
        "start_ts": 2.0,
        "end_ts": 7.0,
        "best_frame_ts": 4.0,
        "best_frame": {
            "timestamp": 4.0,
            "caption": "legacy clip",
            "emotional_core": "awe",
            "gif_worthiness": 0.8,
        },
        "frame_count": 1,
        "gif_worthiness": 0.8,
        "guarded_export_window": True,
    }
    monkeypatch.setattr(
        test_video_adaptive,
        "_read_upstream_manifest",
        lambda *_args: {
            "clips": [legacy_clip],
            "scored_frames": [],
        },
    )
    monkeypatch.setattr(
        test_video_adaptive.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="20.0\n", stderr=""
        ),
    )
    monkeypatch.setattr(
        test_video_adaptive,
        "materialize_action_candidates",
        lambda **_kwargs: ActionMaterialization(
            clips=(legacy_clip,),
            transition_metrics={
                "input": 1,
                "split": 0,
                "trim": 0,
                "drop": 0,
                "unverified": 0,
                "hard_cut": 0,
                "soft_transition": 0,
                "motion": 0,
            },
            action_metrics={
                "input": 1,
                "output": 1,
                "cv": 0,
                "extended": 0,
                "trimmed": 0,
                "split": 0,
                "ambient_motion": 0,
                "vlm_checked": 0,
                "vlm_succeeded": 0,
                "vlm_failed": 0,
                "fallback": 0,
                "low_loop_quality": 0,
                "cv_ms": 0.0,
                "vlm_ms": 0.0,
                "total_ms": 0.0,
                "fallback_reasons": {},
            },
        ),
    )

    test_video_adaptive._stage_rank_dedup(
        str(tmp_path / "source.mp4"),
        str(tmp_path / "exports"),
        str(tmp_path),
        cfg,
        {"synthesize_manifest": [_synthesize_artifact_ref("ignored")]},
        None,
    )
    manifest = _validate_standalone_rank(tmp_path)
    assert manifest["clips"][0]["action_boundary_mode"] == "disabled"
    assert manifest["clips"][0]["action_analysis_version"] == 1
    assert manifest["action_guard"]["action_analysis_version"] == 1

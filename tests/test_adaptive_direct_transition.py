from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from app.services.transition_guard import GuardSegment, TransitionGuardResult
from scripts import test_video_adaptive


def _cfg() -> dict:
    return {
        "sample_interval": 10, "refine_interval": 10, "refine_radius": 0,
        "refine_threshold": 0.8, "max_duration": 5.0, "min_duration": 2.0,
        "worthiness_threshold": 0.5, "merge_gap": 12,
        "merge_score_threshold": 0.5, "max_merge_span_s": 24,
        "merge_peak_threshold": 0.5, "embed_sim_threshold": 0.94,
        "embed_dedup_enabled": True, "temporal_dedup_enabled": False,
        "temporal_dedup_min_gap_s": 12, "output_ratio": 1.0,
        "max_output": 2, "gif_fps": 12, "gif_max_width": 320,
        "clear_output_dir": False, "potplayer_pbf_enabled": False,
        "preference_memory_enabled": False, "base_score_weight": 0.5,
        "preference_score_weight": 0.5, "vlm_temperature": 0.1,
        "vlm_top_p": 0.9, "vlm_top_k": 20, "min_brightness": 0,
        "transition_guard_enabled": True, "transition_min_duration_s": 2.0,
        "transition_boundary_margin_s": 0.25, "transition_scan_fps": 8,
        "transition_scan_width": 320, "transition_motion_compensation": True,
        "transition_hard_threshold": 0.65, "transition_soft_threshold": 0.4,
        "transition_soft_run_frames": 3, "transition_rescore_split_segments": True,
    }


def test_direct_pipeline_fans_guarded_segments_out_before_dedup(tmp_path, monkeypatch):
    """A split window produces two guarded candidates while honoring max_output.

    Removing direct guard materialization would leave the original one clip,
    so this regression catches both a missing guard call and a guard placed
    after ranking/deduplication.
    """
    frames_dir = tmp_path / "frames"
    export_dir = tmp_path / "exports"
    frames_dir.mkdir()
    export_dir.mkdir()
    source_frame = Image.new("RGB", (32, 32), (100, 110, 120))

    def fake_run(command, **_kwargs):
        if command[0] == "ffprobe":
            return SimpleNamespace(stdout="16.0\n", returncode=0)
        Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
        source_frame.save(command[-1], "JPEG", quality=95)
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(test_video_adaptive.subprocess, "run", fake_run)
    monkeypatch.setattr(test_video_adaptive, "stop_model", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(test_video_adaptive, "wait_model", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(test_video_adaptive, "wait_for_llm", lambda **_kwargs: False)
    monkeypatch.setattr(test_video_adaptive.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(test_video_adaptive, "get_index", lambda: SimpleNamespace(count=0))

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"response": '{"caption":"moment","emotional_core":"awe","gif_worthiness":0.9,"aesthetic_notes":["light"],"reason":"peak"}'}

    monkeypatch.setattr(test_video_adaptive.httpx, "post", lambda *_args, **_kwargs: FakeResponse())
    guard_calls = []

    def fake_guard(video_path, start_s, end_s, anchor_ts_s, config):
        guard_calls.append((video_path, start_s, end_s, anchor_ts_s, config))
        return TransitionGuardResult(
            transition_action="split",
            segments=(GuardSegment(8.0, 10.0), GuardSegment(11.0, 13.0)),
            boundaries=(), hard_cut_count=1, soft_transition_count=0,
            motion_type="coherent_camera_motion", transition_risk=0.8,
            guard_reason="confirmed cut",
        )

    monkeypatch.setattr(test_video_adaptive, "guard_candidate_window", fake_guard)
    rescore_calls = []

    def fake_rescore(**kwargs):
        rescore_calls.append(kwargs)
        return ({
            "timestamp": kwargs["timestamp"], "path": kwargs["frame_path"],
            "caption": "rescored", "emotional_core": "joy",
            "gif_worthiness": 0.8, "aesthetic_notes": [], "reason": "clean",
        }, None)

    monkeypatch.setattr(test_video_adaptive, "_score_vlm_frame", fake_rescore)

    def fake_export_attempt(**kwargs):
        Path(kwargs["output_path"]).write_bytes(b"GIF89a")
        return SimpleNamespace(success=True, size_bytes=6, error=None)

    monkeypatch.setattr(test_video_adaptive, "run_gif_export_attempt", fake_export_attempt)
    monkeypatch.setattr(
        test_video_adaptive,
        "compute_text_embedding",
        lambda text: [1.0, 0.0] if text.startswith("moment") else [0.0, 1.0],
    )
    ranker_inputs = []

    def fake_ranker(clips, _score):
        ranker_inputs.extend(clips)
        return sorted(clips, key=lambda clip: clip["gif_worthiness"], reverse=True)

    monkeypatch.setattr(test_video_adaptive, "rank_clips_for_export", fake_ranker)

    result = test_video_adaptive.run_pipeline(
        str(tmp_path / "source.mp4"), str(frames_dir), str(export_dir), _cfg()
    )

    assert len(guard_calls) == 1
    assert guard_calls[0][1:4] == pytest.approx((8.12, 12.82, 10.0))
    assert len(rescore_calls) == 1
    assert rescore_calls[0]["timestamp"] == 12.0
    assert result["dedup_input_clips"] == 2
    assert result["embedding_deduped_clips"] == 2
    assert len(ranker_inputs) == 2
    assert result["planned_output_count"] == 2
    assert len(result["top_clips"]) == 2
    assert {(clip["start_ts"], clip["end_ts"]) for clip in result["top_clips"]} == {
        (8.0, 10.0), (11.0, 13.0)
    }
    assert {clip["transition_action"] for clip in result["top_clips"]} == {"split"}
    assert result["transition_guard"] == {
        "input": 1, "split": 1, "trim": 0, "drop": 0, "unverified": 0,
        "hard_cut": 1, "soft_transition": 0, "motion": 1,
    }

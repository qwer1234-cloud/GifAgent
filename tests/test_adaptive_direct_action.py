import math

import pytest

from app.services.action_pipeline import ActionMaterialization
from scripts import test_video_adaptive
from tests.test_adaptive_direct_transition import _run_direct_pipeline_fixture


def _action_clip(
    start_ts: float,
    end_ts: float,
    *,
    split_index: int = 1,
    split_count: int = 1,
    mode: str = "cv",
    fallback_reason: str | None = None,
) -> dict:
    midpoint = (start_ts + end_ts) / 2.0
    caption = (
        "first action stage" if split_index == 1 else "second action stage"
    )
    return {
        "start_ts": start_ts,
        "end_ts": end_ts,
        "best_frame": {
            "timestamp": midpoint,
            "path": f"frame-{midpoint}.jpg",
            "caption": caption,
            "emotional_core": "excitement",
            "gif_worthiness": 0.90,
            "aesthetic_notes": ["motion"],
            "reason": "complete action",
        },
        "best_frame_ts": midpoint,
        "best_frame_path": f"frame-{midpoint}.jpg",
        "frame_count": 1,
        "gif_worthiness": 0.90 - (split_index - 1) * 0.01,
        "caption": caption,
        "emotional_core": "excitement",
        "guarded_export_window": True,
        "transition_action": "keep",
        "transition_risk": 0.1,
        "motion_type": "subject_action",
        "guard_reason": "clean segment",
        "action_boundary_mode": mode,
        "action_start_ts": start_ts + 0.5,
        "action_peak_ts": midpoint,
        "action_end_ts": end_ts - 0.5,
        "action_completeness_score": 0.88,
        "action_boundary_confidence": 0.84,
        "loop_quality_score": 0.64,
        "action_split_index": split_index,
        "action_split_count": split_count,
        "action_split_reason": (
            "stable_motion_valley" if split_count > 1 else None
        ),
        "action_vlm_verified": False,
        "action_fallback_reason": fallback_reason,
        "action_analysis_version": 1,
    }


def _materialization(clips: tuple[dict, ...], **metrics) -> ActionMaterialization:
    action_metrics = {
        "input": 1,
        "output": len(clips),
        "cv": 1,
        "extended": 0,
        "trimmed": 0,
        "split": int(len(clips) > 1),
        "ambient_motion": 0,
        "vlm_checked": 0,
        "vlm_succeeded": 0,
        "vlm_failed": 0,
        "fallback": 0,
        "low_loop_quality": 0,
        "cv_ms": 4.0,
        "vlm_ms": 0.0,
        "total_ms": 5.0,
        "fallback_reasons": {},
    }
    action_metrics.update(metrics)
    return ActionMaterialization(
        clips=clips,
        transition_metrics={
            "input": 1,
            "split": 0,
            "trim": 0,
            "drop": 0,
            "unverified": 0,
            "hard_cut": 0,
            "soft_transition": 0,
            "motion": 1,
        },
        action_metrics=action_metrics,
    )


def test_direct_action_split_happens_before_dedup(tmp_path, monkeypatch):
    materialized = _materialization(
        (
            _action_clip(2.0, 7.0, split_index=1, split_count=2),
            _action_clip(8.0, 14.0, split_index=2, split_count=2),
        ),
        extended=1,
        split=1,
    )
    monkeypatch.setattr(
        test_video_adaptive,
        "materialize_action_candidates",
        lambda **_kwargs: materialized,
        raising=False,
    )

    result = _run_direct_pipeline_fixture(
        tmp_path,
        monkeypatch,
        max_output=2,
        cfg_overrides={
            "action_guard_enabled": True,
            "action_vlm_verify_enabled": True,
        },
    )

    assert result["dedup_input_clips"] == 2
    assert len(result["top_clips"]) == 2
    assert result["action_guard"]["split"] == 1
    assert result["action_guard"]["input"] == 1
    assert result["action_guard"]["output"] == 2
    assert all(clip["guarded_export_window"] for clip in result["top_clips"])
    assert {
        (clip["start_ts"], clip["end_ts"]) for clip in result["top_clips"]
    } == {(2.0, 7.0), (8.0, 14.0)}


def test_direct_sequence_verification_uses_frozen_vlm_runtime_once_per_candidate(
    tmp_path, monkeypatch
):
    sequence_responses = []

    def fake_materialize(**kwargs):
        sequence_responses.append(
            kwargs["sequence_generator"](b"contact-sheet", "action prompt")
        )
        return _materialization(
            (_action_clip(2.0, 7.0, mode="hybrid_vlm"),),
            cv=0,
            vlm_checked=1,
            vlm_succeeded=1,
            vlm_ms=3.0,
        )

    monkeypatch.setattr(
        test_video_adaptive,
        "materialize_action_candidates",
        fake_materialize,
        raising=False,
    )
    runtime = test_video_adaptive.VlmRuntimeConfig(
        provider="ollama",
        model="frozen-action-model",
        base_url="http://frozen-vlm.example",
        manage_lifecycle=False,
        launch_mode="none",
        retry_delay_s=0.0,
    )

    result = _run_direct_pipeline_fixture(
        tmp_path,
        monkeypatch,
        max_output=1,
        cfg_overrides={
            "action_guard_enabled": True,
            "action_vlm_verify_enabled": True,
        },
        vlm_runtime=runtime,
    )

    action_calls = [
        call
        for call in result["_fixture_http_calls"]
        if call[1]["json"]["prompt"] == "action prompt"
    ]
    assert len(action_calls) == 1
    assert action_calls[0][0] == "http://frozen-vlm.example/api/generate"
    assert action_calls[0][1]["json"]["model"] == "frozen-action-model"
    assert action_calls[0][1]["json"]["images"]
    assert len(sequence_responses) == 1
    assert result["action_guard"]["vlm_checked"] == 1


def test_direct_fallback_window_stays_inside_transition_segment(
    tmp_path, monkeypatch
):
    fallback = _action_clip(
        7.0,
        15.0,
        mode="fallback_fixed",
        fallback_reason="low_cv_confidence",
    )
    monkeypatch.setattr(
        test_video_adaptive,
        "materialize_action_candidates",
        lambda **_kwargs: _materialization(
            (fallback,),
            cv=0,
            fallback=1,
            fallback_reasons={"low_cv_confidence": 1},
        ),
        raising=False,
    )

    result = _run_direct_pipeline_fixture(
        tmp_path,
        monkeypatch,
        max_output=1,
        cfg_overrides={"action_guard_enabled": True},
    )

    assert result["top_clips"][0]["start_ts"] >= 7.0
    assert result["top_clips"][0]["end_ts"] <= 15.0
    assert result["top_clips"][0]["action_boundary_mode"] == "fallback_fixed"
    assert result["top_clips"][0]["action_fallback_reason"] == "low_cv_confidence"


def test_direct_action_metrics_are_finite_and_include_frozen_hash(
    tmp_path, monkeypatch
):
    materializer_configs = []

    def fake_materialize(**kwargs):
        materializer_configs.append(kwargs["config"])
        return _materialization((_action_clip(2.0, 7.0),))

    monkeypatch.setattr(
        test_video_adaptive,
        "materialize_action_candidates",
        fake_materialize,
        raising=False,
    )

    result = _run_direct_pipeline_fixture(
        tmp_path,
        monkeypatch,
        max_output=1,
        cfg_overrides={
            "action_guard_enabled": True,
            "action_config_hash": "canonical-action-hash",
            "min_duration": 3.0,
            "max_duration": 18.0,
        },
        total_duration_s=30.0,
    )

    assert materializer_configs[0]["action_min_duration_s"] == 3.0
    assert materializer_configs[0]["action_max_duration_s"] == 18.0
    assert result["action_config_hash"] == "canonical-action-hash"
    assert result["action_input_count"] == 1
    assert result["action_output_count"] == 1
    assert all(
        math.isfinite(result["action_guard"][name])
        for name in ("cv_ms", "vlm_ms", "total_ms")
    )
    assert result["top_clips"][0]["action_analysis_version"] == 1
    assert result["gif_exports"][0]["action_boundary_confidence"] == pytest.approx(
        0.84
    )
    assert result["gif_exports"][0]["action_start_ts"] == pytest.approx(2.5)
    assert result["gif_exports"][0]["action_peak_ts"] == pytest.approx(4.5)
    assert result["gif_exports"][0]["action_end_ts"] == pytest.approx(6.5)
    assert result["top_clips"][0]["action_start_ts"] == pytest.approx(2.5)
    assert result["top_clips"][0]["action_peak_ts"] == pytest.approx(4.5)
    assert result["top_clips"][0]["action_end_ts"] == pytest.approx(6.5)

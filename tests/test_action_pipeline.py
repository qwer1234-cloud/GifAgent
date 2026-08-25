"""Shared transition-first action materialization behavior."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
import math

import numpy as np
import pytest

from app.services import action_pipeline
from app.services.action_boundary import (
    ActionBoundaryCandidate,
    ActionMotionAnalysis,
)
from app.services.action_pipeline import materialize_action_candidates
from app.services.temporal_evidence import (
    TemporalEvidence,
    TemporalFrame,
    TemporalMediaError,
    TemporalPairEvidence,
)
from app.services.transition_guard import GuardSegment, TransitionGuardResult


ACTION_PIPELINE_CFG = {
    "action_guard_enabled": True,
    "action_vlm_verify_enabled": True,
    "action_analysis_window_s": 30.0,
    "action_preferred_min_duration_s": 4.0,
    "action_preferred_max_duration_s": 12.0,
    "action_min_duration_s": 2.0,
    "action_max_duration_s": 20.0,
    "action_scan_fps": 4.0,
    "action_boundary_confidence_threshold": 0.65,
    "action_loop_adjust_s": 0.75,
    "action_vlm_min_worthiness": 0.60,
    "action_fallback_mode": "fixed_window",
    "transition_rescore_split_segments": True,
}


class FixedEvidenceCache:
    def __init__(self, evidence: TemporalEvidence):
        self.evidence = evidence
        self.calls: list[tuple[str, float, float, object]] = []

    def scan(self, video_path, start_s, end_s, config):
        self.calls.append((str(video_path), start_s, end_s, config))
        return self.evidence.slice(start_s, end_s)


class FailingEvidenceCache:
    def __init__(self):
        self.calls = 0

    def scan(self, video_path, start_s, end_s, config):
        self.calls += 1
        raise TemporalMediaError("unreadable source")


def make_flat_evidence(start_s: float, end_s: float) -> TemporalEvidence:
    fps = 4.0
    indexes = range(round(start_s * fps), round(end_s * fps) + 1)
    gray = np.full((8, 8), 96, dtype=np.uint8)
    hsv = np.zeros((8, 8, 3), dtype=np.uint8)
    hsv[..., 2] = 96
    frames = tuple(
        TemporalFrame(index, index / fps, gray.copy(), hsv.copy())
        for index in indexes
    )
    pairs = tuple(
        TemporalPairEvidence(
            frame.timestamp_s,
            previous.gray,
            frame.gray,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            1.0,
            np.zeros_like(gray),
        )
        for previous, frame in zip(frames, frames[1:])
    )
    return TemporalEvidence(start_s, end_s, fps, 8, frames, pairs)


def _guard(
    *segments: tuple[float, float],
    action: str = "keep",
    motion_type: str = "static_or_local_motion",
) -> TransitionGuardResult:
    safe_segments = tuple(GuardSegment(start, end, "clean") for start, end in segments)
    return TransitionGuardResult(
        transition_action=action,
        segments=safe_segments,
        boundaries=(),
        hard_cut_count=0,
        soft_transition_count=0,
        motion_type=motion_type,
        transition_risk=0.0,
        guard_reason="clean",
        original_start_s=safe_segments[0].start_s if safe_segments else 0.0,
        original_end_s=safe_segments[-1].end_s if safe_segments else 0.0,
        anchor_segment=safe_segments[0] if safe_segments else None,
    )


def _candidate(start_s: float, peak_s: float, end_s: float, confidence: float = 0.9):
    return ActionBoundaryCandidate(
        start_s=start_s,
        peak_s=peak_s,
        end_s=end_s,
        confidence=confidence,
        start_settle=1.0,
        end_settle=1.0,
        peak_inclusion=1.0,
        boundary_quiet=1.0,
    )


def _analysis(
    start_s: float,
    peak_s: float,
    end_s: float,
    confidence: float = 0.9,
) -> ActionMotionAnalysis:
    candidate = _candidate(start_s, peak_s, end_s, confidence)
    return ActionMotionAnalysis(
        motion_type="subject_action",
        candidates=(candidate,),
        residual_curve=((start_s, 0.0), (peak_s, 0.2), (end_s, 0.0)),
        active_runs=((start_s, end_s),),
        stable_valleys=(),
        confidence=confidence,
    )


def _analysis_with_candidates(
    candidates: tuple[ActionBoundaryCandidate, ...],
    confidence: float = 0.4,
) -> ActionMotionAnalysis:
    return ActionMotionAnalysis(
        motion_type="subject_action",
        candidates=candidates,
        residual_curve=((10.0, 0.0), (13.0, 0.2), (16.0, 0.0)),
        active_runs=((11.0, 15.0),),
        stable_valleys=(),
        confidence=confidence,
    )


def _vlm_response(index: int, confidence: float = 0.8) -> str:
    return json.dumps(
        {
            "selected_candidate_index": index,
            "action_label": "movement",
            "first_phase": "preparation",
            "anchor_phase": "ongoing",
            "last_phase": "complete",
            "complete": True,
            "confidence": confidence,
            "reason": "complete",
        }
    )


def _clip(start_s: float = 10.0, end_s: float = 16.0, anchor_s: float = 13.0):
    return {
        "start_ts": start_s,
        "end_ts": end_s,
        "best_frame_ts": anchor_s,
        "frame_count": 1,
        "gif_worthiness": 0.9,
    }


def _frame(timestamp_s: float, worthiness: float = 0.9):
    return {
        "timestamp": timestamp_s,
        "path": f"frame-{timestamp_s}.jpg",
        "gif_worthiness": worthiness,
    }


def _run(
    *,
    clip=None,
    scored_frames=None,
    evidence=None,
    total_duration_s: float = 60.0,
    config=None,
    frame_scorer=lambda timestamp_s, label: None,
    sequence_generator=lambda image_bytes, prompt: "",
):
    clip = clip or _clip()
    evidence = evidence or make_flat_evidence(0.0, total_duration_s)
    return materialize_action_candidates(
        video_path="source.mp4",
        clip=clip,
        scored_frames=scored_frames or [_frame(float(clip["best_frame_ts"]))],
        total_duration_s=total_duration_s,
        config=config or ACTION_PIPELINE_CFG,
        evidence_cache=FixedEvidenceCache(evidence),
        frame_scorer=frame_scorer,
        sequence_generator=sequence_generator,
    )


def test_materializer_runs_transition_before_action(monkeypatch):
    events = []
    evidence = make_flat_evidence(0.0, 30.0)
    clip = _clip()
    scored_frames = [_frame(13.0)]

    def fake_guard(
        video_path, start_s, end_s, anchor_ts_s, config, temporal_evidence=None
    ):
        events.append("transition")
        return _guard((10.0, 16.0))

    def fake_action(evidence, safe_start_s, safe_end_s, anchor_ts_s, config):
        events.append("action")
        return _analysis(11.0, 13.0, 15.0)

    monkeypatch.setattr(action_pipeline, "guard_candidate_window", fake_guard)
    monkeypatch.setattr(action_pipeline, "analyze_action_motion", fake_action)

    result = materialize_action_candidates(
        video_path="source.mp4",
        clip=clip,
        scored_frames=scored_frames,
        total_duration_s=60.0,
        config=ACTION_PIPELINE_CFG,
        evidence_cache=FixedEvidenceCache(evidence),
        frame_scorer=lambda timestamp_s, label: {
            "timestamp": timestamp_s,
            "path": f"{label}.jpg",
            "gif_worthiness": 0.8,
        },
        sequence_generator=lambda image_bytes, prompt: json.dumps(
            {
                "selected_candidate_index": 0,
                "action_label": "movement",
                "first_phase": "preparation",
                "anchor_phase": "ongoing",
                "last_phase": "complete",
                "complete": True,
                "confidence": 0.8,
                "reason": "complete",
            }
        ),
    )

    assert events == ["transition", "action"]
    assert result.clips
    assert all(candidate["guarded_export_window"] for candidate in result.clips)
    assert result.clips[0]["action_boundary_mode"] == "cv"


def test_unverified_action_uses_transition_clamped_fixed_window(monkeypatch):
    monkeypatch.setattr(
        action_pipeline,
        "guard_candidate_window",
        lambda *args, **kwargs: _guard((10.0, 16.0)),
    )
    monkeypatch.setattr(
        action_pipeline,
        "analyze_action_motion",
        lambda *args, **kwargs: ActionMotionAnalysis(
            motion_type="unknown",
            candidates=(),
            residual_curve=(),
            active_runs=(),
            stable_valleys=(),
            confidence=0.0,
            analysis_error="unverified",
        ),
    )

    result = _run(
        clip=_clip(anchor_s=15.0),
        scored_frames=[_frame(15.0)],
    )

    assert result.clips[0]["action_boundary_mode"] == "fallback_fixed"
    assert 10.0 <= result.clips[0]["start_ts"]
    assert result.clips[0]["end_ts"] <= 16.0
    assert result.action_metrics["fallback"] == 1
    assert result.action_metrics["fallback_reasons"] == {"unverified": 1}


def test_valid_thirty_second_config_keeps_one_hard_capped_fallback(monkeypatch):
    monkeypatch.setattr(
        action_pipeline,
        "guard_candidate_window",
        lambda *args, **kwargs: _guard((3.0, 33.0)),
    )
    monkeypatch.setattr(
        action_pipeline,
        "analyze_action_motion",
        lambda *args, **kwargs: ActionMotionAnalysis(
            "unknown", (), (), (), (), 0.0, "unverified"
        ),
    )

    result = _run(
        clip=_clip(anchor_s=15.0),
        scored_frames=[_frame(15.0)],
        config={
            **ACTION_PIPELINE_CFG,
            "action_analysis_window_s": 30.0,
            "action_preferred_max_duration_s": 30.0,
            "action_max_duration_s": 30.0,
        },
    )

    assert len(result.clips) == 1
    assert result.clips[0]["start_ts"] == pytest.approx(7.0)
    assert result.clips[0]["end_ts"] == pytest.approx(27.0)
    assert result.clips[0]["end_ts"] - result.clips[0]["start_ts"] == 20.0
    assert result.clips[0]["action_boundary_mode"] == "fallback_fixed"
    assert result.action_metrics["fallback"] == 1
    assert result.action_metrics["output"] == 1


@pytest.mark.parametrize(
    ("anchor_s", "expected_start_s", "expected_end_s"),
    ((5.0, 0.0, 30.0), (50.0, 38.0, 68.0), (95.0, 70.0, 100.0)),
)
def test_analysis_scan_is_one_thirty_second_window_with_40_60_bias(
    monkeypatch, anchor_s, expected_start_s, expected_end_s
):
    cache = FixedEvidenceCache(make_flat_evidence(0.0, 100.0))
    monkeypatch.setattr(
        action_pipeline,
        "guard_candidate_window",
        lambda *args, **kwargs: _guard((expected_start_s, expected_end_s)),
    )
    monkeypatch.setattr(
        action_pipeline,
        "analyze_action_motion",
        lambda *args, **kwargs: ActionMotionAnalysis(
            "ambient_camera_motion", (), (), (), (), 0.9
        ),
    )

    materialize_action_candidates(
        video_path="source.mp4",
        clip=_clip(
            start_s=max(0.0, anchor_s - 3.0),
            end_s=min(100.0, anchor_s + 3.0),
            anchor_s=anchor_s,
        ),
        scored_frames=[_frame(anchor_s)],
        total_duration_s=100.0,
        config=ACTION_PIPELINE_CFG,
        evidence_cache=cache,
        frame_scorer=lambda timestamp_s, label: _frame(timestamp_s),
        sequence_generator=lambda image_bytes, prompt: "",
    )

    assert len(cache.calls) == 1
    assert cache.calls[0][1:3] == (expected_start_s, expected_end_s)


def test_low_confidence_action_uses_at_most_one_vlm_call(monkeypatch):
    monkeypatch.setattr(
        action_pipeline,
        "guard_candidate_window",
        lambda *args, **kwargs: _guard((10.0, 16.0)),
    )
    monkeypatch.setattr(
        action_pipeline,
        "analyze_action_motion",
        lambda *args, **kwargs: _analysis(11.0, 13.0, 15.0, confidence=0.4),
    )
    calls = 0

    def sequence_generator(image_bytes, prompt):
        nonlocal calls
        calls += 1
        return json.dumps(
            {
                "selected_candidate_index": 0,
                "action_label": "movement",
                "first_phase": "preparation",
                "anchor_phase": "ongoing",
                "last_phase": "complete",
                "complete": True,
                "confidence": 0.8,
                "reason": "complete",
            }
        )

    result = _run(sequence_generator=sequence_generator)

    assert calls == 1
    assert result.clips[0]["action_vlm_verified"] is True
    assert result.clips[0]["action_boundary_mode"] == "hybrid_vlm"
    assert result.action_metrics["vlm_checked"] == 1
    assert result.action_metrics["vlm_succeeded"] == 1
    assert result.action_metrics["vlm_failed"] == 0


def test_near_agreeing_vlm_and_cv_candidates_use_transition_safe_wider_envelope(
    monkeypatch,
):
    monkeypatch.setattr(
        action_pipeline,
        "guard_candidate_window",
        lambda *args, **kwargs: _guard((10.0, 16.0)),
    )
    monkeypatch.setattr(
        action_pipeline,
        "analyze_action_motion",
        lambda *args, **kwargs: _analysis_with_candidates(
            (
                _candidate(11.0, 13.0, 14.5, confidence=0.4),
                _candidate(11.5, 13.0, 15.0, confidence=0.4),
            )
        ),
    )

    result = _run(
        sequence_generator=lambda image_bytes, prompt: _vlm_response(
            1, confidence=0.5
        )
    )

    assert result.clips[0]["action_boundary_mode"] == "hybrid_vlm"
    assert result.clips[0]["action_start_ts"] == 11.0
    assert result.clips[0]["action_end_ts"] == 15.0


def test_low_confidence_vlm_disagreement_uses_fixed_fallback(monkeypatch):
    monkeypatch.setattr(
        action_pipeline,
        "guard_candidate_window",
        lambda *args, **kwargs: _guard((10.0, 16.0)),
    )
    monkeypatch.setattr(
        action_pipeline,
        "analyze_action_motion",
        lambda *args, **kwargs: _analysis_with_candidates(
            (
                _candidate(11.0, 13.0, 14.0, confidence=0.4),
                _candidate(10.2, 14.5, 15.8, confidence=0.4),
            )
        ),
    )

    result = _run(
        sequence_generator=lambda image_bytes, prompt: _vlm_response(
            1, confidence=0.5
        )
    )

    assert result.clips[0]["action_boundary_mode"] == "fallback_fixed"
    assert result.clips[0]["action_fallback_reason"] == (
        "vlm_low_confidence_disagreement"
    )


def test_high_confidence_vlm_disagreement_uses_selected_candidate(monkeypatch):
    monkeypatch.setattr(
        action_pipeline,
        "guard_candidate_window",
        lambda *args, **kwargs: _guard((10.0, 16.0)),
    )
    monkeypatch.setattr(
        action_pipeline,
        "analyze_action_motion",
        lambda *args, **kwargs: _analysis_with_candidates(
            (
                _candidate(11.0, 13.0, 14.0, confidence=0.4),
                _candidate(10.2, 14.5, 15.8, confidence=0.4),
            )
        ),
    )

    result = _run(
        sequence_generator=lambda image_bytes, prompt: _vlm_response(
            1, confidence=0.8
        )
    )

    assert result.clips[0]["action_boundary_mode"] == "hybrid_vlm"
    assert result.clips[0]["action_start_ts"] == 10.2
    assert result.clips[0]["action_peak_ts"] == 14.5
    assert result.clips[0]["action_end_ts"] == 15.8


def test_unselected_fallback_passes_none_to_finalizer(monkeypatch):
    monkeypatch.setattr(
        action_pipeline,
        "guard_candidate_window",
        lambda *args, **kwargs: _guard((10.0, 16.0)),
    )
    monkeypatch.setattr(
        action_pipeline,
        "analyze_action_motion",
        lambda *args, **kwargs: ActionMotionAnalysis(
            "unknown", (), (), (), (), 0.0, "unverified"
        ),
    )
    selected_indexes = []
    real_finalizer = action_pipeline.finalize_action_analysis

    def finalizer(
        analysis,
        evidence,
        safe_start_s,
        safe_end_s,
        anchor_ts_s,
        selected_candidate_index,
        config,
    ):
        selected_indexes.append(selected_candidate_index)
        return real_finalizer(
            analysis,
            evidence,
            safe_start_s,
            safe_end_s,
            anchor_ts_s,
            selected_candidate_index,
            config,
        )

    monkeypatch.setattr(action_pipeline, "finalize_action_analysis", finalizer)

    result = _run()

    assert selected_indexes == [None]
    assert result.clips[0]["action_fallback_reason"] == "unverified"
    assert result.clips[0]["diagnostics"]["selected_candidate_index"] is None


def test_multiple_low_confidence_segments_share_one_vlm_budget(monkeypatch):
    monkeypatch.setattr(
        action_pipeline,
        "guard_candidate_window",
        lambda *args, **kwargs: _guard(
            (10.0, 13.0), (13.5, 16.5), action="split"
        ),
    )
    monkeypatch.setattr(
        action_pipeline,
        "analyze_action_motion",
        lambda evidence, safe_start_s, safe_end_s, anchor_ts_s, config: _analysis(
            safe_start_s + 0.2,
            anchor_ts_s,
            safe_end_s - 0.2,
            confidence=0.4,
        ),
    )
    calls = 0

    def sequence_generator(image_bytes, prompt):
        nonlocal calls
        calls += 1
        return "{}"

    result = _run(
        clip=_clip(end_s=16.5),
        scored_frames=[_frame(11.0), _frame(15.0)],
        sequence_generator=sequence_generator,
    )

    assert calls == 1
    assert result.action_metrics["vlm_checked"] == 1
    assert result.action_metrics["vlm_failed"] == 1
    assert result.action_metrics["fallback"] == 2


def test_ambient_camera_motion_skips_vlm(monkeypatch):
    monkeypatch.setattr(
        action_pipeline,
        "guard_candidate_window",
        lambda *args, **kwargs: _guard((10.0, 16.0)),
    )
    monkeypatch.setattr(
        action_pipeline,
        "analyze_action_motion",
        lambda *args, **kwargs: ActionMotionAnalysis(
            "ambient_camera_motion", (), (), (), (), 0.9
        ),
    )

    result = _run(
        sequence_generator=lambda image_bytes, prompt: pytest.fail(
            "ambient motion must not reach the VLM"
        )
    )

    assert result.action_metrics["ambient_motion"] == 1
    assert result.action_metrics["vlm_checked"] == 0
    assert result.clips[0]["action_boundary_mode"] == "ambient_camera_motion"
    assert result.action_metrics["fallback_reasons"] == {
        "ambient_camera_motion": 1
    }


def test_action_disabled_preserves_transition_fanout_and_rescores_only_missing_child(
    monkeypatch,
):
    monkeypatch.setattr(
        action_pipeline,
        "guard_candidate_window",
        lambda *args, **kwargs: _guard(
            (10.0, 13.0), (13.5, 16.0), action="split"
        ),
    )
    monkeypatch.setattr(
        action_pipeline,
        "analyze_action_motion",
        lambda *args, **kwargs: pytest.fail("disabled action analysis must not run"),
    )
    scores: list[tuple[float, str]] = []

    def frame_scorer(timestamp_s, label):
        scores.append((timestamp_s, label))
        return _frame(timestamp_s, 0.8)

    result = _run(
        scored_frames=[_frame(11.0)],
        config={**ACTION_PIPELINE_CFG, "action_guard_enabled": False},
        frame_scorer=frame_scorer,
        sequence_generator=lambda *args: pytest.fail("disabled action VLM must not run"),
    )

    assert len(result.clips) == 2
    assert len(scores) == 1
    assert scores[0][0] == pytest.approx(14.75)
    assert all(candidate["guarded_export_window"] for candidate in result.clips)
    assert all("action_boundary_mode" not in candidate for candidate in result.clips)
    assert result.transition_metrics["split"] == 1
    assert result.action_metrics["cv"] == 0


def test_action_disabled_guards_the_legacy_biased_export_window(monkeypatch):
    cache = FixedEvidenceCache(make_flat_evidence(0.0, 30.0))
    guard_calls: list[tuple[float, float, float]] = []

    def guard(video_path, start_s, end_s, anchor_ts_s, config, **kwargs):
        guard_calls.append((start_s, end_s, anchor_ts_s))
        return _guard((start_s, end_s))

    monkeypatch.setattr(action_pipeline, "guard_candidate_window", guard)

    result = materialize_action_candidates(
        video_path="source.mp4",
        clip={
            "start_ts": 13.0,
            "end_ts": 13.0,
            "best_frame_ts": 13.0,
            "frame_count": 1,
            "gif_worthiness": 0.5,
        },
        scored_frames=[_frame(13.0, 0.5)],
        total_duration_s=60.0,
        config={
            **ACTION_PIPELINE_CFG,
            "action_guard_enabled": False,
            "min_duration": 2.0,
            "max_duration": 10.0,
        },
        evidence_cache=cache,
        frame_scorer=lambda timestamp_s, label: None,
        sequence_generator=lambda image_bytes, prompt: "",
    )

    assert cache.calls[0][1:3] == pytest.approx((10.6, 16.6))
    assert guard_calls == [pytest.approx((10.6, 16.6, 13.0))]
    assert result.clips[0]["start_ts"] == pytest.approx(10.6)
    assert result.clips[0]["end_ts"] == pytest.approx(16.6)


def test_action_disabled_uses_legacy_export_minimum_for_transition_children(
    monkeypatch,
):
    monkeypatch.setattr(
        action_pipeline,
        "guard_candidate_window",
        lambda *args, **kwargs: _guard((10.0, 11.5)),
    )

    result = _run(
        scored_frames=[_frame(10.5)],
        config={
            **ACTION_PIPELINE_CFG,
            "action_guard_enabled": False,
            "action_min_duration_s": 2.0,
            "min_duration": 1.0,
            "max_duration": 10.0,
        },
    )

    assert len(result.clips) == 1
    assert result.clips[0]["start_ts"] == 10.0
    assert result.clips[0]["end_ts"] == 11.5


def test_action_disabled_media_fallback_remains_transition_only(monkeypatch):
    cache = FailingEvidenceCache()
    monkeypatch.setattr(
        action_pipeline,
        "guard_candidate_window",
        lambda *args, **kwargs: pytest.fail(
            "failed shared scan must not invoke a second guard scan"
        ),
    )

    result = materialize_action_candidates(
        video_path="unreadable.mp4",
        clip=_clip(),
        scored_frames=[_frame(13.0)],
        total_duration_s=60.0,
        config={
            **ACTION_PIPELINE_CFG,
            "action_guard_enabled": False,
            "transition_guard_enabled": False,
        },
        evidence_cache=cache,
        frame_scorer=lambda timestamp_s, label: None,
        sequence_generator=lambda image_bytes, prompt: "",
    )

    assert len(result.clips) == 1
    assert cache.calls == 1
    assert "action_boundary_mode" not in result.clips[0]
    assert result.action_metrics["fallback"] == 0


def test_unreadable_media_raises_without_retrying_guard_or_cache(
    monkeypatch,
):
    cache = FailingEvidenceCache()
    monkeypatch.setattr(
        action_pipeline,
        "guard_candidate_window",
        lambda *args, **kwargs: pytest.fail(
            "failed shared scan must not invoke a second guard scan"
        ),
    )

    with pytest.raises(TemporalMediaError, match="unreadable source"):
        materialize_action_candidates(
            video_path="unreadable.mp4",
            clip=_clip(),
            scored_frames=[_frame(13.0)],
            total_duration_s=60.0,
            config=ACTION_PIPELINE_CFG,
            evidence_cache=cache,
            frame_scorer=lambda timestamp_s, label: None,
            sequence_generator=lambda image_bytes, prompt: "",
        )
    assert cache.calls == 1


def test_media_fallback_is_fixed_40_60_and_never_exceeds_twenty_seconds(
    monkeypatch,
):
    cache = FailingEvidenceCache()
    monkeypatch.setattr(
        action_pipeline,
        "guard_candidate_window",
        lambda *args, **kwargs: pytest.fail(
            "disabled transition guard needs no media retry"
        ),
    )

    result = materialize_action_candidates(
        video_path="unreadable.mp4",
        clip=_clip(anchor_s=15.0),
        scored_frames=[_frame(15.0)],
        total_duration_s=60.0,
        config={
            **ACTION_PIPELINE_CFG,
            "transition_guard_enabled": False,
            "action_preferred_max_duration_s": 20.0,
            "action_max_duration_s": 20.0,
        },
        evidence_cache=cache,
        frame_scorer=lambda timestamp_s, label: None,
        sequence_generator=lambda image_bytes, prompt: "",
    )

    assert cache.calls == 1
    assert len(result.clips) == 1
    assert result.clips[0]["start_ts"] == pytest.approx(7.0)
    assert result.clips[0]["end_ts"] == pytest.approx(27.0)
    assert result.clips[0]["end_ts"] - result.clips[0]["start_ts"] == 20.0
    assert result.clips[0]["action_boundary_mode"] == "fallback_fixed"
    assert result.clips[0]["guarded_export_window"] is True
    assert result.clips[0]["transition_action"] == "keep"
    assert result.clips[0]["motion_type"] == "disabled"
    assert result.action_metrics["fallback"] == 1


def test_transition_drop_retains_original_window(monkeypatch):
    monkeypatch.setattr(
        action_pipeline,
        "guard_candidate_window",
        lambda *args, **kwargs: _guard((10.0, 16.0), action="drop"),
    )
    monkeypatch.setattr(
        action_pipeline,
        "analyze_action_motion",
        lambda *args, **kwargs: _analysis(11.0, 13.0, 15.0),
    )

    result = _run()

    assert len(result.clips) == 1
    assert result.clips[0]["end_ts"] - result.clips[0]["start_ts"] >= 2.0
    assert result.transition_metrics["drop"] == 1
    assert result.action_metrics["output"] == 1


def test_child_rescore_failure_discards_only_that_child(monkeypatch):
    monkeypatch.setattr(
        action_pipeline,
        "guard_candidate_window",
        lambda *args, **kwargs: _guard(
            (10.0, 13.0), (13.5, 16.5), action="split"
        ),
    )
    monkeypatch.setattr(
        action_pipeline,
        "analyze_action_motion",
        lambda evidence, safe_start_s, safe_end_s, anchor_ts_s, config: _analysis(
            safe_start_s + 0.2,
            anchor_ts_s,
            safe_end_s - 0.2,
        ),
    )
    rescored: list[float] = []

    def frame_scorer(timestamp_s, label):
        rescored.append(timestamp_s)
        return None

    result = _run(
        clip=_clip(end_s=16.5),
        scored_frames=[_frame(11.0)],
        frame_scorer=frame_scorer,
    )

    assert len(rescored) == 1
    assert len(result.clips) == 1
    assert result.clips[0]["best_frame_ts"] == 11.0


def test_per_segment_action_failure_falls_back_without_losing_siblings(monkeypatch):
    monkeypatch.setattr(
        action_pipeline,
        "guard_candidate_window",
        lambda *args, **kwargs: _guard(
            (10.0, 13.0), (13.5, 16.5), action="split"
        ),
    )

    def analyze(evidence, safe_start_s, safe_end_s, anchor_ts_s, config):
        if safe_start_s >= 13.5:
            raise RuntimeError("cv exploded")
        return _analysis(10.2, 11.0, 12.8)

    monkeypatch.setattr(action_pipeline, "analyze_action_motion", analyze)

    result = _run(
        clip=_clip(end_s=16.5),
        scored_frames=[_frame(11.0), _frame(15.0)],
    )

    assert len(result.clips) == 2
    assert {candidate["action_boundary_mode"] for candidate in result.clips} == {
        "cv",
        "fallback_fixed",
    }
    assert result.action_metrics["fallback"] == 1
    assert result.action_metrics["fallback_reasons"] == {"cv exploded": 1}


def test_metrics_are_complete_finite_serializable_and_result_is_frozen(monkeypatch):
    monkeypatch.setattr(
        action_pipeline,
        "guard_candidate_window",
        lambda *args, **kwargs: _guard((10.0, 16.0)),
    )
    monkeypatch.setattr(
        action_pipeline,
        "analyze_action_motion",
        lambda *args, **kwargs: _analysis(11.0, 13.0, 15.0),
    )

    result = _run()

    assert set(result.transition_metrics) == {
        "input",
        "split",
        "trim",
        "drop",
        "unverified",
        "hard_cut",
        "soft_transition",
        "motion",
    }
    assert set(result.action_metrics) == {
        "input",
        "output",
        "cv",
        "extended",
        "trimmed",
        "split",
        "ambient_motion",
        "vlm_checked",
        "vlm_succeeded",
        "vlm_failed",
        "fallback",
        "low_loop_quality",
        "cv_ms",
        "vlm_ms",
        "total_ms",
        "fallback_reasons",
    }
    assert isinstance(result.clips, tuple)
    assert result.clips[0]["action_split_index"] == 1
    assert result.clips[0]["action_split_count"] == 1
    assert all(
        math.isfinite(result.action_metrics[key])
        for key in ("cv_ms", "vlm_ms", "total_ms")
    )
    json.dumps(result.transition_metrics)
    json.dumps(result.action_metrics)
    with pytest.raises(FrozenInstanceError):
        result.clips = ()

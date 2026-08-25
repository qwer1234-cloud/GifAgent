"""Transition-first orchestration for action-complete export candidates."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, replace
import math
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Mapping

from app.services.action_boundary import (
    ActionBoundaryCandidate,
    ActionBoundaryConfig,
    ActionBoundaryResult,
    ActionMotionAnalysis,
    ActionSegment,
    analyze_action_motion,
    finalize_action_analysis,
)
from app.services.action_candidates import build_action_clips
from app.services.action_vlm import ActionVlmDecision, verify_action_candidates
from app.services.gif_windows import build_export_window
from app.services.temporal_evidence import (
    TemporalEvidenceCache,
    TemporalMediaError,
    TemporalScanConfig,
)
from app.services.transition_candidates import build_guarded_clips
from app.services.transition_guard import (
    GuardSegment,
    TransitionGuardConfig,
    TransitionGuardResult,
    guard_candidate_window,
)


FrameScorer = Callable[[float, str], dict[str, Any] | None]
SequenceGenerator = Callable[[bytes, str], str]


@dataclass(frozen=True)
class ActionMaterialization:
    clips: tuple[dict[str, Any], ...]
    transition_metrics: dict[str, int]
    action_metrics: dict[str, int | float | dict[str, int]]


_TRANSITION_KEYS = (
    "input",
    "split",
    "trim",
    "drop",
    "unverified",
    "hard_cut",
    "soft_transition",
    "motion",
)
_ACTION_COUNTER_KEYS = (
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
)


def _finite(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if math.isfinite(parsed) else fallback


def _clip_window(
    clip: Mapping[str, Any], total_duration_s: float
) -> tuple[float, float, float]:
    start_s = min(total_duration_s, max(0.0, _finite(clip.get("start_ts"), 0.0)))
    end_s = min(
        total_duration_s,
        max(start_s, _finite(clip.get("end_ts"), start_s)),
    )
    midpoint = (start_s + end_s) / 2.0
    best_frame = clip.get("best_frame")
    best_timestamp = (
        best_frame.get("timestamp")
        if isinstance(best_frame, Mapping)
        else clip.get("best_frame_ts")
    )
    anchor_s = min(end_s, max(start_s, _finite(best_timestamp, midpoint)))
    return start_s, end_s, anchor_s


def _analysis_window(
    anchor_ts_s: float, total_duration_s: float, config: ActionBoundaryConfig
) -> tuple[float, float]:
    duration = min(config.analysis_window_s, total_duration_s)
    start_s = max(0.0, anchor_ts_s - duration * 0.4)
    start_s = min(start_s, total_duration_s - duration)
    return start_s, start_s + duration


def _transition_metrics(result: TransitionGuardResult) -> dict[str, int]:
    metrics = {key: 0 for key in _TRANSITION_KEYS}
    metrics["input"] = 1
    if result.transition_action in {"split", "trim", "drop", "unverified"}:
        metrics[result.transition_action] = 1
    metrics["hard_cut"] = int(result.hard_cut_count)
    metrics["soft_transition"] = int(result.soft_transition_count)
    if result.motion_type == "coherent_camera_motion":
        metrics["motion"] = 1
    return metrics


def _empty_action_metrics() -> dict[str, int | float | dict[str, int]]:
    metrics: dict[str, int | float | dict[str, int]] = {
        key: 0 for key in _ACTION_COUNTER_KEYS
    }
    metrics.update(
        cv_ms=0.0,
        vlm_ms=0.0,
        total_ms=0.0,
        fallback_reasons={},
    )
    metrics["input"] = 1
    return metrics


def _config_mapping(
    config: Mapping[str, Any] | ActionBoundaryConfig | None,
) -> Mapping[str, Any]:
    return asdict(config) if isinstance(config, ActionBoundaryConfig) else (config or {})


def _segment_anchor(
    segment_start_s: float,
    segment_end_s: float,
    original_anchor_s: float,
    scored_frames: list[dict[str, Any]],
) -> float:
    scored: list[tuple[float, float]] = []
    for frame in scored_frames:
        timestamp = _finite(frame.get("timestamp"), math.nan)
        worthiness = _finite(frame.get("gif_worthiness"), math.nan)
        if (
            math.isfinite(timestamp)
            and math.isfinite(worthiness)
            and segment_start_s <= timestamp <= segment_end_s
        ):
            scored.append((worthiness, timestamp))
    if scored:
        return max(scored)[1]
    if segment_start_s <= original_anchor_s <= segment_end_s:
        return original_anchor_s
    return (segment_start_s + segment_end_s) / 2.0


def _fallback_result(
    *,
    analysis: ActionMotionAnalysis,
    safe_start_s: float,
    safe_end_s: float,
    anchor_ts_s: float,
    config: ActionBoundaryConfig,
    reason: str,
) -> ActionBoundaryResult:
    duration = min(
        20.0,
        config.preferred_max_duration_s,
        safe_end_s - safe_start_s,
    )
    start_s = anchor_ts_s - duration * 0.4
    end_s = anchor_ts_s + duration * 0.6
    if start_s < safe_start_s:
        end_s += safe_start_s - start_s
        start_s = safe_start_s
    if end_s > safe_end_s:
        start_s -= end_s - safe_end_s
        end_s = safe_end_s
    start_s = max(safe_start_s, start_s)
    end_s = min(safe_end_s, end_s)
    confidence = _finite(analysis.confidence, 0.0)
    return ActionBoundaryResult(
        action_boundary_mode="fallback_fixed",
        safe_start_s=safe_start_s,
        safe_end_s=safe_end_s,
        anchor_ts_s=anchor_ts_s,
        boundary_candidates=analysis.candidates,
        segments=(
            ActionSegment(
                start_s,
                end_s,
                anchor_ts_s,
                "fallback_fixed",
                False,
            ),
        ),
        action_start_ts=None,
        action_peak_ts=None,
        action_end_ts=None,
        action_completeness_score=None,
        action_boundary_confidence=confidence,
        loop_quality_score=None,
        action_split_reason=None,
        action_vlm_verified=False,
        action_fallback_reason=reason,
        action_analysis_version=config.analysis_version,
        diagnostics={"selected_candidate_index": None, "motion_type": analysis.motion_type},
        analysis_error=analysis.analysis_error,
    )


def _safe_segments(
    guard_result: TransitionGuardResult,
    scan_start_s: float,
    scan_end_s: float,
) -> tuple[GuardSegment, ...]:
    """Use guard segments, or the original scan window when the guard dropped."""
    if guard_result.transition_action != "drop":
        return guard_result.segments
    start = guard_result.original_start_s
    end = guard_result.original_end_s
    if (
        isinstance(start, (int, float))
        and isinstance(end, (int, float))
        and math.isfinite(float(start))
        and math.isfinite(float(end))
        and float(end) > float(start)
    ):
        return (GuardSegment(float(start), float(end), "original_window"),)
    if scan_end_s > scan_start_s:
        return (GuardSegment(scan_start_s, scan_end_s, "original_window"),)
    return ()


def _hard_cut_timestamps(guard_result: TransitionGuardResult) -> list[float]:
    return [
        float(boundary.timestamp_s)
        for boundary in guard_result.boundaries
        if boundary.boundary_type == "hard_cut"
    ]


def _transition_clip(
    clip: Mapping[str, Any], guard_result: TransitionGuardResult
) -> dict[str, Any]:
    return {
        **clip,
        "transition_action": guard_result.transition_action,
        "transition_risk": guard_result.transition_risk,
        "motion_type": guard_result.motion_type,
        "guard_reason": guard_result.guard_reason,
        "hard_cut_timestamps": _hard_cut_timestamps(guard_result),
    }


def _fallback_reason(
    analysis: ActionMotionAnalysis,
    *,
    vlm_attempted: bool,
    vlm_succeeded: bool,
    vlm_complete: bool,
) -> str:
    if analysis.analysis_error:
        return str(analysis.analysis_error)
    if analysis.motion_type == "ambient_camera_motion":
        return "ambient_camera_motion"
    if not analysis.candidates:
        return analysis.motion_type or "no_action_candidate"
    if vlm_attempted and not vlm_succeeded:
        return "vlm_verification_failed"
    if vlm_succeeded and not vlm_complete:
        return "vlm_incomplete"
    return "low_cv_confidence"


def _resolve_vlm_selection(
    analysis: ActionMotionAnalysis,
    decision: ActionVlmDecision,
    safe_start_s: float,
    safe_end_s: float,
    config: ActionBoundaryConfig,
) -> tuple[ActionMotionAnalysis, int | None, str | None]:
    """Reconcile a VLM choice with the top CV proposal without crossing safety bounds."""
    selected_index = decision.selected_candidate_index
    selected = analysis.candidates[selected_index]
    cv_top = analysis.candidates[0]
    near_agreement = (
        abs(selected.peak_s - cv_top.peak_s) <= 1e-6
        and abs(selected.start_s - cv_top.start_s) <= 1.0 + 1e-9
        and abs(selected.end_s - cv_top.end_s) <= 1.0 + 1e-9
    )
    wider_start_s = min(selected.start_s, cv_top.start_s)
    wider_end_s = max(selected.end_s, cv_top.end_s)
    wider_is_safe = (
        safe_start_s - 1e-9 <= wider_start_s <= wider_end_s <= safe_end_s + 1e-9
        and wider_end_s - wider_start_s <= min(20.0, config.max_duration_s) + 1e-9
    )
    if near_agreement and wider_is_safe:
        start_source = selected if selected.start_s <= cv_top.start_s else cv_top
        end_source = selected if selected.end_s >= cv_top.end_s else cv_top
        merged = ActionBoundaryCandidate(
            start_s=wider_start_s,
            peak_s=cv_top.peak_s,
            end_s=wider_end_s,
            confidence=max(
                selected.confidence, cv_top.confidence, decision.confidence
            ),
            start_settle=start_source.start_settle,
            end_settle=end_source.end_settle,
            peak_inclusion=max(
                selected.peak_inclusion, cv_top.peak_inclusion
            ),
            boundary_quiet=min(
                selected.boundary_quiet, cv_top.boundary_quiet
            ),
        )
        candidates = list(analysis.candidates)
        candidates[selected_index] = merged
        return (
            replace(
                analysis,
                candidates=tuple(candidates),
                confidence=max(analysis.confidence, merged.confidence),
            ),
            selected_index,
            None,
        )
    if decision.confidence >= config.boundary_confidence_threshold:
        return analysis, selected_index, None
    return analysis, None, "vlm_low_confidence_disagreement"


def _update_shape_metrics(
    metrics: dict[str, int | float | dict[str, int]],
    result: ActionBoundaryResult,
    original_start_s: float,
    original_end_s: float,
) -> None:
    if not result.segments:
        return
    output_start_s = min(segment.start_s for segment in result.segments)
    output_end_s = max(segment.end_s for segment in result.segments)
    if output_start_s < original_start_s - 1e-9 or output_end_s > original_end_s + 1e-9:
        metrics["extended"] = int(metrics["extended"]) + 1
    if output_start_s > original_start_s + 1e-9 or output_end_s < original_end_s - 1e-9:
        metrics["trimmed"] = int(metrics["trimmed"]) + 1
    if result.action_boundary_mode == "split_action" or len(result.segments) > 1:
        metrics["split"] = int(metrics["split"]) + 1
    if result.loop_quality_score is not None and result.loop_quality_score < 0.5:
        metrics["low_loop_quality"] = int(metrics["low_loop_quality"]) + 1


def _rescore_children(
    candidates: list[dict[str, Any]],
    frame_scorer: FrameScorer,
    enabled: bool,
) -> list[dict[str, Any]]:
    retained: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        if not candidate.get("needs_rescore"):
            retained.append(candidate)
            continue
        if not enabled:
            continue
        midpoint = (
            float(candidate["start_ts"]) + float(candidate["end_ts"])
        ) / 2.0
        try:
            payload = frame_scorer(midpoint, f"action_{index:02d}_{midpoint:.3f}")
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        worthiness = _finite(payload.get("gif_worthiness"), math.nan)
        if not math.isfinite(worthiness):
            continue
        normalized = {
            **payload,
            "timestamp": midpoint,
            "gif_worthiness": worthiness,
        }
        candidate = {
            **candidate,
            "best_frame": normalized,
            "best_frame_ts": midpoint,
            "best_frame_path": normalized.get("path", ""),
            "frame_count": 1,
            "gif_worthiness": worthiness,
            "needs_rescore": False,
        }
        retained.append(candidate)
    return retained


def _index_action_children(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    count = len(candidates)
    return [
        {
            **candidate,
            "action_split_index": index,
            "action_split_count": count,
        }
        for index, candidate in enumerate(candidates, start=1)
    ]


def _transition_only_candidates(
    clip: Mapping[str, Any],
    guard_result: TransitionGuardResult,
    scored_frames: list[dict[str, Any]],
    min_duration_s: float,
    frame_scorer: FrameScorer,
    rescore_enabled: bool,
) -> list[dict[str, Any]]:
    candidates = build_guarded_clips(
        dict(clip),
        guard_result,
        scored_frames,
        min_duration_s,
    )
    for candidate in candidates:
        candidate["guarded_export_window"] = True
    return _rescore_children(candidates, frame_scorer, rescore_enabled)


def materialize_action_candidates(
    *,
    video_path: str | Path,
    clip: dict[str, Any],
    scored_frames: list[dict[str, Any]],
    total_duration_s: float,
    config: Mapping[str, Any] | ActionBoundaryConfig | None,
    evidence_cache: TemporalEvidenceCache,
    frame_scorer: FrameScorer,
    sequence_generator: SequenceGenerator,
) -> ActionMaterialization:
    """Materialize one merged clip through transition, action, and fan-out."""
    total_started = perf_counter()
    total_duration_s = _finite(total_duration_s, math.nan)
    if not math.isfinite(total_duration_s) or total_duration_s <= 0.0:
        raise ValueError("total_duration_s must be finite and positive")
    config_values = _config_mapping(config)
    action_config = (
        config
        if isinstance(config, ActionBoundaryConfig)
        else ActionBoundaryConfig.from_mapping(config_values, strict=False)
    )
    transition_config = TransitionGuardConfig.from_mapping(config_values)
    legacy_min_duration_s = _finite(
        config_values.get("min_duration"), action_config.min_duration_s
    )
    original_start_s, original_end_s, original_anchor_s = _clip_window(
        clip, total_duration_s
    )
    if action_config.enabled:
        scan_start_s, scan_end_s = _analysis_window(
            original_anchor_s, total_duration_s, action_config
        )
    else:
        legacy_window = build_export_window(
            clip,
            total_duration_s=total_duration_s,
            min_duration_s=config_values.get(
                "min_duration", action_config.min_duration_s
            ),
            max_duration_s=config_values.get(
                "max_duration", action_config.max_duration_s
            ),
        )
        scan_start_s, scan_end_s = legacy_window.start_s, legacy_window.end_s
    scan_config = TemporalScanConfig(
        transition_config.scan_fps,
        transition_config.scan_width,
        transition_config.motion_compensation,
    )
    action_metrics = _empty_action_metrics()
    rescore_enabled = bool(
        config_values.get("transition_rescore_split_segments", True)
    )

    try:
        evidence = evidence_cache.scan(
            video_path, scan_start_s, scan_end_s, scan_config
        )
    except (OSError, TemporalMediaError) as media_error:
        if transition_config.enabled:
            raise
        safe_segment = GuardSegment(scan_start_s, scan_end_s, "clean")
        guard_result = TransitionGuardResult(
            transition_action="keep",
            segments=(safe_segment,),
            boundaries=(),
            hard_cut_count=0,
            soft_transition_count=0,
            motion_type="disabled",
            transition_risk=0.0,
            guard_reason="transition guard disabled",
            original_start_s=scan_start_s,
            original_end_s=scan_end_s,
            anchor_ts_s=original_anchor_s,
            anchor_segment=safe_segment,
        )
        transition_metrics = _transition_metrics(guard_result)
        if not action_config.enabled:
            candidates = _transition_only_candidates(
                clip,
                guard_result,
                scored_frames,
                legacy_min_duration_s,
                frame_scorer,
                rescore_enabled,
            )
        else:
            reason = f"temporal_media_error:{type(media_error).__name__}"
            candidates = []
            fallback_reasons: Counter[str] = Counter()
            for segment in guard_result.segments:
                safe_start_s = float(segment.start_s)
                safe_end_s = float(segment.end_s)
                if safe_end_s - safe_start_s < action_config.min_duration_s:
                    continue
                anchor_ts_s = _segment_anchor(
                    safe_start_s,
                    safe_end_s,
                    original_anchor_s,
                    scored_frames,
                )
                analysis = ActionMotionAnalysis(
                    "unknown", (), (), (), (), 0.0, str(media_error)
                )
                result = _fallback_result(
                    analysis=analysis,
                    safe_start_s=safe_start_s,
                    safe_end_s=safe_end_s,
                    anchor_ts_s=anchor_ts_s,
                    config=action_config,
                    reason=reason,
                )
                _update_shape_metrics(
                    action_metrics, result, original_start_s, original_end_s
                )
                built = build_action_clips(
                    _transition_clip(clip, guard_result),
                    result,
                    scored_frames,
                    action_config.min_duration_s,
                )
                candidates.extend(built)
                action_metrics["fallback"] = (
                    int(action_metrics["fallback"]) + 1
                )
                fallback_reasons[reason] += 1
            candidates = _rescore_children(
                candidates, frame_scorer, rescore_enabled
            )
            candidates = _index_action_children(candidates)
            action_metrics["fallback_reasons"] = dict(fallback_reasons)
        action_metrics["output"] = len(candidates)
        action_metrics["total_ms"] = max(
            0.0, (perf_counter() - total_started) * 1000.0
        )
        return ActionMaterialization(
            tuple(candidates), transition_metrics, action_metrics
        )

    guard_result = guard_candidate_window(
        video_path,
        scan_start_s,
        scan_end_s,
        original_anchor_s,
        config_values,
        temporal_evidence=evidence,
    )
    transition_metrics = _transition_metrics(guard_result)

    if not action_config.enabled:
        candidates = _transition_only_candidates(
            clip,
            guard_result,
            scored_frames,
            legacy_min_duration_s,
            frame_scorer,
            rescore_enabled,
        )
        action_metrics["output"] = len(candidates)
        action_metrics["total_ms"] = max(
            0.0, (perf_counter() - total_started) * 1000.0
        )
        return ActionMaterialization(
            tuple(candidates), transition_metrics, action_metrics
        )

    candidates: list[dict[str, Any]] = []
    fallback_reasons: Counter[str] = Counter()
    vlm_attempted_globally = False
    worthiness = _finite(clip.get("gif_worthiness"), 0.0)
    safe_segments = _safe_segments(guard_result, scan_start_s, scan_end_s)
    for segment in safe_segments:
        safe_start_s = float(segment.start_s)
        safe_end_s = float(segment.end_s)
        if safe_end_s - safe_start_s < action_config.min_duration_s:
            continue
        anchor_ts_s = _segment_anchor(
            safe_start_s,
            safe_end_s,
            original_anchor_s,
            scored_frames,
        )
        segment_evidence = evidence.slice(safe_start_s, safe_end_s)
        cv_started = perf_counter()
        analysis_exception: Exception | None = None
        try:
            analysis = analyze_action_motion(
                segment_evidence,
                safe_start_s,
                safe_end_s,
                anchor_ts_s,
                action_config,
            )
        except Exception as exc:
            analysis_exception = exc
            analysis = ActionMotionAnalysis(
                "unknown", (), (), (), (), 0.0, str(exc)
            )
        action_metrics["cv_ms"] = float(action_metrics["cv_ms"]) + max(
            0.0, (perf_counter() - cv_started) * 1000.0
        )
        if analysis.motion_type == "ambient_camera_motion":
            action_metrics["ambient_motion"] = (
                int(action_metrics["ambient_motion"]) + 1
            )

        selected_index: int | None = None
        vlm_attempted = False
        vlm_succeeded = False
        vlm_complete = False
        vlm_rejection_reason: str | None = None
        if (
            analysis.motion_type == "subject_action"
            and analysis.candidates
            and analysis.confidence >= action_config.boundary_confidence_threshold
        ):
            selected_index = 0
            action_metrics["cv"] = int(action_metrics["cv"]) + 1
        elif (
            analysis.motion_type != "ambient_camera_motion"
            and analysis.candidates
            and action_config.vlm_verify_enabled
            and worthiness >= action_config.vlm_min_worthiness
            and not vlm_attempted_globally
        ):
            vlm_attempted = True
            vlm_attempted_globally = True
            action_metrics["vlm_checked"] = int(action_metrics["vlm_checked"]) + 1
            vlm_started = perf_counter()
            try:
                decision = verify_action_candidates(
                    segment_evidence,
                    analysis.candidates,
                    sequence_generator,
                )
            except Exception:
                decision = None
            action_metrics["vlm_ms"] = float(action_metrics["vlm_ms"]) + max(
                0.0, (perf_counter() - vlm_started) * 1000.0
            )
            if decision is None:
                action_metrics["vlm_failed"] = (
                    int(action_metrics["vlm_failed"]) + 1
                )
            else:
                vlm_succeeded = True
                vlm_complete = decision.complete
                action_metrics["vlm_succeeded"] = (
                    int(action_metrics["vlm_succeeded"]) + 1
                )
                if decision.complete:
                    (
                        analysis,
                        selected_index,
                        vlm_rejection_reason,
                    ) = _resolve_vlm_selection(
                        analysis,
                        decision,
                        safe_start_s,
                        safe_end_s,
                        action_config,
                    )

        fallback_reason = _fallback_reason(
            analysis,
            vlm_attempted=vlm_attempted,
            vlm_succeeded=vlm_succeeded,
            vlm_complete=vlm_complete,
        )
        if vlm_rejection_reason is not None:
            fallback_reason = vlm_rejection_reason
        if analysis_exception is not None:
            fallback_reason = str(analysis_exception)
        try:
            result = finalize_action_analysis(
                analysis,
                segment_evidence,
                safe_start_s,
                safe_end_s,
                anchor_ts_s,
                selected_index,
                action_config,
            )
        except Exception as exc:
            fallback_reason = str(exc)
            result = _fallback_result(
                analysis=analysis,
                safe_start_s=safe_start_s,
                safe_end_s=safe_end_s,
                anchor_ts_s=anchor_ts_s,
                config=action_config,
                reason=fallback_reason,
            )
        if result.action_boundary_mode == "fallback_fixed":
            reason = (
                result.action_fallback_reason
                if selected_index is not None and result.action_fallback_reason
                else fallback_reason
            )
            if any(
                segment.end_s - segment.start_s > 20.0 + 1e-9
                for segment in result.segments
            ):
                result = _fallback_result(
                    analysis=analysis,
                    safe_start_s=safe_start_s,
                    safe_end_s=safe_end_s,
                    anchor_ts_s=anchor_ts_s,
                    config=action_config,
                    reason=reason,
                )
            result = replace(result, action_fallback_reason=reason)
            action_metrics["fallback"] = int(action_metrics["fallback"]) + 1
            fallback_reasons[str(reason)] += 1
        elif vlm_succeeded and vlm_complete:
            result = replace(result, action_vlm_verified=True)
        _update_shape_metrics(
            action_metrics, result, original_start_s, original_end_s
        )
        base_clip = _transition_clip(clip, guard_result)
        built = build_action_clips(
            base_clip,
            result,
            scored_frames,
            action_config.min_duration_s,
        )
        if analysis.motion_type == "ambient_camera_motion":
            public_mode = "ambient_camera_motion"
        elif result.action_boundary_mode == "fallback_fixed":
            public_mode = "fallback_fixed"
        elif result.action_vlm_verified:
            public_mode = "hybrid_vlm"
        else:
            public_mode = "cv"
        for child in built:
            child["action_boundary_mode"] = public_mode
        candidates.extend(built)

    candidates = _rescore_children(candidates, frame_scorer, rescore_enabled)
    candidates = _index_action_children(candidates)
    action_metrics["output"] = len(candidates)
    action_metrics["fallback_reasons"] = dict(fallback_reasons)
    action_metrics["total_ms"] = max(
        0.0, (perf_counter() - total_started) * 1000.0
    )
    return ActionMaterialization(
        tuple(candidates), transition_metrics, action_metrics
    )

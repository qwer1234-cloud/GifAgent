"""Pure fan-out from final action segments to export candidates."""

from __future__ import annotations

import math
from typing import Any

from app.services.action_boundary import ActionBoundaryResult


def _frames_in_segment(
    scored_frames: list[dict[str, Any]], start_s: float, end_s: float
) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for frame in scored_frames:
        try:
            timestamp = float(frame["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(timestamp) and start_s <= timestamp <= end_s:
            frames.append(frame)
    return frames


def build_action_clips(
    clip: dict[str, Any],
    action_result: ActionBoundaryResult,
    scored_frames: list[dict[str, Any]],
    min_duration_s: float,
) -> list[dict[str, Any]]:
    """Copy action metadata onto one immutable export window per segment."""
    try:
        export_min_duration = max(0.0, float(min_duration_s))
    except (TypeError, ValueError):
        export_min_duration = 0.0
    candidates: list[dict[str, Any]] = []
    for segment in action_result.segments:
        start_s, end_s = float(segment.start_s), float(segment.end_s)
        duration_s = end_s - start_s
        if (
            not math.isfinite(duration_s)
            or duration_s < export_min_duration
            or duration_s > 20.0 + 1e-9
        ):
            continue
        segment_frames = _frames_in_segment(scored_frames, start_s, end_s)
        candidate = {
            **clip,
            "start_ts": start_s,
            "end_ts": end_s,
            "frame_count": len(segment_frames),
            "guarded_export_window": True,
            "action_boundary_mode": action_result.action_boundary_mode,
            "action_start_ts": action_result.action_start_ts,
            "action_peak_ts": action_result.action_peak_ts,
            "action_end_ts": action_result.action_end_ts,
            "action_completeness_score": action_result.action_completeness_score,
            "action_boundary_confidence": action_result.action_boundary_confidence,
            "loop_quality_score": action_result.loop_quality_score,
            "action_split_reason": action_result.action_split_reason,
            "action_vlm_verified": action_result.action_vlm_verified,
            "action_fallback_reason": action_result.action_fallback_reason,
            "action_analysis_version": action_result.action_analysis_version,
            "diagnostics": dict(action_result.diagnostics),
        }
        if segment_frames:
            best = max(
                segment_frames,
                key=lambda frame: float(frame.get("gif_worthiness", 0.0)),
            )
            candidate.update(
                best_frame=best,
                best_frame_ts=float(best["timestamp"]),
                best_frame_path=best.get("path", ""),
                gif_worthiness=best.get("gif_worthiness", 0.0),
                needs_rescore=False,
            )
        else:
            candidate.update(
                best_frame=None,
                best_frame_ts=None,
                best_frame_path="",
                needs_rescore=True,
            )
        candidates.append(candidate)
    return candidates

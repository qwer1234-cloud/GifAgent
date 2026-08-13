"""Pure candidate fan-out for windows inspected by the transition guard."""

from __future__ import annotations

from typing import Any

from app.services.transition_guard import TransitionGuardResult


def _frames_in_segment(
    scored_frames: list[dict[str, Any]], start_s: float, end_s: float
) -> list[dict[str, Any]]:
    return [
        frame for frame in scored_frames
        if start_s <= float(frame["timestamp"]) <= end_s
    ]


def build_guarded_clips(
    clip: dict[str, Any],
    guard_result: TransitionGuardResult,
    scored_frames: list[dict[str, Any]],
    min_duration_s: float,
) -> list[dict[str, Any]]:
    """Fan a guarded window into clean candidates without scoring any media.

    Every retained guard segment receives its own candidate.  A segment that
    does not contain an already-scored frame is retained as a rescore request,
    leaving the caller to decide whether and how to run a VLM.
    """
    # The transition guard has its own scan-time minimum.  Export callers may
    # require a larger GIF minimum, so enforce that final invariant here,
    # before candidates can be embedded, ranked, or assigned an ID.
    try:
        export_min_duration = max(0.0, float(min_duration_s))
    except (TypeError, ValueError):
        export_min_duration = 0.0
    if guard_result.transition_action == "drop":
        return []
    candidates: list[dict[str, Any]] = []

    for segment in guard_result.segments:
        start_s, end_s = float(segment.start_s), float(segment.end_s)
        if end_s - start_s < export_min_duration:
            continue
        segment_frames = _frames_in_segment(scored_frames, start_s, end_s)
        candidate = {
            **clip,
            "start_ts": start_s,
            "end_ts": end_s,
            "frame_count": len(segment_frames),
            "transition_action": guard_result.transition_action,
            "transition_risk": guard_result.transition_risk,
            "motion_type": guard_result.motion_type,
            "guard_reason": guard_result.guard_reason,
        }
        if segment_frames:
            best = max(
                segment_frames, key=lambda frame: float(frame.get("gif_worthiness", 0.0))
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

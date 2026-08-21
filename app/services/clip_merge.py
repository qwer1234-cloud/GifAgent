"""Merge scored adaptive frames into GIF clip candidates.

Region-aware merge avoids pairwise chaining that collapses an entire
dense high-score video into a single mega-clip.
"""

from __future__ import annotations

from typing import Any

from app.services.export_ranking import adult_priority_score


def _clip_from_group(group: list[dict[str, Any]]) -> dict[str, Any]:
    best = max(
        group,
        key=lambda item: (
            adult_priority_score(item),
            float(item.get("gif_worthiness") or 0.0),
        ),
    )
    return {
        "start_ts": group[0]["timestamp"],
        "end_ts": group[-1]["timestamp"],
        "best_frame": best,
        "best_frame_ts": best["timestamp"],
        "best_frame_path": best.get("path", ""),
        "frame_count": len(group),
        "gif_worthiness": best["gif_worthiness"],
        "sex_act": best.get("sex_act", 0.0),
        "emotional_core": best.get("emotional_core", "?"),
        "caption": best.get("caption", ""),
    }


def merge_scored_frames_into_clips(
    frames: list[dict[str, Any]],
    *,
    merge_gap: float,
    merge_score_threshold: float,
    max_merge_span_s: float = 24.0,
    peak_threshold: float | None = None,
    shot_boundaries: list[float] | tuple[float, ...] | None = None,
) -> list[dict[str, Any]]:
    """Merge timestamp-sorted scored frames into clip dicts.

    Rules:
    1. Adjacent frames merge only when gap <= ``merge_gap`` AND both scores
       are >= ``merge_score_threshold``.
    2. A group is flushed before its ``end - start`` would exceed
       ``max_merge_span_s`` (hard cap against mega-clips).
    3. If ``peak_threshold`` is set and a multi-frame group's best score is
       below it, demote to a single-frame clip of the best frame only.
    4. A supplied shot boundary prevents a merge when it lies after the
       previous frame and at or before the next frame.
    """
    if not frames:
        return []

    ordered = sorted(frames, key=lambda x: float(x["timestamp"]))
    peak_threshold = (
        float(peak_threshold)
        if peak_threshold is not None
        else None
    )
    max_span = max(0.0, float(max_merge_span_s))
    boundaries = tuple(sorted(float(boundary) for boundary in shot_boundaries or ()))

    def crosses_shot_boundary(previous_timestamp: float, timestamp: float) -> bool:
        return any(previous_timestamp < boundary <= timestamp for boundary in boundaries)

    clips: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = [ordered[0]]

    def flush() -> None:
        nonlocal current
        if not current:
            return
        clip = _clip_from_group(current)
        if (
            peak_threshold is not None
            and clip["frame_count"] > 1
            and float(clip["gif_worthiness"]) < peak_threshold
        ):
            # Demote weak multi-frame runs to the single best frame.
            best = clip["best_frame"]
            clip = _clip_from_group([best])
        clips.append(clip)
        current = []

    for frame in ordered[1:]:
        prev = current[-1]
        gap = float(frame["timestamp"]) - float(prev["timestamp"])
        both_good = (
            float(frame.get("gif_worthiness", 0.0)) >= merge_score_threshold
            and float(prev.get("gif_worthiness", 0.0)) >= merge_score_threshold
        )
        span_if_added = float(frame["timestamp"]) - float(current[0]["timestamp"])
        within_span = max_span <= 0 or span_if_added <= max_span

        crosses_boundary = crosses_shot_boundary(
            float(prev["timestamp"]), float(frame["timestamp"])
        )

        if gap <= merge_gap and both_good and within_span and not crosses_boundary:
            current.append(frame)
        else:
            flush()
            current = [frame]

    flush()
    return clips

"""Shared, bounded time windows for adaptive GIF exports."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping


@dataclass(frozen=True)
class ExportWindow:
    """An export-safe section of a source video, measured in seconds."""

    start_s: float
    end_s: float
    duration_s: float


def _finite_number(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def build_export_window(
    clip: Mapping[str, object],
    *,
    total_duration_s: float,
    min_duration_s: float,
    max_duration_s: float,
    single_frame_max_duration_s: float | None = None,
) -> ExportWindow:
    """Build a centered, video-clamped window that never exceeds ``max_duration_s``.

    Single frames keep the established score interpolation, but interpolate
    towards ``single_frame_max_duration_s`` instead of the global maximum:
    one scored frame is no evidence about the rest of the window.  Omitting
    it falls back to ``max_duration_s``, preserving every existing caller.
    Multi-frame candidates retain their evidence-backed span-plus-three
    seconds and ignore the single-frame cap entirely.
    """
    total_duration = max(0.0, _finite_number(total_duration_s))
    max_duration = max(0.0, _finite_number(max_duration_s))
    min_duration = min(max_duration, max(0.0, _finite_number(min_duration_s)))
    frame_count = int(max(0.0, _finite_number(clip.get("frame_count"), 1.0)))
    worthiness = min(1.0, max(0.0, _finite_number(clip.get("gif_worthiness"), 0.0)))

    if frame_count > 1:
        span = max(
            0.0,
            _finite_number(clip.get("end_ts")) - _finite_number(clip.get("start_ts")),
        )
        requested_duration = min(max_duration, span + 3.0)
    else:
        single_cap = max_duration
        if single_frame_max_duration_s is not None:
            single_cap = min(
                max_duration,
                max(
                    min_duration,
                    _finite_number(single_frame_max_duration_s, max_duration),
                ),
            )
        requested_duration = min_duration + (single_cap - min_duration) * worthiness

    duration = min(requested_duration, total_duration)
    best_frame = clip.get("best_frame")
    nested_timestamp = (
        best_frame.get("timestamp")
        if isinstance(best_frame, Mapping)
        else None
    )
    anchor = min(
        total_duration,
        max(0.0, _finite_number(clip.get("best_frame_ts", nested_timestamp))),
    )
    # Preserve the existing 40% before / 60% after timing bias.
    start = max(0.0, anchor - duration * 0.4)
    start = min(start, total_duration - duration)
    end = start + duration
    return ExportWindow(start_s=start, end_s=end, duration_s=duration)

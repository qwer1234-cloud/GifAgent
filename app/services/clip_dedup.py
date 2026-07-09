"""Clip-level deduplication helpers for adaptive GIF export."""

from __future__ import annotations

from typing import Any


def _clip_score(clip: dict[str, Any]) -> float:
    return float(clip.get("final_score") or clip.get("gif_worthiness") or 0.0)


def _clip_peak_ts(clip: dict[str, Any]) -> float:
    best_frame = clip.get("best_frame")
    if isinstance(best_frame, dict) and best_frame.get("timestamp") is not None:
        return float(best_frame["timestamp"])
    if clip.get("start_ts") is not None and clip.get("end_ts") is not None:
        return (float(clip["start_ts"]) + float(clip["end_ts"])) / 2.0
    return float(clip.get("start_ts") or 0.0)


def temporal_dedup_clips(
    clips: list[dict[str, Any]],
    *,
    min_gap_s: float,
) -> list[dict[str, Any]]:
    """Keep the highest-scored clip within each peak-time window."""
    if min_gap_s <= 0 or len(clips) <= 1:
        return clips

    kept: list[dict[str, Any]] = []
    kept_peaks: list[float] = []
    for clip in sorted(clips, key=_clip_score, reverse=True):
        peak = _clip_peak_ts(clip)
        if any(abs(peak - kept_peak) <= min_gap_s for kept_peak in kept_peaks):
            continue
        kept.append(clip)
        kept_peaks.append(peak)
    return kept

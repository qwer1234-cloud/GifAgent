"""Export-window and palette helpers shared by direct and staged exports."""
from __future__ import annotations

import math
import os

from app.services.boundary_snap import guard_result_from_cut_times, snap_window
from app.services.gif_encode import (
    DEFAULT_DIFF_MODE,
    DEFAULT_DITHER,
    DEFAULT_STATS_MODE,
    build_palette_filters,
    is_divisible_gif_fps,
    nearest_divisible_gif_fps,
)
from app.services.temporal_evidence import TemporalEvidenceCache


def _temporal_media_duration(duration_s: float, scan_fps: float) -> float:
    """Return the last in-media instant for inclusive temporal sampling."""
    duration_s = float(duration_s)
    scan_fps = float(scan_fps)
    if duration_s <= 0.0 or scan_fps <= 0.0:
        return duration_s
    last_sample_s = max(0.0, duration_s - (0.5 / scan_fps))
    return math.nextafter(last_sample_s, 0.0)


def _ffmpeg_seconds(value: float) -> str:
    """Format seconds without scientific notation (unsupported by FFmpeg)."""
    rendered = f"{float(value):.9f}".rstrip("0").rstrip(".")
    return rendered if rendered and rendered != "-0" else "0"


def _apply_boundary_snaps(
    clips: list[dict],
    video_path: str,
    cfg: dict,
    cache: TemporalEvidenceCache,
) -> dict:
    """Snap each clip window after transition guard, before dedup."""
    stats = {"snapped": 0, "kept": 0, "unavailable": 0}
    if not cfg.get("boundary_snap_enabled") or not clips:
        return stats
    radius = float(cfg.get("boundary_snap_radius_s", 0.6))
    for clip in clips:
        # ``guarded_export_window`` only means "export uses start_ts/end_ts
        # instead of re-centering".  Snap still refines those bounds, but
        # never crosses hard cuts recorded on the clip.
        result = snap_window(
            video_path,
            float(clip.get("start_ts") or 0),
            float(clip.get("end_ts") or 0),
            radius_s=radius,
            guard_result=guard_result_from_cut_times(
                clip.get("hard_cut_timestamps")
            ),
            config=cfg,
            cache=cache,
            guarded_export_window=False,
        )
        clip["start_ts"] = result.start_s
        clip["end_ts"] = result.end_s
        clip["snap_action"] = result.snap_action
        stats[result.snap_action] = stats.get(result.snap_action, 0) + 1
    print(
        "  Boundary snap: "
        f"snapped={stats['snapped']} kept={stats['kept']} "
        f"unavailable={stats['unavailable']}"
    )
    return stats


def _single_frame_cap(cfg: dict, max_duration: float) -> float:
    """Resolve the single-frame export ceiling from a frozen config.

    Snapshots written before this key existed fall back to the configured
    maximum, so their exports keep the length they always had.
    """
    return float(cfg.get("single_frame_max_duration_s", max_duration))


def _palette_filters_for(cfg: dict) -> tuple[str, str]:
    """Resolve the palette fragments from a frozen pipeline config.

    Direct and staged exports both read through here so the same snapshot
    always yields the same two filtergraph fragments.
    """
    return build_palette_filters(
        stats_mode=str(cfg.get("gif_palette_stats_mode", DEFAULT_STATS_MODE)),
        dither=str(cfg.get("gif_dither", DEFAULT_DITHER)),
        diff_mode=str(cfg.get("gif_diff_mode", DEFAULT_DIFF_MODE)),
    )


_WARNED_FPS: set[int] = set()


def _warn_once_on_indivisible_fps(fps: int) -> None:
    """Warn when *fps* cannot be expressed as an exact GIF frame delay.

    Historical snapshots carry ``gif_fps: 24``, which must keep running, so
    this never raises.
    """
    if is_divisible_gif_fps(fps) or fps in _WARNED_FPS:
        return
    _WARNED_FPS.add(fps)
    nearest = ", ".join(str(v) for v in nearest_divisible_gif_fps(fps))
    print(
        f"  [gif] WARNING gif_fps={fps} does not divide 100, so GIF frame "
        f"delays are rounded and playback is uneven; nearest exact "
        f"rates: {nearest}",
        flush=True,
    )

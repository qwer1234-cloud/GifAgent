"""Opt-in sub-second export-window snapping onto motion minima."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from app.services.temporal_evidence import (
    TemporalEvidenceCache,
    TemporalMediaError,
    TemporalScanConfig,
)
from app.services.transition_guard import BoundaryEvidence, TransitionGuardResult


@dataclass(frozen=True)
class SnapResult:
    start_s: float
    end_s: float
    snap_action: str
    reason: str


def snap_window(
    video_path: str,
    start_s: float,
    end_s: float,
    *,
    radius_s: float,
    guard_result: TransitionGuardResult | None,
    config: dict[str, Any],
    cache: TemporalEvidenceCache,
    guarded_export_window: bool = False,
) -> SnapResult:
    """Nudge *start_s*/*end_s* to nearby motion minima without crossing cuts.

    Decode failures and duration-floor violations return the original
    window with ``snap_action="unavailable"`` or ``"kept"``.  A clip
    already marked ``guarded_export_window`` is never moved.
    """
    original = (float(start_s), float(end_s))
    if guarded_export_window:
        return SnapResult(*original, "kept", "guarded_export_window")

    min_duration = float(config.get("transition_min_duration_s", 2.0))
    margin = float(config.get("transition_boundary_margin_s", 0.25))
    radius = max(0.0, float(radius_s))
    if radius <= 0.0:
        return SnapResult(*original, "kept", "radius_disabled")

    scan_cfg = TemporalScanConfig(
        fps=float(config.get("transition_scan_fps", 8)),
        width=int(config.get("transition_scan_width", 320)),
        motion_compensation=bool(config.get("transition_motion_compensation", True)),
    )
    scan_start = max(0.0, original[0] - radius)
    scan_end = original[1] + radius
    try:
        evidence = cache.scan(video_path, scan_start, scan_end, scan_cfg)
    except (TemporalMediaError, ValueError, OSError) as exc:
        return SnapResult(*original, "unavailable", f"decode_failed:{exc}")

    cuts = _hard_cut_times(guard_result)
    motion = [
        (pair.timestamp_s, float(pair.compensated_residual))
        for pair in evidence.pairs
    ]
    if not motion:
        return SnapResult(*original, "kept", "no_motion_samples")

    new_start = _snap_bound(
        original[0], motion, radius, cuts, margin, toward="start",
    )
    new_end = _snap_bound(
        original[1], motion, radius, cuts, margin, toward="end",
    )
    if new_end <= new_start or (new_end - new_start) < min_duration:
        return SnapResult(*original, "kept", "min_duration")
    if abs(new_start - original[0]) < 1e-6 and abs(new_end - original[1]) < 1e-6:
        return SnapResult(*original, "kept", "already_on_minimum")
    return SnapResult(new_start, new_end, "snapped", "motion_minimum")


def guard_result_from_cut_times(
    timestamps: Iterable[object] | None,
) -> TransitionGuardResult | None:
    """Rebuild a cut-only guard result from timestamps stored on a clip."""
    if not timestamps:
        return None
    cuts: list[float] = []
    for item in timestamps:
        try:
            stamp = float(item)
        except (TypeError, ValueError):
            continue
        if stamp not in cuts:
            cuts.append(stamp)
    if not cuts:
        return None

    boundaries = tuple(
        BoundaryEvidence(
            timestamp_s=stamp,
            boundary_type="hard_cut",
            confidence=1.0,
            histogram_distance=0.0,
            edge_distance=0.0,
            luma_change=0.0,
            compensated_residual=0.0,
            inlier_ratio=0.0,
            translate_x=0.0,
            translate_y=0.0,
            scale=1.0,
        )
        for stamp in cuts
    )
    return TransitionGuardResult(
        transition_action="split",
        segments=(),
        boundaries=boundaries,
        hard_cut_count=len(boundaries),
        soft_transition_count=0,
        motion_type="cut",
        transition_risk=1.0,
        guard_reason="hard_cut",
    )


def _hard_cut_times(guard_result: TransitionGuardResult | None) -> list[float]:
    if guard_result is None:
        return []
    return [
        float(boundary.timestamp_s)
        for boundary in guard_result.boundaries
        if boundary.boundary_type == "hard_cut"
    ]


def _crosses_cut(old: float, new: float, cuts: Iterable[float]) -> bool:
    lo, hi = (old, new) if old <= new else (new, old)
    return any(lo < cut < hi or lo < cut <= hi for cut in cuts)


def _inside_margin(ts: float, cuts: Iterable[float], margin: float) -> bool:
    return any(abs(ts - cut) < margin - 1e-9 for cut in cuts)


def _snap_bound(
    target: float,
    motion: list[tuple[float, float]],
    radius: float,
    cuts: list[float],
    margin: float,
    *,
    toward: str,
) -> float:
    lo, hi = target - radius, target + radius
    window = [(ts, energy) for ts, energy in motion if lo <= ts <= hi]
    if not window:
        return target
    # Prefer the lowest motion; break ties by closeness to the original bound.
    ranked = sorted(window, key=lambda item: (item[1], abs(item[0] - target)))
    for ts, _energy in ranked:
        if _inside_margin(ts, cuts, margin):
            continue
        if _crosses_cut(target, ts, cuts):
            continue
        return ts
    return target

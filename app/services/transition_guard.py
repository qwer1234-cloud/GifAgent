"""Media-only, motion-aware transition detection for candidate GIF windows.

The guard deliberately uses inexpensive, low resolution OpenCV features.  It
does not decide whether a moment is interesting; it only says whether a window
can safely be exported without crossing an edit boundary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from pathlib import Path
from typing import Mapping

import cv2
import numpy as np

from app.services.temporal_evidence import (
    TemporalEvidence,
    TemporalEvidenceCache,
    TemporalPairEvidence,
    TemporalScanConfig,
)


@dataclass(frozen=True)
class TransitionGuardConfig:
    enabled: bool = True
    scan_fps: float = 8.0
    scan_width: int = 320
    boundary_margin_s: float = 0.25
    min_duration_s: float = 2.0
    motion_compensation: bool = True
    hard_threshold: float = 0.65
    soft_threshold: float = 0.40
    soft_run_frames: int = 3
    rescore_split_segments: bool = True

    @classmethod
    def from_mapping(cls, values: Mapping[str, object] | None) -> "TransitionGuardConfig":
        values = values or {}

        def value(name: str, default: object) -> object:
            return values.get(f"transition_{name}", values.get(name, default))

        def boolean(name: str, default: bool) -> bool:
            raw = value(name, default)
            if isinstance(raw, str):
                return raw.strip().lower() in {"1", "true", "yes", "on"}
            return bool(raw)

        def number(name: str, default: float, minimum: float) -> float:
            try:
                parsed = float(value(name, default))
            except (TypeError, ValueError):
                return default
            if not math.isfinite(parsed):
                return default
            return max(minimum, parsed)

        def integer(name: str, default: int, minimum: int) -> int:
            try:
                parsed = float(value(name, default))
            except (TypeError, ValueError):
                return default
            if not math.isfinite(parsed):
                return default
            return max(minimum, int(parsed))

        return cls(
            enabled=boolean("guard_enabled", cls.enabled),
            scan_fps=number("scan_fps", cls.scan_fps, 0.1),
            scan_width=integer("scan_width", cls.scan_width, 1),
            boundary_margin_s=number("boundary_margin_s", cls.boundary_margin_s, 0.0),
            min_duration_s=number("min_duration_s", cls.min_duration_s, 0.1),
            motion_compensation=boolean("motion_compensation", cls.motion_compensation),
            hard_threshold=number("hard_threshold", cls.hard_threshold, 0.0),
            soft_threshold=number("soft_threshold", cls.soft_threshold, 0.0),
            soft_run_frames=integer("soft_run_frames", cls.soft_run_frames, 1),
            rescore_split_segments=boolean("rescore_split_segments", cls.rescore_split_segments),
        )


@dataclass(frozen=True)
class BoundaryEvidence:
    timestamp_s: float
    boundary_type: str
    confidence: float
    histogram_distance: float
    edge_distance: float
    luma_change: float
    compensated_residual: float
    inlier_ratio: float
    translate_x: float
    translate_y: float
    scale: float


@dataclass(frozen=True)
class GuardSegment:
    start_s: float
    end_s: float
    reason: str = "clean"


@dataclass(frozen=True)
class TransitionGuardResult:
    transition_action: str
    segments: tuple[GuardSegment, ...]
    boundaries: tuple[BoundaryEvidence, ...]
    hard_cut_count: int
    soft_transition_count: int
    motion_type: str
    transition_risk: float
    guard_reason: str
    guard_error: str | None = None
    # Preserve the caller's requested window and make the segment selected for
    # its score anchor explicit.  ``segments`` remains the complete fan-out
    # set for a valid split; ``anchor_segment`` is the one containing the
    # original best-frame timestamp.
    original_start_s: float | None = 0.0
    original_end_s: float | None = 0.0
    anchor_ts_s: float | None = 0.0
    anchor_segment: GuardSegment | None = None

    def to_dict(self) -> dict[str, object]:
        """Return JSON-safe primitive values with a stable field layout."""
        return {
            "transition_action": self.transition_action,
            "segments": [asdict(segment) for segment in self.segments],
            "boundaries": [asdict(boundary) for boundary in self.boundaries],
            "hard_cut_count": self.hard_cut_count,
            "soft_transition_count": self.soft_transition_count,
            "motion_type": self.motion_type,
            "transition_risk": self.transition_risk,
            "guard_reason": self.guard_reason,
            "guard_error": self.guard_error,
            "original_start_s": self.original_start_s,
            "original_end_s": self.original_end_s,
            "anchor_ts_s": self.anchor_ts_s,
            "anchor_segment": asdict(self.anchor_segment) if self.anchor_segment else None,
        }


def _finite_or_none(value: object) -> float | None:
    """Prevent malformed caller timestamps from leaking NaN/Infinity to JSON."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _result_error(message: str, start_s: object = 0.0, end_s: object = 0.0, anchor_ts_s: object = 0.0) -> TransitionGuardResult:
    return TransitionGuardResult(
        transition_action="unverified", segments=(), boundaries=(), hard_cut_count=0,
        soft_transition_count=0, motion_type="unknown", transition_risk=1.0,
        guard_reason="media scan could not be verified", guard_error=message,
        original_start_s=_finite_or_none(start_s), original_end_s=_finite_or_none(end_s),
        anchor_ts_s=_finite_or_none(anchor_ts_s),
    )


def _evidence(metric: TemporalPairEvidence, config: TransitionGuardConfig) -> BoundaryEvidence:
    residual, inlier_ratio, dx, dy, scale = (
        (metric.compensated_residual, metric.inlier_ratio, metric.translate_x, metric.translate_y, metric.scale)
        if config.motion_compensation else (1.0, 0.0, 0.0, 0.0, 1.0)
    )
    # Raw pixel metrics are small at scan resolution.  Calibrate each feature
    # against a visible, one-frame edit rather than allowing the size of the
    # resized image to change the public thresholds' meaning.
    score = min(
        1.0,
        0.45 * min(1.0, metric.histogram_distance / 0.06)
        + 0.30 * min(1.0, metric.edge_distance / 0.35)
        + 0.25 * min(1.0, metric.luma_change / 0.18),
    )
    structure_recovered = residual < 0.18 and inlier_ratio >= 0.45
    if score >= config.hard_threshold and residual >= 0.08:
        kind = "hard_cut"
    # The public soft threshold controls the normalized per-pair change
    # strength.  Histogram/edge floors keep coherent pans (large pixel/edge
    # displacement but a stable palette) out of the dissolve run.
    elif (
        score >= config.soft_threshold
        and metric.histogram_distance >= 0.03
        and metric.edge_distance >= 0.05
    ):
        kind = "soft_change"
    elif structure_recovered:
        kind = "coherent_camera_motion"
    else:
        kind = "none"
    confidence = min(1.0, max(score, residual if kind == "hard_cut" else 0.0))
    return BoundaryEvidence(metric.timestamp_s, kind, confidence, metric.histogram_distance, metric.edge_distance, metric.luma_change, residual, inlier_ratio, dx, dy, scale)


def _structure_recovers(before: np.ndarray, after: np.ndarray) -> bool:
    """Whether frames on either side of an exposure spike are the same shot."""
    luma_change = float(np.mean(cv2.absdiff(before, after)) / 255.0)
    edges_before = cv2.Canny(before, 60, 140)
    edges_after = cv2.Canny(after, 60, 140)
    edge_distance = float(np.mean(edges_before != edges_after))
    hist_before = cv2.calcHist([before], [0], None, [32], [0, 256])
    hist_after = cv2.calcHist([after], [0], None, [32], [0, 256])
    cv2.normalize(hist_before, hist_before)
    cv2.normalize(hist_after, hist_after)
    histogram_distance = float(cv2.compareHist(hist_before, hist_after, cv2.HISTCMP_BHATTACHARYYA))
    return luma_change < 0.12 and edge_distance < 0.18 and histogram_distance < 0.20


def _flash_indexes(evidence: list[BoundaryEvidence], pairs: list[TemporalPairEvidence]) -> set[int]:
    """Find an exposure spike only when the surrounding shot recovers."""
    indexes: set[int] = set()
    for index in range(len(evidence) - 1):
        if (
            evidence[index].luma_change >= 0.25
            and evidence[index + 1].luma_change >= 0.25
            and _structure_recovers(pairs[index].previous_gray, pairs[index + 1].gray)
        ):
            indexes.update((index, index + 1))
    return indexes


def _has_stable_affine_model(item: BoundaryEvidence) -> bool:
    """Use a tight residual bound so a blend cannot masquerade as a pan."""
    return item.compensated_residual < 0.012 and item.inlier_ratio >= 0.45


def _segments(start_s: float, end_s: float, boundaries: list[BoundaryEvidence], config: TransitionGuardConfig) -> tuple[GuardSegment, ...]:
    points = sorted({boundary.timestamp_s for boundary in boundaries})
    raw: list[tuple[float, float]] = []
    left = start_s
    for point in points:
        raw.append((left, max(left, point - config.boundary_margin_s)))
        left = min(end_s, point + config.boundary_margin_s)
    raw.append((left, end_s))
    return tuple(GuardSegment(round(a, 6), round(b, 6), "trimmed_at_transition" if points else "clean") for a, b in raw if b - a >= config.min_duration_s)


def guard_candidate_window(
    video_path: str | Path,
    start_s: float,
    end_s: float,
    anchor_ts_s: float,
    config_values: Mapping[str, object] | None = None,
    *,
    temporal_evidence: TemporalEvidence | None = None,
) -> TransitionGuardResult:
    """Inspect one candidate window and return export-safe segments.

    Invalid or unreadable media is deliberately *unverified*, never silently
    treated as clean: callers can retain it with a penalty or request review.
    """
    config = TransitionGuardConfig.from_mapping(config_values)
    try:
        start_s, end_s, anchor_ts_s = float(start_s), float(end_s), float(anchor_ts_s)
    except (TypeError, ValueError):
        return _result_error("window timestamps must be numeric")
    if not all(math.isfinite(value) for value in (start_s, end_s, anchor_ts_s)) or end_s <= start_s:
        return _result_error("window timestamps are invalid", start_s, end_s, anchor_ts_s)
    if not config.enabled:
        segment = GuardSegment(start_s, end_s)
        return TransitionGuardResult(
            "keep", (segment,), (), 0, 0, "disabled", 0.0,
            "transition guard disabled", original_start_s=start_s,
            original_end_s=end_s, anchor_ts_s=anchor_ts_s, anchor_segment=segment,
        )
    try:
        if temporal_evidence is None:
            temporal_evidence = TemporalEvidenceCache().scan(
                video_path, start_s, end_s,
                TemporalScanConfig(config.scan_fps, config.scan_width, config.motion_compensation),
            )
        elif (
            temporal_evidence.start_s > start_s + 1e-6
            or temporal_evidence.end_s < end_s - 1e-6
        ):
            raise ValueError("supplied temporal evidence does not cover requested window")
        pairs = list(temporal_evidence.slice(start_s, end_s).pairs)
    except (cv2.error, OSError, ValueError) as exc:
        return _result_error(str(exc), start_s, end_s, anchor_ts_s)
    if len(pairs) < 2:
        return _result_error("window was too short or contained too few decodable frames", start_s, end_s, anchor_ts_s)

    evidence = [_evidence(pair, config) for pair in pairs]
    confirmed: list[BoundaryEvidence] = []
    flash_indexes = _flash_indexes(evidence, pairs)
    for index, item in enumerate(evidence):
        if index in flash_indexes:
            evidence[index] = BoundaryEvidence(**{**asdict(item), "boundary_type": "flash_or_exposure"})
            continue
        if item.boundary_type == "hard_cut":
            confirmed.append(item)

    # A dissolve/fade is a sustained sequence of normalized soft-threshold
    # crossings rather than one exceptional frame.
    run: list[BoundaryEvidence] = []
    for item in evidence:
        # Sustained palette/edge changes are a dissolve/fade only if a stable
        # global affine model cannot explain them.  The tighter residual bound
        # preserves coherent pans while a blend's persistent image residual
        # remains eligible for soft-boundary confirmation.
        unstable = item.boundary_type == "soft_change" and not _has_stable_affine_model(item)
        run = run + [item] if unstable else []
        if len(run) == config.soft_run_frames:
            midpoint = run[len(run) // 2]
            kind = "fade" if all(member.luma_change >= member.histogram_distance for member in run) else "dissolve"
            confirmed_item = BoundaryEvidence(**{**asdict(midpoint), "boundary_type": kind})
            confirmed.append(confirmed_item)
            run = []

    # Do not split the same edit twice when a hard score appears inside a soft run.
    unique: list[BoundaryEvidence] = []
    for item in sorted(confirmed, key=lambda boundary: boundary.timestamp_s):
        if not unique or item.timestamp_s - unique[-1].timestamp_s > config.boundary_margin_s * 2:
            unique.append(item)
    segments = _segments(start_s, end_s, unique, config)
    hard_count = sum(item.boundary_type == "hard_cut" for item in unique)
    soft_count = sum(item.boundary_type in {"dissolve", "fade"} for item in unique)
    stable_motion = [
        item for item in evidence
        if item.compensated_residual < 0.18 and item.inlier_ratio >= 0.45
        and (abs(item.translate_x) + abs(item.translate_y) >= 0.5 or abs(item.scale - 1.0) >= 0.01)
    ]
    motion_type = "coherent_camera_motion" if stable_motion else "static_or_local_motion"
    risk = max((item.confidence for item in unique), default=0.0)
    if not segments:
        return TransitionGuardResult(
            "drop", (), tuple(unique), hard_count, soft_count, motion_type, risk,
            "transition margins left no exportable segment", original_start_s=start_s,
            original_end_s=end_s, anchor_ts_s=anchor_ts_s,
        )
    anchor_segment = next(
        (segment for segment in segments if segment.start_s <= anchor_ts_s <= segment.end_s),
        None,
    )
    # Splits deliberately retain every viable segment for the downstream
    # candidate fan-out.  A single retained segment, however, must still be
    # the segment that contains the original score anchor; otherwise keeping
    # it would silently move the candidate to the other side of an edit.
    if anchor_segment is None and len(segments) == 1:
        return TransitionGuardResult(
            "drop", (), tuple(unique), hard_count, soft_count, motion_type, risk,
            "anchor timestamp falls inside a transition safety margin",
            original_start_s=start_s, original_end_s=end_s, anchor_ts_s=anchor_ts_s,
        )
    if len(segments) > 1:
        action, reason = "split", "confirmed transition boundaries split the candidate window"
    elif unique:
        action, reason = "trim", "transition margin trimmed the candidate window"
    elif segments[0].start_s != start_s or segments[0].end_s != end_s:
        action, reason = "trim", "window was clamped to a clean segment"
    else:
        action, reason = "keep", "no confirmed transition boundary"
    return TransitionGuardResult(
        action, segments, tuple(unique), hard_count, soft_count, motion_type, risk, reason,
        original_start_s=start_s, original_end_s=end_s, anchor_ts_s=anchor_ts_s,
        anchor_segment=anchor_segment,
    )

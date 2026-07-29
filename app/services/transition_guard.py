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
        }


@dataclass(frozen=True)
class _PairMetric:
    timestamp_s: float
    previous_gray: np.ndarray
    gray: np.ndarray
    histogram_distance: float
    edge_distance: float
    luma_change: float

    @property
    def score(self) -> float:
        # Histogram detects palette changes, edges preserve structural evidence,
        # and luma catches fades/exposure changes.
        return min(1.0, 0.45 * self.histogram_distance + 0.30 * self.edge_distance + 0.25 * self.luma_change)


def _result_error(message: str) -> TransitionGuardResult:
    return TransitionGuardResult(
        transition_action="unverified", segments=(), boundaries=(), hard_cut_count=0,
        soft_transition_count=0, motion_type="unknown", transition_risk=1.0,
        guard_reason="media scan could not be verified", guard_error=message,
    )


def _resize_frame(frame: np.ndarray, width: int) -> tuple[np.ndarray, np.ndarray]:
    height = max(1, round(frame.shape[0] * width / frame.shape[1]))
    small = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), cv2.cvtColor(small, cv2.COLOR_BGR2HSV)


def _scan_pairs(path: Path, start_s: float, end_s: float, config: TransitionGuardConfig) -> list[_PairMetric]:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        capture.release()
        raise ValueError("OpenCV could not open the source video")
    try:
        source_fps = capture.get(cv2.CAP_PROP_FPS)
        if not math.isfinite(source_fps) or source_fps <= 0:
            source_fps = config.scan_fps
        capture.set(cv2.CAP_PROP_POS_MSEC, start_s * 1000.0)
        sample_interval = 1.0 / config.scan_fps
        next_sample = start_s - 1e-6
        previous_gray: np.ndarray | None = None
        previous_hsv: np.ndarray | None = None
        pairs: list[_PairMetric] = []
        fallback_index = 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            frame_index = capture.get(cv2.CAP_PROP_POS_FRAMES) - 1
            timestamp_s = frame_index / source_fps if frame_index >= 0 else start_s + fallback_index / source_fps
            fallback_index += 1
            if timestamp_s > end_s + 1e-6:
                break
            if timestamp_s + 1e-6 < next_sample:
                continue
            next_sample += sample_interval
            gray, hsv = _resize_frame(frame, config.scan_width)
            if previous_gray is not None and previous_hsv is not None:
                hist_a = cv2.calcHist([previous_hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
                hist_b = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
                cv2.normalize(hist_a, hist_a)
                cv2.normalize(hist_b, hist_b)
                histogram_distance = float(cv2.compareHist(hist_a, hist_b, cv2.HISTCMP_BHATTACHARYYA))
                edges_a = cv2.Canny(previous_gray, 60, 140)
                edges_b = cv2.Canny(gray, 60, 140)
                edge_distance = float(np.mean(edges_a != edges_b))
                luma_change = float(np.mean(cv2.absdiff(previous_gray, gray)) / 255.0)
                pairs.append(_PairMetric(timestamp_s, previous_gray, gray, histogram_distance, edge_distance, luma_change))
            previous_gray, previous_hsv = gray, hsv
        return pairs
    finally:
        capture.release()


def _motion_evidence(metric: _PairMetric) -> tuple[float, float, float, float, float]:
    """Return residual, inlier ratio, dx, dy, and scale for a frame pair."""
    points = cv2.goodFeaturesToTrack(metric.previous_gray, maxCorners=160, qualityLevel=0.01, minDistance=5, blockSize=5)
    if points is None or len(points) < 8:
        return 1.0, 0.0, 0.0, 0.0, 1.0
    next_points, status, _ = cv2.calcOpticalFlowPyrLK(metric.previous_gray, metric.gray, points, None)
    if next_points is None or status is None:
        return 1.0, 0.0, 0.0, 0.0, 1.0
    good = status.ravel().astype(bool)
    if int(good.sum()) < 6:
        return 1.0, 0.0, 0.0, 0.0, 1.0
    transform, inliers = cv2.estimateAffinePartial2D(points[good], next_points[good], method=cv2.RANSAC)
    if transform is None or inliers is None:
        return 1.0, 0.0, 0.0, 0.0, 1.0
    warped = cv2.warpAffine(metric.previous_gray, transform, (metric.gray.shape[1], metric.gray.shape[0]), borderMode=cv2.BORDER_REFLECT)
    residual = float(np.mean(cv2.absdiff(warped, metric.gray)) / 255.0)
    inlier_ratio = float(np.mean(inliers.ravel().astype(bool)))
    dx, dy = float(transform[0, 2]), float(transform[1, 2])
    scale = float(math.hypot(transform[0, 0], transform[1, 0]))
    return residual, inlier_ratio, dx, dy, scale


def _evidence(metric: _PairMetric, config: TransitionGuardConfig) -> BoundaryEvidence:
    residual, inlier_ratio, dx, dy, scale = _motion_evidence(metric) if config.motion_compensation else (1.0, 0.0, 0.0, 0.0, 1.0)
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


def _flash_indexes(evidence: list[BoundaryEvidence]) -> set[int]:
    """Find a two-sided exposure spike before classifying either side as a cut."""
    indexes: set[int] = set()
    for index in range(len(evidence) - 1):
        if evidence[index].luma_change >= 0.25 and evidence[index + 1].luma_change >= 0.25:
            indexes.update((index, index + 1))
    return indexes


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
        return _result_error("window timestamps are invalid")
    if not config.enabled:
        return TransitionGuardResult("keep", (GuardSegment(start_s, end_s),), (), 0, 0, "disabled", 0.0, "transition guard disabled")
    try:
        pairs = _scan_pairs(Path(video_path), start_s, end_s, config)
    except (cv2.error, OSError, ValueError) as exc:
        return _result_error(str(exc))
    if len(pairs) < 2:
        return _result_error("window was too short or contained too few decodable frames")

    evidence = [_evidence(pair, config) for pair in pairs]
    confirmed: list[BoundaryEvidence] = []
    flash_indexes = _flash_indexes(evidence)
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
        # A dissolve can retain an apparently excellent identity affine fit:
        # the two blended images are aligned even though their content is not.
        # The sustained histogram/edge condition that produced ``soft_change``
        # is therefore the deciding evidence here; camera pans do not satisfy
        # it because their colour histogram remains stable.
        unstable = item.boundary_type == "soft_change"
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
        return TransitionGuardResult("drop", (), tuple(unique), hard_count, soft_count, motion_type, risk, "transition margins left no exportable segment")
    if len(segments) > 1:
        action, reason = "split", "confirmed transition boundaries split the candidate window"
    elif unique:
        action, reason = "trim", "transition margin trimmed the candidate window"
    elif segments[0].start_s != start_s or segments[0].end_s != end_s:
        action, reason = "trim", "window was clamped to a clean segment"
    else:
        action, reason = "keep", "no confirmed transition boundary"
    return TransitionGuardResult(action, segments, tuple(unique), hard_count, soft_count, motion_type, risk, reason)

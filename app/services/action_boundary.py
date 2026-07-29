"""Motion-compensated CV action analysis and boundary proposals."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
import math
from types import MappingProxyType
from typing import Any, Mapping

import numpy as np

from app.services.temporal_evidence import TemporalEvidence, TemporalPairEvidence


@dataclass(frozen=True)
class ActionBoundaryConfig:
    enabled: bool = True
    vlm_verify_enabled: bool = True
    analysis_version: int = 1
    analysis_window_s: float = 30.0
    preferred_min_duration_s: float = 4.0
    preferred_max_duration_s: float = 12.0
    min_duration_s: float = 2.0
    max_duration_s: float = 20.0
    scan_fps: float = 4.0
    boundary_confidence_threshold: float = 0.65
    loop_adjust_s: float = 0.75
    vlm_min_worthiness: float = 0.60
    fallback_mode: str = "fixed_window"

    @classmethod
    def from_mapping(
        cls, values: Mapping[str, Any] | None, strict: bool = False
    ) -> "ActionBoundaryConfig":
        source = values or {}
        defaults = cls()
        aliases = {
            "enabled": "action_guard_enabled",
            "vlm_verify_enabled": "action_vlm_verify_enabled",
            "analysis_version": "action_analysis_version",
            "analysis_window_s": "action_analysis_window_s",
            "preferred_min_duration_s": "action_preferred_min_duration_s",
            "preferred_max_duration_s": "action_preferred_max_duration_s",
            "min_duration_s": "action_min_duration_s",
            "max_duration_s": "action_max_duration_s",
            "scan_fps": "action_scan_fps",
            "boundary_confidence_threshold": "action_boundary_confidence_threshold",
            "loop_adjust_s": "action_loop_adjust_s",
            "vlm_min_worthiness": "action_vlm_min_worthiness",
            "fallback_mode": "action_fallback_mode",
        }
        parsed: dict[str, Any] = {}
        boolean_names = {"enabled", "vlm_verify_enabled"}
        integer_names = {"analysis_version"}
        string_names = {"fallback_mode"}
        for item in fields(cls):
            name = item.name
            raw = source.get(name, source.get(aliases[name], getattr(defaults, name)))
            try:
                if name in boolean_names:
                    parsed[name] = _parse_bool(raw)
                elif name in string_names:
                    if not isinstance(raw, str):
                        raise ValueError(f"{name} must be a string")
                    parsed[name] = raw
                elif name in integer_names:
                    number = _finite_number(raw, name)
                    if not number.is_integer():
                        raise ValueError(f"{name} must be an integer")
                    parsed[name] = int(number)
                else:
                    parsed[name] = _finite_number(raw, name)
            except (TypeError, ValueError):
                if strict:
                    raise ValueError(f"invalid {name}") from None
                parsed[name] = getattr(defaults, name)

        try:
            _validate_config(parsed)
        except ValueError:
            if strict:
                raise
            _repair_non_strict_config(parsed, defaults)
            _validate_config(parsed)
        return cls(**parsed)


@dataclass(frozen=True)
class ActionBoundaryCandidate:
    start_s: float
    peak_s: float
    end_s: float
    confidence: float
    start_settle: float
    end_settle: float
    peak_inclusion: float
    boundary_quiet: float


@dataclass(frozen=True)
class ActionMotionAnalysis:
    motion_type: str
    candidates: tuple[ActionBoundaryCandidate, ...]
    residual_curve: tuple[tuple[float, float], ...]
    active_runs: tuple[tuple[float, float], ...]
    stable_valleys: tuple[float, ...]
    confidence: float
    analysis_error: str | None = None


class _ImmutableDiagnostics(Mapping[str, float | int | str | None]):
    __slots__ = ("_values",)

    def __init__(
        self, values: Mapping[str, float | int | str | None] | None = None
    ) -> None:
        object.__setattr__(self, "_values", MappingProxyType(dict(values or {})))

    def __getitem__(self, key: str) -> float | int | str | None:
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __deepcopy__(
        self, memo: dict[int, Any]
    ) -> "_ImmutableDiagnostics":
        return self


@dataclass(frozen=True)
class ActionSegment:
    start_s: float
    end_s: float
    peak_s: float
    reason: str
    needs_rescore: bool


@dataclass(frozen=True)
class ActionBoundaryResult:
    action_boundary_mode: str
    safe_start_s: float
    safe_end_s: float
    anchor_ts_s: float
    boundary_candidates: tuple[ActionBoundaryCandidate, ...]
    segments: tuple[ActionSegment, ...]
    action_start_ts: float | None
    action_peak_ts: float | None
    action_end_ts: float | None
    action_completeness_score: float | None
    action_boundary_confidence: float
    loop_quality_score: float | None
    action_split_reason: str | None
    action_vlm_verified: bool
    action_fallback_reason: str | None
    action_analysis_version: int = 1
    diagnostics: Mapping[str, float | int | str | None] = field(
        default_factory=_ImmutableDiagnostics
    )
    analysis_error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagnostics", _ImmutableDiagnostics(self.diagnostics))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["diagnostics"] = dict(self.diagnostics)
        return payload


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and math.isfinite(float(value)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError("invalid boolean")


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _validate_config(values: Mapping[str, Any]) -> None:
    if values["analysis_version"] != 1:
        raise ValueError("unsupported analysis_version")
    for name in ("boundary_confidence_threshold", "vlm_min_worthiness"):
        if not 0.0 <= values[name] <= 1.0:
            raise ValueError(f"{name} must be in [0, 1]")
    if values["preferred_min_duration_s"] > values["preferred_max_duration_s"]:
        raise ValueError("preferred_min_duration_s must not exceed preferred_max_duration_s")
    if values["preferred_max_duration_s"] > values["max_duration_s"]:
        raise ValueError("preferred_max_duration_s must not exceed max_duration_s")
    if values["max_duration_s"] > values["analysis_window_s"]:
        raise ValueError("max_duration_s must not exceed analysis_window_s")
    if values["min_duration_s"] < 2.0:
        raise ValueError("min_duration_s must be at least 2 seconds")
    if values["scan_fps"] <= 0.0:
        raise ValueError("scan_fps must be positive")
    if values["analysis_window_s"] <= 0.0 or values["max_duration_s"] <= 0.0:
        raise ValueError("durations must be positive")
    if values["fallback_mode"] != "fixed_window":
        raise ValueError("unsupported fallback_mode")


def _repair_non_strict_config(
    values: dict[str, Any], defaults: ActionBoundaryConfig
) -> None:
    if values["analysis_version"] != 1:
        values["analysis_version"] = defaults.analysis_version
    for name in ("boundary_confidence_threshold", "vlm_min_worthiness"):
        if not 0.0 <= values[name] <= 1.0:
            values[name] = getattr(defaults, name)
    if values["min_duration_s"] < 2.0:
        values["min_duration_s"] = defaults.min_duration_s
    if values["scan_fps"] <= 0.0:
        values["scan_fps"] = defaults.scan_fps
    if values["analysis_window_s"] <= 0.0:
        values["analysis_window_s"] = defaults.analysis_window_s
    if values["max_duration_s"] <= 0.0:
        values["max_duration_s"] = defaults.max_duration_s
    if values["fallback_mode"] != "fixed_window":
        values["fallback_mode"] = defaults.fallback_mode
    duration_relationship_invalid = (
        values["preferred_min_duration_s"] > values["preferred_max_duration_s"]
        or values["preferred_max_duration_s"] > values["max_duration_s"]
        or values["max_duration_s"] > values["analysis_window_s"]
    )
    if duration_relationship_invalid:
        for name in (
            "analysis_window_s",
            "preferred_min_duration_s",
            "preferred_max_duration_s",
            "max_duration_s",
        ):
            values[name] = getattr(defaults, name)


def _motion_value(pair: TemporalPairEvidence) -> tuple[float, float]:
    residual_energy = float(np.mean(pair.residual_map) / 255.0)
    pixel_floor = max(6.0, float(np.percentile(pair.residual_map, 75)))
    changed_ratio = float(np.mean(pair.residual_map >= pixel_floor))
    return 0.65 * residual_energy + 0.35 * changed_ratio, residual_energy


def _median_filter(values: np.ndarray) -> np.ndarray:
    if len(values) < 2:
        return values.copy()
    padded = np.pad(values, (1, 1), mode="edge")
    return np.asarray([np.median(padded[index : index + 3]) for index in range(len(values))])


def _close_short_gaps(active: np.ndarray, maximum_gap: int) -> np.ndarray:
    closed = active.copy()
    index = 0
    while index < len(closed):
        if closed[index]:
            index += 1
            continue
        start = index
        while index < len(closed) and not closed[index]:
            index += 1
        if start > 0 and index < len(closed) and index - start <= maximum_gap:
            closed[start:index] = True
    return closed


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    index = 0
    while index < len(mask):
        if not mask[index]:
            index += 1
            continue
        start = index
        while index + 1 < len(mask) and mask[index + 1]:
            index += 1
        runs.append((start, index))
        index += 1
    return runs


def _stable_regions(stable: np.ndarray, minimum_samples: int) -> list[tuple[int, int]]:
    return [(start, end) for start, end in _runs(stable) if end - start + 1 >= minimum_samples]


def _is_ambient_camera_motion(
    pairs: tuple[TemporalPairEvidence, ...],
    residual_energies: np.ndarray,
    active_threshold: float,
    active: np.ndarray,
    fps: float,
) -> bool:
    if not pairs:
        return False
    reliable = np.asarray([pair.inlier_ratio >= 0.45 for pair in pairs])
    if float(np.mean(reliable)) < 0.70:
        return False
    reliable_pairs = [pair for pair, keep in zip(pairs, reliable) if keep]
    translations = np.asarray(
        [(pair.translate_x, pair.translate_y) for pair in reliable_pairs], dtype=float
    )
    scales = np.asarray([pair.scale - 1.0 for pair in reliable_pairs], dtype=float)
    median_vector = np.median(translations, axis=0)
    translation_size = float(np.linalg.norm(median_vector))
    zoom_size = float(abs(np.median(scales)))
    if translation_size < 0.15 and zoom_size < 0.0005:
        return False
    if translation_size >= 0.15:
        projections = translations @ (median_vector / translation_size)
        coherent_fraction = float(np.mean(projections >= -0.05))
    else:
        median_scale = float(np.median(scales))
        coherent_fraction = float(np.mean(scales * median_scale >= 0.0))
    if coherent_fraction < 0.70:
        return False
    material_run_samples = max(2, round(0.5 * fps))
    has_material_local_run = any(
        end - start + 1 >= material_run_samples for start, end in _runs(active)
    )
    # The required median-energy rule rejects uncompensated/global disruption.
    # A contiguous local residual run is additional evidence that a subject is
    # acting during otherwise coherent camera motion. Window-wide active
    # fraction is deliberately irrelevant: short complete actions are valid.
    return (
        float(np.median(residual_energies)) < active_threshold
        and not has_material_local_run
    )


def _clamp_score(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def _quiet_score(value: float, active_threshold: float) -> float:
    return _clamp_score((active_threshold - value) / max(active_threshold, 1e-9))


def _select_run(
    runs: list[tuple[int, int]], timestamps: np.ndarray, anchor_ts_s: float
) -> tuple[int, int] | None:
    containing = [
        run for run in runs if timestamps[run[0]] - 1e-9 <= anchor_ts_s <= timestamps[run[1]] + 1e-9
    ]
    if containing:
        return min(containing, key=lambda run: (timestamps[run[1]] - timestamps[run[0]], run[0]))
    ranked = sorted(
        runs,
        key=lambda run: (
            min(abs(anchor_ts_s - timestamps[run[0]]), abs(anchor_ts_s - timestamps[run[1]])),
            run[0],
        ),
    )
    if not ranked:
        return None
    distance = min(
        abs(anchor_ts_s - timestamps[ranked[0][0]]),
        abs(anchor_ts_s - timestamps[ranked[0][1]]),
    )
    return ranked[0] if distance <= 1.0 + 1e-9 else None


def _boundary_candidates(
    curve: np.ndarray,
    timestamps: np.ndarray,
    selected: tuple[int, int],
    stable: np.ndarray,
    active_threshold: float,
    safe_start_s: float,
    safe_end_s: float,
    anchor_ts_s: float,
) -> tuple[ActionBoundaryCandidate, ...]:
    run_start, run_end = selected
    peak_index = run_start + int(np.argmax(curve[run_start : run_end + 1]))
    before = [index for index in range(run_start - 1, -1, -1) if stable[index]][:2]
    after = [index for index in range(run_end + 1, len(curve)) if stable[index]][:2]
    left_edge_active = run_start == 0 or timestamps[run_start] <= safe_start_s + (
        timestamps[1] - timestamps[0] if len(timestamps) > 1 else 0.25
    ) + 1e-9
    right_edge_active = run_end == len(curve) - 1 or timestamps[run_end] >= safe_end_s - 1e-9
    if not before:
        before = [run_start]
    if not after:
        after = [run_end]

    candidates: list[ActionBoundaryCandidate] = []
    for start_index in before:
        for end_index in after:
            start_s = safe_start_s if left_edge_active else float(timestamps[start_index])
            end_s = safe_end_s if right_edge_active else float(timestamps[end_index])
            start_settle = 0.0 if left_edge_active else _quiet_score(curve[start_index], active_threshold)
            end_settle = 0.0 if right_edge_active else _quiet_score(curve[end_index], active_threshold)
            peak_inclusion = _clamp_score(
                1.0
                if start_s <= timestamps[peak_index] <= end_s
                else 1.0 - abs(anchor_ts_s - timestamps[peak_index])
            )
            boundary_quiet = _clamp_score((start_settle + end_settle) / 2.0)
            confidence = _clamp_score(
                0.30 * start_settle
                + 0.35 * end_settle
                + 0.20 * peak_inclusion
                + 0.15 * boundary_quiet
            )
            if left_edge_active or right_edge_active:
                confidence = min(0.60, confidence)
            candidates.append(
                ActionBoundaryCandidate(
                    start_s,
                    float(timestamps[peak_index]),
                    end_s,
                    confidence,
                    start_settle,
                    end_settle,
                    peak_inclusion,
                    boundary_quiet,
                )
            )
    unique = {
        (candidate.start_s, candidate.end_s): candidate
        for candidate in candidates
    }
    return tuple(
        sorted(
            unique.values(),
            key=lambda candidate: (
                -candidate.confidence,
                abs(candidate.peak_s - anchor_ts_s),
                candidate.start_s,
                candidate.end_s,
            ),
        )[:3]
    )


def analyze_action_motion(
    evidence: TemporalEvidence,
    safe_start_s: float,
    safe_end_s: float,
    anchor_ts_s: float,
    config_values: Mapping[str, Any] | ActionBoundaryConfig | None,
) -> ActionMotionAnalysis:
    """Analyze compensated residual motion and rank complete action bounds."""
    try:
        config = (
            config_values
            if isinstance(config_values, ActionBoundaryConfig)
            else ActionBoundaryConfig.from_mapping(config_values, strict=False)
        )
        if not all(math.isfinite(value) for value in (safe_start_s, safe_end_s, anchor_ts_s)):
            raise ValueError("analysis timestamps must be finite")
        if safe_end_s < safe_start_s or not safe_start_s <= anchor_ts_s <= safe_end_s:
            raise ValueError("invalid safe action window or anchor")
        if evidence.start_s > safe_start_s + 1e-6 or evidence.end_s < safe_end_s - 1e-6:
            raise ValueError("temporal evidence does not cover the safe action window")
        sampled = evidence.slice(safe_start_s, safe_end_s).resample(config.scan_fps)
        if len(sampled.pairs) < 3:
            raise ValueError("too few temporal pairs for action analysis")
        values_and_energy = tuple(_motion_value(pair) for pair in sampled.pairs)
        raw_curve = np.asarray([item[0] for item in values_and_energy], dtype=float)
        residual_energies = np.asarray([item[1] for item in values_and_energy], dtype=float)
        curve = _median_filter(raw_curve)
        timestamps = np.asarray([pair.timestamp_s for pair in sampled.pairs], dtype=float)
        baseline = float(np.median(curve))
        mad = float(np.median(np.abs(curve - baseline)))
        active_threshold = baseline + max(2.5 * mad, 0.015)
        stable_threshold = baseline + max(1.25 * mad, 0.0075)
        raw_active = curve > active_threshold
        active = _close_short_gaps(raw_active, round(0.5 * config.scan_fps))
        active_index_runs = _runs(active)
        stable = curve <= stable_threshold
        stable_regions = _stable_regions(stable, max(1, round(1.0 * config.scan_fps)))
        stable_valleys = tuple(
            float(timestamps[(start + end) // 2]) for start, end in stable_regions
        )
        residual_curve = tuple(
            (float(timestamp), float(value)) for timestamp, value in zip(timestamps, curve)
        )
        active_runs = tuple(
            (float(timestamps[start]), float(timestamps[end])) for start, end in active_index_runs
        )
        if _is_ambient_camera_motion(
            sampled.pairs, residual_energies, active_threshold, active, config.scan_fps
        ):
            return ActionMotionAnalysis(
                "ambient_camera_motion",
                (),
                residual_curve,
                active_runs,
                stable_valleys,
                _clamp_score(float(np.mean([pair.inlier_ratio for pair in sampled.pairs]))),
            )
        selected = _select_run(active_index_runs, timestamps, anchor_ts_s)
        if selected is None:
            return ActionMotionAnalysis(
                "no_subject_action", (), residual_curve, active_runs, stable_valleys, 0.0
            )
        candidates = _boundary_candidates(
            curve,
            timestamps,
            selected,
            stable,
            active_threshold,
            safe_start_s,
            safe_end_s,
            anchor_ts_s,
        )
        confidence = candidates[0].confidence if candidates else 0.0
        return ActionMotionAnalysis(
            "subject_action",
            candidates,
            residual_curve,
            active_runs,
            stable_valleys,
            confidence,
        )
    except (TypeError, ValueError) as exc:
        return ActionMotionAnalysis("unknown", (), (), (), (), 0.0, str(exc))


def _validate_finite_analysis(
    analysis: ActionMotionAnalysis,
    evidence: TemporalEvidence,
    safe_start_s: float,
    safe_end_s: float,
    anchor_ts_s: float,
) -> None:
    scalar_values = (
        safe_start_s,
        safe_end_s,
        anchor_ts_s,
        analysis.confidence,
        evidence.start_s,
        evidence.end_s,
        evidence.fps,
    )
    candidate_values = (
        value
        for candidate in analysis.candidates
        for value in (
            candidate.start_s,
            candidate.peak_s,
            candidate.end_s,
            candidate.confidence,
            candidate.start_settle,
            candidate.end_settle,
            candidate.peak_inclusion,
            candidate.boundary_quiet,
        )
    )
    curve_values = (value for point in analysis.residual_curve for value in point)
    run_values = (value for run in analysis.active_runs for value in run)
    if not all(
        math.isfinite(float(value))
        for values in (scalar_values, candidate_values, curve_values, run_values, analysis.stable_valleys)
        for value in values
    ):
        raise ValueError("action analysis contains non-finite values")
    if safe_end_s < safe_start_s or not safe_start_s <= anchor_ts_s <= safe_end_s:
        raise ValueError("invalid safe action window or anchor")
    if evidence.start_s > safe_start_s + 1e-6 or evidence.end_s < safe_end_s - 1e-6:
        raise ValueError("temporal evidence does not cover the safe action window")


def _biased_fixed_window(
    safe_start_s: float,
    safe_end_s: float,
    anchor_ts_s: float,
    duration_s: float,
) -> tuple[float, float]:
    duration = min(duration_s, safe_end_s - safe_start_s)
    start_s = anchor_ts_s - 0.40 * duration
    end_s = anchor_ts_s + 0.60 * duration
    if start_s < safe_start_s:
        end_s += safe_start_s - start_s
        start_s = safe_start_s
    if end_s > safe_end_s:
        start_s -= end_s - safe_end_s
        end_s = safe_end_s
    return max(safe_start_s, start_s), min(safe_end_s, end_s)


def _padded_window(
    candidate: ActionBoundaryCandidate,
    safe_start_s: float,
    safe_end_s: float,
    config: ActionBoundaryConfig,
) -> tuple[float, float]:
    core_duration = candidate.end_s - candidate.start_s
    if core_duration < 0.0:
        raise ValueError("action candidate ends before it starts")
    if core_duration > config.max_duration_s:
        before, after = 0.4, 0.6
    else:
        available_padding = max(0.0, config.max_duration_s - core_duration)
        before = min(0.4, available_padding)
        after = min(0.6, max(0.0, available_padding - before))
    start_s = max(safe_start_s, candidate.start_s - before)
    end_s = min(safe_end_s, candidate.end_s + after)
    if end_s - start_s + 1e-9 < config.min_duration_s:
        start_s, end_s = _biased_fixed_window(
            safe_start_s,
            safe_end_s,
            candidate.peak_s,
            config.min_duration_s,
        )
    return start_s, end_s


def _curve_value_at(analysis: ActionMotionAnalysis, timestamp_s: float) -> float:
    if not analysis.residual_curve:
        return 0.0
    return min(
        analysis.residual_curve,
        key=lambda point: (abs(point[0] - timestamp_s), point[0]),
    )[1]


def _segment_peak(
    analysis: ActionMotionAnalysis,
    start_s: float,
    end_s: float,
    default_s: float,
) -> float:
    in_range = [
        point for point in analysis.residual_curve if start_s - 1e-9 <= point[0] <= end_s + 1e-9
    ]
    if not in_range:
        return min(max(default_s, start_s), end_s)
    return max(in_range, key=lambda point: (point[1], -abs(point[0] - default_s), -point[0]))[0]


def _split_at_stable_valleys(
    analysis: ActionMotionAnalysis,
    start_s: float,
    end_s: float,
    min_duration_s: float,
    max_duration_s: float,
) -> list[tuple[float, float]] | None:
    valleys = tuple(
        timestamp
        for timestamp in analysis.stable_valleys
        if start_s + min_duration_s <= timestamp <= end_s - min_duration_s
    )

    def split(left_s: float, right_s: float) -> list[tuple[float, float]] | None:
        if right_s - left_s <= max_duration_s + 1e-9:
            return [(left_s, right_s)]
        midpoint = (left_s + right_s) / 2.0
        ranked = sorted(
            (
                timestamp
                for timestamp in valleys
                if left_s + min_duration_s <= timestamp <= right_s - min_duration_s
            ),
            key=lambda timestamp: (
                abs(timestamp - midpoint),
                _curve_value_at(analysis, timestamp),
                timestamp,
            ),
        )
        for timestamp in ranked:
            before = split(left_s, timestamp)
            if before is None:
                continue
            after = split(timestamp, right_s)
            if after is not None:
                return before + after
        return None

    return split(start_s, end_s)


def _frame_at(evidence: TemporalEvidence, timestamp_s: float):
    return min(
        evidence.frames,
        key=lambda frame: (abs(frame.timestamp_s - timestamp_s), frame.timestamp_s),
    )


def _structural_similarity(left: np.ndarray, right: np.ndarray) -> float:
    return _clamp_score(1.0 - float(np.mean(np.abs(left.astype(float) - right.astype(float)))) / 255.0)


def _color_similarity(left: np.ndarray, right: np.ndarray) -> float:
    hue_delta = np.abs(left[..., 0].astype(float) - right[..., 0].astype(float))
    hue_delta = np.minimum(hue_delta, 180.0 - hue_delta) / 90.0
    saturation_delta = np.abs(left[..., 1].astype(float) - right[..., 1].astype(float)) / 255.0
    value_delta = np.abs(left[..., 2].astype(float) - right[..., 2].astype(float)) / 255.0
    return _clamp_score(1.0 - float(np.mean((hue_delta + saturation_delta + value_delta) / 3.0)))


def _subject_position(gray: np.ndarray) -> tuple[float, float] | None:
    contrast = np.abs(gray.astype(float) - float(np.median(gray)))
    mask = contrast >= max(8.0, float(np.percentile(contrast, 80)))
    points = np.argwhere(mask)
    if len(points) == 0:
        return None
    height, width = gray.shape[:2]
    return float(np.mean(points[:, 1]) / max(1, width - 1)), float(
        np.mean(points[:, 0]) / max(1, height - 1)
    )


def _subject_position_similarity(left: np.ndarray, right: np.ndarray) -> float:
    left_position = _subject_position(left)
    right_position = _subject_position(right)
    if left_position is None and right_position is None:
        return 1.0
    if left_position is None or right_position is None:
        return 0.0
    distance = math.hypot(
        left_position[0] - right_position[0],
        left_position[1] - right_position[1],
    )
    return _clamp_score(1.0 - distance / math.sqrt(2.0))


def _motion_vector(evidence: TemporalEvidence, timestamp_s: float) -> tuple[float, float]:
    if not evidence.pairs:
        return 0.0, 0.0
    pair = min(
        evidence.pairs,
        key=lambda item: (abs(item.timestamp_s - timestamp_s), item.timestamp_s),
    )
    return pair.translate_x, pair.translate_y


def _motion_direction_continuity(
    evidence: TemporalEvidence, start_s: float, end_s: float
) -> float:
    left = _motion_vector(evidence, start_s)
    right = _motion_vector(evidence, end_s)
    left_size, right_size = math.hypot(*left), math.hypot(*right)
    if left_size <= 1e-9 and right_size <= 1e-9:
        return 1.0
    if left_size <= 1e-9 or right_size <= 1e-9:
        return 0.5
    cosine = (left[0] * right[0] + left[1] * right[1]) / (left_size * right_size)
    return _clamp_score((cosine + 1.0) / 2.0)


def _loop_score(
    evidence: TemporalEvidence, start_s: float, end_s: float
) -> float:
    start_frame = _frame_at(evidence, start_s)
    end_frame = _frame_at(evidence, end_s)
    return _clamp_score(
        0.40 * _structural_similarity(start_frame.gray, end_frame.gray)
        + 0.25 * _color_similarity(start_frame.hsv, end_frame.hsv)
        + 0.20 * _subject_position_similarity(start_frame.gray, end_frame.gray)
        + 0.15 * _motion_direction_continuity(evidence, start_s, end_s)
    )


def _adjust_loop_endpoints(
    evidence: TemporalEvidence,
    padded_start_s: float,
    padded_end_s: float,
    core_start_s: float,
    core_end_s: float,
    safe_start_s: float,
    safe_end_s: float,
    config: ActionBoundaryConfig,
) -> tuple[float, float, float]:
    if not evidence.frames:
        return padded_start_s, padded_end_s, 0.0
    loop_adjust_s = max(0.0, config.loop_adjust_s)
    start_ceiling = min(core_start_s, padded_start_s + loop_adjust_s)
    end_floor = max(core_end_s, padded_end_s - loop_adjust_s)
    starts = {
        padded_start_s,
        *(
            frame.timestamp_s
            for frame in evidence.frames
            if padded_start_s - 1e-9 <= frame.timestamp_s <= start_ceiling + 1e-9
        ),
    }
    ends = {
        padded_end_s,
        *(
            frame.timestamp_s
            for frame in evidence.frames
            if end_floor - 1e-9 <= frame.timestamp_s <= padded_end_s + 1e-9
        ),
    }
    ranked: list[tuple[float, float, float, float]] = []
    for start_s in starts:
        for end_s in ends:
            if (
                start_s < safe_start_s - 1e-9
                or start_s > core_start_s + 1e-9
                or end_s < core_end_s - 1e-9
                or end_s > safe_end_s + 1e-9
                or end_s - start_s < config.min_duration_s - 1e-9
                or end_s - start_s > config.max_duration_s + 1e-9
            ):
                continue
            score = _loop_score(evidence, start_s, end_s)
            displacement = abs(start_s - padded_start_s) + abs(end_s - padded_end_s)
            ranked.append((-score, displacement, start_s, end_s))
    if not ranked:
        return padded_start_s, padded_end_s, _loop_score(
            evidence, padded_start_s, padded_end_s
        )
    negative_score, _, start_s, end_s = min(ranked)
    return start_s, end_s, -negative_score


def finalize_action_analysis(
    analysis: ActionMotionAnalysis,
    evidence: TemporalEvidence,
    safe_start_s: float,
    safe_end_s: float,
    anchor_ts_s: float,
    selected_candidate_index: int | None,
    config: ActionBoundaryConfig | Mapping[str, Any] | None,
) -> ActionBoundaryResult:
    """Validate CV output and apply the final 2--20 second action policy."""
    parsed_config = (
        config
        if isinstance(config, ActionBoundaryConfig)
        else ActionBoundaryConfig.from_mapping(config, strict=False)
    )
    _validate_finite_analysis(
        analysis, evidence, safe_start_s, safe_end_s, anchor_ts_s
    )
    if (
        selected_candidate_index is not None
        and (
            isinstance(selected_candidate_index, bool)
            or not isinstance(selected_candidate_index, int)
        )
    ):
        raise ValueError("selected_candidate_index must be an integer or None")
    diagnostics: dict[str, float | int | str | None] = {
        "selected_candidate_index": selected_candidate_index,
        "motion_type": analysis.motion_type,
    }
    selected = (
        analysis.candidates[selected_candidate_index]
        if analysis.motion_type == "subject_action"
        and selected_candidate_index is not None
        and 0 <= selected_candidate_index < len(analysis.candidates)
        else None
    )
    if selected is None:
        start_s, end_s = _biased_fixed_window(
            safe_start_s,
            safe_end_s,
            anchor_ts_s,
            parsed_config.preferred_max_duration_s,
        )
        segment = ActionSegment(start_s, end_s, anchor_ts_s, "fallback_fixed", False)
        return ActionBoundaryResult(
            "fallback_fixed",
            safe_start_s,
            safe_end_s,
            anchor_ts_s,
            analysis.candidates,
            (segment,),
            None,
            None,
            None,
            None,
            analysis.confidence,
            None,
            None,
            False,
            analysis.motion_type,
            parsed_config.analysis_version,
            _ImmutableDiagnostics(diagnostics),
            analysis.analysis_error,
        )

    padded_start_s, padded_end_s = _padded_window(
        selected, safe_start_s, safe_end_s, parsed_config
    )
    completeness = _clamp_score(
        0.25 * selected.start_settle
        + 0.30 * selected.end_settle
        + 0.20 * selected.peak_inclusion
        + 0.15 * selected.boundary_quiet
        + 0.10 * 0.5
    )
    if selected.end_s - selected.start_s > parsed_config.max_duration_s + 1e-9:
        split_windows = _split_at_stable_valleys(
            analysis,
            padded_start_s,
            padded_end_s,
            parsed_config.min_duration_s,
            parsed_config.max_duration_s,
        )
        if split_windows is None:
            start_s, end_s = _biased_fixed_window(
                safe_start_s,
                safe_end_s,
                anchor_ts_s,
                parsed_config.max_duration_s,
            )
            return ActionBoundaryResult(
                "fallback_fixed",
                safe_start_s,
                safe_end_s,
                anchor_ts_s,
                analysis.candidates,
                (ActionSegment(start_s, end_s, anchor_ts_s, "fallback_fixed", False),),
                selected.start_s,
                selected.peak_s,
                selected.end_s,
                None,
                selected.confidence,
                None,
                None,
                False,
                "long_action_split_fallback",
                parsed_config.analysis_version,
                _ImmutableDiagnostics(diagnostics),
                analysis.analysis_error,
            )
        segments = tuple(
            ActionSegment(
                start_s,
                end_s,
                _segment_peak(analysis, start_s, end_s, selected.peak_s),
                "stable_motion_valley",
                False,
            )
            for start_s, end_s in split_windows
        )
        return ActionBoundaryResult(
            "split_action",
            safe_start_s,
            safe_end_s,
            anchor_ts_s,
            analysis.candidates,
            segments,
            selected.start_s,
            selected.peak_s,
            selected.end_s,
            completeness,
            selected.confidence,
            None,
            "stable_motion_valley",
            False,
            None,
            parsed_config.analysis_version,
            _ImmutableDiagnostics(diagnostics),
            analysis.analysis_error,
        )

    adjusted_start_s, adjusted_end_s, loop_quality = _adjust_loop_endpoints(
        evidence.slice(safe_start_s, safe_end_s).resample(parsed_config.scan_fps),
        padded_start_s,
        padded_end_s,
        selected.start_s,
        selected.end_s,
        safe_start_s,
        safe_end_s,
        parsed_config,
    )
    segment = ActionSegment(
        adjusted_start_s,
        adjusted_end_s,
        selected.peak_s,
        "complete_action",
        False,
    )
    return ActionBoundaryResult(
        "complete_action",
        safe_start_s,
        safe_end_s,
        anchor_ts_s,
        analysis.candidates,
        (segment,),
        selected.start_s,
        selected.peak_s,
        selected.end_s,
        completeness,
        selected.confidence,
        loop_quality,
        None,
        False,
        None,
        parsed_config.analysis_version,
        _ImmutableDiagnostics(diagnostics),
        analysis.analysis_error,
    )

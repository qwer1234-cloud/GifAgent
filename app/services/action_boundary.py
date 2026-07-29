"""Motion-compensated CV action analysis and boundary proposals."""

from __future__ import annotations

from dataclasses import dataclass, fields
import math
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
            "min_duration_s",
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

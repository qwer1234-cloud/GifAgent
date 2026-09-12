"""Source video curve features extraction, versioning, and compatibility validation.

Provides versioned source-domain curve feature extraction contracts (curve-source-v1)
without pulling heavy ML/inference libraries into the main app process.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

CURVE_SOURCE_FEATURE_VERSION = "curve-source-v1"

CURVE_SOURCE_RUNTIME_PROTOCOL_VERSION = "curve-source-runtime-v1"


@dataclass(frozen=True)
class CurveWindow:
    """One snap-frozen candidate window on a source video."""

    preexport_key: str
    start_us: int
    end_us: int


@dataclass(frozen=True)
class CurveFeatureResult:
    """Strict per-window source curve feature outcome."""

    preexport_key: str
    status: str
    feature_version: str
    extractor_sha256: str
    features: dict[str, float] | None
    actual_timestamps: tuple[float, ...]
    input_hash: str
    missing_reasons: tuple[str, ...]
    wall_clock_ms: float

CURVE_SOURCE_FEATURE_NAMES = (
    "pose_valid_ratio",
    "subject_area_ratio",
    "extension_mean",
    "extension_peak",
    "extension_range",
    "torso_speed_p95",
    "motion_smoothness",
    "visibility_ratio",
    "border_clip_ratio",
    "verticality_mean",
)

SOURCE_DECODE_PROFILE_V1 = {
    "fps": 8,
    "max_width": 320,
    "max_height": 320,
    "color_format": "rgb24",
}

DEFAULT_SOURCE_EXTRACTOR_SHA256 = hashlib.sha256(
    json.dumps(
        {
            "version": CURVE_SOURCE_FEATURE_VERSION,
            "feature_names": CURVE_SOURCE_FEATURE_NAMES,
            "decode_profile": SOURCE_DECODE_PROFILE_V1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


def validate_feature_compatibility(
    model_meta: Mapping[str, Any],
    features_meta: Mapping[str, Any],
) -> list[str]:
    """Validate whether curve features are compatible with a given model version/meta.

    Both sides must carry a complete extractor identity; a missing identity on
    either side is incompatible, never silently accepted.
    """
    reasons: list[str] = []

    model_ver = model_meta.get("feature_version")
    feat_ver = features_meta.get("feature_version")
    if not model_ver or not feat_ver or model_ver != feat_ver:
        reasons.append("feature_version_mismatch")

    model_extractor = model_meta.get("extractor_sha256")
    feat_extractor = features_meta.get("extractor_sha256")
    if not model_extractor or not feat_extractor:
        reasons.append("extractor_sha256_missing")
    elif model_extractor != feat_extractor:
        reasons.append("extractor_sha256_mismatch")

    return reasons


def validate_window_feature_values(
    features: Mapping[str, Any] | None,
    actual_timestamps: list[float] | tuple[float, ...],
    window: CurveWindow | None = None,
) -> list[str]:
    """Strictly validate an AVAILABLE feature payload.

    All ten feature names must be present with finite, non-boolean numeric
    values, and at least three real timestamps must fall inside the window.
    """
    reasons: list[str] = []

    if features is None or frozenset(features) != frozenset(CURVE_SOURCE_FEATURE_NAMES):
        reasons.append("feature_set_incomplete")
        return reasons

    for name in CURVE_SOURCE_FEATURE_NAMES:
        value = features[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            reasons.append("feature_value_invalid")
            break

    timestamps = list(actual_timestamps)
    if len(timestamps) < 3:
        reasons.append("insufficient_actual_timestamps")
    for ts in timestamps:
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
            reasons.append("timestamp_invalid")
            break
        if window is not None:
            start_s = window.start_us / 1_000_000.0
            end_s = window.end_us / 1_000_000.0
            if ts < start_s - 1e-6 or ts > end_s + 1e-6:
                reasons.append("timestamp_outside_window")
                break

    return reasons


def window_input_hash(
    source_sha256: str,
    window: CurveWindow,
    feature_version: str = CURVE_SOURCE_FEATURE_VERSION,
    decode_profile: Mapping[str, Any] | None = None,
    extractor_sha256: str = DEFAULT_SOURCE_EXTRACTOR_SHA256,
) -> str:
    """Canonical input hash for one extraction window; echoed by the worker."""
    payload = {
        "source_sha256": source_sha256,
        "preexport_key": window.preexport_key,
        "start_us": window.start_us,
        "end_us": window.end_us,
        "feature_version": feature_version,
        "decode_profile": dict(
            SOURCE_DECODE_PROFILE_V1 if decode_profile is None else decode_profile
        ),
        "extractor_sha256": extractor_sha256,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def format_source_feature_response(
    *,
    status: str,
    features: dict[str, float] | None = None,
    actual_timestamps: list[float] | None = None,
    missing_reasons: list[str] | None = None,
    input_hash: str = "",
    wall_clock_ms: float = 0.0,
    extractor_sha256: str = DEFAULT_SOURCE_EXTRACTOR_SHA256,
    feature_version: str = CURVE_SOURCE_FEATURE_VERSION,
) -> dict[str, Any]:
    """Format a standardized source curve feature response payload.

    An AVAILABLE response is guarded: missing, non-finite, boolean, or
    incomplete feature sets raise instead of being zero-filled into a fake
    AVAILABLE payload.
    """
    if status == "AVAILABLE":
        reasons = validate_window_feature_values(features, actual_timestamps or [])
        if reasons:
            raise ValueError(f"feature_set_incomplete: {reasons}")
        assert features is not None  # validate_window_feature_values guaranteed it
        clean_features = {
            name: round(float(features[name]), 6)
            for name in CURVE_SOURCE_FEATURE_NAMES
        }
    else:
        clean_features = None

    return {
        "status": status,
        "feature_version": feature_version,
        "extractor_sha256": extractor_sha256,
        "features": clean_features,
        "actual_timestamps": list(actual_timestamps or []),
        "missing_reasons": list(missing_reasons or []),
        "input_hash": input_hash,
        "wall_clock_ms": round(float(wall_clock_ms), 2),
    }


def extract_source_features(request: dict[str, Any]) -> dict[str, Any]:
    """Validate request and extract or dispatch source curve feature extraction."""
    t0 = time.perf_counter()
    source_path = request.get("source_path") or request.get("source_video_path")
    source_sha = request.get("source_sha256") or request.get("source_video_sha256")
    start_s = request.get("start_s")
    end_s = request.get("end_s")
    feature_ver = request.get("feature_version", CURVE_SOURCE_FEATURE_VERSION)

    input_bytes = json.dumps(
        {
            "source_sha256": source_sha or "",
            "start_s": start_s,
            "end_s": end_s,
            "feature_version": feature_ver,
            "profile": SOURCE_DECODE_PROFILE_V1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    input_hash = hashlib.sha256(input_bytes).hexdigest()

    missing_reasons: list[str] = []
    if not source_path:
        missing_reasons.append("source_path_missing")
    else:
        path_obj = Path(source_path)
        if not path_obj.is_file():
            missing_reasons.append(f"file_not_found: {source_path}")

    if start_s is None or end_s is None:
        missing_reasons.append("window_bounds_missing")
    elif isinstance(start_s, (int, float)) and isinstance(end_s, (int, float)) and end_s <= start_s:
        missing_reasons.append("invalid_window_bounds")

    if feature_ver != CURVE_SOURCE_FEATURE_VERSION:
        missing_reasons.append(f"unsupported_feature_version: {feature_ver}")

    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    if missing_reasons:
        return format_source_feature_response(
            status="UNAVAILABLE",
            features=None,
            actual_timestamps=[],
            missing_reasons=missing_reasons,
            input_hash=input_hash,
            wall_clock_ms=elapsed_ms,
        )

    runtime = request.get("runtime")
    if runtime is not None:
        assert start_s is not None and end_s is not None
        window = CurveWindow(
            preexport_key=str(request.get("preexport_key") or input_hash),
            start_us=int(round(float(start_s) * 1_000_000)),
            end_us=int(round(float(end_s) * 1_000_000)),
        )
        results = runtime.evaluate_video(
            Path(str(source_path)),
            str(source_sha),
            (window,),
            float(request.get("timeout_s") or 120.0),
        )
        result = results[0]
        return {
            "status": result.status,
            "feature_version": result.feature_version,
            "extractor_sha256": result.extractor_sha256,
            "features": result.features,
            "actual_timestamps": list(result.actual_timestamps),
            "missing_reasons": list(result.missing_reasons),
            "input_hash": result.input_hash,
            "wall_clock_ms": round(float(result.wall_clock_ms), 2),
        }

    # Main-app fallback: without a configured out-of-process runtime the
    # extraction is explicitly UNAVAILABLE, never a synthetic payload.
    return format_source_feature_response(
        status="UNAVAILABLE",
        features=None,
        actual_timestamps=[],
        missing_reasons=["inference_runtime_unavailable"],
        input_hash=input_hash,
        wall_clock_ms=elapsed_ms,
    )

"""Canonical action-completeness configuration freezing."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping

from app.services.action_boundary import ActionBoundaryConfig


def freeze_action_config(
    adaptive: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], str]:
    """Validate, normalize, and hash the action-only adaptive subset."""
    values = adaptive or {}
    action_values = {
        "action_guard_enabled": values.get("action_guard_enabled", True),
        "action_vlm_verify_enabled": values.get(
            "action_vlm_verify_enabled", True
        ),
        "action_analysis_version": values.get("action_analysis_version", 1),
        "action_analysis_window_s": values.get(
            "action_analysis_window_s", 30.0
        ),
        "action_preferred_min_duration_s": values.get(
            "action_preferred_min_duration_s", 4.0
        ),
        "action_min_duration_s": values.get("min_duration", 2.0),
        "action_max_duration_s": values.get("max_duration", 20.0),
        "action_scan_fps": values.get("action_scan_fps", 4.0),
        "action_boundary_confidence_threshold": values.get(
            "action_boundary_confidence_threshold", 0.65
        ),
        "action_loop_adjust_s": values.get("action_loop_adjust_s", 0.75),
        "action_vlm_min_worthiness": values.get(
            "action_vlm_min_worthiness", 0.60
        ),
        "action_fallback_mode": values.get(
            "action_fallback_mode", "fixed_window"
        ),
    }
    try:
        minimum_duration = float(action_values["action_min_duration_s"])
        maximum_duration = float(action_values["action_max_duration_s"])
        if not math.isfinite(minimum_duration) or not math.isfinite(
            maximum_duration
        ):
            raise ValueError
    except (TypeError, ValueError):
        minimum_duration, maximum_duration = 2.0, 20.0
    if "action_preferred_min_duration_s" not in values:
        action_values["action_preferred_min_duration_s"] = max(
            minimum_duration, min(4.0, maximum_duration)
        )
    if "action_preferred_max_duration_s" in values:
        action_values["action_preferred_max_duration_s"] = values[
            "action_preferred_max_duration_s"
        ]
    else:
        action_values["action_preferred_max_duration_s"] = min(
            12.0, maximum_duration
        )
    try:
        config = ActionBoundaryConfig.from_mapping(action_values, strict=True)
    except ValueError as exc:
        key_aliases = {
            "preferred_min_duration_s": "action_preferred_min_duration_s",
            "preferred_max_duration_s": "action_preferred_max_duration_s",
            "analysis_window_s": "action_analysis_window_s",
            "min_duration_s": "min_duration",
            "max_duration_s": "max_duration",
            "analysis_version": "action_analysis_version",
            "scan_fps": "action_scan_fps",
            "boundary_confidence_threshold": (
                "action_boundary_confidence_threshold"
            ),
            "loop_adjust_s": "action_loop_adjust_s",
            "vlm_min_worthiness": "action_vlm_min_worthiness",
            "fallback_mode": "action_fallback_mode",
        }
        for internal_name, external_name in key_aliases.items():
            if internal_name in str(exc):
                raise ValueError(f"{external_name}: {exc}") from None
        raise

    normalized = {
        "min_duration": config.min_duration_s,
        "max_duration": config.max_duration_s,
        "action_guard_enabled": config.enabled,
        "action_vlm_verify_enabled": config.vlm_verify_enabled,
        "action_analysis_version": config.analysis_version,
        "action_analysis_window_s": config.analysis_window_s,
        "action_preferred_min_duration_s": config.preferred_min_duration_s,
        "action_preferred_max_duration_s": config.preferred_max_duration_s,
        "action_scan_fps": config.scan_fps,
        "action_boundary_confidence_threshold": (
            config.boundary_confidence_threshold
        ),
        "action_loop_adjust_s": config.loop_adjust_s,
        "action_vlm_min_worthiness": config.vlm_min_worthiness,
        "action_fallback_mode": config.fallback_mode,
    }
    action_hash = hashlib.sha256(
        json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return normalized, action_hash

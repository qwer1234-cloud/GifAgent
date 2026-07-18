"""Shared config building for Quality Lab experiments.

Provides ``build_task_config`` which deep-merges experiment overrides
with a base config and freezes the result for use by the task engine.
"""

from __future__ import annotations

import json
from copy import deepcopy

from app.task_engine.fingerprints import canonical_hash


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*.

    - Nested dicts are merged recursively.
    - Lists and scalars are replaced entirely by the override value.
    - ``None`` values in the override delete the key from the result.
    """
    result = deepcopy(base)
    for key, value in override.items():
        if value is None:
            result.pop(key, None)
        elif (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def compute_business_config_hash(config: dict) -> str:
    """Canonical ``config_hash`` over the business config.

    Excludes runtime metadata: keys starting with ``_`` (e.g.
    ``_experiment``, ``_task``) and ``config_hash`` itself.  This is the
    single source of truth shared by the task router (``POST
    /api/tasks/jobs``) and the Quality Lab ``build_task_config`` so that a
    task job and its originating experiment config produce identical
    hashes when their business config matches (fourth-review §8.5).
    """
    business = {
        k: v for k, v in config.items()
        if not k.startswith("_") and k != "config_hash"
    }
    return canonical_hash(business)


def build_task_config(
    *,
    base_config: dict,
    experiment_overrides: dict,
    video_paths: list[str],
    experiment_metadata: dict,
) -> dict:
    """Build the final frozen task config for a Quality Lab experiment item.

    1. Deep-merge experiment overrides into the base config.
    2. Add video_paths and _experiment metadata.
    3. Compute a canonical config_hash from the business config (excluding
       runtime metadata like run_id, item_id, config_hash itself).
    4. Return the frozen config dict ready for use as a task job config_json.

    The config uses a flat pipeline structure at the top level (e.g.,
    ``adaptive``, ``vlm``, ``models``) — no ``config_snapshot`` nesting.
    """
    # Merge experiment overrides into the pipeline config.
    merged = deep_merge(base_config, experiment_overrides)

    # Add metadata.
    merged["video_paths"] = list(video_paths)
    merged["_experiment"] = dict(experiment_metadata)

    # Compute config_hash from business config only (exclude runtime metadata).
    merged["config_hash"] = compute_business_config_hash(merged)

    return merged


# ---------------------------------------------------------------------------
# Phase 3: Unified config normalization
# ---------------------------------------------------------------------------


def normalize_task_config(raw_config: dict) -> dict:
    """Normalize a task config to the unified top-level business format.

    Handles both new-format (top-level business config) and historical
    format (``config_snapshot`` wrapper).

    Rules:
    1. If ``config_snapshot`` exists, use it as the base config.
    2. Top-level business keys (adaptive, preference_memory, vlm, models,
       video_paths) override the snapshot.
    3. ``_task`` metadata (limit, extensions) is extracted.
    4. P1-1: Preserves metadata keys: ``_experiment``, ``config_hash``,
       ``task_work_dir``, ``export_base_dir``.

    Returns a unified dict with business config at the top level.

    This is the single entry point used by Worker, Quality Lab, and
    stage scripts.  No other module should implement its own config
    unpacking logic.
    """
    # Start with config_snapshot if present (historical format).
    snapshot = raw_config.get("config_snapshot") or {}
    if isinstance(snapshot, dict) and snapshot:
        config = deepcopy(snapshot)
    else:
        config = {}

    # Deep-merge top-level business keys over the snapshot base.
    business_keys = {
        "adaptive", "preference_memory", "vlm", "models",
        "video_paths",
    }
    for key in business_keys:
        if key in raw_config:
            value = raw_config[key]
            if isinstance(config.get(key), dict) and isinstance(value, dict):
                config[key] = deep_merge(config[key], value)
            elif value is not None:
                config[key] = deepcopy(value)

    # P1-1: Explicitly preserve metadata keys that must survive
    # normalization (needed by Worker, Quality Lab, and stage scripts).
    _preserved_meta = {"_experiment", "config_hash", "task_work_dir", "export_base_dir"}
    for key in _preserved_meta:
        if key in raw_config:
            config[key] = deepcopy(raw_config[key])

    # Also merge any extra top-level keys that are not metadata.
    metadata_keys = {"config_snapshot", "limit", "extensions", "_task",
                     "config_hash", "_experiment", "task_work_dir",
                     "export_base_dir"}
    for key, value in raw_config.items():
        if key in metadata_keys or key in business_keys:
            continue
        if key not in config:
            config[key] = deepcopy(value)

    # Extract _task metadata if present.
    task_meta = raw_config.get("_task") or {}
    if "limit" in raw_config and "limit" not in task_meta:
        config["_task_limit"] = raw_config["limit"]
    if "extensions" in raw_config and "extensions" not in task_meta:
        config["_task_extensions"] = raw_config["extensions"]
    if task_meta:
        config["_task"] = dict(task_meta)

    return config

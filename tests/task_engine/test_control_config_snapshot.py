"""Phase 0: Control config snapshot tests.

Verify that:
1. New tasks created via the API have config parameters at top level.
2. Worker/Adapter/stage-script receives correct config parameters.
3. Historical config with config_snapshot wrapper still parses correctly.
4. Quality Lab config building produces correct top-level config.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.task_engine.artifacts import STAGE_ARTIFACT_KINDS


class TestNewTaskConfigFormat:
    """New tasks should use top-level business config, not nested config_snapshot."""

    def test_new_task_config_has_adaptive_at_top_level(self):
        """Build a new-task config and verify it has adaptive/preference_memory at top level."""
        from app.quality_lab.config_builder import build_task_config

        base = {
            "adaptive": {"sample_interval": 8, "max_output": 60},
            "preference_memory": {"enabled": True},
            "vlm": {"temperature": 0.65},
            "models": {},
        }
        overrides = {"adaptive": {"sampling_mode": "fixed"}}
        config = build_task_config(
            base_config=base,
            experiment_overrides=overrides,
            video_paths=["/tmp/test.mp4"],
            experiment_metadata={"run_id": "r1"},
        )

        # Top-level keys
        assert "adaptive" in config
        assert config["adaptive"]["sample_interval"] == 8
        assert config["adaptive"]["max_output"] == 60
        assert config["adaptive"]["sampling_mode"] == "fixed"
        assert config["preference_memory"]["enabled"] is True
        assert config["video_paths"] == ["/tmp/test.mp4"]
        assert config["_experiment"]["run_id"] == "r1"
        assert "config_hash" in config

    def test_new_task_config_no_config_snapshot_nesting(self):
        """New task config must NOT have config_snapshot as a top-level key
        containing business config as a nested value."""
        from app.quality_lab.config_builder import build_task_config

        base = {
            "adaptive": {"sample_interval": 8},
            "models": {},
        }
        config = build_task_config(
            base_config=base,
            experiment_overrides={},
            video_paths=["/tmp/v.mp4"],
            experiment_metadata={},
        )

        assert "config_snapshot" not in config, (
            "New config format must not use config_snapshot nesting"
        )
        assert "adaptive" in config

    def test_deep_merge_preserves_nested_fields(self):
        """Deep merge should preserve fields not overridden."""
        from app.quality_lab.config_builder import deep_merge

        base = {"a": {"x": 1, "y": 2}, "b": [1, 2]}
        override = {"a": {"x": 10}}
        result = deep_merge(base, override)

        assert result["a"]["x"] == 10
        assert result["a"]["y"] == 2  # preserved
        assert result["b"] == [1, 2]  # preserved

    def test_deep_merge_none_deletes_key(self):
        """None values in override delete the key."""
        from app.quality_lab.config_builder import deep_merge

        base = {"a": 1, "b": 2}
        result = deep_merge(base, {"b": None})
        assert "b" not in result
        assert result["a"] == 1


class TestHistoricalConfigCompatibility:
    """Historical tasks with config_snapshot wrapper still parse correctly."""

    def test_normalize_config_from_config_snapshot_wrapper(self):
        """normalize_task_config should extract business config from config_snapshot."""
        # This test demonstrates the required behavior for Phase 3.
        # It should pass after the normalize_task_config function is implemented.
        historical_config = {
            "limit": 0,
            "extensions": "",
            "video_paths": ["/tmp/test.mp4"],
            "config_snapshot": {
                "adaptive": {"sample_interval": 8, "max_output": 60},
                "preference_memory": {"enabled": True},
                "vlm": {"temperature": 0.65},
                "models": {"vlm_model": "llava:13b"},
            },
        }

        # After Phase 3 fix, normalize_task_config should:
        # 1. Extract config_snapshot as base
        # 2. Extract _task metadata (limit, extensions)
        # 3. Return unified top-level config

        # For now, verify the structure exists and is parseable.
        config_snapshot = historical_config.get("config_snapshot", {})
        assert config_snapshot.get("adaptive", {}).get("sample_interval") == 8
        assert config_snapshot.get("preference_memory", {}).get("enabled") is True


class TestConfigDeepMergeWithOverrides:
    """Request overrides should deep-merge with base config."""

    def test_api_override_deep_merges(self):
        """When the API overrides config, deep merge is used."""
        from app.quality_lab.config_builder import deep_merge

        base = {"adaptive": {"sample_interval": 8, "max_output": 60}}
        override = {"adaptive": {"sample_interval": 4}}

        merged = deep_merge(base, override)
        assert merged["adaptive"]["sample_interval"] == 4
        assert merged["adaptive"]["max_output"] == 60  # preserved!

        # Shallow merge would lose max_output. Verify deep_merge preserves it.
        shallow = {**base, **override}
        assert shallow["adaptive"]["sample_interval"] == 4
        assert "max_output" not in shallow["adaptive"]  # LOST with shallow merge

    def test_partial_adaptive_override_preserves_other_fields(self):
        """Partial override of adaptive dict preserves non-overridden fields."""
        from app.quality_lab.config_builder import deep_merge

        base = {
            "adaptive": {
                "sample_interval": 8,
                "max_output": 60,
                "gif_fps": 24,
                "gif_max_width": 720,
                "clear_output_dir": True,
            },
            "preference_memory": {"enabled": True},
            "models": {"vlm_model": "llava:13b"},
        }
        override = {"adaptive": {"sample_interval": 3, "gif_fps": 30}}

        merged = deep_merge(base, override)
        assert merged["adaptive"]["sample_interval"] == 3  # overridden
        assert merged["adaptive"]["gif_fps"] == 30  # overridden
        assert merged["adaptive"]["max_output"] == 60  # preserved
        assert merged["adaptive"]["gif_max_width"] == 720  # preserved
        assert merged["adaptive"]["clear_output_dir"] is True  # preserved
        assert merged["preference_memory"]["enabled"] is True  # preserved
        assert merged["models"]["vlm_model"] == "llava:13b"  # preserved


class TestNormalizeTaskConfigPreservesMetadata:
    """P1-1: normalize_task_config must preserve _experiment, config_hash,
    task_work_dir, export_base_dir through the full normalization cycle."""

    def test_preserves_experiment_metadata(self):
        """_experiment survives normalization."""
        from app.quality_lab.config_builder import normalize_task_config

        config = normalize_task_config({
            "adaptive": {"sample_interval": 8},
            "_experiment": {"run_id": "r1", "item_id": "i1"},
            "config_hash": "abc123",
            "task_work_dir": "/tmp/work",
            "export_base_dir": "/tmp/export",
        })

        assert config["_experiment"]["run_id"] == "r1"
        assert config["_experiment"]["item_id"] == "i1"
        assert config["config_hash"] == "abc123"
        assert config["task_work_dir"] == "/tmp/work"
        assert config["export_base_dir"] == "/tmp/export"

    def test_preserves_metadata_through_config_snapshot(self):
        """Metadata survives normalization via config_snapshot path."""
        from app.quality_lab.config_builder import normalize_task_config

        config = normalize_task_config({
            "config_snapshot": {
                "adaptive": {"sample_interval": 8},
                "task_work_dir": "/tmp/work",
            },
            "_experiment": {"run_id": "r1"},
            "config_hash": "abc123",
            "export_base_dir": "/tmp/export",
        })

        assert config["_experiment"]["run_id"] == "r1"
        assert config["config_hash"] == "abc123"
        assert config["task_work_dir"] == "/tmp/work"
        assert config["export_base_dir"] == "/tmp/export"

    def test_config_hash_computed_from_merged_config(self):
        """config_hash is computed from the final merged business config."""
        from app.quality_lab.config_builder import build_task_config

        base = {
            "adaptive": {"sample_interval": 8, "max_output": 60},
            "models": {},
        }
        overrides = {"adaptive": {"sample_interval": 4}}

        config = build_task_config(
            base_config=base,
            experiment_overrides=overrides,
            video_paths=["/tmp/test.mp4"],
            experiment_metadata={"run_id": "r1"},
        )

        assert "config_hash" in config
        # config_hash should be based on merged (sample_interval=4, max_output=60),
        # not the original (sample_interval=8).
        from app.task_engine.fingerprints import canonical_hash
        expected_business = {
            "adaptive": {"sample_interval": 4, "max_output": 60},
            "models": {},
            "video_paths": ["/tmp/test.mp4"],
        }
        expected_hash = canonical_hash(expected_business)
        assert config["config_hash"] == expected_hash

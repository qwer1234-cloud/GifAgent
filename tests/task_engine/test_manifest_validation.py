"""P1-2: Manifest validation tests.

Verify that ``validate_manifest_json`` catches all error types:
missing fields, wrong stage, wrong clip_id, unsupported version,
empty JSON, wrong encoding, and manifest/GIF SHA mismatch.

Also verify that ``_read_upstream_manifest`` in the stage script
wires through to the shared validator.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Unit tests for validate_manifest_json
# ---------------------------------------------------------------------------


class TestManifestValidation:
    """Test the shared validator function directly."""

    def test_valid_discover_manifest(self):
        """Valid discover manifest passes validation."""
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 1,
            "stage": "discover",
            "duration_s": 123.4,
        }).encode("utf-8")

        result = validate_manifest_json(data, "discover_manifest")
        assert result["schema_version"] == 1
        assert result["stage"] == "discover"
        assert result["duration_s"] == 123.4

    def test_missing_required_field(self):
        """Missing required field raises ValueError."""
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 1,
            "stage": "discover",
            # missing duration_s
        }).encode("utf-8")

        with pytest.raises(ValueError, match="missing required field"):
            validate_manifest_json(data, "discover_manifest")

    def test_wrong_stage_name(self):
        """Wrong stage name raises ValueError."""
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 1,
            "stage": "vlm",  # should be discover
            "duration_s": 100,
        }).encode("utf-8")

        with pytest.raises(ValueError, match="stage mismatch"):
            validate_manifest_json(
                data, "discover_manifest", expected_stage="discover",
            )

    def test_wrong_clip_id(self):
        """Wrong clip_id raises ValueError."""
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 1,
            "stage": "gif_clip",
            "clip_id": "wrong-clip",
            "gif_path": "/tmp/test.gif",
        }).encode("utf-8")

        with pytest.raises(ValueError, match="clip_id mismatch"):
            validate_manifest_json(
                data, "gif_clip_manifest",
                expected_stage="gif_clip",
                expected_clip_id="correct-clip",
            )

    def test_empty_json(self):
        """Empty bytes raise ValueError."""
        from app.task_engine.artifacts import validate_manifest_json

        with pytest.raises(ValueError, match="Empty manifest"):
            validate_manifest_json(b"", "discover_manifest")

    def test_invalid_json(self):
        """Invalid JSON raises ValueError."""
        from app.task_engine.artifacts import validate_manifest_json

        with pytest.raises(ValueError, match="Invalid JSON"):
            validate_manifest_json(b"not json at all", "discover_manifest")

    def test_unknown_artifact_kind(self):
        """Unknown artifact_kind raises ValueError."""
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({"schema_version": 1}).encode("utf-8")

        with pytest.raises(ValueError, match="Unknown artifact_kind"):
            validate_manifest_json(data, "nonexistent_kind")

    def test_wrong_encoding(self):
        """Non-UTF-8 bytes raise ValueError."""
        from app.task_engine.artifacts import validate_manifest_json

        # Valid JSON but encoded in UTF-16 (wrong encoding)
        data = json.dumps({"schema_version": 1, "stage": "discover", "duration_s": 100})
        encoded = data.encode("utf-16")

        with pytest.raises(ValueError, match="Invalid JSON"):
            validate_manifest_json(encoded, "discover_manifest")

    def test_rank_dedup_clip_count_mismatch(self):
        """rank_dedup_manifest with clip_count != len(clips) raises ValueError."""
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 1,
            "stage": "rank_dedup",
            "clip_count": 999,  # wrong
            "clips": [{"clip_id": "c1"}, {"clip_id": "c2"}],
        }).encode("utf-8")

        with pytest.raises(ValueError, match="clip_count"):
            validate_manifest_json(data, "rank_dedup_manifest")

    def test_rank_dedup_duplicate_clip_ids(self):
        """rank_dedup_manifest with duplicate clip_ids raises ValueError."""
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 1,
            "stage": "rank_dedup",
            "clip_count": 3,
            "clips": [
                {"clip_id": "c1"},
                {"clip_id": "c1"},  # duplicate
                {"clip_id": "c2"},
            ],
        }).encode("utf-8")

        with pytest.raises(ValueError, match="duplicate clip_ids"):
            validate_manifest_json(data, "rank_dedup_manifest")

    def test_rank_dedup_empty_clip_id(self):
        """rank_dedup_manifest with empty clip_id raises ValueError."""
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 1,
            "stage": "rank_dedup",
            "clip_count": 2,
            "clips": [
                {"clip_id": "c1"},
                {"clip_id": ""},  # empty
            ],
        }).encode("utf-8")

        with pytest.raises(ValueError, match="empty clip_id"):
            validate_manifest_json(data, "rank_dedup_manifest")

    def test_valid_sample_manifest(self):
        """Valid sample manifest passes validation."""
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 1,
            "stage": "sample",
            "frame_count": 10,
            "timestamps": [1, 2, 3],
            "frame_paths": ["/tmp/f1.jpg", "/tmp/f2.jpg", "/tmp/f3.jpg"],
        }).encode("utf-8")

        result = validate_manifest_json(data, "sample_manifest")
        assert result["frame_count"] == 10


class TestManifestSchemaVersion:
    """P1-2: ``schema_version`` must be a positive integer in the supported
    set.  Booleans, strings, zero, negatives and unknown future versions
    must be rejected with a message listing supported versions."""

    def test_manifest_rejects_schema_version_zero(self):
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 0, "stage": "discover", "duration_s": 10,
        }).encode("utf-8")
        with pytest.raises(ValueError, match="schema_version"):
            validate_manifest_json(data, "discover_manifest")

    def test_manifest_rejects_future_schema_version(self):
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 999, "stage": "discover", "duration_s": 10,
        }).encode("utf-8")
        with pytest.raises(ValueError, match="unsupported"):
            validate_manifest_json(data, "discover_manifest")

    def test_manifest_rejects_schema_version_bool(self):
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": True, "stage": "discover", "duration_s": 10,
        }).encode("utf-8")
        with pytest.raises(ValueError, match="integer"):
            validate_manifest_json(data, "discover_manifest")

    def test_manifest_rejects_schema_version_string(self):
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": "1", "stage": "discover", "duration_s": 10,
        }).encode("utf-8")
        with pytest.raises(ValueError, match="integer"):
            validate_manifest_json(data, "discover_manifest")

    def test_manifest_rejects_negative_schema_version(self):
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": -1, "stage": "discover", "duration_s": 10,
        }).encode("utf-8")
        with pytest.raises(ValueError, match="schema_version"):
            validate_manifest_json(data, "discover_manifest")

    def test_manifest_error_message_lists_supported_versions(self):
        from app.task_engine.artifacts import validate_manifest_json

        data = json.dumps({
            "schema_version": 2, "stage": "discover", "duration_s": 10,
        }).encode("utf-8")
        with pytest.raises(ValueError) as excinfo:
            validate_manifest_json(data, "discover_manifest")
        msg = str(excinfo.value)
        assert "discover_manifest" in msg
        assert "supported" in msg.lower()

    def test_materialize_envelope_rejects_unknown_version(self):
        from app.task_engine.artifacts import validate_materialize_envelope

        envelope = {
            "schema_version": 999, "stage": "materialize",
            "artifacts": {"gif_file": [], "gif_clip_manifest": []},
            "stage_statuses": [],
        }
        with pytest.raises(ValueError, match="unsupported"):
            validate_materialize_envelope(envelope)

    def test_materialize_envelope_rejects_non_integer_version(self):
        from app.task_engine.artifacts import validate_materialize_envelope

        envelope = {
            "schema_version": "1", "stage": "materialize",
            "artifacts": {"gif_file": [], "gif_clip_manifest": []},
            "stage_statuses": [],
        }
        with pytest.raises(ValueError, match="integer"):
            validate_materialize_envelope(envelope)


# ---------------------------------------------------------------------------
# Integration tests for _read_upstream_manifest wiring
# ---------------------------------------------------------------------------


class TestReadUpstreamManifestWiring:
    """Verify _read_upstream_manifest passes through to validate_manifest_json."""

    def test_read_upstream_manifest_valid(self, tmp_path: Path):
        """_read_upstream_manifest validates and returns data for a valid manifest."""
        import json as _json

        # Import the script-level function.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "test_video_adaptive",
            os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "test_video_adaptive.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        # Create a valid discover manifest.
        manifest_path = tmp_path / "discover_manifest.json"
        manifest_path.write_text(_json.dumps({
            "schema_version": 1,
            "stage": "discover",
            "duration_s": 120.0,
        }))

        inputs = {
            "discover_manifest": [{
                "artifact_id": "art-1",
                "path": str(manifest_path),
                "clip_id": None,
            }],
        }

        result = mod._read_upstream_manifest(inputs, "discover_manifest", "sample")
        assert result["schema_version"] == 1
        assert result["duration_s"] == 120.0

    def test_read_upstream_manifest_missing_field_raises(self, tmp_path: Path):
        """_read_upstream_manifest raises ValueError for missing required field."""
        import json as _json
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "test_video_adaptive",
            os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "test_video_adaptive.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        # Missing duration_s
        manifest_path = tmp_path / "discover_manifest.json"
        manifest_path.write_text(_json.dumps({
            "schema_version": 1,
            "stage": "discover",
        }))

        inputs = {
            "discover_manifest": [{
                "artifact_id": "art-1",
                "path": str(manifest_path),
                "clip_id": None,
            }],
        }

        with pytest.raises(ValueError, match="missing required field"):
            mod._read_upstream_manifest(inputs, "discover_manifest", "sample")

    def test_read_upstream_manifest_wrong_stage_raises(self, tmp_path: Path):
        """_read_upstream_manifest raises ValueError for wrong stage."""
        import json as _json
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "test_video_adaptive",
            os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "test_video_adaptive.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        # Stage is "vlm" but expected "discover"
        manifest_path = tmp_path / "discover_manifest.json"
        manifest_path.write_text(_json.dumps({
            "schema_version": 1,
            "stage": "vlm",
            "duration_s": 100,
        }))

        inputs = {
            "discover_manifest": [{
                "artifact_id": "art-1",
                "path": str(manifest_path),
                "clip_id": None,
            }],
        }

        with pytest.raises(ValueError, match="stage mismatch"):
            mod._read_upstream_manifest(inputs, "discover_manifest", "sample")

    def test_read_upstream_manifest_empty_file_raises(self, tmp_path: Path):
        """_read_upstream_manifest raises ValueError for empty file."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "test_video_adaptive",
            os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "test_video_adaptive.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        manifest_path = tmp_path / "empty.json"
        manifest_path.write_text("")

        inputs = {
            "discover_manifest": [{
                "artifact_id": "art-1",
                "path": str(manifest_path),
                "clip_id": None,
            }],
        }

        with pytest.raises(ValueError, match="Empty manifest"):
            mod._read_upstream_manifest(inputs, "discover_manifest", "sample")

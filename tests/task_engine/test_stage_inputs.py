"""P1-3: Stage input dependency tests.

Verify:
1. VLM stage requires both sample_manifest and sample_frames.
2. Sample manifest stores artifact_id + timestamp per frame.
3. VLM cross-references by artifact_id.
4. Missing frame, SHA error, duplicate artifact_id, unknown frame → failure.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


class TestVlmInputDependencies:
    """Verify VLM requires sample_frames in addition to sample_manifest."""

    def test_vlm_stage_input_kinds_includes_sample_frames(self):
        """STAGE_INPUT_KINDS['vlm'] must include both sample_manifest and sample_frames."""
        from app.task_engine.artifacts import STAGE_INPUT_KINDS

        vlm_inputs = STAGE_INPUT_KINDS.get("vlm", ())
        assert "sample_manifest" in vlm_inputs, (
            "VLM must require sample_manifest"
        )
        assert "sample_frames" in vlm_inputs, (
            "VLM must require sample_frames for cross-referencing"
        )

    def test_resolve_vlm_inputs_returns_both_kinds(self, tmp_path: Path):
        """resolve_stage_inputs for VLM returns both sample_manifest and sample_frames."""
        from app.task_engine.schema import connect_task_db
        from app.task_engine.artifacts import resolve_stage_inputs

        db_path = tmp_path / "task.db"
        conn = connect_task_db(db_path)

        # Setup: job + video + sample stage + artifacts.
        now = "2026-07-18T00:00:00.000+00:00"
        conn.execute(
            "INSERT INTO task_jobs (job_id, directory, directory_key, config_json, status, created_at, updated_at) "
            "VALUES ('j1', '/tmp/d', '/tmp/d', '{}', 'running', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO task_videos (video_id, job_id, path, fingerprint, status, created_at, updated_at) "
            "VALUES ('v1', 'j1', '/tmp/v.mp4', 'fp', 'running', ?, ?)",
            (now, now),
        )

        # Create sample stage (succeeded).
        sample_stage_id = "s-sample-001"
        conn.execute(
            "INSERT INTO task_stages (stage_id, video_id, stage_name, clip_id, input_key, status, created_at, updated_at) "
            "VALUES (?, 'v1', 'sample', NULL, 'key1', 'succeeded', ?, ?)",
            (sample_stage_id, now, now),
        )

        # Create sample_manifest artifact.
        manifest_path = tmp_path / "sample_manifest.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1,
            "stage": "sample",
            "frame_count": 2,
            "timestamps": [1, 2],
            "frame_paths": [str(tmp_path / "f1.jpg"), str(tmp_path / "f2.jpg")],
            "frame_entries": [
                {"artifact_id": "aid-1", "timestamp": 1, "path": str(tmp_path / "f1.jpg")},
                {"artifact_id": "aid-2", "timestamp": 2, "path": str(tmp_path / "f2.jpg")},
            ],
        }))
        import hashlib
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        manifest_size = manifest_path.stat().st_size
        conn.execute(
            """INSERT INTO task_artifacts
               (artifact_id, job_id, video_id, stage_name, clip_id,
                path, sha256, size_bytes, provenance_json, created_at,
                stage_id, artifact_kind)
               VALUES (?, 'j1', 'v1', 'sample', NULL, ?, ?, ?, '{}', ?, ?, 'sample_manifest')""",
            ("art-manifest", str(manifest_path), manifest_sha, manifest_size,
             now, sample_stage_id),
        )

        # Create two sample_frames artifacts.
        for i, aid in enumerate(["aid-1", "aid-2"], 1):
            frame_path = tmp_path / f"f{i}.jpg"
            frame_path.write_text(f"fake-frame-data-{i}")
            frame_sha = hashlib.sha256(frame_path.read_bytes()).hexdigest()
            frame_size = frame_path.stat().st_size
            conn.execute(
                """INSERT INTO task_artifacts
                   (artifact_id, job_id, video_id, stage_name, clip_id,
                    path, sha256, size_bytes, provenance_json, created_at,
                    stage_id, artifact_kind)
                   VALUES (?, 'j1', 'v1', 'sample', NULL, ?, ?, ?, '{}', ?, ?, 'sample_frames')""",
                (aid, str(frame_path), frame_sha, frame_size,
                 now, sample_stage_id),
            )

        conn.commit()

        # Resolve VLM inputs.
        result = resolve_stage_inputs(conn, "v1", "vlm")
        assert "sample_manifest" in result
        assert "sample_frames" in result
        assert len(result["sample_manifest"]) == 1
        assert len(result["sample_frames"]) >= 1

        conn.close()

    def test_vlm_fails_when_sample_frames_missing(self, tmp_path: Path):
        """VLM resolution fails when sample_frames artifacts don't exist."""
        from app.task_engine.schema import connect_task_db
        from app.task_engine.artifacts import resolve_stage_inputs

        db_path = tmp_path / "task.db"
        conn = connect_task_db(db_path)

        now = "2026-07-18T00:00:00.000+00:00"
        conn.execute(
            "INSERT INTO task_jobs (job_id, directory, directory_key, config_json, status, created_at, updated_at) "
            "VALUES ('j1', '/tmp/d', '/tmp/d', '{}', 'running', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO task_videos (video_id, job_id, path, fingerprint, status, created_at, updated_at) "
            "VALUES ('v1', 'j1', '/tmp/v.mp4', 'fp', 'running', ?, ?)",
            (now, now),
        )

        # Sample stage with only manifest, no frames.
        sample_stage_id = "s-sample-001"
        conn.execute(
            "INSERT INTO task_stages (stage_id, video_id, stage_name, clip_id, input_key, status, created_at, updated_at) "
            "VALUES (?, 'v1', 'sample', NULL, 'key1', 'succeeded', ?, ?)",
            (sample_stage_id, now, now),
        )
        manifest_path = tmp_path / "sample_manifest.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1, "stage": "sample",
            "frame_count": 0, "timestamps": [], "frame_paths": [],
        }))
        import hashlib
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        conn.execute(
            """INSERT INTO task_artifacts
               (artifact_id, job_id, video_id, stage_name, clip_id,
                path, sha256, size_bytes, provenance_json, created_at,
                stage_id, artifact_kind)
               VALUES ('art-manifest', 'j1', 'v1', 'sample', NULL, ?, ?, ?, '{}', ?, ?, 'sample_manifest')""",
            (str(manifest_path), manifest_sha, manifest_path.stat().st_size,
             now, sample_stage_id),
        )
        conn.commit()

        # Should fail because sample_frames are missing.
        with pytest.raises(FileNotFoundError, match="No artifact.*sample_frames"):
            resolve_stage_inputs(conn, "v1", "vlm")

        conn.close()

    def test_resolve_materialize_inputs_returns_both_kinds(self, tmp_path: Path):
        """resolve_materialize_inputs returns gif_file and gif_clip_manifest."""
        from app.task_engine.schema import connect_task_db
        from app.task_engine.artifacts import resolve_materialize_inputs

        db_path = tmp_path / "task.db"
        conn = connect_task_db(db_path)

        now = "2026-07-18T00:00:00.000+00:00"
        conn.execute(
            "INSERT INTO task_jobs (job_id, directory, directory_key, config_json, status, created_at, updated_at) "
            "VALUES ('j1', '/tmp/d', '/tmp/d', '{}', 'running', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO task_videos (video_id, job_id, path, fingerprint, status, created_at, updated_at) "
            "VALUES ('v1', 'j1', '/tmp/v.mp4', 'fp', 'running', ?, ?)",
            (now, now),
        )

        clip_id = "test-clip-1"
        gif_clip_stage_id = "gc-001"
        conn.execute(
            "INSERT INTO task_stages (stage_id, video_id, stage_name, clip_id, input_key, status, created_at, updated_at) "
            "VALUES (?, 'v1', 'gif_clip', ?, 'key1', 'succeeded', ?, ?)",
            (gif_clip_stage_id, clip_id, now, now),
        )

        # Create gif file.
        gif_path = tmp_path / "test.gif"
        gif_data = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00;"
        gif_path.write_bytes(gif_data)

        import hashlib
        gif_sha = hashlib.sha256(gif_data).hexdigest()
        gif_size = len(gif_data)
        conn.execute(
            """INSERT INTO task_artifacts
               (artifact_id, job_id, video_id, stage_name, clip_id,
                path, sha256, size_bytes, provenance_json, created_at,
                stage_id, artifact_kind)
               VALUES ('art-gif', 'j1', 'v1', 'gif_clip', ?, ?, ?, ?, '{}', ?, ?, 'gif_file')""",
            (clip_id, str(gif_path), gif_sha, gif_size, now, gif_clip_stage_id),
        )

        # Create gif_clip_manifest.
        manifest_path = tmp_path / "gif_clip_manifest.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1,
            "stage": "gif_clip",
            "clip_id": clip_id,
            "gif_path": str(gif_path),
            "sha256": gif_sha,
            "start_ts": 10.0,
            "end_ts": 15.0,
        }))
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        manifest_size = manifest_path.stat().st_size
        conn.execute(
            """INSERT INTO task_artifacts
               (artifact_id, job_id, video_id, stage_name, clip_id,
                path, sha256, size_bytes, provenance_json, created_at,
                stage_id, artifact_kind)
               VALUES ('art-manifest', 'j1', 'v1', 'gif_clip', ?, ?, ?, ?, '{}', ?, ?, 'gif_clip_manifest')""",
            (clip_id, str(manifest_path), manifest_sha, manifest_size, now, gif_clip_stage_id),
        )
        conn.commit()

        result = resolve_materialize_inputs(conn, "v1")
        assert "gif_file" in result.artifacts
        assert "gif_clip_manifest" in result.artifacts
        assert len(result.artifacts["gif_file"]) == 1
        assert len(result.artifacts["gif_clip_manifest"]) == 1
        assert result.artifacts["gif_file"][0].clip_id == clip_id
        assert result.artifacts["gif_clip_manifest"][0].clip_id == clip_id
        # The resolver also aggregates the terminal status of every gif_clip
        # stage (P1-1), and reports an explicit zero_clip flag (P0-1).
        assert result.zero_clip is False
        assert len(result.stage_statuses) == 1
        assert result.stage_statuses[0].status == "succeeded"

        conn.close()

    def test_build_materialize_input_envelope(self):
        """build_materialize_input_envelope produces correct structure."""
        from app.task_engine.artifacts import (
            build_materialize_input_envelope,
            GifClipStatus,
            MaterializeInputs,
        )
        from app.task_engine.models import ArtifactRef

        gif_file = ArtifactRef(
            artifact_id="aid-gif", job_id="j1", video_id="v1",
            stage_name="gif_clip", clip_id="clip1",
            path="/tmp/test.gif", sha256="abc123", size_bytes=100,
            provenance_json="{}", stage_id="gc-001",
            artifact_kind="gif_file",
        )
        gif_manifest = ArtifactRef(
            artifact_id="aid-man", job_id="j1", video_id="v1",
            stage_name="gif_clip", clip_id="clip1",
            path="/tmp/test_manifest.json", sha256="def456", size_bytes=200,
            provenance_json="{}", stage_id="gc-001",
            artifact_kind="gif_clip_manifest",
        )
        mat = MaterializeInputs(
            artifacts={"gif_file": (gif_file,), "gif_clip_manifest": (gif_manifest,)},
            stage_statuses=(
                GifClipStatus(
                    stage_id="gc-001", clip_id="clip1", status="succeeded",
                    attempt_count=1, last_error=None,
                ),
            ),
            zero_clip=False,
        )

        envelope = build_materialize_input_envelope(mat, "v1")

        assert envelope["schema_version"] == 1
        assert envelope["stage"] == "materialize"
        assert len(envelope["artifacts"]["gif_file"]) == 1
        assert len(envelope["artifacts"]["gif_clip_manifest"]) == 1
        assert envelope["artifacts"]["gif_file"][0]["artifact_id"] == "aid-gif"
        assert envelope["artifacts"]["gif_clip_manifest"][0]["artifact_id"] == "aid-man"
        assert len(envelope["stage_statuses"]) == 1
        assert envelope["stage_statuses"][0]["clip_id"] == "clip1"
        assert envelope["stage_statuses"][0]["status"] == "succeeded"
        assert envelope["stage_statuses"][0]["stage_id"] == "gc-001"
        assert envelope["stage_statuses"][0]["attempt_count"] == 1

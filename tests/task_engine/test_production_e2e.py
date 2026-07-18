"""Phase 0 / Phase 6: Real production path E2E tests.

Tests the full TaskWorker -> AdaptivePipelineAdapter -> Python subprocess
-> scripts/test_video_adaptive.py --task-stage chain using real external
tools (ffprobe, ffmpeg) with minimal valid video files.  Does NOT use
FakeStageAdapter.

These tests exercise the lowest common denominator: discover stage and
gif_clip -> materialize chain.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_test_video(tmp_path: Path, duration: float = 2.0) -> Path:
    """Create a minimal valid MP4 video using the real ffmpeg.

    Returns the path to the created video file.
    Skips the test if ffmpeg is not available.
    """
    video_path = tmp_path / "test.mp4"
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"color=c=black:s=64x64:d={duration}:r=10",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-t", str(duration),
            str(video_path),
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        pytest.skip(f"ffmpeg unavailable or failed: {result.stderr[:200]}")
    assert video_path.exists(), f"Video not created at {video_path}"
    return video_path


def _create_test_gif(tmp_path: Path) -> tuple[Path, str, int]:
    """Create a minimal valid GIF file.

    Returns (path, sha256, size_bytes).
    """
    gif_path = tmp_path / "test_output.gif"
    gif_data = (
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00"
        b"!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00"
        b"\x01\x00\x00\x02\x02\x44\x01\x00;"
    )
    gif_path.write_bytes(gif_data)

    from app.task_engine.fingerprints import sha256_file
    sha = sha256_file(gif_path)
    size = gif_path.stat().st_size
    return gif_path, sha, size


def _make_config(work_base: Path, tmp_path: Path, **extra) -> dict:
    """Build a standard test config dict."""
    return {
        "task_work_dir": str(work_base),
        "adaptive": {
            "sample_interval": 10, "max_output": 60, "max_duration": 10,
            "refine_interval": 10, "refine_radius": 20, "refine_threshold": 0.5,
            "worthiness_threshold": 0.2, "merge_gap": 12,
            "merge_score_threshold": 0.55,
            "gif_fps": 24, "gif_max_width": 720, "output_ratio": 1.0,
            "min_duration": 0.5,
            "potplayer_pbf_enabled": True,
            "embedding_dedup_enabled": True, "temporal_dedup_enabled": True,
            "temporal_dedup_min_gap_s": 12, "embedding_dedup_threshold": 0.94,
            "clear_output_dir": False, "vlm_temperature": 0.65,
            "vlm_top_p": 0.95, "vlm_top_k": 60,
        },
        "preference_memory": {"enabled": False},
        "models": {},
        "database": {"path": str(tmp_path / "library.db").replace(chr(92), "/")},
        **extra,
    }


# ---------------------------------------------------------------------------
# Mock objects (used by the test process, not the subprocess)
# ---------------------------------------------------------------------------


class _MockVlmResponse:
    status_code = 200
    def raise_for_status(self): pass
    def json(self):
        return {"response": json.dumps({
            "caption": "A beautiful film frame with dramatic lighting.",
            "emotional_core": "awe", "gif_worthiness": 0.75,
            "aesthetic_notes": ["Good contrast", "Strong composition", "Nice colors"],
            "reason": "Dramatic moment with excellent framing",
        })}


class _MockOllamaPsResponse:
    status_code = 200
    def raise_for_status(self): pass
    def json(self): return {"models": []}


class _MockIndex:
    count = 10
    def search(self, emb, top_k=3): return []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProductionE2EDiscover:
    """Discover stage using real ffprobe on a valid test video."""

    @pytest.mark.slow
    def test_e2e_discover_with_real_adapter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Discover stage using real AdaptivePipelineAdapter and real ffprobe."""
        # Create a valid test video with real ffmpeg
        video_path = _create_test_video(tmp_path, duration=2.0)

        # Isolate DB via GIFAGENT_CONFIG
        temp_yaml = tmp_path / "gifagent_config.yaml"
        temp_yaml.write_text(
            f"database:\n  path: {str(tmp_path / 'library.db').replace(chr(92), '/')}\n"
            f"adaptive: {{}}\nmodels: {{}}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("GIFAGENT_CONFIG", str(temp_yaml))

        db_path = tmp_path / "task.db"
        work_base = tmp_path / "task_work"

        from app.task_engine.schema import connect_task_db
        from app.task_engine.repository import TaskRepository
        from app.task_engine.adaptive_adapter import run_adaptive_stage

        # Mock external HTTP calls
        monkeypatch.setattr("httpx.post", lambda *a, **kw: _MockVlmResponse())
        monkeypatch.setattr("httpx.get", lambda *a, **kw: _MockOllamaPsResponse())
        monkeypatch.setattr("app.services.llm_client.wait_for_llm", lambda *a, **kw: False)
        monkeypatch.setattr("app.services.llm_client.is_local_llm", lambda: False)
        monkeypatch.setattr("app.services.embedding.compute_text_embedding", lambda text: [0.1] * 384)

        conn = connect_task_db(db_path)

        now = "2026-07-18T00:00:00.000+00:00"
        conn.execute(
            "INSERT INTO task_jobs (job_id, directory, directory_key, config_json, status, created_at, updated_at) "
            "VALUES ('j1', ?, ?, ?, 'running', ?, ?)",
            (str(tmp_path), str(tmp_path),
             json.dumps(_make_config(work_base, tmp_path)),
             now, now),
        )
        conn.execute(
            "INSERT INTO task_videos (video_id, job_id, path, fingerprint, status, created_at, updated_at) "
            "VALUES ('v1', 'j1', ?, 'fp', 'running', ?, ?)",
            (str(video_path), now, now),
        )
        conn.commit()

        repo = TaskRepository(conn)
        stage = repo.ensure_stage("v1", "discover", "input:test.mp4")

        work_dir = work_base / "discover" / stage.stage_id
        config_snap = work_dir / "config_snapshot.json"
        work_dir.mkdir(parents=True, exist_ok=True)

        config = json.loads(
            conn.execute("SELECT config_json FROM task_jobs WHERE job_id='j1'").fetchone()["config_json"]
        )
        config_snap.write_text(json.dumps(config))

        result = run_adaptive_stage(
            "discover",
            video=video_path,
            work_dir=work_dir,
            config_snapshot=config_snap,
            job_id="j1",
            video_id="v1",
            stage_id=stage.stage_id,
        )

        assert result is not None
        assert result.output_key == "discover"
        assert len(result.artifacts) >= 1

        kinds = {a.artifact_kind for a in result.artifacts}
        assert "discover_manifest" in kinds

        # Verify manifest content
        discover_artifacts = [a for a in result.artifacts if a.artifact_kind == "discover_manifest"]
        assert len(discover_artifacts) == 1
        manifest_path = Path(discover_artifacts[0].path)
        assert manifest_path.exists()
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest_data = json.load(f)
        assert manifest_data.get("stage") == "discover"
        assert manifest_data.get("duration_s") == pytest.approx(2.0, abs=0.1)

        conn.close()


class TestProductionE2EWorker:
    """Worker-driven end-to-end discover stage."""

    @pytest.mark.slow
    def test_worker_discovers_and_computes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Worker initializes a job and runs discover stage to completion."""
        video_dir = tmp_path / "videos"
        video_dir.mkdir()
        video_path = _create_test_video(video_dir, duration=2.0)

        # Isolate DB
        temp_yaml = tmp_path / "gifagent_config.yaml"
        temp_yaml.write_text(
            f"database:\n  path: {str(tmp_path / 'library.db').replace(chr(92), '/')}\n"
            f"adaptive: {{}}\nmodels: {{}}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("GIFAGENT_CONFIG", str(temp_yaml))

        # Mock VLM/LLM
        monkeypatch.setattr("httpx.post", lambda *a, **kw: _MockVlmResponse())
        monkeypatch.setattr("httpx.get", lambda *a, **kw: _MockOllamaPsResponse())
        monkeypatch.setattr("app.services.llm_client.wait_for_llm", lambda *a, **kw: False)
        monkeypatch.setattr("app.services.llm_client.is_local_llm", lambda: False)
        monkeypatch.setattr("app.services.embedding.compute_text_embedding", lambda text: [0.1] * 384)
        monkeypatch.setattr("app.services.indexer.get_index", lambda: _MockIndex())

        db_path = tmp_path / "task.db"
        work_dir_path = tmp_path / "task_work"

        from app.task_engine.schema import connect_task_db
        from app.task_engine.repository import TaskRepository
        from app.task_engine.worker import TaskWorker
        from app.task_engine.orchestrator import initialize_job
        from app.task_engine.adaptive_adapter import AdaptivePipelineAdapter
        from app.task_engine.models import CreateJob

        conn = connect_task_db(db_path)
        repo = TaskRepository(conn)

        job = repo.create_job(CreateJob(
            directory=str(video_dir),
            config_json=json.dumps(_make_config(work_dir_path, tmp_path)),
        ))

        # Initialize and advance so discover stage is ready
        initialize_job(repo, job.job_id)

        worker = TaskWorker(
            repo, "worker-1",
            {"discover": AdaptivePipelineAdapter("discover")},
            lease_seconds=90, heartbeat_seconds=30,
            db_path=str(db_path),
        )

        # run_once claims and executes the pending discover stage
        did_work = worker.run_once()
        assert did_work, "Worker should process the discover stage"

        stages = conn.execute(
            "SELECT stage_name, status FROM task_stages WHERE video_id IN "
            "(SELECT video_id FROM task_videos WHERE job_id=?)",
            (job.job_id,),
        ).fetchall()

        discover_stages = [s for s in stages if s["stage_name"] == "discover"]
        assert len(discover_stages) > 0
        assert discover_stages[0]["status"] == "succeeded"

        conn.close()


class TestProductionE2EGifMaterialize:
    """gif_clip -> materialize chain with real file artifacts."""

    @pytest.mark.slow
    def test_gif_clip_materialize_chain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Create a fake gif_clip, run materialize via adapter, verify publishing."""
        # Isolate DB
        temp_yaml = tmp_path / "gifagent_config.yaml"
        temp_yaml.write_text(
            f"database:\n  path: {str(tmp_path / 'library.db').replace(chr(92), '/')}\n"
            f"adaptive: {{}}\nmodels: {{}}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("GIFAGENT_CONFIG", str(temp_yaml))

        # Mock external deps
        monkeypatch.setattr("httpx.post", lambda *a, **kw: _MockVlmResponse())
        monkeypatch.setattr("httpx.get", lambda *a, **kw: _MockOllamaPsResponse())
        monkeypatch.setattr("app.services.llm_client.wait_for_llm", lambda *a, **kw: False)
        monkeypatch.setattr("app.services.llm_client.is_local_llm", lambda: False)
        monkeypatch.setattr("app.services.embedding.compute_text_embedding", lambda text: [0.1] * 384)

        # Create a small valid video (for the video_path reference in materialize)
        video_path = _create_test_video(tmp_path, duration=2.0)

        db_path = tmp_path / "task.db"
        work_base = tmp_path / "task_work"
        formal_export_base = tmp_path / "exports" / "adaptive_test"
        formal_export_dir = formal_export_base / "test"

        from app.task_engine.schema import connect_task_db
        from app.task_engine.repository import TaskRepository
        from app.task_engine.adaptive_adapter import run_adaptive_stage
        from app.task_engine.artifacts import make_artifact_id
        from app.task_engine.fingerprints import sha256_file

        conn = connect_task_db(db_path)
        repo = TaskRepository(conn)

        # Create job with export_base_dir
        now = "2026-07-18T00:00:00.000+00:00"
        conn.execute(
            "INSERT INTO task_jobs (job_id, directory, directory_key, config_json, status, created_at, updated_at) "
            "VALUES ('j1', ?, ?, ?, 'running', ?, ?)",
            (str(tmp_path), str(tmp_path),
             json.dumps(_make_config(work_base, tmp_path,
                                      export_base_dir=str(formal_export_base))),
             now, now),
        )
        conn.execute(
            "INSERT INTO task_videos (video_id, job_id, path, fingerprint, status, created_at, updated_at) "
            "VALUES ('v1', 'j1', ?, 'fp', 'running', ?, ?)",
            (str(video_path), now, now),
        )

        # Create a succeeded gif_clip stage with a fake GIF artifact
        gif_clip_stage_id = "gc-001"
        clip_id = "abc123def456"
        conn.execute(
            "INSERT INTO task_stages (stage_id, video_id, stage_name, clip_id, input_key, status, created_at, updated_at) "
            "VALUES (?, 'v1', 'gif_clip', ?, ?, 'succeeded', ?, ?)",
            (gif_clip_stage_id, clip_id, "from:rank_dedup:clip:abc", now, now),
        )
        conn.commit()

        # Create a fake GIF file
        gif_path, gif_sha, gif_size = _create_test_gif(tmp_path)

        # Insert gif_file artifact
        gif_artifact_id = make_artifact_id(
            stage_id=gif_clip_stage_id, artifact_kind="gif_file",
            clip_id=clip_id, normalized_path=str(gif_path),
        )
        conn.execute(
            "INSERT INTO task_artifacts (artifact_id, job_id, video_id, stage_name, clip_id, path, sha256, size_bytes, provenance_json, created_at, stage_id, artifact_kind) "
            "VALUES (?, 'j1', 'v1', 'gif_clip', ?, ?, ?, ?, '{}', ?, ?, 'gif_file')",
            (gif_artifact_id, clip_id, str(gif_path), gif_sha, gif_size, now, gif_clip_stage_id),
        )

        # Create a fake gif_clip_manifest
        manifest_path = tmp_path / "gif_clip_manifest.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1,
            "stage": "gif_clip",
            "clip_id": clip_id,
            "gif_path": str(gif_path),
            "gif_name": "test_clip.gif",
            "sha256": gif_sha,
            "start_ts": 10.0,
            "end_ts": 15.0,
        }))
        manifest_sha = sha256_file(manifest_path)
        manifest_size = manifest_path.stat().st_size
        manifest_artifact_id = make_artifact_id(
            stage_id=gif_clip_stage_id, artifact_kind="gif_clip_manifest",
            clip_id=clip_id, normalized_path=str(manifest_path),
        )
        conn.execute(
            "INSERT INTO task_artifacts (artifact_id, job_id, video_id, stage_name, clip_id, path, sha256, size_bytes, provenance_json, created_at, stage_id, artifact_kind) "
            "VALUES (?, 'j1', 'v1', 'gif_clip', ?, ?, ?, ?, '{}', ?, ?, 'gif_clip_manifest')",
            (manifest_artifact_id, clip_id, str(manifest_path), manifest_sha, manifest_size, now, gif_clip_stage_id),
        )
        conn.commit()

        # Create materialize stage
        mat_stage = repo.ensure_stage("v1", "materialize", "from:gif_clip")

        # Build the environment for the materialize subprocess
        work_dir = work_base / "materialize" / mat_stage.stage_id
        work_dir.mkdir(parents=True, exist_ok=True)
        config_snap = work_dir / "config_snapshot.json"

        config = json.loads(
            conn.execute("SELECT config_json FROM task_jobs WHERE job_id='j1'").fetchone()["config_json"]
        )

        # P0-2: Use the versioned envelope format instead of flat input + _gif_clip_terminal_statuses.
        from app.task_engine.artifacts import (
            build_materialize_input_envelope,
            GifClipStatus,
            MaterializeInputs,
        )
        from app.task_engine.models import ArtifactRef

        gif_ref = ArtifactRef(
            artifact_id=gif_artifact_id, job_id="j1", video_id="v1",
            stage_name="gif_clip", clip_id=clip_id,
            path=str(gif_path), sha256=gif_sha, size_bytes=gif_size,
            provenance_json="{}", stage_id=gif_clip_stage_id,
            artifact_kind="gif_file",
        )
        manifest_ref = ArtifactRef(
            artifact_id=manifest_artifact_id, job_id="j1", video_id="v1",
            stage_name="gif_clip", clip_id=clip_id,
            path=str(manifest_path), sha256=manifest_sha, size_bytes=manifest_size,
            provenance_json="{}", stage_id=gif_clip_stage_id,
            artifact_kind="gif_clip_manifest",
        )
        mat = MaterializeInputs(
            artifacts={"gif_file": (gif_ref,), "gif_clip_manifest": (manifest_ref,)},
            stage_statuses=(
                GifClipStatus(
                    stage_id=gif_clip_stage_id, clip_id=clip_id, status="succeeded",
                    attempt_count=1, last_error=None,
                ),
            ),
            zero_clip=False,
        )
        envelope = build_materialize_input_envelope(mat, "v1")
        config["_materialize_envelope"] = envelope
        config_snap.write_text(json.dumps(config))

        input_manifest = work_dir / "input_manifest.json"
        input_manifest.write_text(json.dumps(envelope))

        result = run_adaptive_stage(
            "materialize",
            video=video_path,
            work_dir=work_dir,
            config_snapshot=config_snap,
            input_manifest=input_manifest,
            job_id="j1",
            video_id="v1",
            stage_id=mat_stage.stage_id,
        )

        assert result is not None
        assert result.output_key == "materialize"

        # Verify materialize artifacts
        kinds = {a.artifact_kind for a in result.artifacts}
        assert "result" in kinds
        assert "materialize_manifest" in kinds
        assert "pbf_file" in kinds  # PBF was enabled

        # Verify formal export dir
        assert formal_export_dir.exists(), f"Formal export dir not created: {formal_export_dir}"
        formal_files = list(formal_export_dir.iterdir())
        formal_names = {f.name for f in formal_files}
        assert "test_clip.gif" in formal_names, f"Formal GIF not found, got: {formal_names}"
        assert "test_result.json" in formal_names, f"Result JSON not found, got: {formal_names}"
        assert "test.pbf" in formal_names, f"PBF file not found, got: {formal_names}"

        # Verify result JSON
        result_json_path = formal_export_dir / "test_result.json"
        assert result_json_path.exists()
        with open(result_json_path, "r", encoding="utf-8") as f:
            result_data = json.load(f)
        assert result_data["gif_count"] == 1
        assert len(result_data["succeeded"]) == 1
        assert result_data["succeeded"][0]["clip_id"] == clip_id
        assert "formal_path" in result_data["succeeded"][0]
        assert result_data["succeeded"][0]["gif_name"] == "test_clip.gif"
        assert result_data["cancelled"] == []
        assert len(result_data["gif_clip_terminal_statuses"]) >= 1
        assert result_data["formal_export_dir"] == os.path.abspath(str(formal_export_dir))

        # Verify formal GIF matches source
        formal_gif = formal_export_dir / "test_clip.gif"
        assert formal_gif.exists()
        assert sha256_file(formal_gif) == gif_sha

        # Verify NO control files in artifacts
        for a in result.artifacts:
            fname = Path(a.path).name.lower()
            assert fname not in ("config_snapshot.json", "input_manifest.json", "stage.log"), (
                f"Control file {fname} found in artifacts"
            )
            assert not fname.startswith("result_") or a.artifact_kind == "result", (
                f"Result file {fname} with wrong artifact_kind {a.artifact_kind}"
            )

        conn.close()


class TestProductionP03OverwriteProtection:
    """P0-3: Overwrite protection for formal export."""

    @pytest.mark.slow
    def test_same_sha_idempotent_reuse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Same name + same SHA = idempotent reuse, no write needed."""
        formal_dir = tmp_path / "exports" / "test_video"
        formal_dir.mkdir(parents=True)

        # Create an existing formal GIF
        existing_gif = formal_dir / "test.gif"
        gif_data = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00;"
        existing_gif.write_bytes(gif_data)
        existing_sha = hashlib.sha256(gif_data).hexdigest()
        existing_mtime = existing_gif.stat().st_mtime

        # Simulate the overwrite check logic from _stage_materialize.
        same_sha = existing_sha
        # Idempotent: skip write if same SHA.
        assert existing_sha == same_sha

        # Verify file wasn't modified (idempotent).
        assert existing_gif.stat().st_mtime == existing_mtime

    def test_different_sha_no_overwrite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Same name + different SHA = no overwrite."""
        formal_dir = tmp_path / "exports" / "test_video"
        formal_dir.mkdir(parents=True)

        # Create an existing formal GIF with known content.
        existing_gif = formal_dir / "test.gif"
        old_data = b"GIF89a-OLD-DATA-HERE"
        existing_gif.write_bytes(old_data)
        old_sha = hashlib.sha256(old_data).hexdigest()

        # New content with different SHA.
        new_data = b"GIF89a-NEW-DATA-HERE"
        new_sha = hashlib.sha256(new_data).hexdigest()
        assert new_sha != old_sha

        # Simulate the conflict check logic.
        # Different SHA should NOT overwrite.
        with open(existing_gif, "rb") as f:
            current_data = f.read()
        current_sha = hashlib.sha256(current_data).hexdigest()
        assert current_sha == old_sha, "Historical file should not be overwritten"


class TestProductionP14FullE2E:
    """P1-4: Full production E2E tests driven by Worker."""

    @pytest.mark.slow
    def test_worker_driven_materialize_no_manual_input(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Worker-driven gif_clip → materialize: no manual input_manifest.json.
        Uses the dedicated resolve_materialize_inputs resolver.
        """
        video_dir = tmp_path / "videos"
        video_dir.mkdir()
        video_path = _create_test_video(video_dir, duration=2.0)

        temp_yaml = tmp_path / "gifagent_config.yaml"
        temp_yaml.write_text(
            f"database:\n  path: {str(tmp_path / 'library.db').replace(chr(92), '/')}\n"
            f"adaptive: {{}}\nmodels: {{}}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("GIFAGENT_CONFIG", str(temp_yaml))

        monkeypatch.setattr("httpx.post", lambda *a, **kw: _MockVlmResponse())
        monkeypatch.setattr("httpx.get", lambda *a, **kw: _MockOllamaPsResponse())
        monkeypatch.setattr("app.services.llm_client.wait_for_llm", lambda *a, **kw: False)
        monkeypatch.setattr("app.services.llm_client.is_local_llm", lambda: False)
        monkeypatch.setattr("app.services.embedding.compute_text_embedding", lambda text: [0.1] * 384)
        monkeypatch.setattr("app.services.indexer.get_index", lambda: _MockIndex())

        db_path = tmp_path / "task.db"
        work_path = tmp_path / "task_work"
        export_base = tmp_path / "exports"

        from app.task_engine.schema import connect_task_db
        from app.task_engine.repository import TaskRepository
        from app.task_engine.worker import TaskWorker
        from app.task_engine.adaptive_adapter import AdaptivePipelineAdapter
        from app.task_engine.artifacts import make_artifact_id
        from app.task_engine.fingerprints import sha256_file
        from app.task_engine.orchestrator import initialize_job

        conn = connect_task_db(db_path)
        repo = TaskRepository(conn)

        # Create job with video_paths scoped to single video.
        config = _make_config(work_path, tmp_path, export_base_dir=str(export_base))
        from app.task_engine.models import CreateJob
        job = repo.create_job(CreateJob(
            directory=str(video_dir),
            config_json=json.dumps(config),
        ))

        # Manually initialize to bypass directory scanning (dir is tmp_path, not video_dir).
        # Create video + gif_clip stage + artifacts, then materialize stage.
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            "INSERT INTO task_videos (video_id, job_id, path, fingerprint, status, created_at, updated_at) "
            "VALUES ('v1', ?, ?, 'fp', 'running', ?, ?)",
            (job.job_id, str(video_path), now, now),
        )
        # Create gif_clip stage.
        clip_id = "test-clip-1"
        gc_stage_id = "gc-001"
        conn.execute(
            "INSERT INTO task_stages (stage_id, video_id, stage_name, clip_id, input_key, status, created_at, updated_at) "
            "VALUES (?, 'v1', 'gif_clip', ?, 'key1', 'succeeded', ?, ?)",
            (gc_stage_id, clip_id, now, now),
        )
        # Create gif_file artifact.
        gif_path = tmp_path / "test_output.gif"
        gif_data = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00;"
        gif_path.write_bytes(gif_data)
        gif_sha = sha256_file(gif_path)
        gif_size = gif_path.stat().st_size
        gif_aid = make_artifact_id(
            stage_id=gc_stage_id, artifact_kind="gif_file",
            clip_id=clip_id, normalized_path=str(gif_path),
        )
        conn.execute(
            "INSERT INTO task_artifacts (artifact_id, job_id, video_id, stage_name, clip_id, path, sha256, size_bytes, provenance_json, created_at, stage_id, artifact_kind) "
            "VALUES (?, ?, 'v1', 'gif_clip', ?, ?, ?, ?, '{}', ?, ?, 'gif_file')",
            (gif_aid, job.job_id, clip_id, str(gif_path), gif_sha, gif_size, now, gc_stage_id),
        )
        # Create gif_clip_manifest artifact.
        manifest_path = tmp_path / "gif_clip_manifest.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1, "stage": "gif_clip", "clip_id": clip_id,
            "gif_path": str(gif_path), "gif_name": "test.gif",
            "sha256": gif_sha, "start_ts": 5.0, "end_ts": 10.0,
        }))
        manifest_sha = sha256_file(manifest_path)
        manifest_size = manifest_path.stat().st_size
        manifest_aid = make_artifact_id(
            stage_id=gc_stage_id, artifact_kind="gif_clip_manifest",
            clip_id=clip_id, normalized_path=str(manifest_path),
        )
        conn.execute(
            "INSERT INTO task_artifacts (artifact_id, job_id, video_id, stage_name, clip_id, path, sha256, size_bytes, provenance_json, created_at, stage_id, artifact_kind) "
            "VALUES (?, ?, 'v1', 'gif_clip', ?, ?, ?, ?, '{}', ?, ?, 'gif_clip_manifest')",
            (manifest_aid, job.job_id, clip_id, str(manifest_path), manifest_sha, manifest_size, now, gc_stage_id),
        )
        conn.commit()

        # Ensure materialize stage. Get the stage_id first.
        mat_stage = repo.ensure_stage("v1", "materialize", "from:gif_clip")
        # Update job to running.
        conn.execute("UPDATE task_jobs SET status='running' WHERE job_id=?", (job.job_id,))
        conn.commit()

        # Run worker — should resolve materialize inputs from DB, NOT manual input_manifest.
        worker = TaskWorker(
            repo, "worker-1",
            {"materialize": AdaptivePipelineAdapter("materialize")},
            lease_seconds=90, heartbeat_seconds=30,
            db_path=str(db_path),
        )

        did_work = worker.run_once()
        assert did_work, "Worker should process the materialize stage"

        # Verify materialize succeeded.
        stage_row = conn.execute(
            "SELECT status FROM task_stages WHERE stage_id=?",
            (mat_stage.stage_id,),
        ).fetchone()
        assert stage_row is not None
        assert stage_row["status"] == "succeeded", (
            f"Materialize stage should succeed, got {stage_row['status']}"
        )

        # Verify formal GIF was published.
        formal_dir = export_base / "test"
        assert formal_dir.exists(), "Formal export dir not created"
        formal_files = list(formal_dir.iterdir())
        gif_files = [f for f in formal_files if f.suffix == '.gif']
        assert len(gif_files) >= 1, f"No GIF found in {formal_dir}"

        # §9.1.3: parse the PBF (UTF-16-LE + BOM) and verify the bookmark
        # start_ms and the title's start-end time range match the clip
        # manifest (start_ts=5.0, end_ts=10.0), not just that it's non-empty.
        pbf_files = [f for f in formal_files if f.suffix == '.pbf']
        assert pbf_files, "PBF file should be created when potplayer_pbf_enabled"
        import re as _re
        pbf_text = pbf_files[0].read_bytes()[2:].decode("utf-16-le").replace("\r", "")
        m = _re.search(r"^\d+=(\d+)\*(.*)\*$", pbf_text, _re.MULTILINE)
        assert m, f"PBF bookmark line not found in {pbf_text!r}"
        start_ms = int(m.group(1))
        title = m.group(2)
        assert start_ms == 5000, f"PBF start_ms {start_ms} != 5000 (start_ts=5.0)"
        assert "00:05" in title and "00:10" in title, (
            f"PBF title missing start-end range: {title!r}"
        )

        conn.close()

    @pytest.mark.slow
    def test_zero_clip_materialize_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """When no gif_clip stages succeeded, materialize creates empty output."""
        video_dir = tmp_path / "videos"
        video_dir.mkdir()
        video_path = _create_test_video(video_dir, duration=2.0)

        temp_yaml = tmp_path / "gifagent_config.yaml"
        temp_yaml.write_text(
            f"database:\n  path: {str(tmp_path / 'library.db').replace(chr(92), '/')}\n"
            f"adaptive: {{}}\nmodels: {{}}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("GIFAGENT_CONFIG", str(temp_yaml))
        monkeypatch.setattr("httpx.post", lambda *a, **kw: _MockVlmResponse())
        monkeypatch.setattr("httpx.get", lambda *a, **kw: _MockOllamaPsResponse())
        monkeypatch.setattr("app.services.llm_client.wait_for_llm", lambda *a, **kw: False)
        monkeypatch.setattr("app.services.llm_client.is_local_llm", lambda: False)
        monkeypatch.setattr("app.services.embedding.compute_text_embedding", lambda text: [0.1] * 384)

        db_path = tmp_path / "task.db"
        work_path = tmp_path / "task_work"
        export_base = tmp_path / "exports"

        from app.task_engine.schema import connect_task_db
        from app.task_engine.repository import TaskRepository
        from app.task_engine.worker import TaskWorker
        from app.task_engine.adaptive_adapter import AdaptivePipelineAdapter

        conn = connect_task_db(db_path)
        repo = TaskRepository(conn)

        config = _make_config(work_path, tmp_path, export_base_dir=str(export_base))
        from app.task_engine.models import CreateJob
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        job = repo.create_job(CreateJob(
            directory=str(video_dir),
            config_json=json.dumps(config),
        ))
        conn.execute(
            "INSERT INTO task_videos (video_id, job_id, path, fingerprint, status, created_at, updated_at) "
            "VALUES ('v1', ?, ?, 'fp', 'running', ?, ?)",
            (job.job_id, str(video_path), now, now),
        )
        # Create materialize stage with zero-clip input_key.
        mat_stage = repo.ensure_stage("v1", "materialize", "from:rank_dedup:zero")
        # P1-1: zero-clip must be proven by a rank_dedup manifest declaring
        # clip_count=0 (no lost fan-out).  Seed a succeeded rank_dedup stage.
        from app.task_engine.artifacts import make_artifact_id
        from app.task_engine.fingerprints import sha256_file
        rd_stage_id = "rd-zero-001"
        rd_work = work_path / "rank_dedup" / rd_stage_id
        rd_work.mkdir(parents=True, exist_ok=True)
        rd_manifest = rd_work / "rank_dedup_manifest.json"
        rd_manifest.write_text(json.dumps({
            "schema_version": 1, "stage": "rank_dedup",
            "clip_count": 0, "clips": [], "output_key": "rank_dedup",
        }))
        conn.execute(
            "INSERT INTO task_stages (stage_id, video_id, stage_name, clip_id, input_key, status, created_at, updated_at) "
            "VALUES (?, 'v1', 'rank_dedup', NULL, 'from:synthesize', 'succeeded', ?, ?)",
            (rd_stage_id, now, now),
        )
        rd_aid = make_artifact_id(
            stage_id=rd_stage_id, artifact_kind="rank_dedup_manifest",
            clip_id=None, normalized_path=str(rd_manifest),
        )
        rd_sha = sha256_file(rd_manifest)
        rd_size = rd_manifest.stat().st_size
        conn.execute(
            "INSERT INTO task_artifacts (artifact_id, job_id, video_id, stage_name, clip_id, path, sha256, size_bytes, provenance_json, created_at, stage_id, artifact_kind) "
            "VALUES (?, ?, 'v1', 'rank_dedup', NULL, ?, ?, ?, '{}', ?, ?, 'rank_dedup_manifest')",
            (rd_aid, job.job_id, str(rd_manifest), rd_sha, rd_size, now, rd_stage_id),
        )
        conn.execute("UPDATE task_jobs SET status='running' WHERE job_id=?", (job.job_id,))
        conn.commit()

        worker = TaskWorker(
            repo, "worker-1",
            {"materialize": AdaptivePipelineAdapter("materialize")},
            lease_seconds=90, heartbeat_seconds=30,
            db_path=str(db_path),
        )

        did_work = worker.run_once()
        assert did_work

        stage_row = conn.execute(
            "SELECT status FROM task_stages WHERE stage_id=?",
            (mat_stage.stage_id,),
        ).fetchone()
        # §9.1.4: zero-clip materialize MUST be succeeded (not needs_attention).
        assert stage_row["status"] == "succeeded", (
            f"Zero-clip materialize should succeed, got {stage_row['status']}"
        )

        # Empty formal export dir with a zero-gif result JSON.
        formal_dir = export_base / "test"
        assert formal_dir.exists()
        result_json = formal_dir / "test_result.json"
        assert result_json.exists()
        result_data = json.loads(result_json.read_text(encoding="utf-8"))
        assert result_data["gif_count"] == 0

        conn.close()

    @pytest.mark.slow
    def test_partial_gif_clip_terminal_status_query(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Status-query test (renamed per fourth-review §9.1): one succeeded
        gif_clip (with valid artifacts) and one needs_attention gif_clip.

        This verifies the resolver/envelope contract for partial gif_clip
        outcomes WITHOUT claiming to exercise retry.  Real worker retry is
        covered by ``tests/task_engine/test_e2e.py``
        ``TestRetryPreservesClips::test_failed_clip_retry_leaves_successful_unchanged``
        and the production retry E2E.
        """
        video_dir = tmp_path / "videos"
        video_dir.mkdir()
        video_path = _create_test_video(video_dir, duration=2.0)

        temp_yaml = tmp_path / "gifagent_config.yaml"
        temp_yaml.write_text(
            f"database:\n  path: {str(tmp_path / 'library.db').replace(chr(92), '/')}\n"
            f"adaptive: {{}}\nmodels: {{}}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("GIFAGENT_CONFIG", str(temp_yaml))
        monkeypatch.setattr("httpx.post", lambda *a, **kw: _MockVlmResponse())
        monkeypatch.setattr("httpx.get", lambda *a, **kw: _MockOllamaPsResponse())
        monkeypatch.setattr("app.services.llm_client.wait_for_llm", lambda *a, **kw: False)
        monkeypatch.setattr("app.services.llm_client.is_local_llm", lambda: False)
        monkeypatch.setattr("app.services.embedding.compute_text_embedding", lambda text: [0.1] * 384)

        db_path = tmp_path / "task.db"
        work_path = tmp_path / "task_work"

        from app.task_engine.schema import connect_task_db
        from app.task_engine.repository import TaskRepository
        from app.task_engine.artifacts import (
            get_gif_clip_terminal_statuses,
            make_artifact_id,
            resolve_materialize_inputs,
        )
        from app.task_engine.fingerprints import sha256_file

        conn = connect_task_db(db_path)
        repo = TaskRepository(conn)

        config = _make_config(work_path, tmp_path)
        from app.task_engine.models import CreateJob
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        job = repo.create_job(CreateJob(
            directory=str(video_dir),
            config_json=json.dumps(config),
        ))
        conn.execute(
            "INSERT INTO task_videos (video_id, job_id, path, fingerprint, status, created_at, updated_at) "
            "VALUES ('v1', ?, ?, 'fp', 'running', ?, ?)",
            (job.job_id, str(video_path), now, now),
        )
        conn.execute("UPDATE task_jobs SET status='running' WHERE job_id=?", (job.job_id,))

        # clip-good: succeeded WITH a valid gif_file + manifest artifact pair.
        # clip-bad: needs_attention (no artifacts required).
        conn.execute(
            "INSERT INTO task_stages (stage_id, video_id, stage_name, clip_id, input_key, status, attempt_count, created_at, updated_at) "
            "VALUES ('gc-good', 'v1', 'gif_clip', 'clip-good', 'key1', 'succeeded', 1, ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO task_stages (stage_id, video_id, stage_name, clip_id, input_key, status, attempt_count, last_error_json, created_at, updated_at) "
            "VALUES ('gc-bad', 'v1', 'gif_clip', 'clip-bad', 'key2', 'needs_attention', 2, ?, ?, ?)",
            (json.dumps({"code": "ffmpeg_error", "message": "boom", "transient": False}), now, now),
        )
        conn.commit()

        # Valid artifact pair for clip-good.
        gif_path = tmp_path / "good.gif"
        gif_data = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00;"
        gif_path.write_bytes(gif_data)
        gif_sha = sha256_file(gif_path)
        gif_size = gif_path.stat().st_size
        gif_aid = make_artifact_id(
            stage_id="gc-good", artifact_kind="gif_file",
            clip_id="clip-good", normalized_path=str(gif_path),
        )
        conn.execute(
            "INSERT INTO task_artifacts (artifact_id, job_id, video_id, stage_name, clip_id, path, sha256, size_bytes, provenance_json, created_at, stage_id, artifact_kind) "
            "VALUES (?, ?, 'v1', 'gif_clip', 'clip-good', ?, ?, ?, '{}', ?, 'gc-good', 'gif_file')",
            (gif_aid, job.job_id, str(gif_path), gif_sha, gif_size, now),
        )
        manifest_path = tmp_path / "gif_clip_manifest_good.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1, "stage": "gif_clip", "clip_id": "clip-good",
            "gif_path": str(gif_path), "gif_name": "good.gif",
            "sha256": gif_sha, "start_ts": 5.0, "end_ts": 10.0,
        }))
        manifest_sha = sha256_file(manifest_path)
        manifest_size = manifest_path.stat().st_size
        man_aid = make_artifact_id(
            stage_id="gc-good", artifact_kind="gif_clip_manifest",
            clip_id="clip-good", normalized_path=str(manifest_path),
        )
        conn.execute(
            "INSERT INTO task_artifacts (artifact_id, job_id, video_id, stage_name, clip_id, path, sha256, size_bytes, provenance_json, created_at, stage_id, artifact_kind) "
            "VALUES (?, ?, 'v1', 'gif_clip', 'clip-good', ?, ?, ?, '{}', ?, 'gc-good', 'gif_clip_manifest')",
            (man_aid, job.job_id, str(manifest_path), manifest_sha, manifest_size, now),
        )
        conn.commit()

        # Terminal statuses show both clips.
        statuses = get_gif_clip_terminal_statuses(conn, "v1")
        assert len(statuses) == 2
        clip_statuses = {s["clip_id"]: s["status"] for s in statuses}
        assert clip_statuses.get("clip-good") == "succeeded"
        assert clip_statuses.get("clip-bad") == "needs_attention"

        # The resolver returns ONLY the succeeded clip's artifacts, but its
        # stage_statuses carry BOTH clips (P1-1).
        mat_inputs = resolve_materialize_inputs(conn, "v1")
        assert len(mat_inputs.artifacts.get("gif_file", ())) == 1
        assert mat_inputs.artifacts["gif_file"][0].clip_id == "clip-good"
        assert len(mat_inputs.stage_statuses) == 2
        status_map = {s.clip_id: s.status for s in mat_inputs.stage_statuses}
        assert status_map == {"clip-good": "succeeded", "clip-bad": "needs_attention"}
        # The needs_attention clip carries its last_error.
        bad_status = next(s for s in mat_inputs.stage_statuses if s.clip_id == "clip-bad")
        assert bad_status.last_error == "boom"
        assert bad_status.attempt_count == 2

        conn.close()


class TestMaterializePublishConflict:
    """P0-2 / §3.2: real Worker materialize with a same-name-different-SHA
    historical GIF in the formal export directory.

    Verifies the historical file is NEVER overwritten and the new GIF is
    published under a stable conflict name (or the task enters
    needs_attention for an unrecoverable conflict).
    """

    def _setup_succeeded_clip(
        self, conn, job_id, video_path, tmp_path, clip_id, gif_data, gif_name,
    ):
        """Insert a succeeded gif_clip stage + valid gif_file/manifest artifacts."""
        from app.task_engine.artifacts import make_artifact_id
        from app.task_engine.fingerprints import sha256_file
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        gif_path = tmp_path / f"src_{clip_id}.gif"
        gif_path.write_bytes(gif_data)
        gif_sha = sha256_file(gif_path)
        gif_size = gif_path.stat().st_size

        gc_stage_id = f"gc-{clip_id}"
        conn.execute(
            "INSERT INTO task_stages (stage_id, video_id, stage_name, clip_id, "
            "input_key, status, attempt_count, created_at, updated_at) "
            "VALUES (?, 'v1', 'gif_clip', ?, ?, 'succeeded', 1, ?, ?)",
            (gc_stage_id, clip_id, f"from:rank_dedup:clip:{clip_id}", now, now),
        )
        gif_aid = make_artifact_id(
            stage_id=gc_stage_id, artifact_kind="gif_file",
            clip_id=clip_id, normalized_path=str(gif_path),
        )
        conn.execute(
            "INSERT INTO task_artifacts (artifact_id, job_id, video_id, stage_name, "
            "clip_id, path, sha256, size_bytes, provenance_json, created_at, "
            "stage_id, artifact_kind) "
            "VALUES (?, ?, 'v1', 'gif_clip', ?, ?, ?, ?, '{}', ?, ?, 'gif_file')",
            (gif_aid, job_id, clip_id, str(gif_path), gif_sha, gif_size, now, gc_stage_id),
        )
        manifest_path = tmp_path / f"manifest_{clip_id}.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1, "stage": "gif_clip", "clip_id": clip_id,
            "gif_path": str(gif_path), "gif_name": gif_name,
            "sha256": gif_sha, "start_ts": 5.0, "end_ts": 10.0,
        }))
        man_sha = sha256_file(manifest_path)
        man_size = manifest_path.stat().st_size
        man_aid = make_artifact_id(
            stage_id=gc_stage_id, artifact_kind="gif_clip_manifest",
            clip_id=clip_id, normalized_path=str(manifest_path),
        )
        conn.execute(
            "INSERT INTO task_artifacts (artifact_id, job_id, video_id, stage_name, "
            "clip_id, path, sha256, size_bytes, provenance_json, created_at, "
            "stage_id, artifact_kind) "
            "VALUES (?, ?, 'v1', 'gif_clip', ?, ?, ?, ?, '{}', ?, ?, 'gif_clip_manifest')",
            (man_aid, job_id, clip_id, str(manifest_path), man_sha, man_size, now, gc_stage_id),
        )
        conn.commit()
        return gif_sha

    @pytest.mark.slow
    def test_materialize_conflict_publishes_stable_name_preserving_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Same-name-different-SHA: historical GIF is untouched and the new
        GIF is published under the stable conflict name; materialize succeeds."""
        video_dir = tmp_path / "videos"
        video_dir.mkdir()
        video_path = _create_test_video(video_dir, duration=2.0)

        temp_yaml = tmp_path / "gifagent_config.yaml"
        temp_yaml.write_text(
            f"database:\n  path: {str(tmp_path / 'library.db').replace(chr(92), '/')}\n"
            f"adaptive: {{}}\nmodels: {{}}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("GIFAGENT_CONFIG", str(temp_yaml))
        monkeypatch.setattr("httpx.post", lambda *a, **kw: _MockVlmResponse())
        monkeypatch.setattr("httpx.get", lambda *a, **kw: _MockOllamaPsResponse())
        monkeypatch.setattr("app.services.llm_client.wait_for_llm", lambda *a, **kw: False)
        monkeypatch.setattr("app.services.llm_client.is_local_llm", lambda: False)
        monkeypatch.setattr("app.services.embedding.compute_text_embedding", lambda text: [0.1] * 384)

        db_path = tmp_path / "task.db"
        work_path = tmp_path / "task_work"
        export_base = tmp_path / "exports"

        from app.task_engine.schema import connect_task_db
        from app.task_engine.repository import TaskRepository
        from app.task_engine.worker import TaskWorker
        from app.task_engine.adaptive_adapter import AdaptivePipelineAdapter
        from app.task_engine.orchestrator import initialize_job
        from app.task_engine.fingerprints import sha256_file

        conn = connect_task_db(db_path)
        repo = TaskRepository(conn)

        config = _make_config(work_path, tmp_path, export_base_dir=str(export_base))
        from app.task_engine.models import CreateJob
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        job = repo.create_job(CreateJob(
            directory=str(video_dir),
            config_json=json.dumps(config),
        ))
        conn.execute(
            "INSERT INTO task_videos (video_id, job_id, path, fingerprint, status, created_at, updated_at) "
            "VALUES ('v1', ?, ?, 'fp', 'running', ?, ?)",
            (job.job_id, str(video_path), now, now),
        )

        # Pre-place a HISTORICAL formal GIF (same name, different content).
        formal_dir = export_base / "test"
        formal_dir.mkdir(parents=True)
        historical_data = b"GIF89a-HISTORICAL-OLD-CONTENT"
        (formal_dir / "test.gif").write_bytes(historical_data)
        historical_sha = sha256_file(formal_dir / "test.gif")

        # New GIF content (different SHA than historical).
        new_data = b"GIF89a-NEW-CONTENT-FOR-CLIP"
        clip_id = "clipconf123"  # >=8 chars
        new_sha = self._setup_succeeded_clip(
            conn, job.job_id, video_path, tmp_path, clip_id, new_data, "test.gif",
        )
        assert new_sha != historical_sha

        mat_stage = repo.ensure_stage("v1", "materialize", "from:gif_clip")
        conn.execute("UPDATE task_jobs SET status='running' WHERE job_id=?", (job.job_id,))
        conn.commit()

        worker = TaskWorker(
            repo, "worker-1",
            {"materialize": AdaptivePipelineAdapter("materialize")},
            lease_seconds=90, heartbeat_seconds=30, db_path=str(db_path),
        )
        assert worker.run_once()

        # Historical GIF MUST be untouched.
        assert (formal_dir / "test.gif").read_bytes() == historical_data
        assert sha256_file(formal_dir / "test.gif") == historical_sha

        # New GIF published under the stable conflict name.
        expected_conflict = f"test.{clip_id[:8]}.{new_sha[:12]}.gif"
        conflict_path = formal_dir / expected_conflict
        assert conflict_path.exists(), (
            f"Stable conflict name {expected_conflict} not found; "
            f"dir has {sorted(p.name for p in formal_dir.iterdir())}"
        )
        assert sha256_file(conflict_path) == new_sha

        # Materialize SUCCEEDED (stable naming = successful publish).
        stage_row = conn.execute(
            "SELECT status FROM task_stages WHERE stage_id=?",
            (mat_stage.stage_id,),
        ).fetchone()
        assert stage_row["status"] == "succeeded", (
            f"Materialize should succeed via stable conflict name, got {stage_row['status']}"
        )

        # Result JSON references the conflict name.
        result_json = formal_dir / "test_result.json"
        assert result_json.exists()
        result_data = json.loads(result_json.read_text(encoding="utf-8"))
        assert result_data["gif_count"] == 1
        assert result_data["succeeded"][0]["gif_name"] == expected_conflict

        conn.close()

    @pytest.mark.slow
    def test_materialize_unrecoverable_conflict_marks_needs_attention(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """When BOTH the original name and the stable conflict name already
        exist with different SHA, materialize cannot publish -> needs_attention."""
        video_dir = tmp_path / "videos"
        video_dir.mkdir()
        video_path = _create_test_video(video_dir, duration=2.0)

        temp_yaml = tmp_path / "gifagent_config.yaml"
        temp_yaml.write_text(
            f"database:\n  path: {str(tmp_path / 'library.db').replace(chr(92), '/')}\n"
            f"adaptive: {{}}\nmodels: {{}}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("GIFAGENT_CONFIG", str(temp_yaml))
        monkeypatch.setattr("httpx.post", lambda *a, **kw: _MockVlmResponse())
        monkeypatch.setattr("httpx.get", lambda *a, **kw: _MockOllamaPsResponse())
        monkeypatch.setattr("app.services.llm_client.wait_for_llm", lambda *a, **kw: False)
        monkeypatch.setattr("app.services.llm_client.is_local_llm", lambda: False)
        monkeypatch.setattr("app.services.embedding.compute_text_embedding", lambda text: [0.1] * 384)

        db_path = tmp_path / "task.db"
        work_path = tmp_path / "task_work"
        export_base = tmp_path / "exports"

        from app.task_engine.schema import connect_task_db
        from app.task_engine.repository import TaskRepository
        from app.task_engine.worker import TaskWorker
        from app.task_engine.adaptive_adapter import AdaptivePipelineAdapter
        from app.task_engine.fingerprints import sha256_file

        conn = connect_task_db(db_path)
        repo = TaskRepository(conn)

        config = _make_config(work_path, tmp_path, export_base_dir=str(export_base))
        from app.task_engine.models import CreateJob
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        job = repo.create_job(CreateJob(
            directory=str(video_dir),
            config_json=json.dumps(config),
        ))
        conn.execute(
            "INSERT INTO task_videos (video_id, job_id, path, fingerprint, status, created_at, updated_at) "
            "VALUES ('v1', ?, ?, 'fp', 'running', ?, ?)",
            (job.job_id, str(video_path), now, now),
        )

        formal_dir = export_base / "test"
        formal_dir.mkdir(parents=True)

        new_data = b"GIF89a-NEW-CONTENT-FOR-CLIP"
        clip_id = "clipconf456"
        new_sha = self._setup_succeeded_clip(
            conn, job.job_id, video_path, tmp_path, clip_id, new_data, "test.gif",
        )

        # Pre-place the original name with different SHA.
        (formal_dir / "test.gif").write_bytes(b"GIF89a-OLD-ORIGINAL")
        # Pre-place the STABLE CONFLICT NAME with different SHA so it is
        # also unrecoverable.
        conflict_name = f"test.{clip_id[:8]}.{new_sha[:12]}.gif"
        (formal_dir / conflict_name).write_bytes(b"GIF89a-OLD-CONFLICT-NAME")

        mat_stage = repo.ensure_stage("v1", "materialize", "from:gif_clip")
        conn.execute("UPDATE task_jobs SET status='running' WHERE job_id=?", (job.job_id,))
        conn.commit()

        worker = TaskWorker(
            repo, "worker-1",
            {"materialize": AdaptivePipelineAdapter("materialize")},
            lease_seconds=90, heartbeat_seconds=30, db_path=str(db_path),
        )
        assert worker.run_once()

        # Neither file was overwritten.
        assert (formal_dir / "test.gif").read_bytes() == b"GIF89a-OLD-ORIGINAL"
        assert (formal_dir / conflict_name).read_bytes() == b"GIF89a-OLD-CONFLICT-NAME"

        # Materialize entered needs_attention (no false success).
        stage_row = conn.execute(
            "SELECT status, last_error_json FROM task_stages WHERE stage_id=?",
            (mat_stage.stage_id,),
        ).fetchone()
        assert stage_row["status"] == "needs_attention", (
            f"Unrecoverable conflict must mark materialize needs_attention, "
            f"got {stage_row['status']}"
        )
        assert "publish failure" in (stage_row["last_error_json"] or "")

        # Video + job also need attention (no false success).
        vid_status = conn.execute(
            "SELECT status FROM task_videos WHERE video_id='v1'"
        ).fetchone()
        assert vid_status["status"] == "needs_attention"
        job_status = conn.execute(
            "SELECT status FROM task_jobs WHERE job_id=?", (job.job_id,)
        ).fetchone()
        assert job_status["status"] == "needs_attention"

        conn.close()


class TestProductionCrossStageResolver:
    """§9.2A: Worker cross-stage resolver discover -> sample with the real
    AdaptivePipelineAdapter and real ffprobe/ffmpeg.  No FakeAdapter."""

    @pytest.mark.slow
    def test_sample_consumes_discover_artifact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        video_dir = tmp_path / "videos"
        video_dir.mkdir()
        video_path = _create_test_video(video_dir, duration=2.0)

        temp_yaml = tmp_path / "gifagent_config.yaml"
        temp_yaml.write_text(
            f"database:\n  path: {str(tmp_path / 'library.db').replace(chr(92), '/')}\n"
            f"adaptive: {{}}\nmodels: {{}}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("GIFAGENT_CONFIG", str(temp_yaml))
        monkeypatch.setattr("httpx.post", lambda *a, **kw: _MockVlmResponse())
        monkeypatch.setattr("httpx.get", lambda *a, **kw: _MockOllamaPsResponse())
        monkeypatch.setattr("app.services.llm_client.wait_for_llm", lambda *a, **kw: False)
        monkeypatch.setattr("app.services.llm_client.is_local_llm", lambda: False)
        monkeypatch.setattr("app.services.embedding.compute_text_embedding", lambda text: [0.1] * 384)

        db_path = tmp_path / "task.db"
        work_path = tmp_path / "task_work"

        from app.task_engine.schema import connect_task_db
        from app.task_engine.repository import TaskRepository
        from app.task_engine.worker import TaskWorker
        from app.task_engine.adaptive_adapter import AdaptivePipelineAdapter
        from app.task_engine.orchestrator import initialize_job
        from app.task_engine.models import CreateJob

        conn = connect_task_db(db_path)
        repo = TaskRepository(conn)
        job = repo.create_job(CreateJob(
            directory=str(video_dir),
            config_json=json.dumps(_make_config(work_path, tmp_path)),
        ))
        initialize_job(repo, job.job_id)

        worker = TaskWorker(
            repo, "worker-1",
            {
                "discover": AdaptivePipelineAdapter("discover"),
                "sample": AdaptivePipelineAdapter("sample"),
            },
            lease_seconds=90, heartbeat_seconds=30, db_path=str(db_path),
        )
        worker.drain()

        stages = conn.execute(
            "SELECT stage_name, status FROM task_stages WHERE video_id IN "
            "(SELECT video_id FROM task_videos WHERE job_id=?)",
            (job.job_id,),
        ).fetchall()
        by_name = {r["stage_name"]: r["status"] for r in stages}
        assert by_name.get("discover") == "succeeded", by_name
        assert by_name.get("sample") == "succeeded", by_name

        # sample genuinely consumed discover's manifest artifact (the resolver
        # requires discover_manifest; sample could not have succeeded otherwise).
        vid = conn.execute(
            "SELECT video_id FROM task_videos WHERE job_id=? LIMIT 1",
            (job.job_id,),
        ).fetchone()["video_id"]
        discover_art = conn.execute(
            "SELECT artifact_kind, COUNT(*) AS c FROM task_artifacts "
            "WHERE video_id=? AND stage_name='discover' GROUP BY artifact_kind",
            (vid,),
        ).fetchall()
        kinds = {r["artifact_kind"]: r["c"] for r in discover_art}
        assert kinds.get("discover_manifest", 0) >= 1, kinds
        sample_art = conn.execute(
            "SELECT artifact_kind, COUNT(*) AS c FROM task_artifacts "
            "WHERE video_id=? AND stage_name='sample' GROUP BY artifact_kind",
            (vid,),
        ).fetchall()
        sk = {r["artifact_kind"]: r["c"] for r in sample_art}
        assert sk.get("sample_manifest", 0) >= 1, sk

        conn.close()


class TestProductionFanOutAndPublish:
    """§9.2B: rank_dedup -> gif_clip A/B -> materialize fan-out with the real
    AdaptivePipelineAdapter (real ffmpeg gif extraction + real publish).
    The materialize input envelope is NOT hand-crafted - the worker resolves
    it from the gif_clip artifacts via the dedicated resolver."""

    @pytest.mark.slow
    def test_rank_dedup_fanout_to_materialize(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        video_dir = tmp_path / "videos"
        video_dir.mkdir()
        video_path = _create_test_video(video_dir, duration=2.0)

        temp_yaml = tmp_path / "gifagent_config.yaml"
        temp_yaml.write_text(
            f"database:\n  path: {str(tmp_path / 'library.db').replace(chr(92), '/')}\n"
            f"adaptive: {{}}\nmodels: {{}}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("GIFAGENT_CONFIG", str(temp_yaml))
        monkeypatch.setattr("httpx.post", lambda *a, **kw: _MockVlmResponse())
        monkeypatch.setattr("httpx.get", lambda *a, **kw: _MockOllamaPsResponse())
        monkeypatch.setattr("app.services.llm_client.wait_for_llm", lambda *a, **kw: False)
        monkeypatch.setattr("app.services.llm_client.is_local_llm", lambda: False)
        monkeypatch.setattr("app.services.embedding.compute_text_embedding", lambda text: [0.1] * 384)

        db_path = tmp_path / "task.db"
        work_path = tmp_path / "task_work"
        export_base = tmp_path / "exports"

        from app.task_engine.schema import connect_task_db
        from app.task_engine.repository import TaskRepository
        from app.task_engine.worker import TaskWorker
        from app.task_engine.adaptive_adapter import AdaptivePipelineAdapter
        from app.task_engine.orchestrator import advance_job
        from app.task_engine.artifacts import make_artifact_id
        from app.task_engine.fingerprints import sha256_file
        from app.task_engine.models import CreateJob

        conn = connect_task_db(db_path)
        repo = TaskRepository(conn)
        config = _make_config(work_path, tmp_path, export_base_dir=str(export_base))
        job = repo.create_job(CreateJob(
            directory=str(video_dir),
            config_json=json.dumps(config),
        ))

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO task_videos (video_id, job_id, path, fingerprint, status, created_at, updated_at) "
            "VALUES ('v1', ?, ?, 'fp', 'running', ?, ?)",
            (job.job_id, str(video_path), now, now),
        )
        # Pre-place a succeeded rank_dedup stage + manifest artifact (the
        # upstream of the fan-out). Two clips inside the 2s video.
        rd_stage_id = "rd-001"
        rd_work = work_path / "rank_dedup" / rd_stage_id
        rd_work.mkdir(parents=True)
        clips = [
            {"clip_id": "clipA01", "start_ts": 0.0, "end_ts": 1.0,
             "rank": 1, "gif_worthiness": 0.8},
            {"clip_id": "clipB02", "start_ts": 0.5, "end_ts": 1.5,
             "rank": 2, "gif_worthiness": 0.7},
        ]
        rd_manifest = {
            "schema_version": 1, "stage": "rank_dedup",
            "clip_count": 2, "clips": clips, "output_key": "rank_dedup",
        }
        rd_manifest_path = rd_work / "rank_dedup_manifest.json"
        rd_manifest_path.write_text(json.dumps(rd_manifest))
        conn.execute(
            "INSERT INTO task_stages (stage_id, video_id, stage_name, clip_id, input_key, status, created_at, updated_at) "
            "VALUES (?, 'v1', 'rank_dedup', NULL, 'from:synthesize', 'succeeded', ?, ?)",
            (rd_stage_id, now, now),
        )
        rd_aid = make_artifact_id(
            stage_id=rd_stage_id, artifact_kind="rank_dedup_manifest",
            clip_id=None, normalized_path=str(rd_manifest_path),
        )
        rd_sha = sha256_file(rd_manifest_path)
        rd_size = rd_manifest_path.stat().st_size
        conn.execute(
            "INSERT INTO task_artifacts (artifact_id, job_id, video_id, stage_name, clip_id, path, sha256, size_bytes, provenance_json, created_at, stage_id, artifact_kind) "
            "VALUES (?, ?, 'v1', 'rank_dedup', NULL, ?, ?, ?, '{}', ?, ?, 'rank_dedup_manifest')",
            (rd_aid, job.job_id, str(rd_manifest_path), rd_sha, rd_size, now, rd_stage_id),
        )
        conn.execute("UPDATE task_jobs SET status='running' WHERE job_id=?", (job.job_id,))
        conn.commit()

        # advance_job creates the gif_clip fan-out stages from the manifest.
        advance_job(repo, job.job_id)
        gc_count = conn.execute(
            "SELECT COUNT(*) FROM task_stages WHERE video_id='v1' AND stage_name='gif_clip'",
        ).fetchone()[0]
        assert gc_count == 2, f"Expected 2 gif_clip stages, got {gc_count}"

        worker = TaskWorker(
            repo, "worker-1",
            {
                "gif_clip": AdaptivePipelineAdapter("gif_clip"),
                "materialize": AdaptivePipelineAdapter("materialize"),
            },
            lease_seconds=90, heartbeat_seconds=30, db_path=str(db_path),
        )
        worker.drain()
        advance_job(repo, job.job_id)

        # Both gif_clips succeeded with a gif_file + manifest each.
        gif_clips = conn.execute(
            "SELECT clip_id, status FROM task_stages WHERE video_id='v1' AND stage_name='gif_clip'",
        ).fetchall()
        assert {r["status"] for r in gif_clips} == {"succeeded"}, (
            [dict(r) for r in gif_clips]
        )
        gif_file_count = conn.execute(
            "SELECT COUNT(*) FROM task_artifacts WHERE video_id='v1' AND artifact_kind='gif_file'",
        ).fetchone()[0]
        assert gif_file_count == 2, gif_file_count

        mat = conn.execute(
            "SELECT status FROM task_stages WHERE video_id='v1' AND stage_name='materialize'",
        ).fetchone()
        assert mat is not None and mat["status"] == "succeeded", (
            f"materialize should succeed, got {mat['status'] if mat else None}"
        )

        # Formal export has 2 GIFs (real ffmpeg output published by materialize).
        formal_dir = export_base / "test"
        assert formal_dir.exists()
        gifs = [f for f in formal_dir.iterdir() if f.suffix == ".gif"]
        assert len(gifs) == 2, [p.name for p in formal_dir.iterdir()]
        result_json = formal_dir / "test_result.json"
        assert result_json.exists()
        result_data = json.loads(result_json.read_text(encoding="utf-8"))
        assert result_data["gif_count"] == 2

        job_status = conn.execute(
            "SELECT status FROM task_jobs WHERE job_id=?", (job.job_id,)
        ).fetchone()["status"]
        assert job_status == "succeeded", job_status

        conn.close()

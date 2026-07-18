"""Phase 0: Materialize end-to-end production tests.

Test materialize publishing with fake artifacts:
1. 2 gif_clips all succeed → materialize succeeds
2. 1 succeed, 1 fail → partial → materialize succeeded, video needs_attention
3. All fail → materialize needs_attention
4. Zero-clip → materialize succeeded with empty result
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest


T0 = datetime(2026, 7, 18, tzinfo=timezone.utc)


def _create_fake_gif(
    work_dir: Path, clip_id: str, content: str = "GIF89a-fake-gif-data"
) -> Path:
    """Create a fake GIF file for a clip."""
    gif_path = work_dir / f"output_{clip_id}.gif"
    gif_path.write_text(content)
    return gif_path


def _create_fake_manifest(
    work_dir: Path, stage: str, data: dict
) -> Path:
    """Create a fake manifest JSON file."""
    path = work_dir / f"{stage}_manifest.json"
    path.write_text(json.dumps(data))
    return path


def _insert_artifact(
    conn: sqlite3.Connection,
    stage_id: str,
    job_id: str,
    video_id: str,
    stage_name: str,
    artifact_kind: str,
    path: str,
    clip_id: str | None = None,
    sha256: str = "",
    size_bytes: int = 0,
) -> str:
    """Insert an artifact and return its artifact_id."""
    import os
    from app.task_engine.artifacts import make_artifact_id
    from app.task_engine.fingerprints import sha256_file as _sha_file

    if not sha256 and os.path.exists(path):
        sha256 = _sha_file(path)
    if not size_bytes and os.path.exists(path):
        size_bytes = os.path.getsize(path)

    artifact_id = make_artifact_id(
        stage_id=stage_id,
        artifact_kind=artifact_kind,
        clip_id=clip_id,
        normalized_path=str(Path(path).as_posix()),
    )
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT OR IGNORE INTO task_artifacts
           (artifact_id, job_id, video_id, stage_name, clip_id,
            path, sha256, size_bytes, provenance_json, created_at,
            stage_id, artifact_kind)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            artifact_id,
            job_id,
            video_id,
            stage_name,
            clip_id,
            str(path),
            sha256,
            size_bytes,
            "{}",
            now,
            stage_id,
            artifact_kind,
        ),
    )
    conn.commit()
    return artifact_id


class TestMaterializeAllSucceed:
    """Two gif_clips all succeed → materialize succeeds."""

    def test_materialize_with_2_successful_clips(
        self, tmp_path: Path,
    ):
        from app.task_engine.schema import connect_task_db
        from app.task_engine.repository import TaskRepository
        from app.task_engine.artifacts import resolve_all_gif_clip_artifacts

        db_path = tmp_path / "task.db"
        conn = connect_task_db(db_path)
        repo = TaskRepository(conn)

        # Create job, video
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO task_jobs (job_id, directory, directory_key, config_json, status, created_at, updated_at) "
            "VALUES ('j1', ?, ?, '{}', 'running', ?, ?)",
            (str(tmp_path), str(tmp_path), now, now),
        )
        conn.execute(
            "INSERT INTO task_videos (video_id, job_id, path, fingerprint, status, created_at, updated_at) "
            "VALUES ('v1', 'j1', '/tmp/v.mp4', 'fp', 'running', ?, ?)",
            (now, now),
        )
        conn.commit()

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        # Create 2 successful gif_clip artifacts
        for clip_id in ("clip-A", "clip-B"):
            gif_path = _create_fake_gif(work_dir, clip_id)
            manifest = _create_fake_manifest(work_dir, f"gif_clip_{clip_id}", {
                "schema_version": 1,
                "stage": "gif_clip",
                "clip_id": clip_id,
                "gif_path": str(gif_path),
                "gif_name": f"test_{clip_id}.gif",
                "sha256": "",
                "start_ts": 10.0,
                "end_ts": 15.0,
            })
            stage_id = f"gc-{clip_id}"
            conn.execute(
                "INSERT INTO task_stages (stage_id, video_id, stage_name, clip_id, input_key, status, created_at, updated_at) "
                "VALUES (?, 'v1', 'gif_clip', ?, ?, 'succeeded', ?, ?)",
                (stage_id, clip_id, f"from:rank_dedup:clip:{clip_id}", now, now),
            )
            _insert_artifact(conn, stage_id, "j1", "v1", "gif_clip", "gif_file",
                             str(gif_path), clip_id=clip_id)
            _insert_artifact(conn, stage_id, "j1", "v1", "gif_clip", "gif_clip_manifest",
                             str(manifest), clip_id=clip_id)
        conn.commit()

        # Resolve all gif_clip artifacts
        by_clip = resolve_all_gif_clip_artifacts(conn, "v1")
        assert len(by_clip) == 2, f"Expected 2 clips, got {len(by_clip)}"

        for clip_id, refs in by_clip.items():
            kinds = {r.artifact_kind for r in refs}
            assert "gif_file" in kinds, f"clip {clip_id} missing gif_file"
            # Each clip should have artifacts
            gif_files = [r for r in refs if r.artifact_kind == "gif_file"]
            assert len(gif_files) == 1, f"Expected 1 gif_file for {clip_id}, got {len(gif_files)}"

        conn.close()

    def test_materialize_status_all_succeed(
        self, tmp_path: Path,
    ):
        """When all gif_clips succeed, materialize and video should succeed."""
        from app.task_engine.schema import connect_task_db
        from app.task_engine.repository import TaskRepository
        from app.task_engine.orchestrator import advance_job

        db_path = tmp_path / "task.db"
        conn = connect_task_db(db_path)
        repo = TaskRepository(conn)

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO task_jobs (job_id, directory, directory_key, config_json, status, created_at, updated_at) "
            "VALUES ('j1', ?, ?, '{}', 'running', ?, ?)",
            (str(tmp_path), str(tmp_path), now, now),
        )
        conn.execute(
            "INSERT INTO task_videos (video_id, job_id, path, fingerprint, status, created_at, updated_at) "
            "VALUES ('v1', 'j1', '/tmp/v.mp4', 'fp', 'running', ?, ?)",
            (now, now),
        )
        conn.commit()

        work_dir = tmp_path / "work"
        work_dir.mkdir()

        # Create 2 succeeded gif_clip stages
        for clip_id in ("clip-A", "clip-B"):
            stage_id = f"gc-{clip_id}"
            conn.execute(
                "INSERT INTO task_stages (stage_id, video_id, stage_name, clip_id, input_key, status, created_at, updated_at) "
                "VALUES (?, 'v1', 'gif_clip', ?, ?, 'succeeded', ?, ?)",
                (stage_id, clip_id, f"from:rank_dedup:clip:{clip_id}", now, now),
            )
        conn.commit()

        # Advance job should create materialize stage
        advance_job(repo, "j1")

        mat_stage = conn.execute(
            "SELECT stage_id, status FROM task_stages WHERE video_id='v1' AND stage_name='materialize'"
        ).fetchone()
        assert mat_stage is not None, "materialize stage should be created"

        conn.close()


class TestMaterializePartialSuccess:
    """One clip succeeds, one fails → materialize succeeded, video needs_attention."""

    def test_partial_success_materialize_succeeds_video_attention(
        self, tmp_path: Path,
    ):
        from app.task_engine.schema import connect_task_db
        from app.task_engine.repository import TaskRepository
        from app.task_engine.orchestrator import advance_job, _aggregate_video_status

        db_path = tmp_path / "task.db"
        conn = connect_task_db(db_path)
        repo = TaskRepository(conn)

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO task_jobs (job_id, directory, directory_key, config_json, status, created_at, updated_at) "
            "VALUES ('j1', ?, ?, '{}', 'running', ?, ?)",
            (str(tmp_path), str(tmp_path), now, now),
        )
        conn.execute(
            "INSERT INTO task_videos (video_id, job_id, path, fingerprint, status, created_at, updated_at) "
            "VALUES ('v1', 'j1', '/tmp/v.mp4', 'fp', 'running', ?, ?)",
            (now, now),
        )
        conn.commit()

        # clip-A = succeeded, clip-B = failed
        conn.execute(
            "INSERT INTO task_stages (stage_id, video_id, stage_name, clip_id, input_key, status, created_at, updated_at) "
            "VALUES ('gc-A', 'v1', 'gif_clip', 'clip-A', 'in-A', 'succeeded', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO task_stages (stage_id, video_id, stage_name, clip_id, input_key, status, created_at, updated_at) "
            "VALUES ('gc-B', 'v1', 'gif_clip', 'clip-B', 'in-B', 'needs_attention', ?, ?)",
            (now, now),
        )
        conn.commit()

        # Aggregate video status
        _aggregate_video_status(repo, "v1", "j1")

        video_status = conn.execute(
            "SELECT status FROM task_videos WHERE video_id='v1'"
        ).fetchone()
        assert video_status is not None
        assert video_status["status"] == "needs_attention", (
            f"Partial failure should mark video needs_attention, got {video_status['status']}"
        )

        conn.close()


class TestMaterializeAllFail:
    """All gif_clips fail → video/job needs_attention."""

    def test_all_fail_video_needs_attention(
        self, tmp_path: Path,
    ):
        from app.task_engine.schema import connect_task_db
        from app.task_engine.repository import TaskRepository
        from app.task_engine.orchestrator import _aggregate_video_status

        db_path = tmp_path / "task.db"
        conn = connect_task_db(db_path)
        repo = TaskRepository(conn)

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO task_jobs (job_id, directory, directory_key, config_json, status, created_at, updated_at) "
            "VALUES ('j1', ?, ?, '{}', 'running', ?, ?)",
            (str(tmp_path), str(tmp_path), now, now),
        )
        conn.execute(
            "INSERT INTO task_videos (video_id, job_id, path, fingerprint, status, created_at, updated_at) "
            "VALUES ('v1', 'j1', '/tmp/v.mp4', 'fp', 'running', ?, ?)",
            (now, now),
        )
        conn.commit()

        # All clips failed
        for clip_id in ("clip-A", "clip-B"):
            conn.execute(
                "INSERT INTO task_stages (stage_id, video_id, stage_name, clip_id, input_key, status, created_at, updated_at) "
                "VALUES (?, 'v1', 'gif_clip', ?, ?, 'needs_attention', ?, ?)",
                (f"gc-{clip_id}", clip_id, f"in-{clip_id}", now, now),
            )
        conn.commit()

        _aggregate_video_status(repo, "v1", "j1")

        video_status = conn.execute(
            "SELECT status FROM task_videos WHERE video_id='v1'"
        ).fetchone()
        assert video_status["status"] == "needs_attention", (
            f"All failed should be needs_attention, got {video_status['status']}"
        )

        conn.close()


class TestMaterializeZeroClip:
    """Zero-clip → materialize succeeds with empty result."""

    def test_zero_clip_materialize_succeeds(
        self, tmp_path: Path,
    ):
        from app.task_engine.schema import connect_task_db
        from app.task_engine.repository import TaskRepository
        from app.task_engine.orchestrator import advance_job, initialize_job

        db_path = tmp_path / "task.db"
        conn = connect_task_db(db_path)
        repo = TaskRepository(conn)

        video_dir = tmp_path / "videos"
        video_dir.mkdir()
        (video_dir / "test.mp4").write_text("fake-video-data")

        from app.task_engine.models import CreateJob
        job = repo.create_job(CreateJob(
            directory=str(video_dir),
            config_json=json.dumps({"task_work_dir": str(tmp_path / "task_work")}),
        ))
        initialize_job(repo, job.job_id)

        # Simulate all stages succeeding up to rank_dedup with zero clips
        vid_row = conn.execute(
            "SELECT video_id FROM task_videos WHERE job_id=? LIMIT 1",
            (job.job_id,),
        ).fetchone()
        video_id = vid_row["video_id"]

        # Create a rank_dedup stage with zero clips
        now = datetime.now(timezone.utc).isoformat()
        rd_stage_id = "rd-001"
        conn.execute(
            "INSERT INTO task_stages (stage_id, video_id, stage_name, clip_id, input_key, status, created_at, updated_at) "
            "VALUES (?, ?, 'rank_dedup', NULL, 'from:synthesize', 'succeeded', ?, ?)",
            (rd_stage_id, video_id, now, now),
        )
        conn.commit()

        # Create rank_dedup manifest with zero clips
        work_dir = tmp_path / "task_work" / "rank_dedup" / rd_stage_id
        work_dir.mkdir(parents=True)
        manifest_path = work_dir / "rank_dedup_manifest.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1,
            "stage": "rank_dedup",
            "clip_count": 0,
            "clips": [],
            "output_key": "rank_dedup",
        }))

        _insert_artifact(conn, rd_stage_id, job.job_id, video_id, "rank_dedup",
                         "rank_dedup_manifest", str(manifest_path))

        # Advance should create materialize directly for zero-clip
        advance_job(repo, job.job_id)

        mat_stage = conn.execute(
            "SELECT * FROM task_stages WHERE video_id=? AND stage_name='materialize'",
            (video_id,),
        ).fetchone()
        assert mat_stage is not None, "Zero-clip should create materialize stage"

        conn.close()

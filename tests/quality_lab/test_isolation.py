"""Quality Lab single-video isolation tests.

Verifies:
- Only the specified video path is processed (no directory scanning).
- Paths outside the job directory are rejected.
- Non-existent paths are rejected (job goes to needs_attention).
- Two items in the same parent directory get different task job IDs.
- Experiment config_json actually flows into the Stage config snapshot.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.task_engine.models import CreateJob
from app.task_engine.orchestrator import initialize_job
from app.task_engine.repository import TaskRepository
from app.task_engine.schema import connect_task_db


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect_task_db(tmp_path / "task.db")
    yield conn
    conn.close()


@pytest.fixture
def repo(db: sqlite3.Connection) -> TaskRepository:
    return TaskRepository(db)


@pytest.fixture
def video_dir(tmp_path: Path) -> Path:
    d = tmp_path / "videos"
    d.mkdir()
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        (d / name).write_text(f"fake-{name}", encoding="utf-8")
    return d


# =========================================================================
# Single-video isolation: only the specified path is processed
# =========================================================================


class TestSingleVideoIsolation:
    """When ``video_paths`` is provided in config, only that path is used."""

    def test_only_specified_video_is_added(
        self, repo: TaskRepository, video_dir: Path
    ):
        """a.mp4, b.mp4, c.mp4 exist. Only b.mp4 is in video_paths."""
        b_path = str(video_dir / "b.mp4")
        config = {"video_paths": [b_path]}
        job = repo.create_job(
            CreateJob(
                directory=str(video_dir),
                config_json=json.dumps(config),
            )
        )

        videos = initialize_job(repo, job.job_id)
        assert len(videos) == 1, (
            f"Expected 1 video (b.mp4), got {len(videos)}: {videos}"
        )
        assert os.path.basename(videos[0]) == "b.mp4"

        # Verify only one task_videos row exists.
        vid_rows = repo.conn.execute(
            "SELECT path FROM task_videos WHERE job_id=?",
            (job.job_id,),
        ).fetchall()
        assert len(vid_rows) == 1
        assert os.path.basename(vid_rows[0]["path"]) == "b.mp4"

    def test_three_videos_only_one_in_paths(
        self, repo: TaskRepository, video_dir: Path
    ):
        """Three videos exist; video_paths lists only b.mp4."""
        b_path = str(video_dir / "b.mp4")
        config = {"video_paths": [b_path]}
        job = repo.create_job(
            CreateJob(
                directory=str(video_dir),
                config_json=json.dumps(config),
            )
        )

        videos = initialize_job(repo, job.job_id)
        # Only b.mp4 should be discovered
        basenames = [os.path.basename(v) for v in videos]
        assert basenames == ["b.mp4"], f"Expected only b.mp4, got {basenames}"


# =========================================================================
# Reject paths outside job directory
# =========================================================================


class TestPathOutsideJobDirectoryRejected:
    def test_path_outside_directory_not_added(
        self, repo: TaskRepository, video_dir: Path, tmp_path: Path
    ):
        """A video_path that is not inside the job directory is rejected."""
        outside = tmp_path / "outside"
        outside.mkdir()
        outside_file = outside / "alien.mp4"
        outside_file.write_text("intruder")

        config = {"video_paths": [str(outside_file)]}
        job = repo.create_job(
            CreateJob(
                directory=str(video_dir),
                config_json=json.dumps(config),
            )
        )

        videos = initialize_job(repo, job.job_id)
        # The outside file should NOT be in the results.
        outside_in_results = any(
            str(outside_file) in v for v in videos
        )
        assert not outside_in_results, (
            f"Path {outside_file} outside job directory should have been filtered"
        )

        # Job should either be empty (no valid videos) or have only dir-local files.
        job_row = repo.conn.execute(
            "SELECT status FROM task_jobs WHERE job_id=?",
            (job.job_id,),
        ).fetchone()
        assert job_row is not None


# =========================================================================
# Non-existent paths → job goes to needs_attention
# =========================================================================


class TestNonexistentPathHandling:
    def test_nonexistent_path_results_in_empty_or_attention(
        self, repo: TaskRepository, video_dir: Path
    ):
        """A non-existent video_path does not cause silent directory scan."""
        ghost = str(video_dir / "ghost.mp4")
        config = {"video_paths": [ghost]}
        job = repo.create_job(
            CreateJob(
                directory=str(video_dir),
                config_json=json.dumps(config),
            )
        )

        # Should not crash.
        videos = initialize_job(repo, job.job_id)

        # Either no videos found, or the ghost path is not included.
        assert ghost not in videos, "non-existent path leaked into video list"

        # The job status should NOT be empty_directory scan of all files.
        job_row = repo.conn.execute(
            "SELECT status FROM task_jobs WHERE job_id=?",
            (job.job_id,),
        ).fetchone()

        # RED: If the ghost path is filtered out, we get 0 videos.
        # The job should detect this and either mark succeeded (empty)
        # or needs_attention. It should NOT discover all .mp4 files.
        vid_count = repo.conn.execute(
            "SELECT COUNT(*) FROM task_videos WHERE job_id=?",
            (job.job_id,),
        ).fetchone()[0]
        assert vid_count == 0, (
            f"Expected 0 videos for non-existent path, got {vid_count}"
        )


# =========================================================================
# Same directory, different items → different job identity
# =========================================================================


class TestSameDirDifferentItems:
    """Two Quality Lab items in the same parent directory must get
    different task job identities. They cannot reuse the same job
    just because the directory matches."""

    def test_different_items_in_same_dir_get_different_jobs(
        self, repo: TaskRepository, tmp_path: Path
    ):
        """Create two items sharing a parent directory. Verify different jobs."""
        shared = tmp_path / "shared_lab"
        shared.mkdir()
        (shared / "item_a.mp4").write_text("content-a")
        (shared / "item_b.mp4").write_text("content-b")

        # First job with item_a.mp4
        config_a = {"video_paths": [str(shared / "item_a.mp4")]}
        job_a = repo.create_job(
            CreateJob(
                directory=str(shared),
                config_json=json.dumps(config_a),
            )
        )

        # Second job with item_b.mp4 — must NOT return job_a.id
        config_b = {"video_paths": [str(shared / "item_b.mp4")]}
        job_b = repo.create_job(
            CreateJob(
                directory=str(shared),
                config_json=json.dumps(config_b),
            )
        )

        # RED: Currently, create_job for the same directory with an active
        # job will raise ActiveJobConflictError or must somehow distinguish
        # between these two items.
        assert job_b.job_id != job_a.job_id, (
            "Different items in same directory must get different job IDs"
        )

        # Both should succeed initialization with their respective single videos.
        init_a = initialize_job(repo, job_a.job_id)
        init_b = initialize_job(repo, job_b.job_id)

        assert len(init_a) == 1
        assert len(init_b) == 1
        assert os.path.basename(init_a[0]) == "item_a.mp4"
        assert os.path.basename(init_b[0]) == "item_b.mp4"


# =========================================================================
# Config snapshot flows to Stage
# =========================================================================


class TestConfigSnapshotFlow:
    """Experiment config_json must be frozen at job creation and used by
    stages, not overwritten by the global config."""

    def test_config_json_persisted_in_job(self, repo: TaskRepository, video_dir: Path):
        """The config_json provided at creation is exactly what the job stores."""
        experiment_config = {
            "adaptive": {"sample_interval": 99},
            "_experiment": {"run_id": "test-run", "item_id": "test-item"},
        }
        job = repo.create_job(
            CreateJob(
                directory=str(video_dir),
                config_json=json.dumps(experiment_config),
            )
        )

        row = repo.conn.execute(
            "SELECT config_json FROM task_jobs WHERE job_id=?",
            (job.job_id,),
        ).fetchone()
        stored = json.loads(row["config_json"])
        assert stored["adaptive"]["sample_interval"] == 99
        assert stored["_experiment"]["run_id"] == "test-run"

    def test_config_snapshot_in_stage_context(
        self, repo: TaskRepository, video_dir: Path
    ):
        """When the worker builds a StageContext, the config from the job
        is exactly what was provided at creation time."""
        experiment_config = {
            "adaptive": {"sample_interval": 77},
            "vlm": {"temperature": 0.42},
        }
        job = repo.create_job(
            CreateJob(
                directory=str(video_dir),
                config_json=json.dumps(experiment_config),
            )
        )
        initialize_job(repo, job.job_id)

        from app.task_engine.worker import TaskWorker

        worker = TaskWorker(repo, "test-worker", {})
        # Manually build context to verify config snapshot.
        stage = repo.claim_stage("test-worker", _utcnow())
        assert stage is not None
        ctx = worker._build_context(stage)

        assert ctx.config["adaptive"]["sample_interval"] == 77
        assert ctx.config["vlm"]["temperature"] == 0.42

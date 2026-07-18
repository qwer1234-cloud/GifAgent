"""Tests for the job orchestrator — job initialisation, video discovery, stage chaining."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from app.task_engine.models import CreateJob
from app.task_engine.orchestrator import (
    advance_job,
    discover_videos,
    initialize_job,
    _STAGE_ORDER,
)
from app.task_engine.repository import TaskRepository
from app.task_engine.schema import connect_task_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    conn = connect_task_db(tmp_path / "task.db")
    yield conn
    conn.close()


@pytest.fixture
def repo(db):
    return TaskRepository(db)


@pytest.fixture
def video_dir(tmp_path):
    d = tmp_path / "videos"
    d.mkdir()
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        (d / name).write_text(f"fake-{name}", encoding="utf-8")
    # Non-video file that should be ignored.
    (d / "readme.txt").write_text("not a video", encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# discover_videos
# ---------------------------------------------------------------------------

def test_discover_videos_finds_mp4_mkv(video_dir: Path):
    (video_dir / "d.mkv").write_text("fake-d", encoding="utf-8")
    videos = discover_videos(str(video_dir), ".mp4,.mkv")
    assert len(videos) == 4
    assert all(v.endswith((".mp4", ".mkv")) for v in videos)
    # Should be sorted.
    basenames = [os.path.basename(v) for v in videos]
    assert basenames == sorted(basenames)


def test_discover_videos_empty_dir(tmp_path: Path):
    assert discover_videos(str(tmp_path), ".mp4") == []


def test_discover_videos_nonexistent_dir():
    assert discover_videos("/nonexistent/path", ".mp4") == []


def test_discover_videos_filters_by_extension(video_dir: Path):
    videos = discover_videos(str(video_dir), ".mp4")
    assert len(videos) == 3
    assert all(v.endswith(".mp4") for v in videos)


def test_discover_videos_default_extensions(video_dir: Path):
    videos = discover_videos(str(video_dir), "")
    # Default set: .mp4, .mkv, .avi, .mov, .webm, .ts
    assert len(videos) == 3  # a.mp4, b.mp4, c.mp4; readme.txt excluded


# ---------------------------------------------------------------------------
# initialize_job
# ---------------------------------------------------------------------------

def test_initialize_job_creates_videos_and_stages(repo: TaskRepository, video_dir: Path):
    job = repo.create_job(CreateJob(directory=str(video_dir), config_json="{}", extensions=".mp4,.mkv,.ts"))
    assert job.status == "pending"

    videos = initialize_job(repo, job.job_id)
    assert len(videos) == 3  # a.mp4, b.mp4, c.mp4 (no .ts files)

    # Job should now be running.
    job_row = repo.conn.execute("SELECT status FROM task_jobs WHERE job_id=?", (job.job_id,)).fetchone()
    assert job_row["status"] == "running"

    # Videos should exist.
    vid_rows = repo.conn.execute(
        "SELECT video_id, status FROM task_videos WHERE job_id=? ORDER BY path",
        (job.job_id,),
    ).fetchall()
    assert len(vid_rows) == 3
    assert all(r["status"] == "pending" for r in vid_rows)

    # Each video should have a "discover" stage.
    for vr in vid_rows:
        stage = repo.conn.execute(
            "SELECT stage_name, status FROM task_stages WHERE video_id=?",
            (vr["video_id"],),
        ).fetchone()
        assert stage is not None
        assert stage["stage_name"] == "discover"
        assert stage["status"] == "pending"


def test_initialize_job_empty_directory(repo: TaskRepository, tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    job = repo.create_job(CreateJob(directory=str(empty), config_json="{}"))
    videos = initialize_job(repo, job.job_id)
    assert videos == []

    # Job should be succeeded immediately.
    job_row = repo.conn.execute("SELECT status FROM task_jobs WHERE job_id=?", (job.job_id,)).fetchone()
    assert job_row["status"] == "succeeded"


def test_initialize_job_skips_non_pending(repo: TaskRepository, video_dir: Path):
    job = repo.create_job(CreateJob(directory=str(video_dir), config_json="{}"))
    initialize_job(repo, job.job_id)

    # Second call should be a no-op (status is now running).
    videos = initialize_job(repo, job.job_id)
    assert len(videos) == 3  # still returns the video list
    vid_rows = repo.conn.execute(
        "SELECT COUNT(*) FROM task_videos WHERE job_id=?",
        (job.job_id,),
    ).fetchone()[0]
    assert vid_rows == 3


def test_initialize_job_respects_limit(repo: TaskRepository, video_dir: Path):
    job = repo.create_job(CreateJob(directory=str(video_dir), config_json="{}", limit=2))
    videos = initialize_job(repo, job.job_id)
    assert len(videos) == 2
    vid_rows = repo.conn.execute(
        "SELECT COUNT(*) FROM task_videos WHERE job_id=?", (job.job_id,)
    ).fetchone()[0]
    assert vid_rows == 2


# ---------------------------------------------------------------------------
# advance_job
# ---------------------------------------------------------------------------

def test_advance_job_creates_next_stage_after_completion(repo: TaskRepository, video_dir: Path):
    job = repo.create_job(CreateJob(directory=str(video_dir), config_json="{}"))
    initialize_job(repo, job.job_id)

    # Manually complete the "discover" stage for one video.
    vid = repo.conn.execute(
        "SELECT video_id FROM task_videos WHERE job_id=? LIMIT 1",
        (job.job_id,),
    ).fetchone()
    assert vid is not None
    stage = repo.conn.execute(
        "SELECT stage_id FROM task_stages WHERE video_id=? AND stage_name='discover'",
        (vid["video_id"],),
    ).fetchone()
    assert stage is not None

    # Claim and complete the stage.
    claimed = repo.claim_stage("test-worker", _utcnow())
    assert claimed is not None
    repo.complete_stage(claimed.stage_id, "test-worker", "discovered-ok")

    # Advance job — should create "sample" stage.
    status = advance_job(repo, job.job_id)
    assert status == "running"

    next_stage = repo.conn.execute(
        "SELECT stage_name FROM task_stages WHERE video_id=? AND stage_name='sample'",
        (vid["video_id"],),
    ).fetchone()
    assert next_stage is not None


def test_cancel_marks_job_cancelled(repo: TaskRepository, video_dir: Path):
    job = repo.create_job(CreateJob(directory=str(video_dir), config_json="{}"))
    initialize_job(repo, job.job_id)
    repo.append_command(job.job_id, "cancel", {})

    status = advance_job(repo, job.job_id)
    assert status == "cancelled"

    job_row = repo.conn.execute(
        "SELECT status FROM task_jobs WHERE job_id=?", (job.job_id,)
    ).fetchone()
    assert job_row["status"] == "cancelled"


def test_retry_resets_failed_stages(repo: TaskRepository, video_dir: Path):
    from app.task_engine.models import StageError
    job = repo.create_job(CreateJob(directory=str(video_dir), config_json="{}"))
    initialize_job(repo, job.job_id)

    # Fail the first video's stage.
    stage = repo.claim_stage("test-worker", _utcnow())
    assert stage is not None
    repo.fail_stage(stage.stage_id, "test-worker", StageError("test_fail", "test error", transient=False))

    # Advance — should see job as needs_attention.
    status = advance_job(repo, job.job_id)
    assert status == "running"  # other videos still pending

    # Issue retry.
    repo.append_command(job.job_id, "retry", {})
    status = advance_job(repo, job.job_id)

    # Failed stage should be reset to pending.
    reset = repo.conn.execute(
        "SELECT status, attempt_count FROM task_stages WHERE stage_id=?",
        (stage.stage_id,),
    ).fetchone()
    assert reset["status"] == "pending"
    assert reset["attempt_count"] == 0


def test_job_runs_after_first_completion(repo: TaskRepository, video_dir: Path):
    """After completing the first stage, job is running (more stages to process)."""
    job = repo.create_job(CreateJob(directory=str(video_dir), config_json="{}"))
    initialize_job(repo, job.job_id)

    # Complete one discover stage — job should still be running.
    s = repo.claim_stage("test-worker", _utcnow())
    assert s is not None
    repo.complete_stage(s.stage_id, "test-worker", "done")
    status = advance_job(repo, job.job_id)
    assert status == "running"  # more stages to process

    # Verify the next stage was created.
    next_stage = repo.conn.execute(
        "SELECT stage_name FROM task_stages WHERE stage_name='sample' LIMIT 1"
    ).fetchone()
    assert next_stage is not None


def test_job_reaches_terminal_when_last_stage_completes(repo: TaskRepository, video_dir: Path):
    """Regression: when a video's final stage completes, the job must reach a
    terminal state instead of staying ``running`` forever.

    Previously ``all_terminal`` was computed *before* ``_advance_video_stages``
    ran, so the video's transition to ``succeeded`` (which happens inside
    ``_aggregate_video_status``) was never observed and the job stayed running.
    """
    job = repo.create_job(CreateJob(directory=str(video_dir), config_json="{}"))
    initialize_job(repo, job.job_id)

    # Drive every video through the full stage chain.
    for _ in range(3 * len(_STAGE_ORDER)):
        s = repo.claim_stage("test-worker", _utcnow())
        if s is None:
            break
        repo.complete_stage(s.stage_id, "test-worker", f"done-{s.stage_name}")
        advance_job(repo, job.job_id)

    job_row = repo.conn.execute(
        "SELECT status FROM task_jobs WHERE job_id=?", (job.job_id,)
    ).fetchone()
    assert job_row["status"] == "succeeded"

    # Every video should be succeeded too.
    pending = repo.conn.execute(
        "SELECT COUNT(*) FROM task_videos WHERE job_id=? AND status != 'succeeded'",
        (job.job_id,),
    ).fetchone()[0]
    assert pending == 0


def test_job_terminal_with_one_failed_video(repo: TaskRepository, video_dir: Path):
    """A job with one failed video and the rest succeeded must reach
    ``needs_attention`` (terminal), not stay ``running``."""
    from app.task_engine.models import StageError

    job = repo.create_job(CreateJob(directory=str(video_dir), config_json="{}"))
    initialize_job(repo, job.job_id)

    # Fail the first video's discover stage permanently.
    first = repo.claim_stage("test-worker", _utcnow())
    assert first is not None
    repo.fail_stage(first.stage_id, "test-worker", StageError("bad", "broken", transient=False))
    advance_job(repo, job.job_id)

    # Complete the other two videos fully.
    for _ in range(2 * len(_STAGE_ORDER)):
        s = repo.claim_stage("test-worker", _utcnow())
        if s is None:
            break
        repo.complete_stage(s.stage_id, "test-worker", f"done-{s.stage_name}")
        advance_job(repo, job.job_id)

    job_row = repo.conn.execute(
        "SELECT status FROM task_jobs WHERE job_id=?", (job.job_id,)
    ).fetchone()
    assert job_row["status"] == "needs_attention"


# ---------------------------------------------------------------------------
# Integration with worker
# ---------------------------------------------------------------------------

def test_worker_initializes_pending_jobs(repo: TaskRepository, video_dir: Path):
    """Worker.run_once should initialize pending jobs automatically."""
    job = repo.create_job(CreateJob(directory=str(video_dir), config_json="{}", extensions=".mp4,.mkv,.ts"))
    assert job.status == "pending"

    from app.task_engine.worker import TaskWorker
    worker = TaskWorker(repo, "test-worker", {})

    # run_once should pick up the pending job.
    result = worker.run_once()
    assert result is True

    # Job should now be running with videos.
    vids = repo.conn.execute(
        "SELECT COUNT(*) FROM task_videos WHERE job_id=?", (job.job_id,)
    ).fetchone()[0]
    assert vids == 3


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)

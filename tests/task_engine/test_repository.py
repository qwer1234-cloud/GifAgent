from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.task_engine import (
    ActiveJobConflictError,
    CreateJob,
    LeaseOwnershipError,
    StageError,
    TaskRepository,
    apply_task_schema,
    connect_task_db,
)

T0 = datetime(2026, 7, 17, tzinfo=timezone.utc)


def make_repo(tmp_path):
    conn = connect_task_db(tmp_path / "task.db")
    return TaskRepository(conn), conn


def make_stage(repo, directory="C:/video"):
    job = repo.create_job(CreateJob(directory=directory, config_json="{}"))
    video = repo.add_video(job.job_id, f"{directory}/a.mp4", "fp-a")
    stage = repo.ensure_stage(video.video_id, "sample", "input-a")
    return job, video, stage


def stage_status(conn, stage_id):
    row = conn.execute(
        "SELECT status FROM task_stages WHERE stage_id=?", (stage_id,)
    ).fetchone()
    return row[0]


def test_schema_creates_required_tables(tmp_path):
    conn = connect_task_db(tmp_path / "task.db")
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {
        "task_jobs",
        "task_videos",
        "task_stages",
        "task_artifacts",
        "task_events",
        "task_commands",
        "task_migrations",
    }.issubset(tables)


def test_schema_is_idempotent(tmp_path):
    conn = connect_task_db(tmp_path / "task.db")
    apply_task_schema(conn)
    apply_task_schema(conn)


def test_connect_task_db_env_override(tmp_path, monkeypatch):
    env_path = tmp_path / "env.db"
    monkeypatch.setenv("GIFAGENT_TASK_DB", str(env_path))
    conn = connect_task_db()
    conn.close()
    assert env_path.exists()


def test_connect_task_db_explicit_path_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GIFAGENT_TASK_DB", str(tmp_path / "env.db"))
    explicit = tmp_path / "explicit.db"
    conn = connect_task_db(explicit)
    conn.close()
    assert explicit.exists()


def test_create_job_returns_pending_record(tmp_path):
    repo, _ = make_repo(tmp_path)
    job = repo.create_job(
        CreateJob(directory="C:/video", config_json="{}", limit=5, extensions="mp4")
    )
    assert job.job_id
    assert job.directory == "C:/video"
    assert job.status == "pending"


def test_second_active_job_on_same_directory_conflicts(tmp_path):
    repo, _ = make_repo(tmp_path)
    repo.create_job(CreateJob(directory="C:/video", config_json="{}"))
    with pytest.raises(ActiveJobConflictError):
        repo.create_job(CreateJob(directory="C:/video/", config_json="{}"))


def test_active_job_conflict_carries_existing_job_id(tmp_path):
    repo, _ = make_repo(tmp_path)
    job = repo.create_job(CreateJob(directory="C:/video", config_json="{}"))
    with pytest.raises(ActiveJobConflictError) as exc_info:
        repo.create_job(CreateJob(directory="C:/video", config_json="{}"))
    assert exc_info.value.existing_job_id == job.job_id


def test_active_directory_conflict_is_case_insensitive(tmp_path):
    repo, _ = make_repo(tmp_path)
    repo.create_job(CreateJob(directory="C:/Video", config_json="{}"))
    with pytest.raises(ActiveJobConflictError):
        repo.create_job(CreateJob(directory="c:/video", config_json="{}"))


def test_directory_reusable_after_first_job_terminal(tmp_path):
    repo, conn = make_repo(tmp_path)
    job = repo.create_job(CreateJob(directory="C:/video", config_json="{}"))
    conn.execute(
        "UPDATE task_jobs SET status='succeeded' WHERE job_id=?", (job.job_id,)
    )
    conn.commit()
    job2 = repo.create_job(CreateJob(directory="C:/video", config_json="{}"))
    assert job2.job_id != job.job_id


def test_add_video_returns_pending_record(tmp_path):
    repo, _ = make_repo(tmp_path)
    job = repo.create_job(CreateJob(directory="C:/video", config_json="{}"))
    video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
    assert video.video_id
    assert video.job_id == job.job_id
    assert video.path == "C:/video/a.mp4"
    assert video.fingerprint == "fp-a"
    assert video.status == "pending"


def test_add_video_is_idempotent_per_job_and_path(tmp_path):
    repo, conn = make_repo(tmp_path)
    job = repo.create_job(CreateJob(directory="C:/video", config_json="{}"))
    first = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
    second = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
    assert second.video_id == first.video_id
    assert second.fingerprint == "fp-a"
    other = repo.add_video(job.job_id, "C:/video/b.mp4", "fp-b")
    assert other.video_id != first.video_id
    count = conn.execute(
        "SELECT COUNT(*) FROM task_videos WHERE job_id=?", (job.job_id,)
    ).fetchone()[0]
    assert count == 2


def test_ensure_stage_is_idempotent(tmp_path):
    repo, _ = make_repo(tmp_path)
    job = repo.create_job(CreateJob(directory="C:/video", config_json="{}"))
    video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
    first = repo.ensure_stage(video.video_id, "sample", "input-a")
    second = repo.ensure_stage(video.video_id, "sample", "input-a")
    assert first.stage_id == second.stage_id
    assert first.status == "pending"
    assert first.attempt_count == 0
    other = repo.ensure_stage(video.video_id, "sample", "input-b")
    assert other.stage_id != first.stage_id
    clipped = repo.ensure_stage(video.video_id, "gif_clip", "input-a", clip_id="clip-1")
    assert clipped.stage_id not in (first.stage_id, other.stage_id)
    assert repo.ensure_stage(
        video.video_id, "gif_clip", "input-a", clip_id="clip-1"
    ).stage_id == clipped.stage_id


def test_append_command_persists_and_returns_id(tmp_path):
    repo, conn = make_repo(tmp_path)
    job = repo.create_job(CreateJob(directory="C:/video", config_json="{}"))
    command_id = repo.append_command(job.job_id, "pause", {"reason": "user"})
    assert isinstance(command_id, str) and command_id
    row = conn.execute(
        "SELECT job_id, kind, payload_json FROM task_commands WHERE command_id=?",
        (command_id,),
    ).fetchone()
    assert row[0] == job.job_id
    assert row[1] == "pause"
    assert json.loads(row[2]) == {"reason": "user"}


def test_append_command_rejects_unknown_job(tmp_path):
    repo, _ = make_repo(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        repo.append_command("no-such-job", "pause", {})


def test_claim_returns_none_when_nothing_pending(tmp_path):
    repo, _ = make_repo(tmp_path)
    assert repo.claim_stage("worker-1", T0) is None


def test_claim_stage_multi_connection_single_winner(tmp_path):
    db_path = tmp_path / "task.db"
    setup_conn = connect_task_db(db_path)
    setup_repo = TaskRepository(setup_conn)
    job = setup_repo.create_job(CreateJob(directory="C:/video", config_json="{}"))
    video = setup_repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
    stage = setup_repo.ensure_stage(video.video_id, "sample", "input-a")
    setup_conn.close()

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    results: list = [None] * n_threads
    errors: list = []

    def worker(index):
        conn = connect_task_db(db_path)
        try:
            repo = TaskRepository(conn)
            barrier.wait(timeout=15)
            results[index] = repo.claim_stage(f"worker-{index}", T0)
        except Exception as exc:  # noqa: BLE001 - surfaced via assertion below
            errors.append(exc)
        finally:
            conn.close()

    threads = [
        threading.Thread(target=worker, args=(i,)) for i in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive()

    assert not errors
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0].stage_id == stage.stage_id
    assert winners[0].attempt_count == 1


def test_claim_pending_stage_leases_it(tmp_path):
    repo, _ = make_repo(tmp_path)
    _, _, stage = make_stage(repo)
    claimed = repo.claim_stage("worker-1", T0)
    assert claimed is not None
    assert claimed.stage_id == stage.stage_id
    assert claimed.status == "leased"
    assert claimed.attempt_count == 1


def test_unexpired_lease_is_not_claimable(tmp_path):
    repo, _ = make_repo(tmp_path)
    make_stage(repo)
    first = repo.claim_stage("worker-1", T0, lease_seconds=90)
    assert first is not None
    assert repo.claim_stage("worker-2", T0 + timedelta(seconds=10)) is None


def test_expired_lease_is_reclaimable(tmp_path):
    conn = connect_task_db(tmp_path / "task.db")
    repo = TaskRepository(conn)
    job = repo.create_job(CreateJob(directory="C:/video", config_json="{}"))
    video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
    repo.ensure_stage(video.video_id, "sample", "input-a")

    first = repo.claim_stage("worker-1", datetime(2026, 7, 17, tzinfo=timezone.utc), lease_seconds=1)
    second = repo.claim_stage("worker-2", datetime(2026, 7, 17, 0, 0, 2, tzinfo=timezone.utc))

    assert first is not None
    assert second is not None
    assert first.stage_id == second.stage_id
    assert second.attempt_count == 2


def test_complete_stage_stores_output_key(tmp_path):
    repo, conn = make_repo(tmp_path)
    make_stage(repo)
    stage = repo.claim_stage("worker-1", T0)
    repo.complete_stage(stage.stage_id, "worker-1", "out-key")
    row = conn.execute(
        "SELECT status, output_key, lease_owner FROM task_stages WHERE stage_id=?",
        (stage.stage_id,),
    ).fetchone()
    assert row[0] == "succeeded"
    assert row[1] == "out-key"
    assert row[2] is None


def test_complete_stage_clears_last_error(tmp_path):
    repo, conn = make_repo(tmp_path)
    make_stage(repo)
    now = datetime.now(timezone.utc)
    stage = repo.claim_stage("worker-1", now)
    repo.fail_stage(
        stage.stage_id, "worker-1", StageError("timeout", "slow", transient=True)
    )
    row = conn.execute(
        "SELECT last_error_json FROM task_stages WHERE stage_id=?",
        (stage.stage_id,),
    ).fetchone()
    assert row[0] is not None

    reclaimed = repo.claim_stage("worker-1", now + timedelta(seconds=10))
    assert reclaimed is not None
    repo.complete_stage(reclaimed.stage_id, "worker-1", "out-key")
    row = conn.execute(
        "SELECT status, last_error_json FROM task_stages WHERE stage_id=?",
        (stage.stage_id,),
    ).fetchone()
    assert row[0] == "succeeded"
    assert row[1] is None


def test_wrong_worker_cannot_complete_lease(tmp_path):
    repo = TaskRepository(connect_task_db(tmp_path / "task.db"))
    job = repo.create_job(CreateJob(directory="C:/video", config_json="{}"))
    video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
    repo.ensure_stage(video.video_id, "sample", "input-a")
    stage = repo.claim_stage("worker-1", datetime.now(timezone.utc))

    with pytest.raises(LeaseOwnershipError):
        repo.complete_stage(stage.stage_id, "worker-2", "out")


def test_wrong_worker_cannot_fail_lease(tmp_path):
    repo, _ = make_repo(tmp_path)
    make_stage(repo)
    stage = repo.claim_stage("worker-1", T0)
    with pytest.raises(LeaseOwnershipError):
        repo.fail_stage(
            stage.stage_id, "worker-2", StageError("x", "boom", transient=True)
        )


def test_transient_failure_goes_to_retry_wait_and_is_reclaimable(tmp_path):
    repo, conn = make_repo(tmp_path)
    make_stage(repo)
    now = datetime.now(timezone.utc)
    stage = repo.claim_stage("worker-1", now)
    repo.fail_stage(
        stage.stage_id, "worker-1", StageError("timeout", "slow", transient=True)
    )
    assert stage_status(conn, stage.stage_id) == "retry_wait"
    assert repo.claim_stage("worker-2", now) is None
    again = repo.claim_stage("worker-2", now + timedelta(seconds=10))
    assert again is not None
    assert again.stage_id == stage.stage_id
    assert again.attempt_count == 2


def test_first_transient_failure_retries_after_base_delay(tmp_path):
    repo, conn = make_repo(tmp_path)
    make_stage(repo)
    now = datetime.now(timezone.utc)
    stage = repo.claim_stage("worker-1", now)
    repo.fail_stage(
        stage.stage_id, "worker-1", StageError("timeout", "slow", transient=True)
    )
    row = conn.execute(
        "SELECT retry_at FROM task_stages WHERE stage_id=?", (stage.stage_id,)
    ).fetchone()
    retry_at = datetime.fromisoformat(row[0])
    delta = (retry_at - now).total_seconds()
    # RetryPolicy default: base_delay_seconds * 2**(attempt_count-1) = 5 * 2**0
    assert abs(delta - 5.0) < 1.0
    # Not yet claimable before the backoff elapses, claimable shortly after.
    assert repo.claim_stage("worker-2", now + timedelta(seconds=2)) is None
    assert repo.claim_stage("worker-2", now + timedelta(seconds=6)) is not None


def test_non_transient_failure_needs_attention_and_not_claimable(tmp_path):
    repo, conn = make_repo(tmp_path)
    make_stage(repo)
    stage = repo.claim_stage("worker-1", T0)
    repo.fail_stage(
        stage.stage_id, "worker-1", StageError("corrupt", "bad input", transient=False)
    )
    assert stage_status(conn, stage.stage_id) == "needs_attention"
    assert repo.claim_stage("worker-2", T0 + timedelta(hours=1)) is None


def test_transient_failure_exhausts_attempts_to_needs_attention(tmp_path):
    repo, conn = make_repo(tmp_path)
    make_stage(repo)
    now = datetime.now(timezone.utc)
    stage_id = None
    for i in range(3):
        stage = repo.claim_stage("worker-1", now + timedelta(seconds=100 * i))
        assert stage is not None
        stage_id = stage.stage_id
        repo.fail_stage(
            stage_id, "worker-1", StageError("timeout", "slow", transient=True)
        )
    assert stage_status(conn, stage_id) == "needs_attention"
    assert repo.claim_stage("worker-1", now + timedelta(hours=1)) is None


def test_events_are_ordered_filtered_and_limited(tmp_path):
    repo, _ = make_repo(tmp_path)
    _, video, _ = make_stage(repo)
    repo.ensure_stage(video.video_id, "vlm", "input-a")
    first = repo.claim_stage("worker-1", T0)
    repo.complete_stage(first.stage_id, "worker-1", "out-1")
    second = repo.claim_stage("worker-1", T0 + timedelta(seconds=1))
    repo.fail_stage(
        second.stage_id, "worker-1", StageError("x", "boom", transient=False)
    )

    events = repo.list_events()
    assert len(events) == 4
    ids = [e.event_id for e in events]
    assert ids == sorted(ids)
    assert all(isinstance(i, int) for i in ids)
    assert [e.kind for e in events] == [
        "stage.claimed",
        "stage.completed",
        "stage.claimed",
        "stage.failed",
    ]
    assert all(isinstance(e.payload, dict) for e in events)
    assert events[0].payload["stage_id"] == first.stage_id

    rest = repo.list_events(after_id=events[0].event_id)
    assert [e.event_id for e in rest] == ids[1:]

    limited = repo.list_events(limit=2)
    assert [e.event_id for e in limited] == ids[:2]


def test_list_events_clamps_limit_to_at_least_one(tmp_path):
    repo, _ = make_repo(tmp_path)
    make_stage(repo)
    repo.claim_stage("worker-1", T0)
    repo.claim_stage("worker-1", T0 + timedelta(seconds=100))
    assert len(repo.list_events(limit=0)) == 1
    assert len(repo.list_events(limit=-5)) == 1


def test_claim_event_includes_lease_expiry(tmp_path):
    repo, _ = make_repo(tmp_path)
    make_stage(repo)
    stage = repo.claim_stage("worker-1", T0, lease_seconds=90)
    claimed = [e for e in repo.list_events() if e.kind == "stage.claimed"]
    assert len(claimed) == 1
    payload = claimed[0].payload
    assert payload["stage_id"] == stage.stage_id
    expected = (T0 + timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
    assert payload["lease_expires_at"] == expected

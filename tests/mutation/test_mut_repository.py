"""W4 kill tests for task_engine.repository mutation survivors.

Targets survivors from the cosmic-ray run recorded in
``<sprint-root>/w4/mutation_scores/cosmic-ray/repository.json``:

- ``complete_stage_with_artifacts`` ownership guards: every mismatch
  dimension (stage_id / job_id / video_id / stage_name / clip_id) must raise
  in BOTH lexicographic directions, and matching refs must complete cleanly
  (``==``-raising mutants).
- ``create_job`` scope-conflict detection: same scope raises
  ``ActiveJobConflictError``, different scopes are accepted in both
  lexicographic orders.
- ``claim_stage``: default 90-second lease, empty ``stage_names`` short
  circuit, and the stale-implicit-transaction commit.
- ``_find_active_job_id`` returns the active job id.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.task_engine.models import ArtifactRef, CreateJob
from app.task_engine.repository import ActiveJobConflictError, TaskRepository, _iso
from app.task_engine.schema import connect_task_db

T0 = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)


def _repo(tmp_path: Path):
    conn = connect_task_db(tmp_path / "task.db")
    return TaskRepository(conn), conn


def _seed_stage(repo, conn, tmp_path: Path, *, directory="C:/video"):
    job = repo.create_job(CreateJob(directory=directory, config_json="{}"))
    video = repo.add_video(job.job_id, f"{directory}/a.mp4", "fp-a")
    stage = repo.ensure_stage(video.video_id, "sample", "input-a")
    return job, video, stage


def _ref(path: Path, **overrides):
    import hashlib

    data = path.read_bytes()
    defaults = dict(
        artifact_id="art-1",
        job_id="j1",
        video_id="v1",
        stage_name="sample",
        clip_id=None,
        path=str(path),
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        provenance_json="{}",
        stage_id="",
        artifact_kind="frames",
    )
    defaults.update(overrides)
    return ArtifactRef(**defaults)


def test_complete_stage_rejects_mismatched_artifact_ownership(tmp_path):
    repo, conn = _repo(tmp_path)
    job, video, stage = _seed_stage(repo, conn, tmp_path)
    repo.claim_stage("w1", T0, stage_names=["sample"])
    payload = tmp_path / "a.bin"
    payload.write_bytes(b"payload")

    base = dict(job_id=job.job_id, video_id=video.video_id, stage_name="sample", stage_id=stage.stage_id)

    with pytest.raises(ValueError, match="stage_id"):
        repo.complete_stage_with_artifacts(
            stage.stage_id, "w1", "out", (_ref(payload, **{**base, "stage_id": "zzz-not-the-stage"}),)
        )
    with pytest.raises(ValueError, match="stage_id"):
        repo.complete_stage_with_artifacts(
            stage.stage_id, "w1", "out", (_ref(payload, **{**base, "stage_id": "aaa-not-the-stage"}),)
        )
    with pytest.raises(ValueError, match="belongs to job"):
        repo.complete_stage_with_artifacts(
            stage.stage_id, "w1", "out", (_ref(payload, **{**base, "job_id": "zzz-job"}),)
        )
    with pytest.raises(ValueError, match="belongs to video"):
        repo.complete_stage_with_artifacts(
            stage.stage_id, "w1", "out", (_ref(payload, **{**base, "video_id": "zzz-video"}),)
        )
    with pytest.raises(ValueError, match="stage_name"):
        repo.complete_stage_with_artifacts(
            stage.stage_id, "w1", "out", (_ref(payload, **{**base, "stage_name": "zzz"}),)
        )
    with pytest.raises(ValueError, match="clip_id"):
        repo.complete_stage_with_artifacts(
            stage.stage_id, "w1", "out", (_ref(payload, **{**base, "clip_id": "zzz-clip"}),)
        )


def test_complete_stage_accepts_matching_artifact(tmp_path):
    repo, conn = _repo(tmp_path)
    job, video, stage = _seed_stage(repo, conn, tmp_path)
    repo.claim_stage("w1", T0, stage_names=["sample"])
    payload = tmp_path / "ok.bin"
    payload.write_bytes(b"ok-bytes")
    ref = _ref(payload, job_id=job.job_id, video_id=video.video_id, stage_name="sample", stage_id=stage.stage_id)
    repo.complete_stage_with_artifacts(stage.stage_id, "w1", "out", (ref,))
    row = conn.execute("SELECT status FROM task_stages WHERE stage_id=?", (stage.stage_id,)).fetchone()
    assert row["status"] == "succeeded"


def test_create_job_scope_conflict_and_different_scope_accepted(tmp_path):
    repo, conn = _repo(tmp_path)
    first = repo.create_job(CreateJob(directory="C:/video", config_json="{}"))
    with pytest.raises(ActiveJobConflictError):
        repo.create_job(CreateJob(directory="C:/video", config_json="{}"))

    # Distinct video_paths lists are distinct scopes; both lexicographic
    # orders must be accepted (comparison-operator mutants raise on one).
    second = repo.create_job(
        CreateJob(directory="C:/video", config_json='{"video_paths": ["b.mp4"]}')
    )
    assert second.job_id != first.job_id
    third = repo.create_job(
        CreateJob(directory="C:/video", config_json='{"video_paths": ["a.mp4"]}')
    )
    assert third.job_id != second.job_id


def test_claim_stage_default_lease_is_ninety_seconds(tmp_path):
    repo, conn = _repo(tmp_path)
    job, video, stage = _seed_stage(repo, conn, tmp_path)
    claimed = repo.claim_stage("w1", T0)
    assert claimed is not None
    row = conn.execute(
        "SELECT lease_expires_at FROM task_stages WHERE stage_id=?", (stage.stage_id,)
    ).fetchone()
    assert row["lease_expires_at"] == _iso(T0 + timedelta(seconds=90))


def test_claim_stage_empty_stage_names_returns_none(tmp_path):
    repo, conn = _repo(tmp_path)
    _seed_stage(repo, conn, tmp_path)
    assert repo.claim_stage("w1", T0, stage_names=[]) is None


def test_claim_stage_commits_stale_implicit_transaction(tmp_path):
    repo, conn = _repo(tmp_path)
    _seed_stage(repo, conn, tmp_path)
    conn.execute("UPDATE task_jobs SET updated_at='t'")  # opens an implicit txn
    assert conn.in_transaction
    claimed = repo.claim_stage("w2", T0)
    assert claimed is not None


def test_find_active_job_id_returns_job(tmp_path):
    repo, conn = _repo(tmp_path)
    job = repo.create_job(CreateJob(directory="C:/video", config_json="{}"))
    assert repo._find_active_job_id(_directory_key_of(conn, job.job_id)) == job.job_id


def _directory_key_of(conn: sqlite3.Connection, job_id: str) -> str:
    return conn.execute(
        "SELECT directory_key FROM task_jobs WHERE job_id=?", (job_id,)
    ).fetchone()["directory_key"]

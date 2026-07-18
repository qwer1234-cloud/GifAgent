"""P1-6: Tests for task command and status API endpoints."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.task_engine.schema import apply_task_schema
from app.task_engine.repository import TaskRepository


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def repo():
    """Return a (TaskRepository, sqlite3.Connection) backed by :memory:."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    apply_task_schema(conn)
    return TaskRepository(conn), conn


@pytest.fixture
def client(repo):
    """Return a TestClient whose get_task_repo dependency is overridden."""
    from app.routers.tasks import get_task_repo, router

    _repo, _conn = repo

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_task_repo] = lambda: _repo
    return TestClient(app), _repo, _conn


# ---------------------------------------------------------------------------
# POST /api/tasks/jobs
# ---------------------------------------------------------------------------

def test_create_job_returns_201(client, tmp_path):
    cl, repo, conn = client
    work_dir = tmp_path / "my_job"
    work_dir.mkdir()

    resp = cl.post(
        "/api/tasks/jobs",
        json={"directory": str(work_dir), "limit": 10, "extensions": ".mp4"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["directory"] == str(work_dir.resolve())
    assert data["status"] == "pending"
    assert data["folder"] == work_dir.name
    assert data["video_count"] == 0
    assert data["stage_count"] == 0
    assert data["clip_count"] == 0
    assert "job_id" in data
    assert "created_at" in data

    # Verify it is persisted
    row = conn.execute(
        "SELECT * FROM task_jobs WHERE job_id=?", (data["job_id"],)
    ).fetchone()
    assert row is not None
    assert row["directory"] == str(work_dir.resolve())
    assert row["job_limit"] == 10
    assert row["extensions"] == ".mp4"


def test_create_job_defaults_limit_and_extensions(client, tmp_path):
    cl, repo, conn = client
    work_dir = tmp_path / "default_job"
    work_dir.mkdir()

    resp = cl.post("/api/tasks/jobs", json={"directory": str(work_dir)})
    assert resp.status_code == 201
    data = resp.json()
    assert "job_id" in data

    row = conn.execute(
        "SELECT * FROM task_jobs WHERE job_id=?", (data["job_id"],)
    ).fetchone()
    assert row["job_limit"] == 0
    assert row["extensions"] == ""


def test_create_job_nonexistent_directory_returns_400(client, tmp_path):
    cl, repo, conn = client
    dne = tmp_path / "does_not_exist"

    resp = cl.post("/api/tasks/jobs", json={"directory": str(dne)})
    assert resp.status_code == 400


def test_create_job_recomputes_config_hash_not_trusting_request(
    client, tmp_path, monkeypatch,
):
    """§3.5 (fourth review): the persisted ``config_hash`` must equal the
    hash of the FINAL merged business config (server base + request
    override), NOT a stale hash carried in the request body."""
    cl, repo, conn = client
    work_dir = tmp_path / "hash_job"
    work_dir.mkdir()

    base = {
        "adaptive": {"sample_interval": 8, "max_output": 60},
        "models": {},
    }
    monkeypatch.setattr("app.routers.tasks.load_config", lambda: base)

    resp = cl.post(
        "/api/tasks/jobs",
        json={
            "directory": str(work_dir),
            "config_json": {
                "adaptive": {"sample_interval": 4},
                "config_hash": "STALE_OLD_HASH_FROM_REQUEST",
            },
        },
    )
    assert resp.status_code == 201
    job_id = resp.json()["job_id"]

    row = conn.execute(
        "SELECT config_json FROM task_jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    persisted = json.loads(row["config_json"])
    assert persisted["config_hash"] != "STALE_OLD_HASH_FROM_REQUEST"
    # The override took effect and base fields were preserved.
    assert persisted["adaptive"]["sample_interval"] == 4
    assert persisted["adaptive"]["max_output"] == 60

    # The persisted hash must match the hash of the final merged business
    # config (excluding runtime metadata: keys starting with '_' and
    # config_hash itself).
    from app.task_engine.fingerprints import canonical_hash
    expected_business = {
        "adaptive": {"sample_interval": 4, "max_output": 60},
        "models": {},
        "video_paths": [],
    }
    assert persisted["config_hash"] == canonical_hash(expected_business)


def test_duplicate_active_directory_returns_409(client, tmp_path):
    cl, repo, conn = client
    work_dir = tmp_path / "conflict_job"
    work_dir.mkdir()

    resp1 = cl.post("/api/tasks/jobs", json={"directory": str(work_dir)})
    assert resp1.status_code == 201
    job_id_1 = resp1.json()["job_id"]

    resp2 = cl.post("/api/tasks/jobs", json={"directory": str(work_dir)})
    assert resp2.status_code == 409
    detail = resp2.json()["detail"]
    assert detail["detail"] == "active job exists for directory"
    assert detail["existing_job_id"] == job_id_1


def test_completed_job_does_not_block_new_job_same_directory(client, tmp_path):
    cl, repo, conn = client
    work_dir = tmp_path / "completed_job"
    work_dir.mkdir()

    resp1 = cl.post("/api/tasks/jobs", json={"directory": str(work_dir)})
    job_id_1 = resp1.json()["job_id"]
    conn.execute(
        "UPDATE task_jobs SET status='succeeded' WHERE job_id=?", (job_id_1,)
    )
    conn.commit()

    resp2 = cl.post("/api/tasks/jobs", json={"directory": str(work_dir)})
    assert resp2.status_code == 201
    assert resp2.json()["job_id"] != job_id_1


# ---------------------------------------------------------------------------
# POST /api/tasks/jobs/{job_id}/cancel
# ---------------------------------------------------------------------------

def test_cancel_job_appends_command(client, tmp_path):
    cl, repo, conn = client
    work_dir = tmp_path / "cancel_job"
    work_dir.mkdir()

    create_resp = cl.post("/api/tasks/jobs", json={"directory": str(work_dir)})
    job_id = create_resp.json()["job_id"]

    resp = cl.post(f"/api/tasks/jobs/{job_id}/cancel")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "command_id" in data

    # Verify the command was appended (not directly mutating the job)
    cmd_row = conn.execute(
        "SELECT kind FROM task_commands WHERE job_id=?", (job_id,)
    ).fetchone()
    assert cmd_row is not None
    assert cmd_row["kind"] == "cancel"

    # Job status should still be 'pending' — the worker processes the command
    job_row = conn.execute(
        "SELECT status FROM task_jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    assert job_row["status"] == "pending"


def test_cancel_nonexistent_job_returns_404(client):
    cl, repo, conn = client
    resp = cl.post("/api/tasks/jobs/bogus-id/cancel")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/tasks/jobs/{job_id}/retry
# ---------------------------------------------------------------------------

def test_retry_job_appends_command(client, tmp_path):
    cl, repo, conn = client
    work_dir = tmp_path / "retry_job"
    work_dir.mkdir()

    create_resp = cl.post("/api/tasks/jobs", json={"directory": str(work_dir)})
    job_id = create_resp.json()["job_id"]

    resp = cl.post(f"/api/tasks/jobs/{job_id}/retry")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "command_id" in data

    cmd_row = conn.execute(
        "SELECT kind FROM task_commands WHERE job_id=?", (job_id,)
    ).fetchone()
    assert cmd_row is not None
    assert cmd_row["kind"] == "retry"

    # Verify the worker-owned lifecycle rows were NOT directly mutated
    job_row = conn.execute(
        "SELECT status FROM task_jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    assert job_row["status"] == "pending"


def test_retry_nonexistent_job_returns_404(client):
    cl, repo, conn = client
    resp = cl.post("/api/tasks/jobs/bogus-id/retry")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/tasks/jobs
# ---------------------------------------------------------------------------

def test_list_jobs_returns_all_with_counts(client, tmp_path):
    cl, repo, conn = client

    # Create two jobs
    d1 = tmp_path / "list_a"
    d1.mkdir()
    d2 = tmp_path / "list_b"
    d2.mkdir()
    r1 = cl.post("/api/tasks/jobs", json={"directory": str(d1)})
    r2 = cl.post("/api/tasks/jobs", json={"directory": str(d2)})
    j1 = r1.json()["job_id"]
    j2 = r2.json()["job_id"]

    # Add a video + stage to j1 to verify counts
    v_id = "test-video-1"
    conn.execute(
        """INSERT INTO task_videos
           (video_id, job_id, path, fingerprint, status, created_at, updated_at)
           VALUES (?,?,?,?,'pending','2026-07-18T00:00:00+00:00','2026-07-18T00:00:00+00:00')""",
        (v_id, j1, "/videos/test.mp4", "fp-abc"),
    )
    conn.execute(
        """INSERT INTO task_stages
           (stage_id, video_id, stage_name, clip_id, input_key, status,
            attempt_count, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ("s1", v_id, "discover", None, "input://discover", "pending", 0,
         "2026-07-18T00:00:00+00:00", "2026-07-18T00:00:00+00:00"),
    )
    conn.execute(
        """INSERT INTO task_stages
           (stage_id, video_id, stage_name, clip_id, input_key, status,
            attempt_count, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ("s2", v_id, "sample", "clip-001", "input://sample", "pending", 0,
         "2026-07-18T00:00:00+00:00", "2026-07-18T00:00:00+00:00"),
    )
    conn.commit()

    resp = cl.get("/api/tasks/jobs")
    assert resp.status_code == 200
    jobs = resp.json()
    assert len(jobs) == 2

    # Jobs are ordered by created_at DESC
    by_id = {j["job_id"]: j for j in jobs}

    j1_resp = by_id[j1]
    assert j1_resp["folder"] == d1.name
    assert j1_resp["video_count"] == 1
    assert j1_resp["stage_count"] == 2
    assert j1_resp["clip_count"] == 1  # only s2 has a clip_id

    j2_resp = by_id[j2]
    assert j2_resp["folder"] == d2.name
    assert j2_resp["video_count"] == 0
    assert j2_resp["stage_count"] == 0
    assert j2_resp["clip_count"] == 0


# ---------------------------------------------------------------------------
# GET /api/tasks/jobs/{job_id}
# ---------------------------------------------------------------------------

def test_get_job_returns_detail_with_videos(client, tmp_path):
    cl, repo, conn = client
    work_dir = tmp_path / "detail_job"
    work_dir.mkdir()

    resp = cl.post("/api/tasks/jobs", json={"directory": str(work_dir)})
    job_id = resp.json()["job_id"]

    # Insert a video
    conn.execute(
        """INSERT INTO task_videos
           (video_id, job_id, path, fingerprint, status, created_at, updated_at)
           VALUES (?,?,?,?,'pending','2026-07-18T00:00:00+00:00','2026-07-18T00:00:00+00:00')""",
        ("vid-1", job_id, "/videos/shot.mp4", "fp-xyz"),
    )
    conn.commit()

    resp = cl.get(f"/api/tasks/jobs/{job_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["job_id"] == job_id
    assert data["folder"] == work_dir.name
    assert data["video_count"] == 1
    assert len(data["videos"]) == 1
    assert data["videos"][0]["path"] == "/videos/shot.mp4"
    assert data["videos"][0]["fingerprint"] == "fp-xyz"


def test_get_job_not_found_returns_404(client):
    cl, repo, conn = client
    resp = cl.get("/api/tasks/jobs/nonexistent-id")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/tasks/events
# ---------------------------------------------------------------------------

def test_events_pagination_is_stable_by_event_id(client, tmp_path):
    cl, repo, conn = client

    # Insert events directly (they are side-effects of worker operations)
    for i in range(5):
        conn.execute(
            "INSERT INTO task_events (kind, payload_json, created_at) VALUES (?,?,?)",
            ("test.event", json.dumps({"seq": i}), "2026-07-18T00:00:00+00:00"),
        )
    conn.commit()

    # Page 1: first 3 events (after_id=0)
    r1 = cl.get("/api/tasks/events?after_id=0&limit=3")
    assert r1.status_code == 200
    p1 = r1.json()
    assert len(p1) == 3
    assert p1[0]["event_id"] == 1
    assert p1[2]["event_id"] == 3

    # Page 2: next 3 events (after_id=3, should get events 4 and 5)
    r2 = cl.get("/api/tasks/events?after_id=3&limit=3")
    assert r2.status_code == 200
    p2 = r2.json()
    assert len(p2) == 2
    assert p2[0]["event_id"] == 4
    assert p2[1]["event_id"] == 5

    # Verify payload and created_at are present
    assert p1[0]["kind"] == "test.event"
    assert p1[0]["payload"] == {"seq": 0}
    assert p1[0]["created_at"] == "2026-07-18T00:00:00+00:00"


def test_events_default_limit_and_after_id(client, tmp_path):
    cl, repo, conn = client

    # Insert 250 events
    for i in range(250):
        conn.execute(
            "INSERT INTO task_events (kind, payload_json, created_at) VALUES (?,?,?)",
            ("bulk", json.dumps({"i": i}), "2026-07-18T00:00:00+00:00"),
        )
    conn.commit()

    resp = cl.get("/api/tasks/events")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 200  # default limit


# ---------------------------------------------------------------------------
# GET /api/tasks/attention
# ---------------------------------------------------------------------------

def test_attention_lists_jobs_needing_attention(client, tmp_path):
    cl, repo, conn = client

    d1 = tmp_path / "attention_ok"
    d1.mkdir()
    d2 = tmp_path / "attention_bad"
    d2.mkdir()

    r_ok = cl.post("/api/tasks/jobs", json={"directory": str(d1)})
    r_bad = cl.post("/api/tasks/jobs", json={"directory": str(d2)})
    ok_id = r_ok.json()["job_id"]
    bad_id = r_bad.json()["job_id"]

    # Give bad job a video + stage with needs_attention
    conn.execute(
        """INSERT INTO task_videos
           (video_id, job_id, path, fingerprint, status, created_at, updated_at)
           VALUES (?,?,?,?,'pending','2026-07-18T00:00:00+00:00','2026-07-18T00:00:00+00:00')""",
        ("vid-attn", bad_id, "/videos/bad.mp4", "fp-attn"),
    )
    conn.execute(
        """INSERT INTO task_stages
           (stage_id, video_id, stage_name, clip_id, input_key, status,
            attempt_count, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ("s-attn", "vid-attn", "vlm", None, "input://vlm", "needs_attention",
         3, "2026-07-18T00:00:00+00:00", "2026-07-18T00:00:00+00:00"),
    )
    conn.commit()

    resp = cl.get("/api/tasks/attention")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    entry = data[0]
    assert entry["job_id"] == bad_id
    assert entry["folder"] == d2.name
    assert entry["attention_count"] == 1


def test_attention_empty_when_no_jobs_need_it(client):
    cl, repo, conn = client
    resp = cl.get("/api/tasks/attention")
    assert resp.status_code == 200
    assert resp.json() == []

"""P1-6: Task command and status APIs."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.config import load_config
from app.quality_lab.config_builder import deep_merge, normalize_task_config
from app.task_engine.models import CreateJob, JobStatus
from app.task_engine.repository import (
    ActiveJobConflictError,
    TaskRepository,
)
from app.task_engine.schema import connect_task_db

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class CreateJobRequest(BaseModel):
    directory: str
    limit: int = 0
    extensions: str = ""
    video_paths: list[str] = []
    config_json: dict | None = None


class JobResponse(BaseModel):
    job_id: str
    directory: str
    status: JobStatus
    folder: str
    video_count: int
    stage_count: int
    clip_count: int
    created_at: str


class EventResponse(BaseModel):
    event_id: int
    kind: str
    payload: dict
    created_at: str


class CommandResponse(BaseModel):
    status: str
    command_id: str


class JobDetailResponse(JobResponse):
    videos: list[dict]


class AttentionItem(BaseModel):
    job_id: str
    directory: str
    folder: str
    status: str
    attention_count: int
    created_at: str


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------

def get_task_repo():
    """Yield a short-lived TaskRepository per request."""
    conn = connect_task_db()
    try:
        repo = TaskRepository(conn)
        yield repo
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_directory(directory: str) -> str:
    path = Path(os.path.expanduser(directory))
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve(strict=False)
    if not path.exists() or not path.is_dir():
        raise HTTPException(
            status_code=400, detail=f"Directory does not exist: {directory}"
        )
    return str(path)


def _ensure_job_exists(conn: sqlite3.Connection, job_id: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM task_jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")


def _get_job_row(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM task_jobs WHERE job_id=?", (job_id,)
    ).fetchone()


def _row_to_job_response(row: sqlite3.Row, conn: sqlite3.Connection) -> JobResponse:
    video_count = conn.execute(
        "SELECT COUNT(*) FROM task_videos WHERE job_id=?", (row["job_id"],)
    ).fetchone()[0]
    stage_count = conn.execute(
        """SELECT COUNT(*) FROM task_stages s
           JOIN task_videos v ON s.video_id = v.video_id
           WHERE v.job_id=?""",
        (row["job_id"],),
    ).fetchone()[0]
    clip_count = conn.execute(
        """SELECT COUNT(*) FROM task_stages s
           JOIN task_videos v ON s.video_id = v.video_id
           WHERE v.job_id=? AND s.clip_id IS NOT NULL""",
        (row["job_id"],),
    ).fetchone()[0]
    folder = Path(row["directory"]).name
    return JobResponse(
        job_id=row["job_id"],
        directory=row["directory"],
        status=row["status"],
        folder=folder,
        video_count=video_count,
        stage_count=stage_count,
        clip_count=clip_count,
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/jobs", status_code=201, response_model=JobResponse)
def create_job(
    body: CreateJobRequest,
    repo: TaskRepository = Depends(get_task_repo),
):
    """Create a new task job for the given directory."""
    try:
        directory = _resolve_directory(body.directory)
        try:
            full_config = load_config()
        except Exception:
            full_config = {}
        # P1-1: Deep merge overrides into full config before adding metadata.
        merged_config = deep_merge(full_config, body.config_json or {})
        merged_config["video_paths"] = body.video_paths
        merged_config["_task"] = {
            "limit": body.limit,
            "extensions": body.extensions,
        }
        normalized = normalize_task_config(merged_config)
        # P1-3 (fourth-review §8): recompute config_hash from the FINAL
        # merged business config AFTER deep-merge + metadata + normalize.
        # Never trust a config_hash carried in the request body - it can
        # only be used as an expected comparison value, not the final value.
        from app.quality_lab.config_builder import compute_business_config_hash
        normalized["config_hash"] = compute_business_config_hash(normalized)
        config_json = json.dumps(normalized)
        job = repo.create_job(
            CreateJob(
                directory=directory,
                config_json=config_json,
                limit=body.limit,
                extensions=body.extensions,
            )
        )
    except ActiveJobConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "detail": "active job exists for directory",
                "existing_job_id": exc.existing_job_id,
            },
        )
    except HTTPException:
        raise
    except sqlite3.Error:
        raise HTTPException(
            status_code=500, detail="Internal database error"
        )

    row = _get_job_row(repo.conn, job.job_id)
    return _row_to_job_response(row, repo.conn)


@router.post("/jobs/{job_id}/cancel", response_model=CommandResponse)
def cancel_job(
    job_id: str,
    repo: TaskRepository = Depends(get_task_repo),
):
    """Append a cancel command for the given job."""
    _ensure_job_exists(repo.conn, job_id)
    try:
        command_id = repo.append_command(job_id, "cancel", {})
    except sqlite3.Error:
        raise HTTPException(
            status_code=500, detail="Internal database error"
        )
    return CommandResponse(status="ok", command_id=command_id)


@router.post("/jobs/{job_id}/retry", response_model=CommandResponse)
def retry_job(
    job_id: str,
    repo: TaskRepository = Depends(get_task_repo),
):
    """Append a retry command for the given job."""
    _ensure_job_exists(repo.conn, job_id)
    try:
        command_id = repo.append_command(job_id, "retry", {})
    except sqlite3.Error:
        raise HTTPException(
            status_code=500, detail="Internal database error"
        )
    return CommandResponse(status="ok", command_id=command_id)


@router.get("/jobs", response_model=list[JobResponse])
def list_jobs(repo: TaskRepository = Depends(get_task_repo)):
    """List all task jobs with video/stage/clip counts."""
    try:
        rows = repo.conn.execute(
            "SELECT * FROM task_jobs ORDER BY created_at DESC"
        ).fetchall()
    except sqlite3.Error:
        raise HTTPException(
            status_code=500, detail="Internal database error"
        )
    return [_row_to_job_response(row, repo.conn) for row in rows]


@router.get("/jobs/{job_id}", response_model=JobDetailResponse)
def get_job(
    job_id: str,
    repo: TaskRepository = Depends(get_task_repo),
):
    """Return a single task job with embedded video list."""
    try:
        row = _get_job_row(repo.conn, job_id)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"Job not found: {job_id}"
            )

        job_resp = _row_to_job_response(row, repo.conn)
        videos = repo.conn.execute(
            "SELECT * FROM task_videos WHERE job_id=? ORDER BY created_at ASC",
            (job_id,),
        ).fetchall()
    except HTTPException:
        raise
    except sqlite3.Error:
        raise HTTPException(
            status_code=500, detail="Internal database error"
        )

    return {
        **job_resp.model_dump(),
        "videos": [dict(v) for v in videos],
    }


@router.get("/events", response_model=list[EventResponse])
def list_events(
    after_id: int = 0,
    limit: int = Query(default=200, ge=1, le=1000),
    repo: TaskRepository = Depends(get_task_repo),
):
    """Return task events with stable integer-ID-based paging."""
    try:
        events = repo.list_events(after_id=after_id, limit=limit)
    except sqlite3.Error:
        raise HTTPException(
            status_code=500, detail="Internal database error"
        )
    return [
        EventResponse(
            event_id=e.event_id,
            kind=e.kind,
            payload=e.payload,
            created_at=e.created_at,
        )
        for e in events
    ]


@router.get("/attention", response_model=list[AttentionItem])
def list_attention(repo: TaskRepository = Depends(get_task_repo)):
    """List jobs that need attention (job-level or via stages)."""
    try:
        rows = repo.conn.execute(
            """SELECT j.*, COUNT(s.stage_id) AS attention_count
               FROM task_jobs j
               LEFT JOIN task_videos v ON v.job_id = j.job_id
               LEFT JOIN task_stages s
                 ON s.video_id = v.video_id AND s.status = 'needs_attention'
               WHERE j.status = 'needs_attention' OR s.stage_id IS NOT NULL
               GROUP BY j.job_id
               ORDER BY j.created_at DESC"""
        ).fetchall()
    except sqlite3.Error:
        raise HTTPException(
            status_code=500, detail="Internal database error"
        )

    results = []
    for row in rows:
        folder = Path(row["directory"]).name
        results.append(
            {
                "job_id": row["job_id"],
                "directory": row["directory"],
                "folder": folder,
                "status": row["status"],
                "attention_count": row["attention_count"],
                "created_at": row["created_at"],
            }
        )
    return results

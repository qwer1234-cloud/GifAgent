"""P1-4: Candidate feedback API endpoint."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.db import get_connection
from app.services.preference_events import PreferenceEventService


router = APIRouter(prefix="/api/candidates", tags=["candidates"])

_LIST_STATUS_PATTERN = r"^(all|candidate|liked|disliked|neutral|promoted|rejected|archived)$"


class FeedbackRequest(BaseModel):
    rating: str = Field(..., pattern=r"^(like|neutral|dislike|quality_reject|skip)$")
    note: str | None = None
    expected_artifact_path: str | None = None


def _resolve_user_folder(folder: str) -> Path:
    path = Path(os.path.expanduser(folder))
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve(strict=False)
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=400, detail=f"Folder does not exist: {folder}")
    return path


def _resolve_artifact_path(path_value: str | None) -> Path | None:
    if not path_value:
        return None
    path = Path(path_value)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


def _is_relative_to(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


def _path_display(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _row_payload(row) -> dict:
    return {
        "candidate_id": row["candidate_id"],
        "source_run_id": row["source_run_id"],
        "source_run_candidate_id": row["source_run_candidate_id"],
        "start_sec": row["start_sec"],
        "end_sec": row["end_sec"],
        "artifact_path": row["artifact_path"],
        "preview_path": row["preview_path"],
        "display_path": row["display_path"],
        "status": row["status"],
        "base_rag_similarity": row["base_rag_similarity"],
        "final_score": row["final_score"],
    }


def _candidate_rows(conn, *, status: str):
    where_sql = ""
    params: list[object] = []
    if status != "all":
        where_sql = "WHERE status=?"
        params.append(status)

    return conn.execute(
        f"""
        SELECT candidate_id, source_run_id, source_run_candidate_id,
               start_sec, end_sec, artifact_path, preview_path,
               COALESCE(preview_path, artifact_path) AS display_path,
               status, base_rag_similarity, final_score, created_at
        FROM candidate_gifs
        {where_sql}
        ORDER BY created_at DESC
        """,
        params,
    ).fetchall()


def _folder_for_row(row, root: Path) -> Path | None:
    artifact_path = _resolve_artifact_path(row["artifact_path"])
    if artifact_path is None:
        return None
    parent = artifact_path.parent
    if not _is_relative_to(parent, root):
        return None
    return parent


@router.get("/folders")
def list_candidate_folders(
    root: str = Query(..., min_length=1),
    status: str = Query("all", pattern=_LIST_STATUS_PATTERN),
):
    """List recursive subfolders under root that have candidate GIF rows."""
    root_path = _resolve_user_folder(root)
    conn = get_connection()
    rows = _candidate_rows(conn, status=status)

    folders: dict[Path, dict] = {}
    for row in rows:
        folder = _folder_for_row(row, root_path)
        if folder is None:
            continue
        info = folders.setdefault(
            folder,
            {
                "folder": str(folder),
                "relative_folder": "."
                if folder == root_path
                else folder.relative_to(root_path).as_posix(),
                "count": 0,
                "missing_count": 0,
                "status_counts": {},
            },
        )
        info["count"] += 1
        row_status = row["status"]
        info["status_counts"][row_status] = info["status_counts"].get(row_status, 0) + 1
        artifact_path = _resolve_artifact_path(row["artifact_path"])
        if artifact_path is None or not artifact_path.exists():
            info["missing_count"] += 1

    sorted_folders = sorted(
        folders.values(),
        key=lambda f: (f["relative_folder"].count("/"), f["relative_folder"].lower()),
    )
    return {
        "root": str(root_path),
        "folders": sorted_folders,
    }


@router.get("")
def list_candidates(
    status: str = Query("candidate", pattern=_LIST_STATUS_PATTERN),
    limit: int = Query(24, ge=1, le=100),
    offset: int = Query(0, ge=0),
    folder: str | None = None,
):
    """List candidate GIFs with server-side pagination and status filtering."""
    conn = get_connection()

    if folder:
        folder_path = _resolve_user_folder(folder)
        rows = []
        status_counts: dict[str, int] = {}
        moved_errors: list[dict] = []
        for row in _candidate_rows(conn, status="all"):
            row_folder = _folder_for_row(row, folder_path)
            if row_folder != folder_path:
                continue

            artifact_path = _resolve_artifact_path(row["artifact_path"])
            if artifact_path is None or not artifact_path.exists():
                moved_errors.append(
                    {
                        "candidate_id": row["candidate_id"],
                        "artifact_path": row["artifact_path"],
                        "expected_folder": str(folder_path),
                    }
                )
                continue

            row_status = row["status"]
            status_counts[row_status] = status_counts.get(row_status, 0) + 1
            if status == "all" or row_status == status:
                rows.append(row)

        if moved_errors:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "candidate_path_changed_or_missing",
                    "message": (
                        "Candidate GIF paths must stay in their original folder "
                        "and exist on disk before they can be reviewed."
                    ),
                    "items": moved_errors[:20],
                    "count": len(moved_errors),
                },
            )

        total = len(rows)
        page_rows = rows[offset : offset + limit]
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(page_rows) < total,
            "status_counts": status_counts,
            "folder": str(folder_path),
            "candidates": [_row_payload(r) for r in page_rows],
        }

    where_sql = ""
    params: list[object] = []
    if status != "all":
        where_sql = "WHERE status=?"
        params.append(status)

    total = conn.execute(
        f"SELECT COUNT(*) FROM candidate_gifs {where_sql}",
        params,
    ).fetchone()[0]

    status_rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM candidate_gifs GROUP BY status"
    ).fetchall()
    status_counts = {r["status"]: r["count"] for r in status_rows}

    rows = conn.execute(
        f"""
        SELECT candidate_id, source_run_id, source_run_candidate_id,
               start_sec, end_sec, artifact_path, preview_path,
               COALESCE(preview_path, artifact_path) AS display_path,
               status, base_rag_similarity, final_score
        FROM candidate_gifs
        {where_sql}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
        "status_counts": status_counts,
        "candidates": [_row_payload(r) for r in rows],
    }


class FeedbackResponse(BaseModel):
    event_id: str
    candidate_id: str
    rating: str
    status: str


@router.post("/{candidate_id}/feedback", response_model=FeedbackResponse)
def submit_feedback(candidate_id: str, body: FeedbackRequest):
    conn = get_connection()

    # Verify candidate exists
    row = conn.execute(
        "SELECT candidate_id, source_video_sha256, artifact_path FROM candidate_gifs WHERE candidate_id=?",
        (candidate_id,),
    ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail=f"Candidate {candidate_id} not found")

    artifact_path = _resolve_artifact_path(row["artifact_path"])
    if artifact_path is None or not artifact_path.exists():
        raise HTTPException(
            status_code=409,
            detail={
                "error": "candidate_path_changed_or_missing",
                "message": "Candidate GIF location changed or the file is missing.",
                "artifact_path": row["artifact_path"],
            },
        )

    if body.expected_artifact_path:
        expected_path = _resolve_artifact_path(body.expected_artifact_path)
        if expected_path != artifact_path:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "candidate_path_changed",
                    "message": "Candidate GIF path changed after it was loaded.",
                    "expected_artifact_path": _path_display(expected_path)
                    if expected_path
                    else body.expected_artifact_path,
                    "artifact_path": row["artifact_path"],
                },
            )

    service = PreferenceEventService(conn)

    # Load scenario keys from the candidate row
    import json
    scenario_keys_json = conn.execute(
        "SELECT scenario_keys_json FROM candidate_gifs WHERE candidate_id=?",
        (candidate_id,),
    ).fetchone()["scenario_keys_json"]
    scenario_keys = json.loads(scenario_keys_json) if scenario_keys_json else []

    event = service.record_feedback(
        target_type="candidate_gif",
        target_id=candidate_id,
        rating=body.rating,  # type: ignore[arg-type]
        source_video_sha256=row["source_video_sha256"],
        scenario_keys=scenario_keys,
        note=body.note,
    )

    # Re-read updated status
    updated = conn.execute(
        "SELECT status FROM candidate_gifs WHERE candidate_id=?",
        (candidate_id,),
    ).fetchone()

    return FeedbackResponse(
        event_id=event.event_id,
        candidate_id=candidate_id,
        rating=body.rating,
        status=updated["status"],
    )

"""P1-4: Candidate feedback API endpoint."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.db import get_connection
from app.services.preference_events import PreferenceEventService
from app.services.favorites import FavoriteService
from app.services.gif_naming import parse_clip_filename


router = APIRouter(prefix="/api/candidates", tags=["candidates"])

_LIST_STATUS_PATTERN = r"^(all|candidate|favorited|liked|disliked|neutral|promoted|rejected|archived)$"
_GIF_SUFFIX = ".gif"


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


def _iter_gif_files(folder: Path, *, recursive: bool) -> list[Path]:
    if recursive:
        candidates = folder.rglob("*")
    else:
        candidates = folder.iterdir()
    return sorted(
        (
            path.resolve(strict=False)
            for path in candidates
            if path.is_file() and path.suffix.lower() == _GIF_SUFFIX
        ),
        key=lambda p: str(p).lower(),
    )


def _folder_info(folders: dict[Path, dict], root: Path, folder: Path) -> dict:
    return folders.setdefault(
        folder,
        {
            "folder": str(folder),
            "relative_folder": "."
            if folder == root
            else folder.relative_to(root).as_posix(),
            "count": 0,
            "missing_count": 0,
            "unmaterialized_count": 0,
            "status_counts": {},
        },
    )


def _add_folder_count(
    folders: dict[Path, dict],
    root: Path,
    folder: Path,
    *,
    status: str,
    missing: bool = False,
    unmaterialized: bool = False,
) -> None:
    info = _folder_info(folders, root, folder)
    info["count"] += 1
    info["status_counts"][status] = info["status_counts"].get(status, 0) + 1
    if missing:
        info["missing_count"] += 1
    if unmaterialized:
        info["unmaterialized_count"] += 1


def _parse_clip_times(path: Path) -> tuple[float, float]:
    return parse_clip_filename(path)


def _materialize_filesystem_candidates_for_folder(conn, folder: Path) -> int:
    """Create candidate rows for GIFs in the exact selected folder only."""
    existing_paths: set[Path] = set()
    for row in _candidate_rows(conn, status="all"):
        artifact_path = _resolve_artifact_path(row["artifact_path"])
        if artifact_path is not None and artifact_path.parent == folder:
            existing_paths.add(artifact_path)

    created = 0
    now = datetime.now(timezone.utc).isoformat()
    for gif_path in _iter_gif_files(folder, recursive=False):
        if gif_path in existing_paths:
            continue

        artifact_path = _path_display(gif_path)
        digest = hashlib.sha256(str(gif_path).encode("utf-8")).hexdigest()
        candidate_id = f"cand_fs_{digest}"
        start_sec, end_sec = _parse_clip_times(gif_path)
        source_name = gif_path.name.split("@@@", 1)[0] if "@@@" in gif_path.name else gif_path.parent.name
        source_video_path = str(gif_path.parent / source_name)
        source_video_sha256 = hashlib.sha256(
            str(gif_path.parent).encode("utf-8")
        ).hexdigest()

        cursor = conn.execute(
            """INSERT OR IGNORE INTO candidate_gifs
               (candidate_id, source_run_id, source_run_candidate_id,
                source_video_sha256, source_video_path, start_sec, end_sec,
                artifact_path, preview_path,
                vlm_summary_json, tags_json, scenario_keys_json,
                base_rag_similarity, final_score, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                candidate_id,
                "filesystem-folder-import",
                digest,
                source_video_sha256,
                source_video_path,
                start_sec,
                end_sec,
                artifact_path,
                artifact_path,
                "{}",
                "[]",
                "[]",
                None,
                None,
                "candidate",
                now,
                now,
            ),
        )
        created += cursor.rowcount

    if created:
        conn.commit()
    return created


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
    if status == "candidate":
        where_sql = "WHERE c.status=? AND fg.candidate_id IS NULL"
        params.append(status)
    elif status == "favorited":
        where_sql = "WHERE fg.candidate_id IS NOT NULL"
    elif status != "all":
        where_sql = "WHERE c.status=?"
        params.append(status)

    return conn.execute(
        f"""
        SELECT c.candidate_id, c.source_run_id, c.source_run_candidate_id,
               c.start_sec, c.end_sec, c.artifact_path, c.preview_path,
               COALESCE(c.preview_path, c.artifact_path) AS display_path,
               CASE WHEN fg.candidate_id IS NOT NULL THEN 'favorited' ELSE c.status END AS status,
               c.base_rag_similarity, c.final_score, c.created_at
        FROM candidate_gifs c
        LEFT JOIN favorite_gifs fg ON fg.candidate_id = c.candidate_id
        {where_sql}
        ORDER BY c.created_at DESC
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
    """List recursive subfolders under root that have candidate GIF rows or GIF files."""
    root_path = _resolve_user_folder(root)
    conn = get_connection()
    rows = _candidate_rows(conn, status=status)

    folders: dict[Path, dict] = {}
    known_artifact_paths: set[Path] = set()
    known_rows = rows if status == "all" else _candidate_rows(conn, status="all")
    for known_row in known_rows:
        artifact_path = _resolve_artifact_path(known_row["artifact_path"])
        if artifact_path is not None:
            known_artifact_paths.add(artifact_path)
    for row in rows:
        folder = _folder_for_row(row, root_path)
        if folder is None:
            continue
        row_status = row["status"]
        artifact_path = _resolve_artifact_path(row["artifact_path"])
        _add_folder_count(
            folders,
            root_path,
            folder,
            status=row_status,
            missing=artifact_path is None or not artifact_path.exists(),
        )

    if status in {"all", "candidate"}:
        for gif_path in _iter_gif_files(root_path, recursive=True):
            if gif_path in known_artifact_paths:
                continue
            _add_folder_count(
                folders,
                root_path,
                gif_path.parent,
                status="candidate",
                unmaterialized=True,
            )

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
        _materialize_filesystem_candidates_for_folder(conn, folder_path)
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

    all_rows = _candidate_rows(conn, status="all")
    status_counts: dict[str, int] = {}
    for row in all_rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
    filtered_rows = [
        row for row in all_rows
        if status == "all" or row["status"] == status
    ]
    total = len(filtered_rows)
    rows = filtered_rows[offset : offset + limit]

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


class FavoriteRequest(BaseModel):
    expected_artifact_path: str | None = None


class FavoriteResponse(BaseModel):
    candidate_id: str
    status: str
    full_path: str


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


@router.post("/{candidate_id}/favorite", response_model=FavoriteResponse)
def favorite_candidate(candidate_id: str, body: FavoriteRequest):
    conn = get_connection()
    row = conn.execute(
        "SELECT candidate_id, source_video_sha256, artifact_path, scenario_keys_json "
        "FROM candidate_gifs WHERE candidate_id=?",
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
                    "expected_artifact_path": _path_display(expected_path)
                    if expected_path
                    else body.expected_artifact_path,
                    "artifact_path": row["artifact_path"],
                },
            )

    result = FavoriteService(conn).favorite(candidate_id, str(artifact_path))
    if result.get("created"):
        import json

        PreferenceEventService(conn).record_feedback(
            target_type="candidate_gif",
            target_id=candidate_id,
            rating="like",
            source_video_sha256=row["source_video_sha256"],
            scenario_keys=json.loads(row["scenario_keys_json"] or "[]"),
            note="favorite",
            update_candidate_status=False,
        )
    return FavoriteResponse(**result)

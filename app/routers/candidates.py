"""P1-4 / P3-6: Candidate feedback, review queue, pairwise, correction, and explanation API."""

from __future__ import annotations

import hashlib
import json as std_json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
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


@router.post("/undo-last")
def undo_last_action():
    """Undo the newest active candidate review action."""
    conn = get_connection()
    return PreferenceEventService(conn).undo_last_candidate_action()


# ── Phase 3 Task 6: Active review queue, pairwise, correction, explanation ──


class PairwiseRequest(BaseModel):
    winner_id: str
    loser_id: str


class CorrectionRequest(BaseModel):
    replacement_rating: str = Field(
        ...,
        pattern=r"^(like|dislike|neutral|skip|quality_reject|favorite)$",
    )
    reason: str = Field(..., min_length=1)


@router.get("/review-queue")
def get_review_queue(
    limit: int = Query(24, ge=1, le=100),
    seed: int = Query(42, ge=0),
):
    """Return a priority-ordered review queue of candidate GIFs.

    Each item exposes ``reason`` and ``reason_detail`` (Chinese) explaining
    why it was selected — exploit, uncertain, or explore.
    """
    from app.services.review_queue import (
        ReviewCandidate,
        build_review_queue,
    )

    conn = get_connection()

    # Fetch candidates with status 'candidate'
    rows = conn.execute(
        """SELECT candidate_id, source_video_sha256, base_rag_similarity,
                  scenario_keys_json, artifact_path
           FROM candidate_gifs WHERE status='candidate'"""
    ).fetchall()

    if not rows:
        return {"queue": [], "total": 0, "limit": limit}

    candidate_ids = [r["candidate_id"] for r in rows]
    placeholders = ",".join(["?"] * len(candidate_ids))

    # Fetch vectors
    vector_rows = conn.execute(
        f"""SELECT candidate_id, vector_blob
             FROM candidate_vectors
             WHERE candidate_id IN ({placeholders})
               AND vector_type='clip'""",
        candidate_ids,
    ).fetchall()

    vector_map: dict[str, np.ndarray] = {}
    for vr in vector_rows:
        vec = np.frombuffer(vr["vector_blob"], dtype=np.float32)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        vector_map[vr["candidate_id"]] = vec

    # Resolve published profile for scoring.
    current = conn.execute(
        "SELECT profile_version FROM preference_profile_current WHERE slot='current'"
    ).fetchone()

    reranker = None
    if current is not None:
        from app.services.reranker import PreferenceReranker

        reranker = PreferenceReranker(conn)

    # Build ReviewCandidate objects.
    review_candidates: list[ReviewCandidate] = []
    for row in rows:
        cid = row["candidate_id"]
        vec = vector_map.get(cid)
        if vec is None:
            continue

        preference_score = 0.5
        calibrated_probability = 0.5

        if reranker is not None:
            scenario_keys = std_json.loads(
                row["scenario_keys_json"] or "[]"
            )
            base_sim = row["base_rag_similarity"] or 0.5
            result = reranker.score(
                candidate_vector=vec,
                base_rag_similarity=base_sim,
                scenario_keys=scenario_keys,
                profile_version=current["profile_version"],
                enabled=True,
            )
            preference_score = result.get("final_score", 0.5)
            # Clamp calibrated_probability away from boundaries.
            calibrated_probability = max(0.01, min(0.99, preference_score))

        review_candidates.append(ReviewCandidate(
            candidate_id=cid,
            source_video_sha256=row["source_video_sha256"],
            preference_score=preference_score,
            calibrated_probability=calibrated_probability,
            vector=vec,
            cluster_key=row["source_video_sha256"],
        ))

    queue = build_review_queue(review_candidates, limit=limit, seed=seed)

    return {
        "queue": [
            {
                "candidate_id": item.candidate_id,
                "reason": item.reason,
                "reason_detail": item.reason_detail,
                "score": round(item.score, 6),
            }
            for item in queue
        ],
        "total": len(queue),
        "limit": limit,
    }


@router.post("/pairwise")
def pairwise(body: PairwiseRequest):
    """Record a pairwise comparison between two candidates.

    The winner receives a ``like`` event and the loser receives a ``dislike``
    event, both annotated with the pairwise context.
    """
    conn = get_connection()

    winner_row = conn.execute(
        "SELECT source_video_sha256, scenario_keys_json FROM candidate_gifs WHERE candidate_id=?",
        (body.winner_id,),
    ).fetchone()
    if winner_row is None:
        raise HTTPException(status_code=404, detail=f"Winner {body.winner_id} not found")

    loser_row = conn.execute(
        "SELECT source_video_sha256, scenario_keys_json FROM candidate_gifs WHERE candidate_id=?",
        (body.loser_id,),
    ).fetchone()
    if loser_row is None:
        raise HTTPException(status_code=404, detail=f"Loser {body.loser_id} not found")

    service = PreferenceEventService(conn)

    winner_event = service.record_feedback(
        target_type="candidate_gif",
        target_id=body.winner_id,
        rating="like",
        source_video_sha256=winner_row["source_video_sha256"],
        scenario_keys=std_json.loads(winner_row["scenario_keys_json"] or "[]"),
        note=f"pairwise:winner over {body.loser_id}",
    )

    loser_event = service.record_feedback(
        target_type="candidate_gif",
        target_id=body.loser_id,
        rating="dislike",
        source_video_sha256=loser_row["source_video_sha256"],
        scenario_keys=std_json.loads(loser_row["scenario_keys_json"] or "[]"),
        note=f"pairwise:loser to {body.winner_id}",
    )

    return {
        "winner_event_id": winner_event.event_id,
        "loser_event_id": loser_event.event_id,
    }


@router.post("/events/{event_id}/correct")
def correct_event(event_id: str, body: CorrectionRequest):
    """Correct (supersede) a previous feedback event.

    Creates a new ``correction``-kind event that points back to the original
    via ``supersedes_event_id``.
    """
    conn = get_connection()

    # Verify original event exists.
    original = conn.execute(
        "SELECT event_id FROM preference_events WHERE event_id=?",
        (event_id,),
    ).fetchone()
    if original is None:
        raise HTTPException(
            status_code=404,
            detail=f"Preference event {event_id} not found",
        )

    service = PreferenceEventService(conn)
    try:
        correction = service.correct_feedback(
            event_id=event_id,
            replacement=body.replacement_rating,  # type: ignore[arg-type]
            reason=body.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "event_id": correction.event_id,
        "target_type": correction.target_type,
        "target_id": correction.target_id,
        "rating": correction.rating,
        "event_kind": correction.event_kind,
        "supersedes_event_id": correction.supersedes_event_id,
        "created_at": correction.created_at,
    }


@router.get("/{candidate_id}/explanation")
def get_explanation(candidate_id: str):
    """Return an explainable ranking breakdown for a candidate.

    Includes base quality, positive/negative similarity, nearest positive
    example IDs (five or fewer), and the active preference profile version.
    """
    from app.services.ranking_explanations import compute_ranking_explanation

    conn = get_connection()

    row = conn.execute(
        """SELECT candidate_id, base_rag_similarity, scenario_keys_json
           FROM candidate_gifs WHERE candidate_id=?""",
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Candidate {candidate_id} not found")

    # Fetch the candidate's vector.
    vec_row = conn.execute(
        """SELECT vector_blob FROM candidate_vectors
           WHERE candidate_id=? AND vector_type='clip'
           LIMIT 1""",
        (candidate_id,),
    ).fetchone()
    if vec_row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No vector found for candidate {candidate_id}",
        )

    candidate_vector = np.frombuffer(vec_row["vector_blob"], dtype=np.float32)
    norm = np.linalg.norm(candidate_vector)
    if norm > 0:
        candidate_vector = candidate_vector / norm

    base_sim = row["base_rag_similarity"] or 0.5
    scenario_keys = std_json.loads(row["scenario_keys_json"] or "[]")

    result = compute_ranking_explanation(
        conn=conn,
        candidate_id=candidate_id,
        candidate_vector=candidate_vector,
        base_rag_similarity=base_sim,
        scenario_keys=scenario_keys,
        profile_version=None,
        enabled=True,
    )

    return result

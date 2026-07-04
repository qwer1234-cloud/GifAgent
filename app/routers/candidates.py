"""P1-4: Candidate feedback API endpoint."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.db import get_connection
from app.services.preference_events import PreferenceEventService


router = APIRouter(prefix="/api/candidates", tags=["candidates"])

_LIST_STATUS_PATTERN = r"^(all|candidate|liked|disliked|neutral|promoted|rejected|archived)$"


class FeedbackRequest(BaseModel):
    rating: str = Field(..., pattern=r"^(like|neutral|dislike|quality_reject|skip)$")
    note: str | None = None


@router.get("")
def list_candidates(
    status: str = Query("candidate", pattern=_LIST_STATUS_PATTERN),
    limit: int = Query(24, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """List candidate GIFs with server-side pagination and status filtering."""
    conn = get_connection()

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
        "candidates": [
            {
                "candidate_id": r["candidate_id"],
                "source_run_id": r["source_run_id"],
                "source_run_candidate_id": r["source_run_candidate_id"],
                "start_sec": r["start_sec"],
                "end_sec": r["end_sec"],
                "artifact_path": r["artifact_path"],
                "preview_path": r["preview_path"],
                "display_path": r["display_path"],
                "status": r["status"],
                "base_rag_similarity": r["base_rag_similarity"],
                "final_score": r["final_score"],
            }
            for r in rows
        ]
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
        "SELECT candidate_id, source_video_sha256 FROM candidate_gifs WHERE candidate_id=?",
        (candidate_id,),
    ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail=f"Candidate {candidate_id} not found")

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

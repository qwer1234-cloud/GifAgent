"""P1-4: Candidate feedback API endpoint."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import get_connection
from app.services.preference_events import PreferenceEventService


router = APIRouter(prefix="/api/candidates", tags=["candidates"])


class FeedbackRequest(BaseModel):
    rating: str = Field(..., pattern=r"^(like|neutral|dislike|quality_reject|skip)$")
    note: str | None = None


@router.get("")
def list_candidates():
    """List all candidate GIFs with their details."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT candidate_id, source_run_id, source_run_candidate_id,
               start_sec, end_sec, artifact_path, preview_path,
               status, base_rag_similarity, final_score
        FROM candidate_gifs ORDER BY created_at DESC
    """).fetchall()
    return {
        "candidates": [
            {
                "candidate_id": r["candidate_id"],
                "source_run_id": r["source_run_id"],
                "source_run_candidate_id": r["source_run_candidate_id"],
                "start_sec": r["start_sec"],
                "end_sec": r["end_sec"],
                "artifact_path": r["artifact_path"],
                "preview_path": r["preview_path"],
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

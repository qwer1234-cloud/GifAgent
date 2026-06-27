"""P1-5: Preference profile API — build, list, and publish profiles."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import get_connection
from app.services.preference_schema import apply_preference_schema
from app.services.preference_memory import PreferenceMemoryService

router = APIRouter(prefix="/api/preference", tags=["preference"])


class BuildRequest(BaseModel):
    dry_run: bool = False


class PublishResponse(BaseModel):
    status: str
    profile_version: str


@router.get("/profiles")
def list_profiles():
    """List all profile builds."""
    conn = get_connection()
    apply_preference_schema(conn)

    rows = conn.execute(
        """SELECT profile_version, event_watermark, embedding_model, embedding_dim,
                  effective_feedback_count, source_video_count, status, gate_reasons_json,
                  created_at, completed_at
           FROM preference_profile_builds
           ORDER BY created_at DESC"""
    ).fetchall()

    results = []
    for row in rows:
        import json

        results.append(
            {
                "profile_version": row["profile_version"],
                "event_watermark": row["event_watermark"],
                "embedding_model": row["embedding_model"],
                "embedding_dim": row["embedding_dim"],
                "effective_feedback_count": row["effective_feedback_count"],
                "source_video_count": row["source_video_count"],
                "status": row["status"],
                "gate_reasons": json.loads(row["gate_reasons_json"]),
                "created_at": row["created_at"],
                "completed_at": row["completed_at"],
            }
        )

    # Also include current published version
    current = conn.execute(
        "SELECT profile_version, published_at FROM preference_profile_current WHERE slot='current'"
    ).fetchone()

    return {
        "profiles": results,
        "current": (
            {
                "profile_version": current["profile_version"],
                "published_at": current["published_at"],
            }
            if current
            else None
        ),
    }


@router.post("/profiles/build")
def trigger_build(body: BuildRequest):
    """Trigger a profile build. Returns ProfileBuildResult."""
    conn = get_connection()
    apply_preference_schema(conn)

    service = PreferenceMemoryService(conn)
    result = service.build_profile(dry_run=body.dry_run)
    return result


@router.post("/profiles/{profile_version}/publish", response_model=PublishResponse)
def publish_profile(profile_version: str):
    """Publish a completed profile version as the current active profile."""
    conn = get_connection()
    apply_preference_schema(conn)

    service = PreferenceMemoryService(conn)
    try:
        service.publish(profile_version)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return PublishResponse(status="published", profile_version=profile_version)

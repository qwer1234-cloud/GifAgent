"""P1-5: Preference profile API — build, list, publish, and evaluate profiles."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from app.db import get_connection
from app.services.preference_schema import apply_preference_schema
from app.services.preference_memory import PreferenceMemoryService
from app.services.preference_evaluation import PreferenceEvaluationService

router = APIRouter(prefix="/api/preference", tags=["preference"])


class BuildRequest(BaseModel):
    dry_run: bool = False


class PublishResponse(BaseModel):
    status: str
    profile_version: str


class EvaluateRequest(BaseModel):
    profile_version: str
    holdout_path: str


@router.get("/profiles")
def list_profiles():
    """List all profile builds."""
    conn = get_connection()
    try:
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
    finally:
        conn.close()


@router.post("/profiles/build")
def trigger_build(body: BuildRequest | None = Body(default=None)):
    """Trigger a profile build. Returns ProfileBuildResult."""
    conn = get_connection()
    try:
        apply_preference_schema(conn)

        body = body or BuildRequest()
        service = PreferenceMemoryService(conn)
        result = service.build_profile(dry_run=body.dry_run)
        return result
    finally:
        conn.close()


@router.post("/profiles/{profile_version}/publish", response_model=PublishResponse)
def publish_profile(profile_version: str):
    """Publish a completed profile version as the current active profile."""
    conn = get_connection()
    try:
        apply_preference_schema(conn)
        service = PreferenceMemoryService(conn)
        service.publish(profile_version)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if "locked" in str(exc).lower():
            raise HTTPException(
                status_code=503,
                detail="Database is busy while publishing the profile. Please retry.",
            )
        raise
    finally:
        conn.close()

    return PublishResponse(status="published", profile_version=profile_version)


@router.post("/evaluate")
def evaluate_profile(body: EvaluateRequest):
    """Evaluate a built profile against a holdout judgment set.

    Request body: ``{"profile_version": "...", "holdout_path": "path/to/holdout.jsonl"}``

    Returns ``can_publish`` (bool), ``gate_reasons`` (list[str]),
    ``like_at_20``, ``dislike_at_20``, and ``ndcg_at_20`` (float).
    """
    conn = get_connection()
    try:
        apply_preference_schema(conn)

        holdout_path = Path(body.holdout_path)
        if not holdout_path.exists():
            raise HTTPException(
                status_code=400, detail=f"Holdout file not found: {body.holdout_path}"
            )

        service = PreferenceEvaluationService(conn)
        result = service.evaluate(
            body.profile_version, holdout_path=holdout_path
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        conn.close()

    return result

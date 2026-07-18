"""P1-5 / P3-6: Preference profile API — build, list, publish, preview, rollback,
evaluate, and vector-health."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from app.db import get_connection
from app.services.preference_schema import apply_preference_schema
from app.services.preference_memory import PreferenceMemoryService, preview_profile
from app.services.preference_evaluation import PreferenceEvaluationService
from app.services.vector_health import inspect_vector_health

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


# ── Phase 3 Task 6: Profile preview, rollback, vector-health ────────────────


@router.post("/profiles/preview")
def preview_profile_endpoint():
    """Preview whether a profile build would pass all gates.

    Returns gate reasons, metrics, and the computed profile_version --
    *without* writing anything to the database.
    """
    conn = get_connection()
    try:
        apply_preference_schema(conn)

        from app.services.preference_memory import ProfileBuildConfig

        config = ProfileBuildConfig()
        result = preview_profile(conn, config)
        return {
            "profile_version": result.profile_version,
            "status": result.status,
            "gate_reasons": list(result.gate_reasons),
            "metrics": result.metrics,
        }
    finally:
        conn.close()


@router.post("/profiles/{profile_version}/rollback")
def rollback_profile(profile_version: str):
    """Rollback to a previously completed profile version.

    The rollback is recorded as a publication entry so history is preserved.
    """
    conn = get_connection()
    try:
        apply_preference_schema(conn)
        service = PreferenceMemoryService(conn)
        service.rollback(profile_version)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if "locked" in str(exc).lower():
            raise HTTPException(
                status_code=503,
                detail="Database is busy while rolling back. Please retry.",
            )
        raise
    finally:
        conn.close()

    return {"status": "rolled_back", "profile_version": profile_version}


@router.get("/vector-health")
def get_vector_health():
    """Return vector coverage health -- total/available/missing/excluded.

    Provides an overview of which candidates have embedding vectors for the
    base embedding model.
    """
    conn = get_connection()
    try:
        apply_preference_schema(conn)
        health = inspect_vector_health(
            conn,
            model="nomic-embed-text:latest",
        )
        return {
            "total_candidates": health.total_candidates,
            "available": health.available,
            "missing": list(health.missing),
            "excluded": [
                {
                    "candidate_id": exc.candidate_id,
                    "reason": exc.reason,
                    "created_at": exc.created_at,
                }
                for exc in health.excluded
            ],
        }
    finally:
        conn.close()

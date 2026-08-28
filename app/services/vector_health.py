"""Phase 3 Task 2: Candidate vector health inspection.

Provides ``inspect_vector_health()`` to report which candidates have
vectors, which are missing, and which are excluded (corrupt, missing
artifact, or failed embedding).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.services.preference_events import load_latest_scoring_events
from app.services.preference_types import VectorExclusion


@dataclass(frozen=True)
class VectorHealth:
    total_candidates: int
    available: int
    missing: tuple[str, ...]
    excluded: tuple[VectorExclusion, ...]
    missing_feedback: tuple[str, ...] = ()


def inspect_vector_health(
    conn: sqlite3.Connection,
    model: str,
) -> VectorHealth:
    """Inspect vector coverage for the given embedding *model*.

    Returns a ``VectorHealth`` with total/available counts, missing
    candidate IDs, and any explicit exclusions recorded in the
    ``candidate_vector_exclusions`` table for *model*.
    """
    from app.services.preference_memory import REQUIRED_EMBEDDING_DIM

    total = conn.execute(
        "SELECT COUNT(*) FROM candidate_gifs"
    ).fetchone()[0]

    available = conn.execute(
        """SELECT COUNT(DISTINCT candidate_id)
           FROM candidate_vectors
           WHERE embedding_model=?
             AND embedding_dim=?""",
        (model, REQUIRED_EMBEDDING_DIM),
    ).fetchone()[0]

    missing_rows = conn.execute(
        """SELECT cg.candidate_id
           FROM candidate_gifs cg
           WHERE cg.candidate_id NOT IN (
               SELECT cv.candidate_id
               FROM candidate_vectors cv
               WHERE cv.embedding_model=?
                 AND cv.embedding_dim=?
           )
           ORDER BY cg.candidate_id""",
        (model, REQUIRED_EMBEDDING_DIM),
    ).fetchall()
    missing = tuple(row["candidate_id"] for row in missing_rows)

    exclusion_rows = conn.execute(
        """SELECT candidate_id, reason, created_at, embedding_model,
                  attempts, error_class
           FROM candidate_vector_exclusions
           WHERE embedding_model=?
           ORDER BY candidate_id""",
        (model,),
    ).fetchall()
    excluded = tuple(
        VectorExclusion(
            candidate_id=row["candidate_id"],
            reason=row["reason"],
            created_at=row["created_at"],
            embedding_model=row["embedding_model"] or model,
            attempts=int(row["attempts"] or 1),
            error_class=row["error_class"] or "",
        )
        for row in exclusion_rows
    )

    scoring_ids = {
        str(event["target_id"])
        for event in load_latest_scoring_events(conn).values()
        if event.get("target_type") == "candidate_gif"
    }
    missing_feedback = tuple(cid for cid in missing if cid in scoring_ids)

    return VectorHealth(
        total_candidates=total,
        available=available,
        missing=missing,
        excluded=excluded,
        missing_feedback=missing_feedback,
    )

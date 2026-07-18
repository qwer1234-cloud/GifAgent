"""Backfill and maintain candidate GIF embedding vectors."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Sequence
from typing import Any

import numpy as np

from app.services.preference_memory import (
    REQUIRED_EMBEDDING_DIM,
    REQUIRED_EMBEDDING_MODEL,
)

EmbeddingFn = Callable[[str], list[float]]


def _loads_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def build_candidate_embedding_text(row: sqlite3.Row) -> str:
    """Build stable text for candidate vector embedding."""
    vlm_summary = _loads_json(row["vlm_summary_json"], {})
    tags = _loads_json(row["tags_json"], [])
    scenario_keys = _loads_json(row["scenario_keys_json"], [])

    parts: list[str] = []
    artifact_path = row["artifact_path"] or row["preview_path"] or ""
    if artifact_path:
        parts.append(os.path.basename(str(artifact_path)))
    if row["source_video_path"]:
        parts.append(os.path.basename(str(row["source_video_path"])))
    parts.append(f"clip {float(row['start_sec']):.1f}s to {float(row['end_sec']):.1f}s")

    if isinstance(vlm_summary, dict):
        for key in ("caption", "summary", "emotion", "emotional_core", "scene_type", "reason"):
            value = vlm_summary.get(key)
            if value:
                parts.append(str(value))
    if isinstance(tags, list):
        parts.extend(str(tag) for tag in tags if tag)
    if isinstance(scenario_keys, list):
        parts.extend(str(key) for key in scenario_keys if key)

    text = " ".join(part.strip() for part in parts if str(part).strip())
    return text or str(row["candidate_id"])


def _candidate_rows(conn: sqlite3.Connection, *, only_feedback: bool) -> list[sqlite3.Row]:
    feedback_join = ""
    if only_feedback:
        feedback_join = """
        INNER JOIN (
            SELECT DISTINCT target_id
            FROM preference_events
            WHERE target_type='candidate_gif'
              AND rating IN ('like','dislike')
              AND undone_at IS NULL
        ) pe ON pe.target_id = cg.candidate_id
        """

    return conn.execute(
        f"""SELECT cg.candidate_id, cg.source_video_path, cg.start_sec, cg.end_sec,
                  cg.artifact_path, cg.preview_path, cg.vlm_summary_json,
                  cg.tags_json, cg.scenario_keys_json
           FROM candidate_gifs cg
           {feedback_join}
           ORDER BY cg.created_at ASC, cg.candidate_id ASC"""
    ).fetchall()


def _has_vector(
    conn: sqlite3.Connection,
    candidate_id: str,
    *,
    embedding_model: str,
    embedding_dim: int,
) -> bool:
    row = conn.execute(
        """SELECT 1
           FROM candidate_vectors
           WHERE candidate_id=?
             AND vector_type='clip'
             AND embedding_model=?
             AND embedding_dim=?
           LIMIT 1""",
        (candidate_id, embedding_model, embedding_dim),
    ).fetchone()
    return row is not None


def _vector_blob(vector: list[float], *, embedding_dim: int) -> bytes:
    if len(vector) != embedding_dim:
        raise ValueError(f"embedding_dim mismatch: got {len(vector)}, expected {embedding_dim}")
    return np.asarray(vector, dtype=np.float32).tobytes()


def backfill_candidate_vectors(
    conn: sqlite3.Connection,
    *,
    embed_fn: EmbeddingFn,
    embedding_model: str = REQUIRED_EMBEDDING_MODEL,
    embedding_dim: int = REQUIRED_EMBEDDING_DIM,
    only_feedback: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Create missing candidate_vectors rows for candidate GIFs."""
    rows = _candidate_rows(conn, only_feedback=only_feedback)
    result: dict[str, Any] = {
        "scanned": len(rows),
        "missing": 0,
        "inserted": 0,
        "skipped_existing": 0,
        "failed": 0,
        "errors": [],
        "dry_run": dry_run,
        "only_feedback": only_feedback,
        "embedding_model": embedding_model,
        "embedding_dim": embedding_dim,
    }

    for row in rows:
        candidate_id = row["candidate_id"]
        if _has_vector(
            conn,
            candidate_id,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
        ):
            result["skipped_existing"] += 1
            continue

        result["missing"] += 1
        if limit is not None and result["inserted"] >= limit:
            continue
        if dry_run:
            continue

        text = build_candidate_embedding_text(row)
        try:
            blob = _vector_blob(embed_fn(text), embedding_dim=embedding_dim)
            conn.execute(
                """INSERT OR REPLACE INTO candidate_vectors
                   (candidate_id, vector_type, embedding_model, embedding_dim,
                    vector_blob, normalized)
                   VALUES (?,?,?,?,?,?)""",
                (candidate_id, "clip", embedding_model, embedding_dim, blob, 1),
            )
            conn.commit()
            result["inserted"] += 1
        except Exception as exc:
            result["failed"] += 1
            result["errors"].append({"candidate_id": candidate_id, "error": str(exc)})

    return result


def backfill_missing_vectors(
    conn: sqlite3.Connection,
    embedder: EmbeddingFn,
    *,
    candidate_ids: Sequence[str] | None = None,
    batch_size: int = 32,
    embedding_model: str = REQUIRED_EMBEDDING_MODEL,
    embedding_dim: int = REQUIRED_EMBEDDING_DIM,
) -> BackfillReport:
    """Resumable incremental backfill of missing candidate vectors.

    When ``candidate_ids`` is *None* every candidate in
    ``candidate_gifs`` is considered; otherwise only the given IDs are
    processed.  Candidates that already have a vector or are recorded as
    excluded are skipped.  Each batch is committed incrementally and
    embedding failures are recorded as exclusions.
    """
    from app.services.preference_types import BackfillReport

    if candidate_ids is not None:
        placeholders = ",".join(["?"] * len(candidate_ids))
        rows = conn.execute(
            f"""SELECT cg.candidate_id, cg.source_video_path, cg.start_sec,
                       cg.end_sec, cg.artifact_path, cg.preview_path,
                       cg.vlm_summary_json, cg.tags_json, cg.scenario_keys_json
                 FROM candidate_gifs cg
                 WHERE cg.candidate_id IN ({placeholders})
                 ORDER BY cg.created_at ASC, cg.candidate_id ASC""",
            list(candidate_ids),
        ).fetchall()
    else:
        rows = _candidate_rows(conn, only_feedback=False)

    excluded_set = _load_excluded_ids(conn)

    report: BackfillReport = {
        "total": len(rows),
        "inserted": 0,
        "skipped_existing": 0,
        "failed": 0,
        "exclusions": [],
        "batch_commits": 0,
    }

    pending: list[tuple[str, bytes]] = []
    pending_exclusions: list[tuple[str, str]] = []

    for row in rows:
        candidate_id = row["candidate_id"]

        if candidate_id in excluded_set:
            report["skipped_existing"] += 1
            continue

        if _has_vector(
            conn,
            candidate_id,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
        ):
            report["skipped_existing"] += 1
            continue

        text = build_candidate_embedding_text(row)
        try:
            blob = _vector_blob(embedder(text), embedding_dim=embedding_dim)
            pending.append((candidate_id, blob))
        except Exception as exc:
            report["failed"] += 1
            pending_exclusions.append(
                (candidate_id, f"embedding_failed: {exc}")
            )

        # Commit batch when pending reaches batch_size or at end of loop.
        if len(pending) + len(pending_exclusions) >= batch_size:
            _flush_batch(
                conn,
                pending,
                pending_exclusions,
                report,
                embedding_model=embedding_model,
                embedding_dim=embedding_dim,
            )

    # Flush any remainder.
    if pending or pending_exclusions:
        _flush_batch(
            conn,
            pending,
            pending_exclusions,
            report,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
        )

    return report


def _load_excluded_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT candidate_id FROM candidate_vector_exclusions"
    ).fetchall()
    return {row["candidate_id"] for row in rows}


def _flush_batch(
    conn: sqlite3.Connection,
    pending: list[tuple[str, bytes]],
    pending_exclusions: list[tuple[str, str]],
    report: BackfillReport,
    *,
    embedding_model: str,
    embedding_dim: int,
) -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()

    for candidate_id, blob in pending:
        conn.execute(
            """INSERT OR REPLACE INTO candidate_vectors
               (candidate_id, vector_type, embedding_model, embedding_dim,
                vector_blob, normalized)
               VALUES (?,?,?,?,?,?)""",
            (candidate_id, "clip", embedding_model, embedding_dim, blob, 1),
        )
        report["inserted"] += 1
        _cast_exclusions(report["exclusions"]).append(
            {"candidate_id": candidate_id, "status": "inserted"}
        )

    for candidate_id, reason in pending_exclusions:
        conn.execute(
            """INSERT OR REPLACE INTO candidate_vector_exclusions
               (candidate_id, reason, created_at)
               VALUES (?,?,?)""",
            (candidate_id, reason, now),
        )
        _cast_exclusions(report["exclusions"]).append(
            {"candidate_id": candidate_id, "status": "excluded", "reason": reason}
        )

    conn.commit()
    report["batch_commits"] += 1
    pending.clear()
    pending_exclusions.clear()


def _cast_exclusions(
    exclusions: list[dict[str, str]],
) -> list[dict[str, str]]:
    return exclusions

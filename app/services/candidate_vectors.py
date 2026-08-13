"""Backfill and maintain candidate GIF embedding vectors."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Sequence
from typing import Any

import numpy as np

from app.services.embedding import EmbeddingServiceUnavailable
from app.services.preference_memory import (
    REQUIRED_EMBEDDING_DIM,
    REQUIRED_EMBEDDING_MODEL,
)
from app.services.ollama_runtime import EmbeddingRuntimeError

EmbeddingFn = Callable[[str], list[float]]
BatchEmbeddingFn = Callable[[list[str]], list[list[float]]]
ProgressCb = Callable[[int, int], None]
ProgressFn = Callable[[dict[str, Any]], None]


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
    embed_fn: EmbeddingFn | None = None,
    embedding_model: str = REQUIRED_EMBEDDING_MODEL,
    embedding_dim: int = REQUIRED_EMBEDDING_DIM,
    only_feedback: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
    batch_embed_fn: BatchEmbeddingFn | None = None,
    batch_size: int = 32,
    progress_cb: ProgressCb | None = None,
    progress_fn: ProgressFn | None = None,
) -> dict[str, Any]:
    """Create missing candidate_vectors rows for candidate GIFs.

    ``embed_fn`` (single-text) is preserved for legacy callers; at least one
    of ``embed_fn``/``batch_embed_fn`` must be supplied when embedding is
    performed.  When ``batch_embed_fn`` is supplied, missing rows are found
    once, embedded in batches of ``batch_size``, and committed once per
    successful batch.  The first batch call/validation/insert failure rolls
    back the current batch and stops immediately; previously committed
    batches stay durable.
    """
    if batch_size <= 0:
        raise ValueError(f"batch_size must be > 0, got {batch_size}")

    rows = _candidate_rows(conn, only_feedback=only_feedback)
    result: dict[str, Any] = {
        "scanned": len(rows),
        "total": len(rows),
        "processed": 0,
        "current_candidate": None,
        "missing": 0,
        "inserted": 0,
        "skipped_existing": 0,
        "failed": 0,
        "errors": [],
        "dry_run": dry_run,
        "only_feedback": only_feedback,
        "embedding_model": embedding_model,
        "embedding_dim": embedding_dim,
        "aborted": False,
        "remaining": 0,
        "batches": 0,
        "batch_size": batch_size,
        "phase": None,
        "attempts": None,
        "base_url": None,
        "retryable": None,
    }

    missing_rows: list[sqlite3.Row] = []

    def emit_progress() -> None:
        _emit_progress_fn(progress_fn, result)

    for row in rows:
        candidate_id = row["candidate_id"]
        result["current_candidate"] = candidate_id
        if _has_vector(
            conn,
            candidate_id,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
        ):
            result["skipped_existing"] += 1
            result["processed"] += 1
            emit_progress()
            continue
        missing_rows.append(row)

    result["missing"] = len(missing_rows)

    if dry_run:
        result["processed"] = result["skipped_existing"] + result["missing"]
        result["remaining"] = result["missing"] - result["inserted"]
        emit_progress()
        return result

    if batch_embed_fn is not None:
        return _backfill_batch(
            conn,
            missing_rows,
            result,
            batch_embed_fn=batch_embed_fn,
            batch_size=batch_size,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
            limit=limit,
            progress_cb=progress_cb,
            progress_fn=progress_fn,
        )

    if embed_fn is None:
        raise ValueError(
            "backfill_candidate_vectors requires embed_fn or batch_embed_fn"
        )

    for row in missing_rows:
        if limit is not None and result["inserted"] >= limit:
            break
        candidate_id = row["candidate_id"]
        result["current_candidate"] = candidate_id
        try:
            blob = _vector_blob(
                embed_fn(build_candidate_embedding_text(row)),
                embedding_dim=embedding_dim,
            )
            conn.execute(
                """INSERT OR REPLACE INTO candidate_vectors
                   (candidate_id, vector_type, embedding_model, embedding_dim,
                    vector_blob, normalized)
                   VALUES (?,?,?,?,?,?)""",
                (candidate_id, "clip", embedding_model, embedding_dim, blob, 1),
            )
            conn.commit()
            result["inserted"] += 1
        except EmbeddingServiceUnavailable:
            # A missing/unreachable model service is transient. Do not turn it
            # into per-candidate failures or continue an unproductive scan.
            raise
        except Exception as exc:
            result["failed"] += 1
            result["errors"].append({"candidate_id": candidate_id, "error": str(exc)})
        result["processed"] += 1
        emit_progress()

    result["remaining"] = result["missing"] - result["inserted"]

    return result


def _backfill_batch(
    conn: sqlite3.Connection,
    missing_rows: list[sqlite3.Row],
    result: dict[str, Any],
    *,
    batch_embed_fn: BatchEmbeddingFn,
    batch_size: int,
    embedding_model: str,
    embedding_dim: int,
    limit: int | None,
    progress_cb: ProgressCb | None,
    progress_fn: ProgressFn | None = None,
) -> dict[str, Any]:
    """Batch-embed missing rows, committing once per successful batch.

    Failures roll back the in-flight batch and abort immediately; already
    committed batches remain durable.  Transient endpoint failures are not
    written to ``candidate_vector_exclusions``.
    """
    if limit is not None and limit <= 0:
        to_process = []
    elif limit is not None:
        to_process = missing_rows[:limit]
    else:
        to_process = missing_rows
    total = len(to_process)

    _notify_progress(progress_cb, 0, total, result)
    _emit_progress_fn(progress_fn, result)

    for start in range(0, total, batch_size):
        chunk = to_process[start : start + batch_size]
        try:
            texts = [build_candidate_embedding_text(row) for row in chunk]
            vectors = batch_embed_fn(texts)
            blobs = _validate_batch_vectors(vectors, chunk, embedding_dim)
            for row, blob in zip(chunk, blobs):
                conn.execute(
                    """INSERT OR REPLACE INTO candidate_vectors
                       (candidate_id, vector_type, embedding_model, embedding_dim,
                        vector_blob, normalized)
                       VALUES (?,?,?,?,?,?)""",
                    (row["candidate_id"], "clip", embedding_model, embedding_dim, blob, 1),
                )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            result["aborted"] = True
            result["error"] = str(exc)
            first_candidate_id = chunk[0]["candidate_id"] if chunk else None
            error_entry: dict[str, Any] = {
                "candidate_id": first_candidate_id,
                "first_candidate_id": first_candidate_id,
                "error": str(exc),
            }
            if isinstance(exc, EmbeddingRuntimeError):
                result["phase"] = exc.phase
                result["attempts"] = exc.attempts
                result["base_url"] = exc.base_url
                result["retryable"] = exc.retryable
                error_entry.update(
                    phase=exc.phase,
                    attempts=exc.attempts,
                    base_url=exc.base_url,
                    retryable=exc.retryable,
                )
            result["errors"].append(error_entry)
            result["remaining"] = result["missing"] - result["inserted"]
            return result

        result["inserted"] += len(chunk)
        result["batches"] += 1
        result["processed"] = result["skipped_existing"] + result["inserted"]
        result["current_candidate"] = chunk[-1]["candidate_id"]
        _notify_progress(progress_cb, result["inserted"], total, result)
        _emit_progress_fn(progress_fn, result)

    result["remaining"] = result["missing"] - result["inserted"]
    return result


def _validate_batch_vectors(
    vectors: Any,
    chunk: list[sqlite3.Row],
    embedding_dim: int,
) -> list[bytes]:
    """Validate count and every dimension before any insert in the batch."""
    if not isinstance(vectors, list) or len(vectors) != len(chunk):
        raise ValueError(
            f"batch embedder returned "
            f"{len(vectors) if isinstance(vectors, list) else type(vectors).__name__} "
            f"vectors for {len(chunk)} inputs"
        )
    blobs: list[bytes] = []
    for vector in vectors:
        if not isinstance(vector, list) or not vector:
            raise ValueError("each embedding must be a non-empty list")
        blobs.append(_vector_blob(vector, embedding_dim=embedding_dim))
    return blobs


def _notify_progress(
    progress_cb: ProgressCb | None,
    completed: int,
    total: int,
    result: dict[str, Any],
) -> None:
    """Run the progress callback, recording failures without aborting."""
    if progress_cb is None:
        return
    try:
        progress_cb(completed, total)
    except Exception as exc:
        result.setdefault("progress_errors", []).append(str(exc))


def _emit_progress_fn(
    progress_fn: ProgressFn | None,
    result: dict[str, Any],
) -> None:
    """Run the dict progress callback without interrupting durable writes."""
    if progress_fn is None:
        return
    try:
        progress_fn(dict(result))
    except Exception:
        pass


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
        except EmbeddingServiceUnavailable:
            # Service outages must pause the resumable job, not permanently
            # exclude every candidate that has not been reached yet.
            raise
        except Exception as exc:
            report["failed"] += 1
            if isinstance(exc, EmbeddingRuntimeError) and exc.retryable:
                # A transient endpoint failure must never become a durable
                # candidate exclusion; the run is resumable as-is.
                continue
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

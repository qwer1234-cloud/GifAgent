"""Backfill and maintain candidate GIF embedding vectors."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

from app.services.embedding import EmbeddingServiceUnavailable
from app.services.preference_events import load_latest_scoring_events
from app.services.preference_memory import (
    REQUIRED_EMBEDDING_DIM,
    REQUIRED_EMBEDDING_MODEL,
)
from app.services.ollama_runtime import EmbeddingRuntimeError
from app.services.vector_math import blob_to_vector, is_unit_vector, vector_to_blob

EmbeddingFn = Callable[[str], list[float]]
BatchEmbeddingFn = Callable[[list[str]], list[list[float]]]
ProgressCb = Callable[[int, int], None]
ProgressFn = Callable[[dict[str, Any]], None]

# Content-first template. Version 1 was filename + clip times + caption.
EMBEDDING_TEXT_SCHEMA_VERSION = 2


def _loads_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def embedding_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_candidate_embedding_text(row: sqlite3.Row) -> str:
    """Build content-first text for candidate vector embedding.

    Filenames and clip timestamps are omitted: they pull same-source clips
    together without adding semantic signal.
    """
    vlm_summary = _loads_json(row["vlm_summary_json"], {})
    tags = _loads_json(row["tags_json"], [])
    scenario_keys = _loads_json(row["scenario_keys_json"], [])

    parts: list[str] = []
    if isinstance(vlm_summary, dict):
        for key in (
            "caption",
            "summary",
            "emotion",
            "emotional_core",
            "scene_type",
            "reason",
        ):
            value = vlm_summary.get(key)
            if value:
                parts.append(str(value))
    if isinstance(tags, list):
        parts.extend(str(tag) for tag in tags if tag)
    if isinstance(scenario_keys, list):
        parts.extend(str(key) for key in scenario_keys if key)

    text = " ".join(part.strip() for part in parts if str(part).strip())
    return text or str(row["candidate_id"])


def _vector_blob(vector: list[float] | Any, *, embedding_dim: int) -> bytes:
    """Serialize *vector* as an L2-normalized float32 blob."""
    return vector_to_blob(vector, embedding_dim=embedding_dim)


def _candidate_join_sql(
    *,
    embedding_model: str,
    embedding_dim: int,
    candidate_ids: Sequence[str] | None,
) -> tuple[str, list[Any]]:
    params: list[Any] = [embedding_model, embedding_dim]
    where = ""
    if candidate_ids is not None:
        placeholders = ",".join(["?"] * len(candidate_ids))
        where = f" WHERE cg.candidate_id IN ({placeholders})"
        params.extend(candidate_ids)
    sql = f"""
        SELECT cg.candidate_id, cg.source_video_path, cg.start_sec, cg.end_sec,
               cg.artifact_path, cg.preview_path, cg.vlm_summary_json,
               cg.tags_json, cg.scenario_keys_json,
               cv.candidate_id AS vector_candidate_id,
               cv.source_text_hash AS stored_text_hash,
               cv.text_schema_version AS stored_text_schema_version
        FROM candidate_gifs cg
        LEFT JOIN candidate_vectors cv
          ON cv.candidate_id = cg.candidate_id
         AND cv.vector_type = 'clip'
         AND cv.embedding_model = ?
         AND cv.embedding_dim = ?
        {where}
        ORDER BY cg.created_at ASC, cg.candidate_id ASC
    """
    return sql, params


def _load_backfill_rows(
    conn: sqlite3.Connection,
    *,
    embedding_model: str,
    embedding_dim: int,
    only_feedback: bool,
    candidate_ids: Sequence[str] | None,
) -> list[sqlite3.Row]:
    sql, params = _candidate_join_sql(
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        candidate_ids=candidate_ids,
    )
    rows = conn.execute(sql, params).fetchall()
    if not only_feedback:
        return rows
    scoring = load_latest_scoring_events(conn)
    allowed = {
        event["target_id"]
        for event in scoring.values()
        if event.get("target_type") == "candidate_gif"
    }
    return [row for row in rows if row["candidate_id"] in allowed]


def _is_current_vector(row: sqlite3.Row, text_hash: str) -> bool:
    if row["vector_candidate_id"] is None:
        return False
    stored_version = row["stored_text_schema_version"]
    if stored_version is None or int(stored_version) != EMBEDDING_TEXT_SCHEMA_VERSION:
        return False
    stored_hash = row["stored_text_hash"]
    return bool(stored_hash) and str(stored_hash) == text_hash


def _load_excluded_ids(
    conn: sqlite3.Connection,
    embedding_model: str,
    *,
    retry_excluded: bool,
) -> set[str]:
    if retry_excluded:
        return set()
    rows = conn.execute(
        """SELECT candidate_id FROM candidate_vector_exclusions
           WHERE embedding_model=?""",
        (embedding_model,),
    ).fetchall()
    return {row["candidate_id"] for row in rows}


def _error_class(exc: BaseException) -> str:
    if isinstance(exc, EmbeddingRuntimeError):
        return "transient" if exc.retryable else "permanent"
    if isinstance(exc, ValueError):
        return "validation"
    return "unknown"


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, EmbeddingServiceUnavailable):
        return True
    return isinstance(exc, EmbeddingRuntimeError) and bool(exc.retryable)


def _upsert_vector(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    embedding_model: str,
    embedding_dim: int,
    blob: bytes,
    text_hash: str,
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO candidate_vectors
           (candidate_id, vector_type, embedding_model, embedding_dim,
            vector_blob, normalized, text_schema_version, source_text_hash)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            candidate_id,
            "clip",
            embedding_model,
            embedding_dim,
            blob,
            1,
            EMBEDDING_TEXT_SCHEMA_VERSION,
            text_hash,
        ),
    )


def _record_exclusion(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    embedding_model: str,
    reason: str,
    error_class: str,
) -> dict[str, str]:
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        """SELECT attempts FROM candidate_vector_exclusions
           WHERE candidate_id=? AND embedding_model=?""",
        (candidate_id, embedding_model),
    ).fetchone()
    attempts = int(existing["attempts"]) + 1 if existing is not None else 1
    conn.execute(
        """INSERT OR REPLACE INTO candidate_vector_exclusions
           (candidate_id, embedding_model, reason, created_at, attempts, error_class)
           VALUES (?,?,?,?,?,?)""",
        (candidate_id, embedding_model, reason, now, attempts, error_class),
    )
    return {
        "candidate_id": candidate_id,
        "reason": reason,
        "error_class": error_class,
        "attempts": str(attempts),
    }


def _clear_exclusion(
    conn: sqlite3.Connection, *, candidate_id: str, embedding_model: str
) -> None:
    conn.execute(
        """DELETE FROM candidate_vector_exclusions
           WHERE candidate_id=? AND embedding_model=?""",
        (candidate_id, embedding_model),
    )


def _empty_result(
    *,
    scanned: int,
    embedding_model: str,
    embedding_dim: int,
    dry_run: bool,
    only_feedback: bool,
    batch_size: int,
    retry_excluded: bool,
) -> dict[str, Any]:
    return {
        "scanned": scanned,
        "total": scanned,
        "processed": 0,
        "current_candidate": None,
        "missing": 0,
        "inserted": 0,
        "refreshed": 0,
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
        "inserted_ids": [],
        "excluded": [],
        "retry_excluded": retry_excluded,
        "missing_only": False,
    }


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
    candidate_ids: Sequence[str] | None = None,
    retry_excluded: bool = False,
    missing_only: bool = False,
) -> dict[str, Any]:
    """Create or refresh candidate_vectors rows.

    Missing rows and rows whose ``source_text_hash`` / ``text_schema_version``
    no longer match the current template are embedded, unless
    ``missing_only`` is set (then existing blobs are left untouched).
    Batch embedding is preferred; a non-retryable batch failure falls back
    to per-item isolation so one poison text cannot stall the whole job.
    """
    if batch_size <= 0:
        raise ValueError(f"batch_size must be > 0, got {batch_size}")
    if candidate_ids is not None and len(candidate_ids) == 0:
        rows: list[sqlite3.Row] = []
    else:
        rows = _load_backfill_rows(
            conn,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
            only_feedback=only_feedback,
            candidate_ids=candidate_ids,
        )

    result = _empty_result(
        scanned=len(rows),
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        dry_run=dry_run,
        only_feedback=only_feedback,
        batch_size=batch_size,
        retry_excluded=retry_excluded,
    )
    result["missing_only"] = missing_only
    excluded_ids = _load_excluded_ids(
        conn, embedding_model, retry_excluded=retry_excluded
    )

    pending: list[tuple[sqlite3.Row, str, str]] = []
    for row in rows:
        candidate_id = row["candidate_id"]
        result["current_candidate"] = candidate_id
        if candidate_id in excluded_ids:
            result["skipped_existing"] += 1
            result["processed"] += 1
            continue
        text = build_candidate_embedding_text(row)
        text_hash = embedding_text_hash(text)
        has_vector = row["vector_candidate_id"] is not None
        if missing_only:
            current = has_vector
        else:
            current = _is_current_vector(row, text_hash)
        if current:
            result["skipped_existing"] += 1
            result["processed"] += 1
            continue
        pending.append((row, text, text_hash))

    if limit is not None:
        pending = pending[: max(0, limit)]
    result["missing"] = len(pending)
    _emit_progress_fn(progress_fn, result)

    if dry_run:
        result["processed"] = result["skipped_existing"] + result["missing"]
        result["remaining"] = result["missing"]
        _emit_progress_fn(progress_fn, result)
        return result

    if batch_embed_fn is not None:
        return _backfill_batch(
            conn,
            pending,
            result,
            batch_embed_fn=batch_embed_fn,
            embed_fn=embed_fn,
            batch_size=batch_size,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
            progress_cb=progress_cb,
            progress_fn=progress_fn,
        )

    if embed_fn is None:
        raise ValueError(
            "backfill_candidate_vectors requires embed_fn or batch_embed_fn"
        )

    return _backfill_serial(
        conn,
        pending,
        result,
        embed_fn=embed_fn,
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        progress_fn=progress_fn,
    )


def _mark_inserted(
    result: dict[str, Any], candidate_id: str, *, had_vector: bool
) -> None:
    result["inserted"] += 1
    result["inserted_ids"].append(candidate_id)
    if had_vector:
        result["refreshed"] += 1


def _backfill_serial(
    conn: sqlite3.Connection,
    pending: list[tuple[sqlite3.Row, str, str]],
    result: dict[str, Any],
    *,
    embed_fn: EmbeddingFn,
    embedding_model: str,
    embedding_dim: int,
    progress_fn: ProgressFn | None,
) -> dict[str, Any]:
    for row, text, text_hash in pending:
        candidate_id = row["candidate_id"]
        result["current_candidate"] = candidate_id
        try:
            blob = _vector_blob(embed_fn(text), embedding_dim=embedding_dim)
            _upsert_vector(
                conn,
                candidate_id=candidate_id,
                embedding_model=embedding_model,
                embedding_dim=embedding_dim,
                blob=blob,
                text_hash=text_hash,
            )
            _clear_exclusion(
                conn, candidate_id=candidate_id, embedding_model=embedding_model
            )
            conn.commit()
            _mark_inserted(
                result, candidate_id, had_vector=row["vector_candidate_id"] is not None
            )
        except EmbeddingServiceUnavailable:
            raise
        except Exception as exc:
            if _is_transient(exc):
                conn.rollback()
                result["aborted"] = True
                result["error"] = str(exc)
                result["retryable"] = True
                if isinstance(exc, EmbeddingRuntimeError):
                    result["phase"] = exc.phase
                    result["attempts"] = exc.attempts
                    result["base_url"] = exc.base_url
                result["errors"].append(
                    {"candidate_id": candidate_id, "error": str(exc)}
                )
                result["remaining"] = result["missing"] - result["inserted"]
                return result
            result["failed"] += 1
            exclusion = _record_exclusion(
                conn,
                candidate_id=candidate_id,
                embedding_model=embedding_model,
                reason=f"embedding_failed: {exc}",
                error_class=_error_class(exc),
            )
            conn.commit()
            result["excluded"].append(exclusion)
            result["errors"].append({"candidate_id": candidate_id, "error": str(exc)})
        result["processed"] += 1
        _emit_progress_fn(progress_fn, result)

    result["remaining"] = result["missing"] - result["inserted"]
    return result


def _backfill_batch(
    conn: sqlite3.Connection,
    pending: list[tuple[sqlite3.Row, str, str]],
    result: dict[str, Any],
    *,
    batch_embed_fn: BatchEmbeddingFn,
    embed_fn: EmbeddingFn | None,
    batch_size: int,
    embedding_model: str,
    embedding_dim: int,
    progress_cb: ProgressCb | None,
    progress_fn: ProgressFn | None = None,
) -> dict[str, Any]:
    total = len(pending)
    _notify_progress(progress_cb, 0, total, result)
    _emit_progress_fn(progress_fn, result)

    for start in range(0, total, batch_size):
        chunk = pending[start : start + batch_size]
        texts = [item[1] for item in chunk]
        try:
            vectors = batch_embed_fn(texts)
            blobs = _validate_batch_vectors(vectors, chunk, embedding_dim)
            for (row, _text, text_hash), blob in zip(chunk, blobs):
                _upsert_vector(
                    conn,
                    candidate_id=row["candidate_id"],
                    embedding_model=embedding_model,
                    embedding_dim=embedding_dim,
                    blob=blob,
                    text_hash=text_hash,
                )
                _clear_exclusion(
                    conn,
                    candidate_id=row["candidate_id"],
                    embedding_model=embedding_model,
                )
                _mark_inserted(
                    result,
                    row["candidate_id"],
                    had_vector=row["vector_candidate_id"] is not None,
                )
            conn.commit()
        except EmbeddingServiceUnavailable:
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            if _is_transient(exc):
                _record_batch_abort(result, chunk, exc)
                result["remaining"] = result["missing"] - result["inserted"]
                return result
            isolated = _isolate_poison_chunk(
                conn,
                chunk,
                result,
                batch_embed_fn=batch_embed_fn,
                embed_fn=embed_fn,
                embedding_model=embedding_model,
                embedding_dim=embedding_dim,
            )
            if isolated == "abort":
                result["remaining"] = result["missing"] - result["inserted"]
                return result

        result["batches"] += 1
        result["processed"] = result["skipped_existing"] + result["inserted"]
        result["current_candidate"] = chunk[-1][0]["candidate_id"]
        _notify_progress(progress_cb, result["inserted"], total, result)
        _emit_progress_fn(progress_fn, result)

    result["remaining"] = result["missing"] - result["inserted"]
    return result


def _record_batch_abort(
    result: dict[str, Any],
    chunk: list[tuple[sqlite3.Row, str, str]],
    exc: BaseException,
) -> None:
    result["aborted"] = True
    result["error"] = str(exc)
    first_candidate_id = chunk[0][0]["candidate_id"] if chunk else None
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
    else:
        result["retryable"] = False
    result["errors"].append(error_entry)


def _embed_one(
    text: str,
    *,
    embed_fn: EmbeddingFn | None,
    batch_embed_fn: BatchEmbeddingFn,
) -> list[float]:
    if embed_fn is not None:
        return embed_fn(text)
    vectors = batch_embed_fn([text])
    if not isinstance(vectors, list) or len(vectors) != 1:
        raise ValueError("batch embedder returned an unexpected singleton result")
    return vectors[0]


def _isolate_poison_chunk(
    conn: sqlite3.Connection,
    chunk: list[tuple[sqlite3.Row, str, str]],
    result: dict[str, Any],
    *,
    batch_embed_fn: BatchEmbeddingFn,
    embed_fn: EmbeddingFn | None,
    embedding_model: str,
    embedding_dim: int,
) -> str:
    """Embed a failed batch one row at a time. Returns 'ok' or 'abort'."""
    for row, text, text_hash in chunk:
        candidate_id = row["candidate_id"]
        result["current_candidate"] = candidate_id
        try:
            blob = _vector_blob(
                _embed_one(text, embed_fn=embed_fn, batch_embed_fn=batch_embed_fn),
                embedding_dim=embedding_dim,
            )
            _upsert_vector(
                conn,
                candidate_id=candidate_id,
                embedding_model=embedding_model,
                embedding_dim=embedding_dim,
                blob=blob,
                text_hash=text_hash,
            )
            _clear_exclusion(
                conn, candidate_id=candidate_id, embedding_model=embedding_model
            )
            conn.commit()
            _mark_inserted(
                result, candidate_id, had_vector=row["vector_candidate_id"] is not None
            )
        except EmbeddingServiceUnavailable:
            conn.rollback()
            raise
        except Exception as exc:
            if _is_transient(exc):
                conn.rollback()
                _record_batch_abort(result, chunk, exc)
                return "abort"
            result["failed"] += 1
            exclusion = _record_exclusion(
                conn,
                candidate_id=candidate_id,
                embedding_model=embedding_model,
                reason=f"embedding_failed: {exc}",
                error_class=_error_class(exc),
            )
            conn.commit()
            result["excluded"].append(exclusion)
            result["errors"].append({"candidate_id": candidate_id, "error": str(exc)})
    return "ok"


def _validate_batch_vectors(
    vectors: Any,
    chunk: list[tuple[sqlite3.Row, str, str]],
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


def renormalize_stored_vectors(conn: sqlite3.Connection) -> dict[str, Any]:
    """Idempotently L2-normalize every ``candidate_vectors`` blob.

    Does not re-embed; purely local numpy. Safe to run repeatedly.
    """
    rows = conn.execute(
        """SELECT candidate_id, vector_type, embedding_model, embedding_dim,
                  vector_blob
           FROM candidate_vectors"""
    ).fetchall()
    updated = 0
    already_unit = 0
    for row in rows:
        raw = blob_to_vector(row["vector_blob"])
        dim = int(row["embedding_dim"])
        if raw.size != dim:
            raise ValueError(
                f"embedding_dim mismatch for {row['candidate_id']}: "
                f"got {raw.size}, expected {dim}"
            )
        if is_unit_vector(raw):
            already_unit += 1
            continue
        blob = vector_to_blob(raw, embedding_dim=dim)
        conn.execute(
            """UPDATE candidate_vectors
               SET vector_blob=?, normalized=1
               WHERE candidate_id=? AND vector_type=? AND embedding_model=?""",
            (
                blob,
                row["candidate_id"],
                row["vector_type"],
                row["embedding_model"],
            ),
        )
        updated += 1
    conn.commit()
    return {
        "scanned": len(rows),
        "updated": updated,
        "already_unit": already_unit,
    }


def backfill_missing_vectors(
    conn: sqlite3.Connection,
    embedder: EmbeddingFn,
    *,
    candidate_ids: Sequence[str] | None = None,
    batch_size: int = 32,
    embedding_model: str = REQUIRED_EMBEDDING_MODEL,
    embedding_dim: int = REQUIRED_EMBEDDING_DIM,
    retry_excluded: bool = False,
) -> Any:
    """Resumable incremental backfill; thin wrapper over the unified path."""
    from app.services.preference_types import BackfillReport

    def _batch(texts: list[str]) -> list[list[float]]:
        return [embedder(text) for text in texts]

    result = backfill_candidate_vectors(
        conn,
        embed_fn=embedder,
        batch_embed_fn=_batch,
        candidate_ids=candidate_ids,
        batch_size=batch_size,
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        retry_excluded=retry_excluded,
        missing_only=True,
    )
    report: BackfillReport = {
        "total": int(result["scanned"]),
        "inserted": int(result["inserted"]),
        "skipped_existing": int(result["skipped_existing"]),
        "failed": int(result["failed"]),
        "inserted_ids": list(result.get("inserted_ids") or []),
        "excluded": list(result.get("excluded") or []),
        "batch_commits": int(result.get("batches") or 0),
    }
    if report["batch_commits"] == 0 and report["inserted"] + report["failed"] > 0:
        report["batch_commits"] = 1
    return report

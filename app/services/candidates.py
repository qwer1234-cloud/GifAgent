"""P1-3: CandidateService — materialize run candidates into candidate_gifs."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

from app.services.candidate_identity import (
    best_row_for_keys,
    build_clip_identity_index,
    clip_identity_keys,
)
from app.services.preference_types import MaterializedCandidate
from app.services.scenario import normalize_scenario_keys, json_dumps


def _build_candidate_id(
    source_video_sha256: str,
    start_sec: float,
    end_sec: float,
    run_candidate_id: str,
) -> str:
    raw = f"{source_video_sha256}{start_sec}{end_sec}{run_candidate_id}"
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return f"cand_{digest}"


from collections.abc import Callable

EmbeddingFn = Callable[[str], list[float]]


class CandidateService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        embed_fn: EmbeddingFn | None = None,
    ) -> None:
        self.conn = conn
        self.embed_fn = embed_fn
        self._identity_index: dict[str, Any] | None = None

    def _existing_materialized_columns(self) -> str:
        return (
            "candidate_id, source_run_id, source_run_candidate_id, "
            "source_video_sha256, start_sec, end_sec, status, artifact_path"
        )

    def _identity_index_rows(self) -> dict[str, Any]:
        if self._identity_index is None:
            rows = self.conn.execute(
                f"SELECT {self._existing_materialized_columns()} FROM candidate_gifs"
            ).fetchall()
            self._identity_index = build_clip_identity_index(rows)
        return self._identity_index

    def _remember_identity_row(self, row: Any) -> None:
        if self._identity_index is None or row is None:
            return
        if isinstance(row, dict) or hasattr(row, "keys"):
            try:
                artifact_path = row["artifact_path"]
                start_sec = row["start_sec"]
                end_sec = row["end_sec"]
            except (KeyError, IndexError):
                artifact_path, start_sec, end_sec = None, None, None
        elif isinstance(row, (tuple, list)) and len(row) >= 8:
            start_sec = row[4]
            end_sec = row[5]
            artifact_path = row[7]
        else:
            return
        if start_sec is not None and end_sec is not None:
            for key in clip_identity_keys(artifact_path, start_sec, end_sec):
                if key not in self._identity_index:
                    self._identity_index[key] = row

    def _reuse_existing_candidate(
        self, existing: Any, run_candidate: dict[str, Any]
    ) -> MaterializedCandidate:
        is_map = isinstance(existing, dict) or hasattr(existing, "keys")
        candidate_id = existing["candidate_id"] if is_map else existing[0]
        source_run_id = existing["source_run_id"] if is_map else existing[1]
        source_run_candidate_id = existing["source_run_candidate_id"] if is_map else existing[2]
        source_video_sha256 = existing["source_video_sha256"] if is_map else existing[3]
        start_sec = existing["start_sec"] if is_map else existing[4]
        end_sec = existing["end_sec"] if is_map else existing[5]
        status = existing["status"] if is_map else existing[6]

        self._fill_empty_library_text(candidate_id, run_candidate)
        self._best_effort_upsert_vector(candidate_id)
        self._remember_identity_row(existing)
        return MaterializedCandidate(
            candidate_id=candidate_id,
            source_run_id=source_run_id,
            source_run_candidate_id=source_run_candidate_id,
            source_video_sha256=source_video_sha256,
            start_sec=start_sec,
            end_sec=end_sec,
            status=status,
        )

    def _find_existing_candidate(
        self,
        candidate_id: str,
        run_candidate: dict[str, Any],
        start_sec: float,
        end_sec: float,
    ) -> Any | None:
        existing = self.conn.execute(
            f"SELECT {self._existing_materialized_columns()} "
            "FROM candidate_gifs WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if existing is not None:
            return existing
        twin = best_row_for_keys(
            self._identity_index_rows(),
            clip_identity_keys(
                run_candidate.get("artifact_path"), start_sec, end_sec
            ),
        )
        if twin is None:
            return None
        return self.conn.execute(
            f"SELECT {self._existing_materialized_columns()} "
            "FROM candidate_gifs WHERE candidate_id=?",
            (twin["candidate_id"],),
        ).fetchone()

    def materialize_run_candidate(
        self, run_id: str, run_candidate: dict[str, Any]
    ) -> MaterializedCandidate:
        source_video_sha256 = run_candidate["source_video_sha256"]
        start_sec = float(run_candidate["start_sec"])
        end_sec = float(run_candidate["end_sec"])
        run_candidate_id = run_candidate["run_candidate_id"]

        candidate_id = _build_candidate_id(
            source_video_sha256, start_sec, end_sec, run_candidate_id
        )

        existing = self._find_existing_candidate(
            candidate_id, run_candidate, start_sec, end_sec
        )
        if existing is not None:
            return self._reuse_existing_candidate(existing, run_candidate)

        source_video_path = run_candidate.get("source_video_path", "")
        artifact_path = run_candidate.get("artifact_path")
        preview_path = run_candidate.get("preview_path")
        vlm_summary = run_candidate.get("vlm_summary", {})
        tags = run_candidate.get("tags", [])
        base_rag_similarity = run_candidate.get("base_rag_similarity")
        final_score = run_candidate.get("final_score")

        # Build scenario keys from vlm_summary and tags
        emotion = vlm_summary.get("emotion") if isinstance(vlm_summary, dict) else None
        scene_type = vlm_summary.get("scene_type") if isinstance(vlm_summary, dict) else None
        scenario_keys = normalize_scenario_keys(
            emotion=emotion, scene_type=scene_type, tags=tags
        )

        now = datetime.now(timezone.utc).isoformat()

        self.conn.execute(
            """INSERT OR IGNORE INTO candidate_gifs
               (candidate_id, source_run_id, source_run_candidate_id,
                source_video_sha256, source_video_path, start_sec, end_sec,
                artifact_path, preview_path,
                vlm_summary_json, tags_json, scenario_keys_json,
                base_rag_similarity, final_score, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                candidate_id,
                run_id,
                run_candidate_id,
                source_video_sha256,
                source_video_path,
                start_sec,
                end_sec,
                artifact_path,
                preview_path,
                json_dumps(vlm_summary),
                json_dumps(tags),
                json_dumps(scenario_keys),
                base_rag_similarity,
                final_score,
                "candidate",
                now,
                now,
            ),
        )
        # Sync into FTS index with clean deduplication
        summary_text = ""
        if isinstance(vlm_summary, dict):
            summary_text = vlm_summary.get("caption") or vlm_summary.get("summary") or ""
        tags_text = " ".join(str(t) for t in (tags or []) if t)
        fts_source = source_video_path or artifact_path or preview_path or ""
        try:
            self.conn.execute("DELETE FROM candidate_search_fts WHERE candidate_id = ?", (candidate_id,))
            self.conn.execute(
                """INSERT INTO candidate_search_fts
                   (candidate_id, summary, tags, source_path)
                   VALUES (?, ?, ?, ?)""",
                (candidate_id, summary_text, tags_text, fts_source),
            )
        except Exception as exc:
            logger.warning("Failed to sync candidate %s to FTS: %s", candidate_id, exc)
        self.conn.commit()

        self._best_effort_upsert_vector(candidate_id)

        # If INSERT OR IGNORE silently skipped (race), re-read
        row = self.conn.execute(
            f"SELECT {self._existing_materialized_columns()} "
            "FROM candidate_gifs WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        self._remember_identity_row(row)

        is_map = isinstance(row, dict) or hasattr(row, "keys")
        return MaterializedCandidate(
            candidate_id=row["candidate_id"] if is_map else row[0],
            source_run_id=row["source_run_id"] if is_map else row[1],
            source_run_candidate_id=row["source_run_candidate_id"] if is_map else row[2],
            source_video_sha256=row["source_video_sha256"] if is_map else row[3],
            start_sec=row["start_sec"] if is_map else row[4],
            end_sec=row["end_sec"] if is_map else row[5],
            status=row["status"] if is_map else row[6],
        )

    def _payload_has_semantic_text(self, run_candidate: dict[str, Any]) -> bool:
        vlm_summary = run_candidate.get("vlm_summary") or {}
        tags = run_candidate.get("tags") or []
        if isinstance(vlm_summary, dict):
            for key in (
                "caption",
                "summary",
                "emotion",
                "emotional_core",
                "scene_type",
                "reason",
            ):
                if str(vlm_summary.get(key) or "").strip():
                    return True
        return any(str(tag).strip() for tag in tags if tag)

    def _fill_empty_library_text(
        self, candidate_id: str, run_candidate: dict[str, Any]
    ) -> None:
        """If a folder-import row has no VLM text, copy incoming caption/tags."""
        if not self._payload_has_semantic_text(run_candidate):
            return
        from app.services.candidate_vectors import semantic_embedding_text

        row = self.conn.execute(
            """SELECT vlm_summary_json, tags_json, scenario_keys_json
               FROM candidate_gifs WHERE candidate_id=?""",
            (candidate_id,),
        ).fetchone()
        if row is None or semantic_embedding_text(row):
            return

        vlm_summary = run_candidate.get("vlm_summary") or {}
        tags = run_candidate.get("tags") or []
        emotion = vlm_summary.get("emotion") if isinstance(vlm_summary, dict) else None
        scene_type = (
            vlm_summary.get("scene_type") if isinstance(vlm_summary, dict) else None
        )
        scenario_keys = normalize_scenario_keys(
            emotion=emotion, scene_type=scene_type, tags=tags
        )
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """UPDATE candidate_gifs
               SET vlm_summary_json=?, tags_json=?, scenario_keys_json=?,
                   artifact_path=COALESCE(NULLIF(artifact_path, ''), ?),
                   preview_path=COALESCE(NULLIF(preview_path, ''), ?),
                   updated_at=?
               WHERE candidate_id=?""",
            (
                json_dumps(vlm_summary),
                json_dumps(tags),
                json_dumps(scenario_keys),
                run_candidate.get("artifact_path"),
                run_candidate.get("preview_path"),
                now,
                candidate_id,
            ),
        )
        summary_text = ""
        if isinstance(vlm_summary, dict):
            summary_text = vlm_summary.get("caption") or vlm_summary.get("summary") or ""
        tags_text = " ".join(str(t) for t in (tags or []) if t)
        fts_source = (
            run_candidate.get("source_video_path")
            or run_candidate.get("artifact_path")
            or run_candidate.get("preview_path")
            or ""
        )
        try:
            self.conn.execute(
                "DELETE FROM candidate_search_fts WHERE candidate_id = ?",
                (candidate_id,),
            )
            self.conn.execute(
                """INSERT INTO candidate_search_fts
                   (candidate_id, summary, tags, source_path)
                   VALUES (?, ?, ?, ?)""",
                (candidate_id, summary_text, tags_text, fts_source),
            )
        except Exception as exc:
            logger.warning("Failed to refresh FTS for candidate %s: %s", candidate_id, exc)
        self.conn.commit()

    def _best_effort_upsert_vector(self, candidate_id: str) -> None:
        try:
            from app.services.candidate_vectors import upsert_candidate_vector

            upsert_candidate_vector(
                self.conn,
                candidate_id=candidate_id,
                embed_fn=self.embed_fn,
            )
        except Exception as exc:
            logger.warning(
                "Best-effort candidate vector upsert failed for %s: %s",
                candidate_id,
                exc,
            )

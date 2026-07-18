"""Phase 4 Task 3: LibrarySearchService -- FTS5 + vector similarity search.

Exposes three public operations:

* ``search(query, limit, offset)`` -- ranked, filtered, paginated search
* ``index_health()`` -- diagnostic coverage report
* ``rebuild_index(batch_size)`` -- resumable FTS5 index rebuild
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import numpy as np

from app.services.preference_memory import (
    REQUIRED_EMBEDDING_DIM,
    REQUIRED_EMBEDDING_MODEL,
)
from app.services.workbench_schema import (
    IndexHealth,
    RebuildReport,
    SearchPage,
    SearchQuery,
    SearchResultItem,
    apply_search_schema,
)

EmbeddingFn = Callable[[str], list[float]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_json_loads(value: str | None, fallback: Any = None) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


# Characters that have special meaning in FTS5 query syntax.
_FTS_SPECIAL = re.compile(r'[\^"()+\-*]|(?:^|\s)(?:AND|OR|NOT|NEAR)(?=\s|$)', re.IGNORECASE)


def _fts_escape(text: str) -> str:
    """Escape free-text so it is safe for an FTS5 MATCH query.

    Each word is double-quoted, which forces literal matching while still
    allowing the FTS5 tokenizer to apply stemming and prefix expansion.
    """
    tokens = text.split()
    if not tokens:
        return '""'
    return " AND ".join(f'"{t}"' for t in tokens if t.strip())


def _parse_tags(tags_json: str | None) -> list[str]:
    v = _safe_json_loads(tags_json, [])
    return list(v) if isinstance(v, list) else []


def _parse_summary(vlm_json: str | None) -> str:
    v = _safe_json_loads(vlm_json, {})
    if isinstance(v, dict):
        return v.get("caption") or v.get("summary") or ""
    return ""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class LibrarySearchService:
    """FTS5 + candidate-vector search over the GIF library.

    Parameters
    ----------
    conn : sqlite3.Connection
        Read-write connection to the library database.
    embedder : EmbeddingFn | None
        Optional text-embedding function for vector similarity.  When
        *None*, text search uses FTS ranking only.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        embedder: EmbeddingFn | None = None,
    ):
        self.conn = conn
        self.embedder = embedder
        apply_search_schema(conn)

    # ── public API ──────────────────────────────────────────────────────────

    def search(
        self,
        query: SearchQuery,
        *,
        limit: int = 24,
        offset: int = 0,
    ) -> SearchPage:
        """Execute a search.

        1. Exact filters (tags, folder, duration, statuses, dates) are
           applied first as SQL WHERE clauses.
        2. With a non-empty *text* query the results are ranked by a
           combination of FTS5 BM25 score and cosine similarity against
           the query-text embedding.  Falls back to FTS-only when no
           embedder is configured.
        3. Without *text* the results are ordered by ``final_score DESC``
           then ``created_at DESC``.
        """
        # Build exact-filter WHERE clause
        where, params = self._build_filter_where(query)

        # Check for empty result set from filters
        try:
            pre_total = self.conn.execute(
                f"SELECT COUNT(*) FROM candidate_gifs cg WHERE {where}", params
            ).fetchone()[0]
        except sqlite3.OperationalError:
            return SearchPage(items=[], total=0, limit=limit, offset=offset)

        if pre_total == 0:
            return SearchPage(items=[], total=0, limit=limit, offset=offset)

        # Get ordered candidate IDs
        if query.text.strip():
            ranked = self._rank_with_text(where, params, query.text)
        else:
            ranked = self._rank_without_text(where, params)

        # Total is the number of ranked results (filters + text match)
        total = len(ranked)

        # Paginate
        page = ranked[offset : offset + limit]
        page_ids = [cid for cid, _ in page]
        score_map = {cid: sc for cid, sc in page}

        # Fetch full row data for page hits
        items = self._fetch_items(page_ids, score_map)

        # Health
        health = self.index_health()
        degraded = not health.complete

        return SearchPage(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            degraded=degraded,
            diagnosis=health.diagnosis if degraded else None,
        )

    def index_health(self) -> IndexHealth:
        """Inspect FTS5 and vector coverage."""
        try:
            total = self.conn.execute(
                "SELECT COUNT(*) FROM candidate_gifs"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            return IndexHealth(
                total_candidates=0, indexed_in_fts=0,
                vectors_available=0, vectors_missing=0,
                complete=True, diagnosis="No candidate_gifs table.",
            )

        try:
            indexed = self.conn.execute(
                "SELECT COUNT(*) FROM candidate_search_fts"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            indexed = 0

        try:
            vectors_avail = self.conn.execute(
                """SELECT COUNT(DISTINCT candidate_id)
                   FROM candidate_vectors
                   WHERE embedding_model=? AND embedding_dim=?""",
                (REQUIRED_EMBEDDING_MODEL, REQUIRED_EMBEDDING_DIM),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            vectors_avail = 0

        complete = total > 0 and indexed >= total and vectors_avail >= total
        # Trivially healthy when no candidates exist
        if total == 0:
            complete = True

        if not complete:
            parts = []
            if indexed < total:
                parts.append(f"FTS index: {indexed}/{total} candidates indexed")
            if vectors_avail < total:
                parts.append(
                    f"Vectors: {vectors_avail}/{total} candidates have embeddings"
                )
            diagnosis = "; ".join(parts)
        else:
            diagnosis = "All candidates indexed and vectorized."

        return IndexHealth(
            total_candidates=total,
            indexed_in_fts=indexed,
            vectors_available=vectors_avail,
            vectors_missing=total - vectors_avail,
            complete=complete,
            diagnosis=diagnosis,
        )

    def rebuild_index(self, batch_size: int = 200) -> RebuildReport:
        """Resumable FTS5 index rebuild.

        Reads ``search_index_state.last_candidate_id`` and processes only
        candidates whose ID is strictly greater.  Each batch is committed
        independently.
        """
        scanned = 0
        inserted = 0
        skipped = 0
        errors = 0
        error_details: list[str] = []
        batch_commits = 0
        last_id: str | None = None
        last_created_at: str | None = None

        state = self.conn.execute(
            "SELECT last_candidate_id, last_candidate_created_at, indexed_count, total_count "
            "FROM search_index_state WHERE id=1"
        ).fetchone()

        resume_after_id: str | None = None
        resume_after_ts: str | None = None
        if state is not None and state["last_candidate_id"] is not None:
            resume_after_id = state["last_candidate_id"]
            resume_after_ts = state["last_candidate_created_at"]

        # Gather candidates that need indexing.
        # Use (created_at, candidate_id) tuple comparison for correct resume.
        if resume_after_id is not None and resume_after_ts is not None:
            rows = self.conn.execute(
                """SELECT candidate_id, vlm_summary_json, tags_json,
                          source_video_path, artifact_path, preview_path,
                          created_at
                   FROM candidate_gifs
                   WHERE (created_at, candidate_id) > (?, ?)
                   ORDER BY created_at ASC, candidate_id ASC""",
                (resume_after_ts, resume_after_id),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT candidate_id, vlm_summary_json, tags_json,
                          source_video_path, artifact_path, preview_path,
                          created_at
                   FROM candidate_gifs
                   ORDER BY created_at ASC, candidate_id ASC"""
            ).fetchall()

        total_rows = len(rows)

        # Bail early if there is nothing new to index.
        if total_rows == 0:
            if state is not None:
                return RebuildReport(
                    scanned=0, inserted=0, skipped=0, errors=0,
                    error_details=[], batch_commits=0,
                    last_candidate_id=state["last_candidate_id"],
                )
            return RebuildReport(
                scanned=0, inserted=0, skipped=0, errors=0,
                error_details=[], batch_commits=0,
                last_candidate_id=None,
            )

        for i, row in enumerate(rows):
            candidate_id = row["candidate_id"]
            scanned += 1
            last_id = candidate_id
            last_created_at = row["created_at"]

            vlm = _safe_json_loads(row["vlm_summary_json"], {})
            tags = _safe_json_loads(row["tags_json"], [])

            summary = ""
            if isinstance(vlm, dict):
                summary = vlm.get("caption") or vlm.get("summary") or ""

            tags_text = " ".join(str(t) for t in (tags or []) if t)
            source_path = (
                row["source_video_path"]
                or row["artifact_path"]
                or row["preview_path"]
                or ""
            )

            try:
                self.conn.execute(
                    """INSERT OR REPLACE INTO candidate_search_fts
                       (candidate_id, summary, tags, source_path)
                       VALUES (?, ?, ?, ?)""",
                    (candidate_id, summary, tags_text, source_path),
                )
                inserted += 1
            except Exception as exc:
                errors += 1
                error_details.append(f"{candidate_id}: {exc}")

            # Commit batch
            if (i + 1) % batch_size == 0 or i == total_rows - 1:
                indexed_before = state["indexed_count"] if state is not None else 0
                cumulative = indexed_before + inserted

                self.conn.execute(
                    """INSERT OR REPLACE INTO search_index_state
                       (id, last_candidate_id, last_candidate_created_at,
                        indexed_count, total_count, updated_at)
                       VALUES (1, ?, ?, ?, ?, ?)""",
                    (
                        last_id,
                        last_created_at,
                        cumulative,
                        total_rows,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                self.conn.commit()
                batch_commits += 1

        # If resume_after was set and there were no rows, that's fine --
        # we just scanned 0 new rows.
        return RebuildReport(
            scanned=scanned,
            inserted=inserted,
            skipped=skipped,
            errors=errors,
            error_details=error_details,
            batch_commits=batch_commits,
            last_candidate_id=last_id,
        )

    # ── internal helpers ──────────────────────────────────────────────────

    def _build_filter_where(self, query: SearchQuery) -> tuple[str, list]:
        """Build WHERE clause + params for exact filters.

        Returns ``(where_clause, params)`` for use with
        ``SELECT ... FROM candidate_gifs cg WHERE {where_clause}``.
        """
        clauses: list[str] = []
        params: list[Any] = []

        # Tags -- use json_each for correct JSON array matching
        if query.tags:
            for tag in query.tags:
                clauses.append(
                    "EXISTS (SELECT 1 FROM json_each(cg.tags_json) AS je WHERE je.value = ?)"
                )
                params.append(str(tag))

        # Folder substring match against path columns
        if query.folder:
            pattern = f"%{query.folder}%"
            clauses.append(
                "(cg.source_video_path LIKE ? OR cg.artifact_path LIKE ? "
                "OR cg.preview_path LIKE ?)"
            )
            params.extend([pattern, pattern, pattern])

        # Duration range
        if query.min_duration is not None:
            clauses.append("(cg.end_sec - cg.start_sec) >= ?")
            params.append(query.min_duration)

        if query.max_duration is not None:
            clauses.append("(cg.end_sec - cg.start_sec) <= ?")
            params.append(query.max_duration)

        # Status list
        if query.statuses:
            placeholders = ",".join("?" for _ in query.statuses)
            clauses.append(f"cg.status IN ({placeholders})")
            params.extend(query.statuses)

        # Date range
        if query.created_after is not None:
            clauses.append("cg.created_at >= ?")
            params.append(query.created_after)

        if query.created_before is not None:
            clauses.append("cg.created_at <= ?")
            params.append(query.created_before)

        where_clause = " AND ".join(clauses) if clauses else "1=1"
        return where_clause, params

    def _rank_with_text(
        self, where: str, params: list, text: str
    ) -> list[tuple[str, float]]:
        """Rank candidates matching *where* by FTS + optional vector score.

        Returns ``[(candidate_id, combined_score), ...]`` sorted descending
        by score.
        """
        fts_query = _fts_escape(text)

        # Get candidate_ids matching filters
        filtered_ids = [
            r["candidate_id"]
            for r in self.conn.execute(
                f"SELECT cg.candidate_id FROM candidate_gifs cg WHERE {where} "
                f"ORDER BY cg.candidate_id",
                params,
            ).fetchall()
        ]

        if not filtered_ids:
            return []

        # Query FTS for matching candidates
        fts_results: dict[str, float] = {}
        try:
            fts_rows = self.conn.execute(
                """SELECT candidate_id, rank
                   FROM candidate_search_fts
                   WHERE candidate_search_fts MATCH ?
                   ORDER BY rank""",
                (fts_query,),
            ).fetchall()
            for row in fts_rows:
                cid = row["candidate_id"]
                if cid in filtered_ids:
                    # Normalize FTS rank: lower raw rank = better match.
                    # Convert so higher = better, range approx [0, 1).
                    fts_results[cid] = 1.0 / (1.0 + abs(row["rank"]))
        except sqlite3.OperationalError:
            # FTS table might be empty or not exist
            pass

        # Compute vector similarity for filtered candidates
        vec_sims: dict[str, float] = {}
        if self.embedder is not None and fts_results:
            query_vec = self._get_query_vector(text)
            if query_vec is not None:
                vec_sims = self._compute_similarities(list(fts_results.keys()), query_vec)

        # Combine scores
        scored: list[tuple[str, float]] = []
        for cid in filtered_ids:
            if cid not in fts_results and cid not in vec_sims:
                continue
            fts_score = fts_results.get(cid, 0.0)
            vec_score = vec_sims.get(cid, 0.0)
            if self.embedder is not None and vec_sims:
                combined = 0.5 * fts_score + 0.5 * vec_score
            else:
                combined = fts_score
            scored.append((cid, combined))

        # Sort descending by score, then candidate_id for stability
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored

    def _rank_without_text(
        self, where: str, params: list,
    ) -> list[tuple[str, float | None]]:
        """Rank candidates by ``final_score DESC``, then ``created_at DESC``.

        Returns ``[(candidate_id, final_score), ...]``.
        """
        rows = self.conn.execute(
            f"""SELECT cg.candidate_id, cg.final_score
                FROM candidate_gifs cg
                WHERE {where}
                ORDER BY cg.final_score DESC NULLS LAST,
                         cg.created_at DESC,
                         cg.candidate_id DESC""",
            params,
        ).fetchall()
        return [(r["candidate_id"], r["final_score"]) for r in rows]

    def _fetch_items(
        self,
        candidate_ids: list[str],
        score_map: dict[str, float | None],
    ) -> list[SearchResultItem]:
        """Fetch full candidate rows and build ``SearchResultItem`` list."""
        if not candidate_ids:
            return []

        placeholders = ",".join("?" for _ in candidate_ids)
        rows = self.conn.execute(
            f"""SELECT cg.candidate_id, cg.preview_path, cg.source_video_path,
                      cg.start_sec, cg.end_sec,
                      cg.vlm_summary_json, cg.tags_json,
                      cg.status, cg.created_at
               FROM candidate_gifs cg
               WHERE cg.candidate_id IN ({placeholders})
               ORDER BY CASE cg.candidate_id
                   {' '.join(f'WHEN ? THEN {i}' for i in range(len(candidate_ids)))}
               END""",
            candidate_ids + candidate_ids,
        ).fetchall()

        items: list[SearchResultItem] = []
        for row in rows:
            cid = row["candidate_id"]
            items.append(
                SearchResultItem(
                    candidate_id=cid,
                    preview_path=row["preview_path"],
                    source_video_path=row["source_video_path"],
                    start_sec=float(row["start_sec"]),
                    end_sec=float(row["end_sec"]),
                    duration=float(row["end_sec"]) - float(row["start_sec"]),
                    summary=_parse_summary(row["vlm_summary_json"]),
                    tags=_parse_tags(row["tags_json"]),
                    status=row["status"],
                    score=score_map.get(cid),
                    created_at=row["created_at"],
                )
            )
        return items

    def _get_query_vector(self, text: str) -> np.ndarray | None:
        """Embed query text into a vector; returns *None* on failure."""
        if self.embedder is None:
            return None
        try:
            vec = self.embedder(text)
            return np.asarray(vec, dtype=np.float32)
        except Exception:
            return None

    def _compute_similarities(
        self,
        candidate_ids: list[str],
        query_vec: np.ndarray,
    ) -> dict[str, float]:
        """Compute cosine similarity between *query_vec* and stored vectors.

        Only candidates whose vectors exist in ``candidate_vectors`` are
        scored; missing candidates are omitted from the result dict.
        """
        sims: dict[str, float] = {}
        for cid in candidate_ids:
            row = self.conn.execute(
                """SELECT vector_blob FROM candidate_vectors
                   WHERE candidate_id=?
                     AND vector_type='clip'
                     AND embedding_model=?
                     AND embedding_dim=?""",
                (cid, REQUIRED_EMBEDDING_MODEL, REQUIRED_EMBEDDING_DIM),
            ).fetchone()
            if row is None:
                continue
            vec = np.frombuffer(row["vector_blob"], dtype=np.float32)
            # For normalized vectors, cosine similarity = dot product
            sim = float(np.dot(query_vec, vec))
            # Clamp to [0, 1] for safety
            sims[cid] = max(0.0, min(1.0, sim))
        return sims

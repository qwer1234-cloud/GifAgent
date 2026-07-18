"""Workbench API router -- attention inbox, Today-tab, search, timeline,
and collections.

Endpoints
---------
- ``GET /api/workbench/attention``           -- aggregated attention inbox
- ``POST /api/workbench/search``             -- semantic + filtered search
- ``GET /api/workbench/search/index-health``  -- search index health
- ``POST /api/workbench/search/rebuild``      -- rebuild search index
- ``GET /api/workbench/videos/{video_id}/timeline`` -- moment timeline window
- ``GET /api/workbench/collections``          -- list all collections
- ``POST /api/workbench/collections``         -- create a new collection
- ``POST /api/workbench/collections/{collection_id}/refresh`` -- refresh
- ``POST /api/workbench/collections/{collection_id}/freeze``  -- freeze
- ``POST /api/workbench/collections/{collection_id}/export``   -- export
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Optional

from pathlib import Path

import numpy as np

from fastapi import APIRouter, Body, Query

from app.db import get_connection as get_library_conn
from app.quality_lab.schema import connect_quality_db
from app.services.attention import (
    AttentionResponse,
    list_attention_items,
)
from app.services.embedding import compute_text_embedding
from app.services.library_search import LibrarySearchService
from app.services.timeline import (
    TimelineWindow,
    load_timeline_window,
    potplayer_target,
)
from app.services.media_relink import (
    RelinkProposal,
    RelinkResult,
    apply_relink,
    propose_relinks,
)
from app.services.workbench_schema import (
    Collection,
    CollectionVersion,
    ExportReport,
    IndexHealth,
    RebuildReport,
    SearchPage,
    SearchQuery,
)
from app.task_engine.repository import TaskRepository
from app.task_engine.schema import connect_task_db

router = APIRouter(prefix="/api/workbench", tags=["workbench"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_comma_separated(value: str) -> tuple[str, ...]:
    """Split a comma-separated string into a tuple of stripped values."""
    if not value or not value.strip():
        return ()
    return tuple(v.strip() for v in value.split(",") if v.strip())


# ---------------------------------------------------------------------------
# Attention inbox
# ---------------------------------------------------------------------------


@router.get("/attention", response_model=AttentionResponse)
def get_attention(
    limit: int = Query(default=100, ge=1, le=500),
):
    """Return aggregated attention inbox across all data sources.

    Each database is opened with an independent short-lived connection.
    If one database is temporarily unavailable the endpoint returns
    partial results with a ``source_warnings`` entry -- it never fails
    the entire inbox because Quality Lab is locked.
    """
    source_warnings = []

    # -- Task DB --
    task_repo = None
    try:
        task_conn = connect_task_db()
        task_repo = TaskRepository(task_conn)
    except sqlite3.OperationalError as exc:
        source_warnings.append("task_db: database unavailable ({exc})".format(exc=exc))
    except Exception as exc:
        source_warnings.append("task_db: unexpected error ({exc})".format(exc=exc))

    # -- Library DB --
    library_conn = None
    try:
        library_conn = get_library_conn()
    except sqlite3.OperationalError as exc:
        source_warnings.append("library_db: database unavailable ({exc})".format(exc=exc))
    except Exception as exc:
        source_warnings.append("library_db: unexpected error ({exc})".format(exc=exc))

    # -- Quality DB --
    quality_conn = None
    try:
        quality_conn = connect_quality_db()
    except sqlite3.OperationalError as exc:
        source_warnings.append("quality_db: database unavailable ({exc})".format(exc=exc))
    except Exception as exc:
        source_warnings.append("quality_db: unexpected error ({exc})".format(exc=exc))

    # Collect items (each source handles None gracefully)
    try:
        items = list_attention_items(
            task_repo=task_repo,
            library_conn=library_conn,
            quality_conn=quality_conn,
            limit=limit,
        )
    except Exception as exc:
        return AttentionResponse(
            items=[],
            source_warnings=source_warnings + ["aggregation: {exc}".format(exc=exc)],
        )
    finally:
        if task_repo is not None:
            try:
                task_repo.conn.close()
            except Exception:
                pass
        if library_conn is not None:
            try:
                library_conn.close()
            except Exception:
                pass
        if quality_conn is not None:
            try:
                quality_conn.close()
            except Exception:
                pass

    return AttentionResponse(items=items, source_warnings=source_warnings)


# ---------------------------------------------------------------------------
# Search endpoints
# ---------------------------------------------------------------------------


@router.post("/search", response_model=SearchPage)
def search_candidates(
    query_text: str = "",
    tags: str = "",
    folder: str = "",
    min_duration: Optional[float] = None,
    max_duration: Optional[float] = None,
    statuses: str = "",
    created_after: Optional[str] = None,
    created_before: Optional[str] = None,
    limit: int = 24,
    offset: int = 0,
):
    """Search candidates by text, tags, folder, duration, statuses, and dates.

    When *query_text* is empty the results are ordered by ``final_score``.
    When *query_text* is provided, FTS5 full-text search is combined with
    embedding-vector similarity for semantic ranking.

    ``tags`` and ``statuses`` are comma-separated strings.
    """
    from app.services.workbench_schema import SearchQuery

    search_query = SearchQuery(
        text=query_text,
        tags=_parse_comma_separated(tags),
        folder=folder or None,
        min_duration=min_duration,
        max_duration=max_duration,
        statuses=_parse_comma_separated(statuses),
        created_after=created_after,
        created_before=created_before,
    )

    conn = get_library_conn()
    try:
        svc = LibrarySearchService(conn, embedder=compute_text_embedding)
        return svc.search(search_query, limit=limit, offset=offset)
    finally:
        conn.close()


@router.get("/search/index-health", response_model=IndexHealth)
def search_index_health():
    """Return the health of the search index and vector coverage."""
    conn = get_library_conn()
    try:
        svc = LibrarySearchService(conn)
        return svc.index_health()
    finally:
        conn.close()


@router.post("/search/rebuild", response_model=RebuildReport)
def rebuild_search_index(
    batch_size: int = 200,
):
    """Resumable rebuild of the FTS5 search index."""
    conn = get_library_conn()
    try:
        svc = LibrarySearchService(conn)
        return svc.rebuild_index(batch_size=batch_size)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------


@dataclass
class TimelineSpanResponse:
    """JSON-serialisable timeline span."""
    span_id: str
    start_sec: float
    end_sec: float
    label: str
    base_score: float | None = None
    preference_score: float | None = None
    thumbnail_path: str | None = None
    potplayer_target: str | None = None


@dataclass
class TimelineWindowResponse:
    """JSON-serialisable timeline window."""
    video_id: str
    start_sec: float
    end_sec: float
    scenes: list[TimelineSpanResponse]
    candidates: list[TimelineSpanResponse]
    generated_gifs: list[TimelineSpanResponse]


def _to_timeline_response(
    window: TimelineWindow,
    video_path: str | None,
) -> TimelineWindowResponse:
    """Convert a ``TimelineWindow`` dataclass to a JSON-safe response."""

    def _build(span):
        target = None
        if video_path:
            target = potplayer_target(video_path, span.start_sec)
        return TimelineSpanResponse(
            span_id=span.span_id,
            start_sec=span.start_sec,
            end_sec=span.end_sec,
            label=span.label,
            base_score=span.base_score,
            preference_score=span.preference_score,
            thumbnail_path=span.thumbnail_path,
            potplayer_target=target,
        )

    return TimelineWindowResponse(
        video_id=window.video_id,
        start_sec=window.start_sec,
        end_sec=window.end_sec,
        scenes=[_build(s) for s in window.scenes],
        candidates=[_build(s) for s in window.candidates],
        generated_gifs=[_build(s) for s in window.generated_gifs],
    )


@router.get("/videos/{video_id}/timeline")
def get_timeline(
    video_id: str,
    start_sec: float = 0.0,
    end_sec: float = 60.0,
    max_thumbnails: int = 60,
):
    """Return timeline spans overlapping the given viewport window.

    Parameters
    ----------
    video_id:
        The ``media.media_id`` of the source video.
    start_sec, end_sec:
        Viewport time window in seconds (default 0-60).
    max_thumbnails:
        Maximum number of thumbnail paths to populate (default 60).

    The response includes ``potplayer_target`` on each span, which is a
    ``potplayer://`` URL that the desktop launcher can use to jump the
    video to that span's start position.
    """
    conn = get_library_conn()
    try:
        # Resolve video path for PotPlayer targets
        video_row = conn.execute(
            "SELECT file_path FROM media WHERE media_id = ?",
            (video_id,),
        ).fetchone()
        video_path = video_row["file_path"] if video_row else None

        window = load_timeline_window(
            conn,
            video_id=video_id,
            start_sec=start_sec,
            end_sec=end_sec,
            max_thumbnails=max_thumbnails,
        )
        return _to_timeline_response(window, video_path)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Relink endpoints
# ---------------------------------------------------------------------------


@router.post("/relinks/scan", response_model=list[RelinkProposal])
def scan_relinks(
    search_root: str = Body(..., embed=True),
):
    """Scan *search_root* for moved media files and propose relinks.

    This is a read-only operation.  See ``POST /relinks/apply`` to
    commit one proposal.
    """
    conn = get_library_conn()
    try:
        return propose_relinks(conn, Path(search_root))
    finally:
        conn.close()


@router.post("/relinks/apply", response_model=RelinkResult)
def apply_relink_endpoint(
    media_id: str = Body(...),
    old_path: str = Body(...),
    new_path: str = Body(...),
    confidence: str = Body(...),
    fingerprint: str = Body(...),
    confirmed: bool = Body(default=True),
):
    """Apply a single relink proposal.

    Unless *confirmed* is ``false``, the function verifies the
    fingerprint, starts a short transaction, and updates
    ``media.file_path`` together with the three path columns on
    ``candidate_gifs`` (``source_video_path``, ``artifact_path``,
    ``preview_path``).

    Returns a :class:`RelinkResult` with row counts.  Raises
    ``HTTP 422`` when the fingerprint no longer matches or the target
    path is already claimed by another media row.
    """
    proposal = RelinkProposal(
        media_id=media_id,
        old_path=old_path,
        new_path=new_path,
        confidence=confidence,  # type: ignore[arg-type]
        fingerprint=fingerprint,
    )
    conn = get_library_conn()
    try:
        return apply_relink(conn, proposal, confirmed=confirmed)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Collection endpoints
# ---------------------------------------------------------------------------


_COLLECTION_DEFAULTS = {
    "min_duration": None,
    "max_duration": None,
    "diversity_weight": 0.5,
    "profile_version": None,
    "config_id": None,
}


@router.get("/collections", response_model=list[dict])
def list_collections():
    """Return a lightweight list of all collections with summary info."""
    conn = get_library_conn()
    try:
        rows = conn.execute(
            """SELECT collection_id, name, current_version, frozen,
                      target_count, diversity_weight, created_at
               FROM collections
               ORDER BY created_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/collections", response_model=Collection)
def create_collection(
    name: str = Body(..., embed=True),
    query_text: str = "",
    tags: str = "",
    folder: str = "",
    min_duration: Optional[float] = None,
    max_duration: Optional[float] = None,
    statuses: str = "",
    created_after: Optional[str] = None,
    created_before: Optional[str] = None,
    target_count: int = Body(default=24, embed=True),
    diversity_weight: float = 0.5,
    profile_version: Optional[str] = None,
    config_id: Optional[str] = None,
):
    """Create a new smart collection.

    ``tags`` and ``statuses`` are comma-separated strings that are parsed
    into tuples for the internal ``SearchQuery``.
    """
    from app.services.collections import CollectionService, CollectionSpec

    search_query = SearchQuery(
        text=query_text,
        tags=_parse_comma_separated(tags),
        folder=folder or None,
        min_duration=min_duration,
        max_duration=max_duration,
        statuses=_parse_comma_separated(statuses),
        created_after=created_after,
        created_before=created_before,
    )

    spec = CollectionSpec(
        name=name,
        query=search_query,
        target_count=target_count,
        min_duration=min_duration,
        max_duration=max_duration,
        diversity_weight=diversity_weight,
        profile_version=profile_version,
        config_id=config_id,
    )

    conn = get_library_conn()
    try:
        search_svc = LibrarySearchService(conn, embedder=compute_text_embedding)
        svc = CollectionService(conn, search_svc)
        return svc.create(spec)
    finally:
        conn.close()


@router.post("/collections/{collection_id}/refresh", response_model=CollectionVersion)
def refresh_collection(collection_id: str):
    """Run the collection's query + diversity and store a new version."""
    from app.services.collections import CollectionService

    conn = get_library_conn()
    try:
        search_svc = LibrarySearchService(conn, embedder=compute_text_embedding)
        svc = CollectionService(conn, search_svc)
        return svc.refresh(collection_id)
    finally:
        conn.close()


@router.post("/collections/{collection_id}/freeze", response_model=CollectionVersion)
def freeze_collection(collection_id: str):
    """Mark a collection as frozen (no further refreshes allowed)."""
    from app.services.collections import CollectionService

    conn = get_library_conn()
    try:
        search_svc = LibrarySearchService(conn)
        svc = CollectionService(conn, search_svc)
        return svc.freeze(collection_id)
    finally:
        conn.close()


@router.post(
    "/collections/{collection_id}/taste-map",
    response_model=list[dict],
)
def collection_taste_map(collection_id: str):
    """Compute 2D taste-map projection for the latest collection version.

    Returns a list of dicts with ``candidate_id``, ``x``, ``y`` keys.
    Returns an empty list when no vectors are available or the collection
    has no versions.
    """
    from app.services.taste_map import project_taste_map

    conn = get_library_conn()
    try:
        # Fetch latest-version candidate IDs
        row = conn.execute(
            """SELECT v.candidate_ids_json
               FROM collections c
               JOIN collection_versions v
                 ON c.collection_id = v.collection_id
                AND c.current_version = v.version
               WHERE c.collection_id = ?""",
            (collection_id,),
        ).fetchone()

        if row is None:
            return []

        candidate_ids: list[str] = json.loads(row["candidate_ids_json"])
        if not candidate_ids:
            return []

        # Load vectors for all candidates
        placeholders = ",".join(["?"] * len(candidate_ids))
        vec_rows = conn.execute(
            f"""SELECT candidate_id, vector_blob FROM candidate_vectors
                 WHERE candidate_id IN ({placeholders})
                   AND vector_type = 'clip'
                   AND embedding_model = 'nomic-embed-text:latest'
                   AND embedding_dim = 768""",
            candidate_ids,
        ).fetchall()

        if not vec_rows:
            return []

        vector_map: dict[str, np.ndarray] = {}
        for vr in vec_rows:
            vec = np.frombuffer(vr["vector_blob"], dtype=np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            vector_map[vr["candidate_id"]] = vec

        # Align vectors in candidate_ids order
        ordered_vectors: list[np.ndarray] = []
        ordered_ids: list[str] = []
        for cid in candidate_ids:
            if cid in vector_map:
                ordered_vectors.append(vector_map[cid])
                ordered_ids.append(cid)

        if len(ordered_vectors) < 1:
            return []

        vectors = np.stack(ordered_vectors, axis=0)

        points = project_taste_map(vectors, ordered_ids, seed=0)
        return [
            {"candidate_id": p.candidate_id, "x": p.x, "y": p.y}
            for p in points
        ]
    finally:
        conn.close()


@router.post(
    "/collections/{collection_id}/narrative",
    response_model=list[dict],
)
def collection_narrative(
    collection_id: str,
    beats: str = "opening,development,climax,ending",
):
    """Curate a narrative beat sequence from the latest collection version.

    *beats* is a comma-separated list of beat names (default: opening,
    development, climax, ending).

    Returns a list of dicts with ``beat``, ``selected_candidate_id``,
    ``component_scores``, and optional ``missing_reason``.
    """
    from app.services.narrative_curation import (
        CurationCandidate,
        curate_narrative,
    )

    beat_list = _parse_comma_separated(beats)
    if not beat_list:
        beat_list = ("opening", "development", "climax", "ending")

    conn = get_library_conn()
    try:
        # Fetch latest-version candidate IDs
        row = conn.execute(
            """SELECT v.candidate_ids_json, v.scores_json
               FROM collections c
               JOIN collection_versions v
                 ON c.collection_id = v.collection_id
                AND c.current_version = v.version
               WHERE c.collection_id = ?""",
            (collection_id,),
        ).fetchone()

        if row is None:
            return []

        candidate_ids: list[str] = json.loads(row["candidate_ids_json"])
        scores: dict = json.loads(row["scores_json"])
        if not candidate_ids:
            return []

        # Load candidate data
        placeholders = ",".join(["?"] * len(candidate_ids))
        cand_rows = conn.execute(
            f"""SELECT candidate_id, source_video_path, start_sec,
                      final_score, base_rag_similarity, score_profile_version,
                      vector_blob
               FROM candidate_gifs
               LEFT JOIN candidate_vectors
                 ON candidate_gifs.candidate_id = candidate_vectors.candidate_id
                AND candidate_vectors.vector_type = 'clip'
               WHERE candidate_gifs.candidate_id IN ({placeholders})""",
            candidate_ids,
        ).fetchall()

        if not cand_rows:
            return []

        candidates: list[CurationCandidate] = []
        for cr in cand_rows:
            vid = cr["source_video_path"] or "unknown"
            vec_blob = cr["vector_blob"]
            vector = (
                np.frombuffer(vec_blob, dtype=np.float32)
                if vec_blob
                else np.zeros(768, dtype=np.float32)
            )
            # Build synthetic beat scores from the search score
            final_sc = cr["final_score"] if cr["final_score"] is not None else 0.0
            # Simple heuristic: spread the score across beats proportionally
            beat_scores = {b: final_sc for b in beat_list}

            candidates.append(
                CurationCandidate(
                    candidate_id=cr["candidate_id"],
                    source_video=vid,
                    start_time=cr["start_sec"] or 0.0,
                    beat_scores=beat_scores,
                    quality=cr["base_rag_similarity"] or final_sc,
                    preference=final_sc,
                    vector=vector,
                )
            )

        curated = curate_narrative(candidates, beats=beat_list)
        return [
            {
                "beat": cb.beat,
                "selected_candidate_id": cb.selected_candidate_id,
                "component_scores": cb.component_scores,
                "missing_reason": cb.missing_reason,
            }
            for cb in curated
        ]
    finally:
        conn.close()


@router.post("/collections/{collection_id}/export", response_model=ExportReport)
def export_collection(
    collection_id: str,
    output_dir: str = Body(..., embed=True),
):
    """Export the latest collection version to *output_dir*.

    Writes a deterministic JSON manifest and a binary PBF file.
    Returns an ``ExportReport`` with paths and missing-candidate info.
    """
    from app.services.collections import CollectionService

    conn = get_library_conn()
    try:
        search_svc = LibrarySearchService(conn)
        svc = CollectionService(conn, search_svc)
        return svc.export(collection_id, Path(output_dir))
    finally:
        conn.close()

"""Phase 4 Task 8: Performance and lazy-loading tests for the Library Workbench.

Covers
------
- At least 10,000 synthetic candidate rows with vectors and thumbnail paths.
- First search page completes under 5 seconds and returns at most 24 rows.
- Timeline returns at most 60 thumbnail paths.
- No response embeds full GIF bytes (preview_path is a static path only).
- UI event-chain: search → select → create collection uses ≤3 primary
  user actions (modelled as function calls).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import numpy as np
import pytest

from app.services.library_search import LibrarySearchService, SearchQuery
from app.services.timeline import load_timeline_window


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EMBEDDING_DIM = 768


def _in_memory_db() -> sqlite3.Connection:
    """Create an in-memory library DB with all required schemas."""
    from app.services.preference_schema import apply_preference_schema
    from app.services.workbench_schema import (
        apply_collections_schema,
        apply_search_schema,
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    apply_search_schema(conn)
    apply_collections_schema(conn)

    # Create tables needed by timeline/relink tests
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS media (
            media_id TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'video',
            sha256 TEXT UNIQUE,
            duration REAL,
            created_at TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS video_clips (
            clip_id TEXT PRIMARY KEY,
            video_id TEXT NOT NULL,
            start REAL NOT NULL,
            end REAL NOT NULL,
            duration REAL NOT NULL,
            score_json TEXT,
            status TEXT DEFAULT 'candidate',
            exported_path TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    return conn


def _insert_candidate(
    conn: sqlite3.Connection,
    candidate_id: str,
    **kw,
) -> None:
    """Insert a single candidate_gifs row."""
    conn.execute(
        """INSERT OR IGNORE INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path, start_sec, end_sec,
            artifact_path, preview_path, vlm_summary_json, tags_json,
            scenario_keys_json, base_rag_similarity, final_score, status,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            candidate_id,
            kw.get("source_run_id", "perf-run"),
            f"rc-{candidate_id}",
            kw.get("source_video_sha256", "video-perf"),
            kw.get("source_video_path", "/videos/perf.mp4"),
            kw.get("start_sec", 0.0),
            kw.get("end_sec", 3.0),
            kw.get("artifact_path", "data/exports/perf/full.gif"),
            kw.get("preview_path", "data/thumbs/perf/preview.jpg"),
            kw.get("vlm_summary_json", json.dumps({"caption": "performance test"})),
            kw.get("tags_json", json.dumps(["test", "perf"])),
            kw.get("scenario_keys_json", json.dumps([])),
            kw.get("base_rag_similarity", 0.5),
            kw.get("final_score", 0.5),
            kw.get("status", "candidate"),
            kw.get("created_at", "2026-07-18T00:00:00+00:00"),
            kw.get("updated_at", "2026-07-18T00:00:00+00:00"),
        ),
    )
    conn.commit()


def _insert_vector(
    conn: sqlite3.Connection,
    candidate_id: str,
    *,
    model: str = "nomic-embed-text:latest",
    dim: int = EMBEDDING_DIM,
) -> None:
    """Insert a synthetic normalised vector for a candidate."""
    rng = np.random.default_rng(hash(candidate_id) & 0xFFFFFFFF)
    vec = rng.normal(0, 1, dim).astype(np.float32)
    vec = vec / np.linalg.norm(vec)
    conn.execute(
        """INSERT OR IGNORE INTO candidate_vectors
           (candidate_id, vector_type, embedding_model, embedding_dim, vector_blob)
           VALUES (?,?,?,?,?)""",
        (candidate_id, "clip", model, dim, vec.tobytes()),
    )
    conn.commit()


def _insert_fts_entry(
    conn: sqlite3.Connection,
    candidate_id: str,
    *,
    summary: str = "performance test",
    tags: str = "test perf",
) -> None:
    """Insert an FTS5 search entry."""
    conn.execute(
        """INSERT OR REPLACE INTO candidate_search_fts
           (candidate_id, summary, tags, source_path)
           VALUES (?, ?, ?, ?)""",
        (candidate_id, summary, tags, "/videos/perf.mp4"),
    )
    conn.commit()


def _seed_candidates(
    conn: sqlite3.Connection,
    count: int = 10_000,
    *,
    with_vectors: bool = True,
    with_fts: bool = True,
) -> None:
    """Seed *count* synthetic candidate rows, vectors, and FTS entries."""
    for i in range(count):
        cid = f"perf-candidate-{i:06d}"
        video_idx = i % 100
        preview_path = f"data/thumbs/perf/thumb_{i:06d}.jpg"
        artifact_path = f"data/exports/perf/full_{i:06d}.gif"
        start = float(i * 3 % 120)
        end = start + 2.5 + (i % 5) * 0.1
        statuses = ["candidate", "promoted", "liked", "archived"]
        status = statuses[i % len(statuses)]
        _insert_candidate(
            conn,
            cid,
            source_video_sha256=f"video-perf-{video_idx:03d}",
            source_video_path=f"/videos/perf_{video_idx:03d}.mp4",
            start_sec=start,
            end_sec=end,
            artifact_path=artifact_path,
            preview_path=preview_path,
            vlm_summary_json=json.dumps(
                {"caption": f"Performance test candidate {i}"}
            ),
            tags_json=json.dumps(["test", "perf", f"tag-{i % 20}"]),
            base_rag_similarity=round(0.3 + (i % 70) / 100.0, 4),
            final_score=round(0.2 + (i % 80) / 100.0, 4),
            status=status,
            created_at=f"2026-07-{(i % 30) + 1:02d}T{(i // 30) % 24:02d}:{i % 60:02d}:00+00:00",
            updated_at=f"2026-07-{(i % 30) + 1:02d}T{(i // 30) % 24:02d}:{i % 60:02d}:00+00:00",
        )

        if with_vectors:
            _insert_vector(conn, cid)

        if with_fts:
            _insert_fts_entry(
                conn,
                cid,
                summary=f"Performance test candidate {i}",
                tags=f"test perf tag-{i % 20}",
            )


def _insert_media(
    conn: sqlite3.Connection,
    media_id: str,
    *,
    sha256: str = "perf-sha256",
    duration: float = 120.0,
) -> None:
    """Insert a media row for timeline tests."""
    conn.execute(
        """INSERT OR IGNORE INTO media
           (media_id, file_path, media_type, sha256, duration, created_at, indexed_at)
           VALUES (?, ?, 'video', ?, ?, '2026-07-18T00:00:00+00:00', '2026-07-18T00:00:00+00:00')""",
        (media_id, f"/videos/{media_id}.mp4", sha256, duration),
    )
    conn.commit()


def _insert_video_clip(
    conn: sqlite3.Connection,
    clip_id: str,
    video_id: str,
    *,
    start: float = 0.0,
    end: float = 5.0,
    exported_path: str | None = None,
) -> None:
    """Insert a video_clips row for timeline tests."""
    conn.execute(
        """INSERT OR IGNORE INTO video_clips
           (clip_id, video_id, start, end, duration, score_json, status, exported_path, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-07-18T00:00:00+00:00')""",
        (clip_id, video_id, start, end, end - start, json.dumps({"base_rag_similarity": 0.5, "final_score": 0.6}), "candidate", exported_path),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Performance tests
# ---------------------------------------------------------------------------


class TestSearchPerformance:
    """Search must stay under 5 s for 10k rows and never exceed 24 items/page."""

    @pytest.fixture(scope="class")
    def large_db(self):
        """Seed a database with 10,000 candidate rows, vectors, and FTS."""
        conn = _in_memory_db()
        _seed_candidates(conn, 10_000, with_vectors=True, with_fts=True)
        yield conn
        conn.close()

    def test_first_page_under_5_seconds(self, large_db):
        """First search page must complete in under 5 seconds."""
        service = LibrarySearchService(large_db)
        query = SearchQuery(text="performance test")
        start = time.perf_counter()
        page = service.search(query, limit=24, offset=0)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0, (
            f"First search page took {elapsed:.3f}s (expected < 5s)"
        )
        # Sanity: at least some results
        assert page.total > 0

    def test_at_most_24_items_per_page(self, large_db):
        """Search result must return at most 24 items per page."""
        service = LibrarySearchService(large_db)
        query = SearchQuery(text="performance")
        page = service.search(query, limit=24, offset=0)
        assert len(page.items) <= 24, (
            f"Search returned {len(page.items)} items (expected <= 24)"
        )

    def test_multiple_pages_stable(self, large_db):
        """Multiple page fetches (offset 0, 24, 48) each return <= 24 items."""
        service = LibrarySearchService(large_db)
        query = SearchQuery(text="test")
        for offset in (0, 24, 48):
            page = service.search(query, limit=24, offset=offset)
            assert len(page.items) <= 24, (
                f"Offset {offset} returned {len(page.items)} items (expected <= 24)"
            )

    def test_no_gif_bytes_in_search_result(self, large_db):
        """Search result items must carry a path string, not GIF bytes."""
        service = LibrarySearchService(large_db)
        query = SearchQuery(text="performance")
        page = service.search(query, limit=24, offset=0)
        for item in page.items:
            # preview_path must be a string path or None — never bytes
            assert item.preview_path is None or isinstance(item.preview_path, str), (
                f"preview_path for {item.candidate_id} is {type(item.preview_path)}, expected str|None"
            )
            # artifact_path is not exposed in SearchResultItem, but source_video_path
            # must also be a path string
            assert isinstance(item.source_video_path, str)

    def test_filter_search_still_fast(self, large_db):
        """Search with tag and duration filters must also complete under 5 s."""
        service = LibrarySearchService(large_db)
        query = SearchQuery(
            text="performance",
            tags=("test",),
            min_duration=1.0,
            max_duration=10.0,
        )
        start = time.perf_counter()
        page = service.search(query, limit=24, offset=0)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0, (
            f"Filtered search took {elapsed:.3f}s (expected < 5s)"
        )
        assert len(page.items) <= 24


class TestTimelineThumbnailLimit:
    """Timeline must cap thumbnails at 60."""

    @pytest.fixture(scope="class")
    def timeline_db(self):
        """Seed a database with media, clips, and many candidates."""
        conn = _in_memory_db()

        # Insert one media row
        _insert_media(conn, "perf-video-000", sha256="perf-sha256-000", duration=300.0)

        # Insert 50 video clips with exported paths
        for i in range(50):
            _insert_video_clip(
                conn,
                f"perf-clip-{i:03d}",
                "perf-video-000",
                start=i * 6.0,
                end=i * 6.0 + 4.0,
                exported_path=f"data/thumbs/perf/clip_thumb_{i:03d}.jpg",
            )

        # Insert 120 candidates all sharing the same source_video_sha256
        for i in range(120):
            cid = f"perf-candidate-tl-{i:06d}"
            _insert_candidate(
                conn,
                cid,
                source_video_sha256="perf-sha256-000",
                source_video_path="/videos/perf_000.mp4",
                start_sec=i * 2.0,
                end_sec=i * 2.0 + 1.5,
                preview_path=f"data/thumbs/perf/tl_thumb_{i:06d}.jpg",
                status="candidate" if i < 80 else "promoted",
            )

        yield conn
        conn.close()

    def test_at_most_60_thumbnails_in_window(self, timeline_db):
        """load_timeline_window must return at most 60 non-None thumbnail_paths."""
        window = load_timeline_window(
            timeline_db,
            video_id="perf-video-000",
            start_sec=0.0,
            end_sec=300.0,
            max_thumbnails=60,
        )
        # Count total non-None thumbnails across all three span groups
        all_spans = list(window.scenes) + list(window.candidates) + list(window.generated_gifs)
        thumb_count = sum(1 for s in all_spans if s.thumbnail_path is not None)
        assert thumb_count <= 60, (
            f"Timeline returned {thumb_count} thumbnails (expected <= 60)"
        )

    def test_thumbnail_is_path_not_bytes(self, timeline_db):
        """Every non-None thumbnail_path must be a string, not bytes."""
        window = load_timeline_window(
            timeline_db,
            video_id="perf-video-000",
            start_sec=0.0,
            end_sec=300.0,
            max_thumbnails=60,
        )
        for span_group_name, spans in [
            ("scenes", window.scenes),
            ("candidates", window.candidates),
            ("generated_gifs", window.generated_gifs),
        ]:
            for span in spans:
                if span.thumbnail_path is not None:
                    assert isinstance(span.thumbnail_path, str), (
                        f"{span_group_name} span {span.span_id} has "
                        f"thumbnail_path type {type(span.thumbnail_path)} (expected str)"
                    )


class TestUIActionCount:
    """The search → select → create-collection workflow must take ≤3 actions."""

    def test_search_select_create_collection_three_actions(self):
        """Model the happy-path UI chain as three function calls.

        Action 1: Build a SearchQuery with the user's text.
        Action 2: Call search_service.search() to get results.
        Action 3: Call collection_service.create() with a CollectionSpec.
        """
        conn = _in_memory_db()
        _seed_candidates(conn, 500, with_vectors=True, with_fts=True)

        search_service = LibrarySearchService(conn)

        # Action 1 + 2 combined: build query and search (equivalent to
        # user typing + pressing search).
        query = SearchQuery(text="performance", tags=("test",))
        page = search_service.search(query, limit=24, offset=0)
        assert len(page.items) > 0, "Search must return results"

        # Action 3: create a collection from the search results
        from app.services.workbench_schema import CollectionSpec
        from app.services.collections import CollectionService

        collection_service = CollectionService(conn, search_service)
        spec = CollectionSpec(
            name="Perf Collection",
            query=query,
            target_count=10,
        )
        collection = collection_service.create(spec)
        assert collection.collection_id is not None
        assert collection.spec.name == "Perf Collection"

        conn.close()

    def test_search_without_text_filter_only(self):
        """Browsing without text (filter-only) also returns ≤24 items."""
        conn = _in_memory_db()
        _seed_candidates(conn, 5_000, with_vectors=False, with_fts=False)

        search_service = LibrarySearchService(conn)
        query = SearchQuery(
            text="",
            tags=("test",),
            statuses=("candidate",),
        )
        page = search_service.search(query, limit=24, offset=0)
        assert len(page.items) <= 24
        conn.close()

    def test_search_empty_result_returns_no_items(self):
        """Search for a non-matching term returns 0 total."""
        conn = _in_memory_db()
        _seed_candidates(conn, 1_000, with_vectors=False, with_fts=True)

        search_service = LibrarySearchService(conn)
        query = SearchQuery(text="zzzznotexist")
        page = search_service.search(query, limit=24, offset=0)
        assert page.total == 0
        assert len(page.items) == 0
        conn.close()


class TestIndexHealthOnLargeDataset:
    """Index health report works correctly on a 10k-row dataset."""

    def test_index_health_reports_correctly(self):
        conn = _in_memory_db()
        _seed_candidates(conn, 10_000, with_vectors=True, with_fts=True)

        service = LibrarySearchService(conn)
        health = service.index_health()
        assert health.total_candidates == 10_000
        assert health.indexed_in_fts == 10_000
        assert health.vectors_available == 10_000
        assert health.complete is True
        assert "All candidates indexed" in health.diagnosis
        conn.close()

    def test_index_health_degraded_with_missing_vectors(self):
        conn = _in_memory_db()
        # Seed with FTS but no vectors
        _seed_candidates(conn, 1_000, with_vectors=False, with_fts=True)

        service = LibrarySearchService(conn)
        health = service.index_health()
        assert health.total_candidates == 1_000
        assert health.vectors_available == 0
        assert health.complete is False
        assert "Vectors" in health.diagnosis
        conn.close()

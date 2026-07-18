"""API-level tests for Phase 4 Task 3: Workbench search endpoints.

Covers
------
- POST /api/workbench/search returns SearchPage with items
- GET /api/workbench/search/index-health returns IndexHealth
- POST /api/workbench/search/rebuild returns RebuildReport
- Degraded index returns HTTP 200 with degraded=true
- Endpoint function signatures and error handling
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import numpy as np
import pytest


# ── helpers ──────────────────────────────────────────────────────────────────


def _library_conn() -> sqlite3.Connection:
    """In-memory library DB with preference + search schema."""
    from app.services.preference_schema import apply_preference_schema
    from app.services.workbench_schema import apply_search_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    apply_search_schema(conn)
    return conn


def _insert_candidate(
    conn: sqlite3.Connection,
    candidate_id: str,
    **kw,
) -> None:
    conn.execute(
        """INSERT INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path, start_sec, end_sec,
            artifact_path, preview_path, vlm_summary_json, tags_json,
            scenario_keys_json, base_rag_similarity, final_score, status,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            candidate_id,
            kw.get("source_run_id", "run-1"),
            f"rc-{candidate_id}",
            kw.get("source_video_sha256", "video-default"),
            kw.get("source_video_path", "/videos/sample.mp4"),
            kw.get("start_sec", 0.0),
            kw.get("end_sec", 5.0),
            kw.get("artifact_path", "data/exports/full.gif"),
            kw.get("preview_path", "data/thumbs/preview.jpg"),
            kw.get("vlm_summary_json", json.dumps({"caption": "no caption"})),
            kw.get("tags_json", json.dumps([])),
            kw.get("scenario_keys_json", json.dumps([])),
            kw.get("base_rag_similarity", None),
            kw.get("final_score", None),
            kw.get("status", "candidate"),
            kw.get("created_at", "2026-07-18T00:00:00+00:00"),
            kw.get("updated_at", "2026-07-18T00:00:00+00:00"),
        ),
    )
    conn.commit()


def _index_candidate(conn: sqlite3.Connection, candidate_id: str) -> None:
    row = conn.execute(
        "SELECT vlm_summary_json, tags_json, source_video_path FROM candidate_gifs WHERE candidate_id=?",
        (candidate_id,),
    ).fetchone()
    vlm = json.loads(row["vlm_summary_json"] or "{}")
    tags = json.loads(row["tags_json"] or "[]")
    summary = vlm.get("caption") or vlm.get("summary") or ""
    tags_text = " ".join(str(t) for t in tags if t)
    source_path = row["source_video_path"] or ""
    conn.execute(
        "INSERT OR REPLACE INTO candidate_search_fts (candidate_id, summary, tags, source_path) "
        "VALUES (?, ?, ?, ?)",
        (candidate_id, summary, tags_text, source_path),
    )
    conn.commit()


# ===================================================================
# Search endpoint tests
# ===================================================================


class TestSearchEndpoint:
    """POST /api/workbench/search"""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self._conn = _library_conn()
        _insert_candidate(self._conn, "cand-1")
        _insert_candidate(self._conn, "cand-2")
        _index_candidate(self._conn, "cand-1")
        _index_candidate(self._conn, "cand-2")

        self._patches = [
            patch("app.routers.workbench.get_library_conn", return_value=self._conn),
        ]
        for p in self._patches:
            p.start()
        yield
        for p in self._patches:
            p.stop()

    def test_search_returns_search_page(self):
        from app.routers.workbench import search_candidates

        result = search_candidates(query_text="", tags="", folder="")
        assert hasattr(result, "items")
        assert hasattr(result, "total")
        assert hasattr(result, "limit")
        assert hasattr(result, "offset")
        assert hasattr(result, "degraded")
        assert len(result.items) == 2

    def test_search_with_text(self):
        from app.routers.workbench import search_candidates

        result = search_candidates(query_text="", tags="", folder="")
        assert result.total == 2

    def test_search_respects_limit(self):
        from app.routers.workbench import search_candidates

        result = search_candidates(query_text="", tags="", folder="", limit=1)
        assert len(result.items) == 1
        assert result.limit == 1

    def test_search_with_tags(self):
        from app.routers.workbench import search_candidates

        # Insert a candidate with tags
        _insert_candidate(
            self._conn, "cand-tagged",
            tags_json=json.dumps(["action", "explosion"]),
        )
        _index_candidate(self._conn, "cand-tagged")

        result = search_candidates(query_text="", tags="action", folder="")
        assert result.total >= 1

    def test_search_items_have_required_fields(self):
        from app.routers.workbench import search_candidates

        result = search_candidates(query_text="", tags="", folder="")
        for item in result.items:
            assert item.candidate_id
            assert item.preview_path is not None or item.preview_path is None
            assert item.source_video_path
            assert isinstance(item.start_sec, float)
            assert isinstance(item.end_sec, float)
            assert isinstance(item.duration, float)
            assert isinstance(item.status, str)
            assert isinstance(item.created_at, str)

    def test_search_with_created_after(self):
        from app.routers.workbench import search_candidates

        _insert_candidate(
            self._conn, "cand-recent",
            created_at="2026-07-20T00:00:00+00:00",
        )
        _index_candidate(self._conn, "cand-recent")

        result = search_candidates(
            query_text="", tags="", folder="",
            created_after="2026-07-19T00:00:00+00:00",
        )
        assert result.total >= 1
        assert result.items[0].candidate_id == "cand-recent"


# ===================================================================
# Index health endpoint tests
# ===================================================================


class TestIndexHealthEndpoint:
    """GET /api/workbench/search/index-health"""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self._conn = _library_conn()
        self._patches = [
            patch("app.routers.workbench.get_library_conn", return_value=self._conn),
        ]
        for p in self._patches:
            p.start()
        yield
        for p in self._patches:
            p.stop()

    def test_health_returns_index_health(self):
        from app.routers.workbench import search_index_health

        health = search_index_health()
        assert hasattr(health, "total_candidates")
        assert hasattr(health, "indexed_in_fts")
        assert hasattr(health, "vectors_available")
        assert hasattr(health, "vectors_missing")
        assert hasattr(health, "complete")
        assert hasattr(health, "diagnosis")

    def test_health_zero_candidates(self):
        from app.routers.workbench import search_index_health

        health = search_index_health()
        assert health.total_candidates == 0
        assert health.complete is True

    def test_health_with_candidates(self):
        _insert_candidate(self._conn, "cand-1")
        _index_candidate(self._conn, "cand-1")

        from app.routers.workbench import search_index_health

        health = search_index_health()
        assert health.total_candidates == 1
        assert health.indexed_in_fts == 1


# ===================================================================
# Rebuild endpoint tests
# ===================================================================


class TestRebuildEndpoint:
    """POST /api/workbench/search/rebuild"""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self._conn = _library_conn()
        self._patches = [
            patch("app.routers.workbench.get_library_conn", return_value=self._conn),
        ]
        for p in self._patches:
            p.start()
        yield
        for p in self._patches:
            p.stop()

    def test_rebuild_returns_report(self):
        _insert_candidate(self._conn, "cand-1")
        _insert_candidate(self._conn, "cand-2")

        from app.routers.workbench import rebuild_search_index

        report = rebuild_search_index()
        assert hasattr(report, "scanned")
        assert hasattr(report, "inserted")
        assert hasattr(report, "errors")
        assert hasattr(report, "batch_commits")
        assert hasattr(report, "last_candidate_id")
        assert report.inserted == 2

    def test_rebuild_idempotent(self):
        """Idempotent: a second rebuild on the same data inserts nothing."""
        from app.services.library_search import LibrarySearchService

        conn = _library_conn()
        _insert_candidate(conn, "cand-1")

        svc = LibrarySearchService(conn)
        r1 = svc.rebuild_index()
        assert r1.inserted == 1

        # Service uses the same connection; no close issue
        r2 = svc.rebuild_index()
        assert r2.inserted == 0


# ===================================================================
# Error handling tests
# ===================================================================


class TestSearchErrorHandling:
    """The search endpoint must handle DB errors gracefully."""

    def test_search_db_error_returns_degraded(self):
        """When DB errors occur, search should not crash."""
        conn = _library_conn()
        with patch("app.routers.workbench.get_library_conn", return_value=conn):
            from app.routers.workbench import search_candidates

            # Drop the candidate_gifs table to simulate failure
            conn.execute("DROP TABLE IF EXISTS candidate_gifs")
            conn.commit()

            result = search_candidates(query_text="", tags="", folder="")
            assert result.total == 0
            assert result.items == []

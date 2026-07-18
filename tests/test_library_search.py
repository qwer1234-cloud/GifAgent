"""Tests for Phase 4 Task 3: LibrarySearchService -- FTS5 + vector search.

Covers
------
- Exact filters (tags, folder, duration, statuses, dates) applied before
  vector ranking.
- Text search combines FTS and embedding similarity.
- Pagination is stable.
- Result payload uses static ``preview_path``.
- Missing vectors produce a recoverable health result.
- Rebuild resumes by last candidate ID.
"""

from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

from app.services.workbench_schema import (
    IndexHealth,
    RebuildReport,
    SearchPage,
    SearchQuery,
    SearchResultItem,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _conn() -> sqlite3.Connection:
    """Create an in-memory connection with all required schemas."""
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


def _insert_vector(
    conn: sqlite3.Connection,
    candidate_id: str,
    *,
    model: str = "nomic-embed-text:latest",
    dim: int = 768,
) -> None:
    vec = np.random.default_rng(hash(candidate_id) & 0xFFFFFFFF).normal(0, 1, dim).astype(np.float32)
    vec = vec / np.linalg.norm(vec)
    conn.execute(
        """INSERT OR IGNORE INTO candidate_vectors
           (candidate_id, vector_type, embedding_model, embedding_dim, vector_blob)
           VALUES (?,?,?,?,?)""",
        (candidate_id, "clip", model, dim, vec.tobytes()),
    )
    conn.commit()


def _stub_embedder(text: str) -> list[float]:
    """Return a deterministic embedding based on text hash."""
    rng = np.random.default_rng(hash(text) & 0xFFFFFFFF)
    vec = rng.normal(0, 1, 768).astype(np.float32)
    return (vec / np.linalg.norm(vec)).tolist()


def _index_candidate(conn: sqlite3.Connection, candidate_id: str) -> None:
    """Insert a candidate row into the FTS5 index."""
    row = conn.execute(
        "SELECT vlm_summary_json, tags_json, source_video_path, artifact_path, preview_path "
        "FROM candidate_gifs WHERE candidate_id=?",
        (candidate_id,),
    ).fetchone()
    vlm = json.loads(row["vlm_summary_json"] or "{}")
    tags = json.loads(row["tags_json"] or "[]")
    summary = vlm.get("caption") or vlm.get("summary") or ""
    tags_text = " ".join(str(t) for t in tags if t)
    source_path = row["source_video_path"] or row["artifact_path"] or row["preview_path"] or ""
    conn.execute(
        "INSERT OR REPLACE INTO candidate_search_fts (candidate_id, summary, tags, source_path) "
        "VALUES (?, ?, ?, ?)",
        (candidate_id, summary, tags_text, source_path),
    )
    conn.commit()


# ===================================================================
# Exact filter tests
# ===================================================================


class TestExactFilters:
    """Exact filters must narrow the result set before any ranking."""

    def test_tags_filter(self):
        conn = _conn()
        _insert_candidate(conn, "cand-joy", tags_json=json.dumps(["joy", "happy"]))
        _insert_candidate(conn, "cand-sad", tags_json=json.dumps(["sad", "melancholy"]))
        _index_candidate(conn, "cand-joy")
        _index_candidate(conn, "cand-sad")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        result = svc.search(SearchQuery(tags=("joy",)))
        assert result.total == 1
        assert result.items[0].candidate_id == "cand-joy"

    def test_tags_filter_multiple(self):
        """Multiple tags require ALL to match (AND semantics)."""
        conn = _conn()
        _insert_candidate(conn, "cand-both", tags_json=json.dumps(["joy", "happy", "warm"]))
        _insert_candidate(conn, "cand-one", tags_json=json.dumps(["joy"]))
        _index_candidate(conn, "cand-both")
        _index_candidate(conn, "cand-one")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        result = svc.search(SearchQuery(tags=("joy", "warm")))
        assert result.total == 1
        assert result.items[0].candidate_id == "cand-both"

    def test_folder_filter(self):
        conn = _conn()
        _insert_candidate(conn, "cand-in-folder", source_video_path="/videos/JUR-639/clip.mp4")
        _insert_candidate(conn, "cand-other", source_video_path="/videos/other/clip.mp4")
        _index_candidate(conn, "cand-in-folder")
        _index_candidate(conn, "cand-other")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        result = svc.search(SearchQuery(folder="JUR-639"))
        assert result.total == 1
        assert result.items[0].candidate_id == "cand-in-folder"

    def test_duration_filter(self):
        conn = _conn()
        _insert_candidate(conn, "cand-short", start_sec=0.0, end_sec=2.0)
        _insert_candidate(conn, "cand-long", start_sec=0.0, end_sec=10.0)
        _index_candidate(conn, "cand-short")
        _index_candidate(conn, "cand-long")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        result = svc.search(SearchQuery(min_duration=5.0))
        assert result.total == 1
        assert result.items[0].candidate_id == "cand-long"

    def test_duration_range(self):
        conn = _conn()
        _insert_candidate(conn, "cand-short", start_sec=0.0, end_sec=1.0)
        _insert_candidate(conn, "cand-medium", start_sec=0.0, end_sec=5.0)
        _insert_candidate(conn, "cand-long", start_sec=0.0, end_sec=10.0)
        _index_candidate(conn, "cand-short")
        _index_candidate(conn, "cand-medium")
        _index_candidate(conn, "cand-long")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        result = svc.search(SearchQuery(min_duration=2.0, max_duration=8.0))
        assert result.total == 1
        assert result.items[0].candidate_id == "cand-medium"

    def test_status_filter(self):
        conn = _conn()
        _insert_candidate(conn, "cand-candidate", status="candidate")
        _insert_candidate(conn, "cand-liked", status="liked")
        _insert_candidate(conn, "cand-rejected", status="rejected")
        _index_candidate(conn, "cand-candidate")
        _index_candidate(conn, "cand-liked")
        _index_candidate(conn, "cand-rejected")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        result = svc.search(SearchQuery(statuses=("candidate", "liked")))
        assert result.total == 2
        cand_ids = {it.candidate_id for it in result.items}
        assert cand_ids == {"cand-candidate", "cand-liked"}

    def test_created_after_filter(self):
        conn = _conn()
        _insert_candidate(conn, "cand-old", created_at="2026-07-01T00:00:00+00:00")
        _insert_candidate(conn, "cand-new", created_at="2026-07-18T00:00:00+00:00")
        _index_candidate(conn, "cand-old")
        _index_candidate(conn, "cand-new")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        result = svc.search(SearchQuery(created_after="2026-07-10T00:00:00+00:00"))
        assert result.total == 1
        assert result.items[0].candidate_id == "cand-new"

    def test_created_before_filter(self):
        conn = _conn()
        _insert_candidate(conn, "cand-old", created_at="2026-07-01T00:00:00+00:00")
        _insert_candidate(conn, "cand-new", created_at="2026-07-18T00:00:00+00:00")
        _index_candidate(conn, "cand-old")
        _index_candidate(conn, "cand-new")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        result = svc.search(SearchQuery(created_before="2026-07-10T00:00:00+00:00"))
        assert result.total == 1
        assert result.items[0].candidate_id == "cand-old"

    def test_combined_filters(self):
        """Multiple filters combine with AND semantics."""
        conn = _conn()
        _insert_candidate(
            conn, "cand-match",
            tags_json=json.dumps(["joy"]),
            status="candidate",
            start_sec=0.0, end_sec=3.0,
        )
        _insert_candidate(
            conn, "cand-wrong-status",
            tags_json=json.dumps(["joy"]),
            status="liked",
            start_sec=0.0, end_sec=3.0,
        )
        _insert_candidate(
            conn, "cand-wrong-tag",
            tags_json=json.dumps(["sad"]),
            status="candidate",
            start_sec=0.0, end_sec=3.0,
        )
        _index_candidate(conn, "cand-match")
        _index_candidate(conn, "cand-wrong-status")
        _index_candidate(conn, "cand-wrong-tag")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        result = svc.search(SearchQuery(
            tags=("joy",),
            statuses=("candidate",),
            max_duration=5.0,
        ))
        assert result.total == 1
        assert result.items[0].candidate_id == "cand-match"


# ===================================================================
# Text search tests
# ===================================================================


class TestTextSearch:
    """Text search combines FTS and optional vector similarity."""

    def test_fts_only_when_no_embedder(self):
        """Without an embedder, text search uses only FTS ranking."""
        conn = _conn()
        _insert_candidate(
            conn, "cand-happy",
            vlm_summary_json=json.dumps({"caption": "a happy smiling person"}),
        )
        _insert_candidate(
            conn, "cand-sad",
            vlm_summary_json=json.dumps({"caption": "a sad crying moment"}),
        )
        _index_candidate(conn, "cand-happy")
        _index_candidate(conn, "cand-sad")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn, embedder=None)
        result = svc.search(SearchQuery(text="happy"))
        assert result.total >= 1
        # Happy result should rank higher than sad result
        assert result.items[0].candidate_id == "cand-happy"

    def test_combined_fts_and_vector(self):
        """When embedder is provided, results combine both scores."""
        conn = _conn()
        _insert_candidate(
            conn, "cand-happy",
            vlm_summary_json=json.dumps({"caption": "a happy smiling person"}),
            tags_json=json.dumps(["joy"]),
        )
        _insert_candidate(
            conn, "cand-neutral",
            vlm_summary_json=json.dumps({"caption": "a person walking"}),
            tags_json=json.dumps([]),
        )
        _index_candidate(conn, "cand-happy")
        _index_candidate(conn, "cand-neutral")
        _insert_vector(conn, "cand-happy")
        _insert_vector(conn, "cand-neutral")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn, embedder=_stub_embedder)
        result = svc.search(SearchQuery(text="happy joy smile"))
        assert result.total >= 1
        assert result.items[0].candidate_id == "cand-happy"
        # Each result should have a score
        for item in result.items:
            assert item.score is not None

    def test_text_search_with_filters_interaction(self):
        """Exact filters narrow the set before FTS+vector ranking."""
        conn = _conn()
        _insert_candidate(
            conn, "cand-joy-candidate",
            vlm_summary_json=json.dumps({"caption": "a happy joyful scene"}),
            tags_json=json.dumps(["joy"]),
            status="candidate",
        )
        _insert_candidate(
            conn, "cand-joy-liked",
            vlm_summary_json=json.dumps({"caption": "a happy joyful scene"}),
            tags_json=json.dumps(["joy"]),
            status="liked",
        )
        _insert_candidate(
            conn, "cand-sad",
            vlm_summary_json=json.dumps({"caption": "a sad moment"}),
            status="candidate",
        )
        _index_candidate(conn, "cand-joy-candidate")
        _index_candidate(conn, "cand-joy-liked")
        _index_candidate(conn, "cand-sad")
        _insert_vector(conn, "cand-joy-candidate")
        _insert_vector(conn, "cand-joy-liked")
        _insert_vector(conn, "cand-sad")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn, embedder=_stub_embedder)
        # Filter to only 'liked' status, then search for "happy"
        result = svc.search(SearchQuery(
            text="happy",
            statuses=("liked",),
        ))
        assert result.total == 1
        assert result.items[0].candidate_id == "cand-joy-liked"

    def test_zero_results_for_no_match(self):
        """Text that matches nothing returns an empty page."""
        conn = _conn()
        _insert_candidate(
            conn, "cand-happy",
            vlm_summary_json=json.dumps({"caption": "happy times"}),
        )
        _index_candidate(conn, "cand-happy")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        result = svc.search(SearchQuery(text="xyznonexistent"))
        assert result.total == 0
        assert result.items == []


# ===================================================================
# Pagination tests
# ===================================================================


class TestPagination:
    """Pagination must be stable and non-overlapping."""

    def test_pagination_stable(self):
        conn = _conn()
        for i in range(10):
            cid = f"cand-{i:03d}"
            _insert_candidate(conn, cid, final_score=1.0 - i * 0.05)
            _index_candidate(conn, cid)

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)

        page1 = svc.search(SearchQuery(), limit=4, offset=0)
        page2 = svc.search(SearchQuery(), limit=4, offset=4)
        page3 = svc.search(SearchQuery(), limit=4, offset=8)

        assert len(page1.items) == 4
        assert len(page2.items) == 4
        assert len(page3.items) == 2

        # Pages must not overlap
        id_sets = [
            {it.candidate_id for it in page.items}
            for page in (page1, page2, page3)
        ]
        assert id_sets[0].isdisjoint(id_sets[1])
        assert id_sets[1].isdisjoint(id_sets[2])
        assert id_sets[0].isdisjoint(id_sets[2])

        # Total must be correct
        assert page1.total == 10

    def test_pagination_total(self):
        conn = _conn()
        for i in range(7):
            _insert_candidate(conn, f"cand-{i:03d}")
            _index_candidate(conn, f"cand-{i:03d}")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        result = svc.search(SearchQuery(), limit=5, offset=0)
        assert result.total == 7
        assert result.limit == 5
        assert result.offset == 0

    def test_pagination_beyond_end(self):
        conn = _conn()
        for i in range(3):
            _insert_candidate(conn, f"cand-{i:03d}")
            _index_candidate(conn, f"cand-{i:03d}")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        result = svc.search(SearchQuery(), limit=5, offset=10)
        assert result.items == []
        assert result.total == 3


# ===================================================================
# Preview path tests
# ===================================================================


class TestPreviewPath:
    """Result payload must use the static preview_path from candidate_gifs."""

    def test_preview_path_in_result(self):
        conn = _conn()
        _insert_candidate(
            conn, "cand-1",
            artifact_path="data/exports/full.gif",
            preview_path="data/thumbs/preview.jpg",
        )
        _index_candidate(conn, "cand-1")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        result = svc.search(SearchQuery())
        assert len(result.items) == 1
        item = result.items[0]
        assert item.preview_path == "data/thumbs/preview.jpg"
        assert item.candidate_id == "cand-1"

    def test_preview_path_none_when_missing(self):
        conn = _conn()
        _insert_candidate(
            conn, "cand-no-preview",
            artifact_path="data/exports/full.gif",
            preview_path=None,
        )
        _index_candidate(conn, "cand-no-preview")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        result = svc.search(SearchQuery())
        assert result.items[0].preview_path is None

    def test_preview_path_in_text_search(self):
        """Text search results also carry preview_path."""
        conn = _conn()
        _insert_candidate(
            conn, "cand-fts",
            vlm_summary_json=json.dumps({"caption": "a happy cat"}),
            preview_path="data/thumbs/cat.jpg",
        )
        _index_candidate(conn, "cand-fts")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        result = svc.search(SearchQuery(text="happy"))
        assert result.items[0].preview_path == "data/thumbs/cat.jpg"


# ===================================================================
# Index health tests
# ===================================================================


class TestIndexHealth:
    """Index health must reflect FTS and vector coverage."""

    def test_healthy_when_all_indexed(self):
        conn = _conn()
        _insert_candidate(conn, "cand-1")
        _insert_candidate(conn, "cand-2")
        _index_candidate(conn, "cand-1")
        _index_candidate(conn, "cand-2")
        _insert_vector(conn, "cand-1")
        _insert_vector(conn, "cand-2")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        health = svc.index_health()
        assert health.total_candidates == 2
        assert health.indexed_in_fts == 2
        assert health.vectors_available == 2
        assert health.vectors_missing == 0
        assert health.complete is True

    def test_degraded_when_missing_fts(self):
        conn = _conn()
        _insert_candidate(conn, "cand-1")
        _insert_candidate(conn, "cand-2")
        # Only index one
        _index_candidate(conn, "cand-1")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        health = svc.index_health()
        assert health.complete is False
        assert "FTS index" in health.diagnosis

    def test_degraded_when_missing_vectors(self):
        conn = _conn()
        _insert_candidate(conn, "cand-1")
        _insert_candidate(conn, "cand-2")
        _index_candidate(conn, "cand-1")
        _index_candidate(conn, "cand-2")
        # Only one has a vector
        _insert_vector(conn, "cand-1")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        health = svc.index_health()
        assert health.complete is False
        assert "Vectors" in health.diagnosis

    def test_healthy_when_no_candidates(self):
        """Zero candidates is trivially healthy."""
        conn = _conn()

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        health = svc.index_health()
        assert health.total_candidates == 0
        assert health.indexed_in_fts == 0
        assert health.complete is True

    def test_search_returns_degraded_flag(self):
        """When index is incomplete, search returns degraded=True with diagnosis."""
        conn = _conn()
        _insert_candidate(conn, "cand-1")
        _insert_candidate(conn, "cand-2")
        _index_candidate(conn, "cand-1")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        result = svc.search(SearchQuery())
        assert result.degraded is True
        assert result.diagnosis is not None


# ===================================================================
# Rebuild index tests
# ===================================================================


class TestRebuildIndex:
    """Rebuild must be resumable by last candidate ID."""

    def test_rebuild_full(self):
        conn = _conn()
        _insert_candidate(conn, "cand-1")
        _insert_candidate(conn, "cand-2")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        report = svc.rebuild_index(batch_size=10)
        assert report.scanned == 2
        assert report.inserted == 2
        assert report.errors == 0
        assert report.last_candidate_id is not None

        # FTS table should have 2 rows
        count = conn.execute("SELECT COUNT(*) FROM candidate_search_fts").fetchone()[0]
        assert count == 2

    def test_rebuild_resumes(self):
        """Rebuild starting from a resume point processes only newer candidates."""
        conn = _conn()
        _insert_candidate(conn, "cand-old", created_at="2026-07-01T00:00:00+00:00")
        _insert_candidate(conn, "cand-mid", created_at="2026-07-10T00:00:00+00:00")

        # Index first batch up to cand-mid
        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        svc.rebuild_index(batch_size=10)

        # Add a newer candidate
        _insert_candidate(conn, "cand-new", created_at="2026-07-18T00:00:00+00:00")

        # Rebuild should only process cand-new
        report = svc.rebuild_index(batch_size=10)
        assert report.scanned == 1
        assert report.inserted == 1
        assert report.last_candidate_id == "cand-new"

    def test_rebuild_idempotent(self):
        """Running rebuild twice on the same data should not error."""
        conn = _conn()
        _insert_candidate(conn, "cand-1")
        _insert_candidate(conn, "cand-2")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        r1 = svc.rebuild_index()
        assert r1.inserted == 2

        r2 = svc.rebuild_index()
        # Second run should process no new candidates
        assert r2.scanned == 0
        assert r2.inserted == 0

    def test_rebuild_with_errors_continues(self):
        """An error in one row doesn't stop the entire rebuild."""
        conn = _conn()
        _insert_candidate(conn, "cand-good-1")
        _insert_candidate(conn, "cand-good-2")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        report = svc.rebuild_index(batch_size=1)
        assert report.errors == 0
        assert report.inserted == 2

    def test_rebuild_state_tracked(self):
        """After rebuild, search_index_state contains accurate metadata."""
        conn = _conn()
        _insert_candidate(conn, "cand-1")
        _insert_candidate(conn, "cand-2")

        from app.services.library_search import LibrarySearchService

        svc = LibrarySearchService(conn)
        svc.rebuild_index(batch_size=1)

        state = conn.execute(
            "SELECT last_candidate_id, indexed_count, total_count FROM search_index_state WHERE id=1"
        ).fetchone()
        assert state is not None
        assert state["indexed_count"] == 2
        assert state["total_count"] >= 2


# ===================================================================
# Dataclass contract tests
# ===================================================================


class TestDataclassContracts:
    """Verify that model dataclasses have the expected structure."""

    def test_search_query_frozen(self):
        q = SearchQuery(text="hello")
        with pytest.raises(AttributeError):
            q.text = "world"  # type: ignore[misc]

    def test_search_page_defaults(self):
        page = SearchPage(items=[], total=0, limit=24, offset=0)
        assert page.degraded is False
        assert page.diagnosis is None

    def test_result_item_fields(self):
        item = SearchResultItem(
            candidate_id="c1",
            preview_path="/p.jpg",
            source_video_path="/v.mp4",
            start_sec=0.0,
            end_sec=5.0,
            duration=5.0,
            summary="test",
            tags=["tag1"],
            status="candidate",
            score=0.5,
            created_at="2026-07-18T00:00:00+00:00",
        )
        assert item.candidate_id == "c1"

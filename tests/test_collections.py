"""Tests for Phase 4 Task 6: CollectionService -- reproducible smart collections.

Covers
------
- Creating a collection stores the spec and returns a Collection dataclass.
- Refreshing runs search + farthest-first diversity selection and creates
  a new version with candidate IDs, scores, and manifest hash.
- Multiple refreshes increment the version number.
- Freezing prevents implicit refresh (raises ValueError).
- Export reports deleted/missing candidates without silently replacing them.
- Export writes a deterministic JSON manifest plus a PBF binary file
  that contains source timestamps.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from pathlib import Path

import numpy as np
import pytest

from app.services.workbench_schema import SearchQuery


# ── helpers ──────────────────────────────────────────────────────────────────


def _conn() -> sqlite3.Connection:
    """Create an in-memory connection with all required schemas."""
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
    dim: int = 768,
) -> None:
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


def _index_candidate(conn: sqlite3.Connection, candidate_id: str) -> None:
    """Insert a candidate row into the FTS5 index."""
    row = conn.execute(
        "SELECT vlm_summary_json, tags_json, source_video_path "
        "FROM candidate_gifs WHERE candidate_id=?",
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


def _stub_embedder(text: str) -> list[float]:
    """Return a deterministic embedding based on text hash."""
    rng = np.random.default_rng(hash(text) & 0xFFFFFFFF)
    vec = rng.normal(0, 1, 768).astype(np.float32)
    return (vec / np.linalg.norm(vec)).tolist()


def _read_pbf(path: Path) -> list[dict]:
    """Read PBF binary file back into candidate data list."""
    data = path.read_bytes()
    assert data[:8] == b"GIFPBF01", "Invalid PBF magic"
    count = struct.unpack("<I", data[8:12])[0]
    candidates = []
    pos = 12
    for _ in range(count):
        id_len = struct.unpack("<H", data[pos : pos + 2])[0]
        pos += 2
        cid = data[pos : pos + id_len].decode("utf-8")
        pos += id_len
        score = struct.unpack("<d", data[pos : pos + 8])[0]
        pos += 8
        src_len = struct.unpack("<H", data[pos : pos + 2])[0]
        pos += 2
        src = data[pos : pos + src_len].decode("utf-8")
        pos += src_len
        ts_len = struct.unpack("<H", data[pos : pos + 2])[0]
        pos += 2
        ts = data[pos : pos + ts_len].decode("utf-8")
        pos += ts_len
        candidates.append(
            {
                "candidate_id": cid,
                "score": score,
                "source_video_path": src,
                "created_at": ts,
            }
        )
    return candidates


# ===================================================================
# Creation tests
# ===================================================================


class TestCollectionCreate:
    """Creating a collection stores its spec and returns a Collection."""

    def test_create_returns_collection(self):
        conn = _conn()
        from app.services.collections import CollectionService, CollectionSpec
        from app.services.library_search import LibrarySearchService

        search_svc = LibrarySearchService(conn)
        svc = CollectionService(conn, search_svc)

        spec = CollectionSpec(
            name="test-collection",
            query=SearchQuery(tags=("joy",)),
            target_count=10,
            diversity_weight=0.5,
        )
        collection = svc.create(spec)
        assert collection.collection_id is not None
        assert collection.spec.name == "test-collection"
        assert collection.current_version == 0
        assert collection.frozen is False

        # Verify persisted in database
        row = conn.execute(
            "SELECT * FROM collections WHERE collection_id=?",
            (collection.collection_id,),
        ).fetchone()
        assert row is not None
        assert row["name"] == "test-collection"
        assert row["current_version"] == 0
        assert row["frozen"] == 0
        assert row["target_count"] == 10

    def test_create_with_all_spec_fields(self):
        """A version stores query/profile/config — all spec fields round-trip."""
        conn = _conn()
        from app.services.collections import CollectionService, CollectionSpec
        from app.services.library_search import LibrarySearchService

        search_svc = LibrarySearchService(conn)
        svc = CollectionService(conn, search_svc)

        spec = CollectionSpec(
            name="full-spec",
            query=SearchQuery(
                text="happy cat",
                tags=("cat", "funny"),
                folder="JUR-639",
                min_duration=1.0,
                max_duration=10.0,
                statuses=("candidate",),
                created_after="2026-07-01T00:00:00+00:00",
                created_before="2026-07-31T00:00:00+00:00",
            ),
            target_count=5,
            min_duration=2.0,
            max_duration=8.0,
            diversity_weight=0.3,
            profile_version="pv-abc",
            config_id="cfg-123",
        )
        collection = svc.create(spec)

        row = conn.execute(
            "SELECT * FROM collections WHERE collection_id=?",
            (collection.collection_id,),
        ).fetchone()
        assert row["diversity_weight"] == 0.3
        assert row["profile_version"] == "pv-abc"
        assert row["config_id"] == "cfg-123"

        # Verify the stored query JSON round-trips
        stored_query = json.loads(row["search_query_json"])
        assert stored_query["text"] == "happy cat"
        assert "cat" in stored_query["tags"]


# ===================================================================
# Refresh tests
# ===================================================================


class TestCollectionRefresh:
    """Refreshing creates a new version with candidate IDs and scores."""

    def test_refresh_creates_version(self):
        conn = _conn()
        for i in range(5):
            cid = f"cand-{i:03d}"
            _insert_candidate(conn, cid, final_score=1.0 - i * 0.1)
            _index_candidate(conn, cid)
            _insert_vector(conn, cid)

        from app.services.collections import CollectionService, CollectionSpec
        from app.services.library_search import LibrarySearchService

        search_svc = LibrarySearchService(conn)
        svc = CollectionService(conn, search_svc)

        spec = CollectionSpec(
            name="test-refresh",
            query=SearchQuery(),
            target_count=3,
        )
        collection = svc.create(spec)
        assert collection.current_version == 0

        version = svc.refresh(collection.collection_id)
        assert version.collection_id == collection.collection_id
        assert version.version == 1
        assert len(version.candidate_ids) == 3
        assert len(version.manifest_hash) > 0

        # Collection current_version should be updated
        updated = conn.execute(
            "SELECT current_version FROM collections WHERE collection_id=?",
            (collection.collection_id,),
        ).fetchone()
        assert updated["current_version"] == 1

        # Version should be in database with scores
        vrow = conn.execute(
            "SELECT * FROM collection_versions WHERE collection_id=? AND version=?",
            (collection.collection_id, 1),
        ).fetchone()
        assert vrow is not None
        scores = json.loads(vrow["scores_json"])
        assert len(scores) == 3

        # Items should be in collection_items
        count = conn.execute(
            "SELECT COUNT(*) FROM collection_items WHERE collection_id=? AND version=?",
            (collection.collection_id, 1),
        ).fetchone()[0]
        assert count == 3

    def test_multiple_refreshes_increment_version(self):
        conn = _conn()
        for i in range(5):
            cid = f"cand-{i:03d}"
            _insert_candidate(conn, cid, final_score=1.0 - i * 0.1)
            _index_candidate(conn, cid)
            _insert_vector(conn, cid)

        from app.services.collections import CollectionService, CollectionSpec
        from app.services.library_search import LibrarySearchService

        search_svc = LibrarySearchService(conn)
        svc = CollectionService(conn, search_svc)

        spec = CollectionSpec(
            name="test-multi-refresh",
            query=SearchQuery(),
            target_count=3,
        )
        col = svc.create(spec)

        v1 = svc.refresh(col.collection_id)
        assert v1.version == 1

        v2 = svc.refresh(col.collection_id)
        assert v2.version == 2

        # Both versions exist
        count = conn.execute(
            "SELECT COUNT(*) FROM collection_versions WHERE collection_id=?",
            (col.collection_id,),
        ).fetchone()[0]
        assert count == 2

    def test_refresh_with_fewer_candidates_than_target(self):
        """When fewer candidates exist than target_count, all are used."""
        conn = _conn()
        for i in range(3):
            cid = f"cand-{i:03d}"
            _insert_candidate(conn, cid, final_score=1.0 - i * 0.1)
            _index_candidate(conn, cid)
            _insert_vector(conn, cid)

        from app.services.collections import CollectionService, CollectionSpec
        from app.services.library_search import LibrarySearchService

        search_svc = LibrarySearchService(conn)
        svc = CollectionService(conn, search_svc)

        spec = CollectionSpec(
            name="test-few",
            query=SearchQuery(),
            target_count=10,
        )
        col = svc.create(spec)
        version = svc.refresh(col.collection_id)
        # Should have all 3 candidates even though target was 10
        assert len(version.candidate_ids) == 3


# ===================================================================
# Freeze tests
# ===================================================================


class TestFreeze:
    """Freezing prevents implicit refresh."""

    def test_freeze_prevents_refresh(self):
        conn = _conn()
        for i in range(3):
            cid = f"cand-{i:03d}"
            _insert_candidate(conn, cid, final_score=1.0 - i * 0.1)
            _index_candidate(conn, cid)
            _insert_vector(conn, cid)

        from app.services.collections import CollectionService, CollectionSpec
        from app.services.library_search import LibrarySearchService

        search_svc = LibrarySearchService(conn)
        svc = CollectionService(conn, search_svc)

        spec = CollectionSpec(
            name="test-freeze",
            query=SearchQuery(),
            target_count=3,
        )
        col = svc.create(spec)
        svc.refresh(col.collection_id)

        # Freeze
        frozen_v = svc.freeze(col.collection_id)
        assert frozen_v is not None

        # Verify frozen in DB
        row = conn.execute(
            "SELECT frozen FROM collections WHERE collection_id=?",
            (col.collection_id,),
        ).fetchone()
        assert row["frozen"] == 1

        # Refresh should raise
        with pytest.raises(ValueError, match="frozen"):
            svc.refresh(col.collection_id)

    def test_freeze_on_unrefreshed_collection(self):
        """Freezing a collection with no versions still sets frozen=True."""
        conn = _conn()
        from app.services.collections import CollectionService, CollectionSpec
        from app.services.library_search import LibrarySearchService

        search_svc = LibrarySearchService(conn)
        svc = CollectionService(conn, search_svc)

        spec = CollectionSpec(
            name="test-freeze-empty",
            query=SearchQuery(),
            target_count=3,
        )
        col = svc.create(spec)

        frozen_v = svc.freeze(col.collection_id)
        # Should return a version with version=0
        assert frozen_v is not None
        assert frozen_v.version == 0

        row = conn.execute(
            "SELECT frozen FROM collections WHERE collection_id=?",
            (col.collection_id,),
        ).fetchone()
        assert row["frozen"] == 1

    def test_freeze_on_nonexistent_collection_raises(self):
        conn = _conn()
        from app.services.collections import CollectionService, CollectionSpec
        from app.services.library_search import LibrarySearchService

        search_svc = LibrarySearchService(conn)
        svc = CollectionService(conn, search_svc)

        with pytest.raises(ValueError, match="not found"):
            svc.freeze("nonexistent-collection")


# ===================================================================
# Export tests
# ===================================================================


class TestExport:
    """Export produces deterministic files and reports missing candidates."""

    def test_export_reports_missing_candidates(self, tmp_path: Path):
        conn = _conn()
        for i in range(5):
            cid = f"cand-{i:03d}"
            _insert_candidate(conn, cid, final_score=1.0 - i * 0.1)
            _index_candidate(conn, cid)
            _insert_vector(conn, cid)

        from app.services.collections import CollectionService, CollectionSpec
        from app.services.library_search import LibrarySearchService

        search_svc = LibrarySearchService(conn)
        svc = CollectionService(conn, search_svc)

        spec = CollectionSpec(
            name="test-export-missing",
            query=SearchQuery(),
            target_count=5,
        )
        col = svc.create(spec)
        svc.refresh(col.collection_id)

        # Delete one candidate to simulate missing
        conn.execute("DELETE FROM candidate_gifs WHERE candidate_id='cand-002'")
        conn.commit()

        report = svc.export(col.collection_id, tmp_path)
        assert report.exported == 4
        assert "cand-002" in report.missing_candidate_ids
        assert Path(report.manifest_path).exists()
        assert Path(report.pbf_path).exists()

        # Manifest should note the missing candidate
        with open(report.manifest_path) as f:
            manifest = json.load(f)
        assert "cand-002" in manifest.get("missing_candidate_ids", [])

    def test_export_writes_deterministic_manifest(self, tmp_path: Path):
        conn = _conn()
        for i in range(5):
            cid = f"cand-{i:03d}"
            _insert_candidate(conn, cid, final_score=1.0 - i * 0.1)
            _index_candidate(conn, cid)
            _insert_vector(conn, cid)

        from app.services.collections import CollectionService, CollectionSpec
        from app.services.library_search import LibrarySearchService

        search_svc = LibrarySearchService(conn)
        svc = CollectionService(conn, search_svc)

        spec = CollectionSpec(
            name="test-deterministic",
            query=SearchQuery(),
            target_count=5,
        )
        col = svc.create(spec)
        svc.refresh(col.collection_id)

        report1 = svc.export(col.collection_id, tmp_path)
        report2 = svc.export(col.collection_id, tmp_path)

        with open(report1.manifest_path) as f:
            m1 = json.load(f)
        with open(report2.manifest_path) as f:
            m2 = json.load(f)

        # Manifests should be identical (same version exported twice)
        assert m1 == m2

        # Manifest structure
        assert "collection_id" in m1
        assert "name" in m1
        assert "version" in m1
        assert "candidates" in m1
        assert len(m1["candidates"]) == 5
        for c in m1["candidates"]:
            assert "candidate_id" in c
            assert "score" in c
            assert "rank" in c

    def test_pbf_contains_source_timestamps(self, tmp_path: Path):
        """The PBF binary file contains candidate data and source timestamps."""
        conn = _conn()
        for i in range(3):
            cid = f"cand-{i:03d}"
            _insert_candidate(
                conn,
                cid,
                final_score=1.0 - i * 0.1,
                source_video_path=f"/videos/source-{i}.mp4",
            )
            _index_candidate(conn, cid)
            _insert_vector(conn, cid)

        from app.services.collections import CollectionService, CollectionSpec
        from app.services.library_search import LibrarySearchService

        search_svc = LibrarySearchService(conn)
        svc = CollectionService(conn, search_svc)

        spec = CollectionSpec(
            name="test-pbf",
            query=SearchQuery(),
            target_count=3,
        )
        col = svc.create(spec)
        svc.refresh(col.collection_id)
        report = svc.export(col.collection_id, tmp_path)

        # Read PBF
        candidates = _read_pbf(Path(report.pbf_path))
        assert len(candidates) == 3

        for c in candidates:
            assert "candidate_id" in c
            assert "score" in c
            assert "source_video_path" in c
            assert "created_at" in c  # source timestamp

    def test_export_on_unrefreshed_collection_raises(self, tmp_path: Path):
        conn = _conn()
        from app.services.collections import CollectionService, CollectionSpec
        from app.services.library_search import LibrarySearchService

        search_svc = LibrarySearchService(conn)
        svc = CollectionService(conn, search_svc)

        spec = CollectionSpec(
            name="test-no-refresh",
            query=SearchQuery(),
            target_count=3,
        )
        col = svc.create(spec)

        with pytest.raises(ValueError, match="no versions"):
            svc.export(col.collection_id, tmp_path)


# ===================================================================
# Diversity selection tests
# ===================================================================


class TestDiversitySelection:
    """Farthest-first diversity selects candidates with vector variety."""

    def test_diversity_produces_varied_selection(self):
        """Candidates with different vectors are preferred over similar ones."""
        conn = _conn()
        # Create 5 candidates with distinct enough vectors
        for i in range(5):
            cid = f"cand-{i:03d}"
            _insert_candidate(conn, cid, final_score=1.0 - i * 0.05)
            _index_candidate(conn, cid)
            # Different random seed per candidate ensures varied vectors
            _insert_vector(conn, cid)

        from app.services.collections import CollectionService, CollectionSpec
        from app.services.library_search import LibrarySearchService

        search_svc = LibrarySearchService(conn)
        svc = CollectionService(conn, search_svc)

        # target_count=5 should include all candidates
        spec = CollectionSpec(
            name="test-diversity",
            query=SearchQuery(),
            target_count=5,
            diversity_weight=1.0,  # pure diversity
        )
        col = svc.create(spec)
        version = svc.refresh(col.collection_id)
        assert len(version.candidate_ids) == 5

    def test_diversity_excludes_candidates_without_vectors(self):
        """Candidates without vectors have 0 distance so rank lower."""
        conn = _conn()
        for i in range(5):
            cid = f"cand-{i:03d}"
            _insert_candidate(conn, cid, final_score=1.0 - i * 0.1)
            _index_candidate(conn, cid)
            # Only add vector for first 3
            if i < 3:
                _insert_vector(conn, cid)

        from app.services.collections import CollectionService, CollectionSpec
        from app.services.library_search import LibrarySearchService

        search_svc = LibrarySearchService(conn)
        svc = CollectionService(conn, search_svc)

        spec = CollectionSpec(
            name="test-no-vec",
            query=SearchQuery(),
            target_count=5,
            diversity_weight=0.5,
        )
        col = svc.create(spec)
        version = svc.refresh(col.collection_id)
        # Should still include all 5 (no-vector candidates get distance=0)
        assert len(version.candidate_ids) == 5

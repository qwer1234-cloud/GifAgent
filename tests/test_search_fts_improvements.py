import json
import logging
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from app.db import init_db
from app.routers.candidates import _materialize_filesystem_candidates_for_folder
from app.services.candidates import CandidateService
from app.services.collections import CollectionService
from app.services.library_search import LibrarySearchService, SearchQuery
from app.services.preference_schema import apply_preference_schema
from app.services.workbench_schema import (
    SearchResultItem,
    apply_collections_schema,
    apply_search_schema,
)


@pytest.fixture
def test_db(tmp_path):
    db_path = tmp_path / "test_library.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS candidate_gifs (
            candidate_id TEXT PRIMARY KEY,
            source_run_id TEXT,
            source_run_candidate_id TEXT,
            source_video_sha256 TEXT,
            source_video_path TEXT,
            start_sec REAL,
            end_sec REAL,
            artifact_path TEXT,
            preview_path TEXT,
            vlm_summary_json TEXT,
            tags_json TEXT,
            scenario_keys_json TEXT,
            base_rag_similarity REAL,
            final_score REAL,
            status TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS candidate_vectors (
            candidate_id TEXT,
            vector_type TEXT,
            embedding_model TEXT,
            embedding_dim INTEGER,
            vector_blob BLOB,
            PRIMARY KEY(candidate_id, vector_type, embedding_model, embedding_dim)
        );
        """
    )
    apply_search_schema(conn)
    apply_collections_schema(conn)
    apply_preference_schema(conn)
    conn.commit()
    yield conn, db_path
    conn.close()


def test_rebuild_index_force_cleans_empty_fts_when_state_stale(test_db):
    conn, _ = test_db
    # Insert 5 candidates
    for i in range(1, 6):
        conn.execute(
            """INSERT INTO candidate_gifs
               (candidate_id, vlm_summary_json, tags_json, created_at, status)
               VALUES (?, ?, ?, ?, ?)""",
            (
                f"cand_{i:03d}",
                json.dumps({"caption": f"woman dancing scene {i}"}),
                json.dumps(["dance", "joy"]),
                f"2026-08-0{i}T00:00:00+00:00",
                "candidate",
            ),
        )
    conn.commit()

    svc = LibrarySearchService(conn)
    # First build
    report1 = svc.rebuild_index()
    assert report1.inserted == 5
    assert conn.execute("SELECT COUNT(*) FROM candidate_search_fts").fetchone()[0] == 5

    # Simulate broken state: FTS table is dropped/cleared, but state table says completed
    conn.execute("DELETE FROM candidate_search_fts")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM candidate_search_fts").fetchone()[0] == 0
    state_row = conn.execute("SELECT last_candidate_id FROM search_index_state WHERE id=1").fetchone()
    assert state_row["last_candidate_id"] == "cand_005"

    # Rebuild: should automatically detect FTS is empty and do a full clean re-index
    report2 = svc.rebuild_index()
    assert report2.inserted == 5
    assert conn.execute("SELECT COUNT(*) FROM candidate_search_fts").fetchone()[0] == 5


def test_rebuild_index_explicit_force(test_db):
    conn, _ = test_db
    for i in range(1, 4):
        conn.execute(
            """INSERT INTO candidate_gifs
               (candidate_id, vlm_summary_json, tags_json, created_at, status)
               VALUES (?, ?, ?, ?, ?)""",
            (
                f"cand_{i:03d}",
                json.dumps({"caption": f"scene {i}"}),
                json.dumps(["action"]),
                f"2026-08-0{i}T00:00:00+00:00",
                "candidate",
            ),
        )
    conn.commit()

    svc = LibrarySearchService(conn)
    svc.rebuild_index()
    assert conn.execute("SELECT COUNT(*) FROM candidate_search_fts").fetchone()[0] == 3

    # Force rebuild
    report = svc.rebuild_index(force=True)
    assert report.inserted == 3
    assert conn.execute("SELECT COUNT(*) FROM candidate_search_fts").fetchone()[0] == 3


def test_init_db_auto_heals_empty_fts_with_stale_state(test_db, monkeypatch):
    conn, db_path = test_db
    monkeypatch.setattr("app.db.DB_PATH", str(db_path))

    for i in range(1, 4):
        conn.execute(
            """INSERT INTO candidate_gifs
               (candidate_id, vlm_summary_json, tags_json, created_at, status)
               VALUES (?, ?, ?, ?, ?)""",
            (
                f"cand_{i:03d}",
                json.dumps({"caption": f"scene {i}"}),
                json.dumps(["tag"]),
                f"2026-08-0{i}T00:00:00+00:00",
                "candidate",
            ),
        )
    # Stale search_index_state
    conn.execute(
        """INSERT OR REPLACE INTO search_index_state
           (id, last_candidate_id, last_candidate_created_at, indexed_count, total_count, updated_at)
           VALUES (1, 'cand_003', '2026-08-03T00:00:00+00:00', 3, 3, '2026-08-03T00:00:00')"""
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM candidate_search_fts").fetchone()[0] == 0

    # Call init_db
    init_db(apply_preference=True)

    # Verify auto-healing restored FTS index
    new_conn = sqlite3.connect(db_path)
    count = new_conn.execute("SELECT COUNT(*) FROM candidate_search_fts").fetchone()[0]
    assert count == 3
    new_conn.close()


def test_candidate_service_and_import_sync_deduplicates_fts(test_db):
    conn, _ = test_db
    svc = CandidateService(conn)

    run_cand = {
        "run_candidate_id": "rcand_1",
        "source_video_sha256": "v123",
        "source_video_path": "/videos/test.mp4",
        "artifact_path": "data/exports/test.gif",
        "preview_path": "data/thumbs/test.jpg",
        "start_sec": 10.0,
        "end_sec": 15.0,
        "vlm_summary": {"caption": "woman dancing in bedroom"},
        "tags": ["dance", "bedroom"],
        "base_rag_similarity": 0.8,
        "final_score": 0.85,
    }

    # Insert candidate
    res1 = svc.materialize_run_candidate("run_1", run_cand)
    cid = res1.candidate_id

    # Verify FTS row created
    rows = conn.execute(
        "SELECT candidate_id, summary, tags FROM candidate_search_fts WHERE candidate_id = ?",
        (cid,),
    ).fetchall()
    assert len(rows) == 1
    assert "dancing" in rows[0]["summary"]

    # Re-insert with updated caption by clearing candidate_gifs but keeping candidate_search_fts
    conn.execute("DELETE FROM candidate_gifs WHERE candidate_id = ?", (cid,))
    conn.commit()
    run_cand["vlm_summary"] = {"caption": "woman dancing in hallway"}
    svc.materialize_run_candidate("run_1", run_cand)

    # Verify strictly 1 row in UNINDEXED FTS5 table (no duplicate accumulation)
    rows_after = conn.execute(
        "SELECT candidate_id, summary, tags FROM candidate_search_fts WHERE candidate_id = ?",
        (cid,),
    ).fetchall()
    assert len(rows_after) == 1
    assert "hallway" in rows_after[0]["summary"]


def test_import_folder_syncs_and_deduplicates_fts(test_db, tmp_path):
    conn, _ = test_db
    folder = tmp_path / "gifs"
    folder.mkdir()
    gif1 = folder / "movie_clip_001.gif"
    gif1.write_bytes(b"GIF89a dummy content")

    # Import folder
    created = _materialize_filesystem_candidates_for_folder(conn, folder)
    assert created == 1

    # Verify FTS row exists
    fts_rows = conn.execute("SELECT candidate_id, source_path FROM candidate_search_fts").fetchall()
    assert len(fts_rows) == 1

    # Re-import should not duplicate in FTS
    created2 = _materialize_filesystem_candidates_for_folder(conn, folder)
    assert created2 == 0
    fts_rows2 = conn.execute("SELECT candidate_id, source_path FROM candidate_search_fts").fetchall()
    assert len(fts_rows2) == 1


def test_collections_farthest_first_diversity_with_no_vectors_in_selected(test_db):
    conn, _ = test_db
    search_svc = LibrarySearchService(conn)
    svc = CollectionService(conn, search_service=search_svc)

    def _make_item(cid: str, score: float) -> SearchResultItem:
        return SearchResultItem(
            candidate_id=cid,
            preview_path=f"data/thumbs/{cid}.jpg",
            source_video_path=f"/videos/{cid}.mp4",
            start_sec=0.0,
            end_sec=5.0,
            duration=5.0,
            summary="sample summary",
            tags=["sample"],
            status="candidate",
            score=score,
            created_at="2026-08-01T00:00:00+00:00",
        )

    items = [
        _make_item("c1", 0.9),
        _make_item("c2", 0.8),
        _make_item("c3", 0.7),
        _make_item("c4", 0.6),
    ]

    # None of the candidates have vectors in candidate_vectors
    # This previously crashed with ValueError: min() arg is an empty sequence
    selected = svc._farthest_first_select(items, target_count=3, diversity_weight=0.5)
    assert len(selected) == 3
    assert [cid for cid, _ in selected] == ["c1", "c2", "c3"]


def test_rank_with_text_logs_warning_and_surfaces_diagnostic(test_db, caplog):
    conn, _ = test_db
    svc = LibrarySearchService(conn)

    # Corrupt or drop the FTS table to simulate operational error
    conn.execute("DROP TABLE candidate_search_fts")
    conn.commit()

    # Insert a candidate so query has targets
    conn.execute(
        """INSERT INTO candidate_gifs
           (candidate_id, created_at, status)
           VALUES ('cand_1', '2026-08-01T00:00:00+00:00', 'candidate')"""
    )
    conn.commit()

    with caplog.at_level(logging.WARNING):
        res = svc.search(SearchQuery(text="bedroom"))
        assert res.degraded is True
        assert "FTS query error" in res.diagnosis
        assert any("FTS query failed" in r.message for r in caplog.records)


def test_index_health_logs_warning_on_missing_or_failed_tables(test_db, caplog):
    conn, _ = test_db
    svc = LibrarySearchService(conn)

    # Drop candidate_search_fts to trigger warning
    conn.execute("DROP TABLE candidate_search_fts")
    conn.commit()

    with caplog.at_level(logging.WARNING):
        health = svc.index_health()
        assert health.indexed_in_fts == 0
        assert any("candidate_search_fts query failed" in r.message for r in caplog.records)


def test_build_fts_v2_allow_production_guard(tmp_path):
    from scripts.token_sprint.build_fts_v2 import _guard_db_target, BuildError

    sprint_root = Path("data/token_sprint")
    dist_db = tmp_path / "dist" / "GifAgentUI" / "data" / "library.db"
    dist_db.parent.mkdir(parents=True, exist_ok=True)
    dist_db.touch()

    # Without allow_production: must raise BuildError
    with pytest.raises(BuildError, match="must not point under a dist/ tree"):
        _guard_db_target(dist_db, sprint_root, allow_production=False)

    # With allow_production: succeeds
    _guard_db_target(dist_db, sprint_root, allow_production=True)

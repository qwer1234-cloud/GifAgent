"""Regression tests for schemas required immediately after application startup."""
from __future__ import annotations

import sqlite3


def test_init_db_applies_workbench_schemas(monkeypatch, tmp_path):
    """Direct startup routes must not run before search/collection tables exist."""
    from app import db

    db_path = tmp_path / "library.db"
    monkeypatch.setattr(db, "DB_PATH", str(db_path))

    db.init_db(apply_preference=True)

    with sqlite3.connect(db_path) as conn:
        table_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {
        "collections",
        "collection_versions",
        "collection_items",
        "candidate_search_fts",
        "search_index_state",
    } <= table_names


def test_vector_exclusion_schema_migrates_to_model_scoped_pk():
    from app.services.preference_schema import apply_preference_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE candidate_gifs (
            candidate_id TEXT PRIMARY KEY,
            source_run_id TEXT NOT NULL,
            source_run_candidate_id TEXT NOT NULL,
            source_video_sha256 TEXT NOT NULL,
            source_video_path TEXT NOT NULL,
            start_sec REAL NOT NULL,
            end_sec REAL NOT NULL,
            artifact_path TEXT,
            preview_path TEXT,
            vlm_summary_json TEXT NOT NULL DEFAULT '{}',
            tags_json TEXT NOT NULL DEFAULT '[]',
            scenario_keys_json TEXT NOT NULL DEFAULT '[]',
            base_rag_similarity REAL,
            profile_score REAL,
            final_score REAL,
            score_profile_version TEXT,
            status TEXT NOT NULL DEFAULT 'candidate',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE candidate_vectors (
            candidate_id TEXT NOT NULL,
            vector_type TEXT NOT NULL,
            embedding_model TEXT NOT NULL,
            embedding_dim INTEGER NOT NULL,
            vector_blob BLOB NOT NULL,
            normalized INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(candidate_id, vector_type, embedding_model)
        );
        CREATE TABLE candidate_vector_exclusions (
            candidate_id TEXT PRIMARY KEY,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO candidate_gifs
            (candidate_id, source_run_id, source_run_candidate_id,
             source_video_sha256, source_video_path, start_sec, end_sec)
            VALUES ('cand-1', 'run-1', 'rc-1', 'vid', '/v.mp4', 0, 1);
        INSERT INTO candidate_vector_exclusions (candidate_id, reason, created_at)
            VALUES ('cand-1', 'old-reason', '2026-01-01T00:00:00Z');
        """
    )
    apply_preference_schema(conn)

    vector_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(candidate_vectors)")
    }
    assert "text_schema_version" in vector_cols
    assert "source_text_hash" in vector_cols

    pk_cols = [
        name
        for _pk, name in sorted(
            (int(row[5]), str(row[1]))
            for row in conn.execute(
                "PRAGMA table_info(candidate_vector_exclusions)"
            )
            if row[5] > 0
        )
    ]
    assert pk_cols == ["candidate_id", "embedding_model"]
    row = conn.execute(
        "SELECT embedding_model, attempts, error_class, reason "
        "FROM candidate_vector_exclusions WHERE candidate_id='cand-1'"
    ).fetchone()
    assert row["embedding_model"] == "nomic-embed-text:latest"
    assert row["attempts"] == 1
    assert row["reason"] == "old-reason"

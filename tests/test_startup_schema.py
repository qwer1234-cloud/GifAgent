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


def test_curve_preference_schema_is_created_and_idempotent():
    from app.services.preference_schema import apply_preference_schema

    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)
    apply_preference_schema(conn)  # idempotent

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "curve_preference_models" in tables
    assert "curve_preference_qualification_runs" in tables

    run_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(curve_preference_qualification_runs)")
    }
    assert {
        "qualification_id", "model_version", "status", "input_manifest_sha256",
        "profile_snapshot_sha256", "baseline_scores_sha256",
        "proposed_scores_sha256", "metrics_json", "report_path",
        "report_sha256", "source_db_snapshot_sha256", "created_at",
        "qualified_at",
    } <= run_cols


def test_curve_model_rows_survive_schema_reapply():
    from app.services.preference_schema import apply_preference_schema

    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)
    conn.execute(
        """
        INSERT INTO curve_preference_models (
            model_version, feature_version, extractor_sha256, status,
            coefficients_json, intercept, feature_mean_json, feature_std_json,
            feature_names_json, l2_lambda, training_event_watermark,
            training_source_set_sha256, holdout_row_count, metrics_json
        ) VALUES (
            'm-keep', 'curve-v1', 'ex', 'DRAFT', '{}', 0.0, '{}', '{}',
            '[]', 1.0, 'wm', 'srcsha', 0, '{}'
        )
        """
    )
    conn.commit()

    apply_preference_schema(conn)

    row = conn.execute(
        "SELECT model_version, status FROM curve_preference_models"
    ).fetchone()
    assert row == ("m-keep", "DRAFT")


def test_curve_model_table_migrates_missing_manifest_columns():
    from app.services.preference_schema import apply_preference_schema

    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE curve_preference_models (
            model_version TEXT PRIMARY KEY,
            feature_version TEXT NOT NULL,
            extractor_sha256 TEXT NOT NULL,
            status TEXT NOT NULL,
            coefficients_json TEXT NOT NULL,
            intercept REAL NOT NULL,
            feature_mean_json TEXT NOT NULL,
            feature_std_json TEXT NOT NULL,
            feature_names_json TEXT NOT NULL,
            l2_lambda REAL NOT NULL,
            training_event_watermark TEXT NOT NULL,
            training_source_set_sha256 TEXT NOT NULL,
            holdout_row_count INTEGER NOT NULL,
            metrics_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO curve_preference_models (
            model_version, feature_version, extractor_sha256, status,
            coefficients_json, intercept, feature_mean_json, feature_std_json,
            feature_names_json, l2_lambda, training_event_watermark,
            training_source_set_sha256, holdout_row_count, metrics_json
        ) VALUES (
            'm-old', 'curve-v1', 'ex', 'DRAFT', '{}', 0.0, '{}', '{}',
            '[]', 1.0, 'wm', 'srcsha', 0, '{}'
        )
        """
    )

    apply_preference_schema(conn)

    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(curve_preference_models)")
    }
    assert {
        "holdout_source_set_sha256", "training_manifest_sha256",
        "holdout_manifest_sha256", "training_manifest_json",
        "holdout_manifest_json", "curve_model_confidence", "published_at",
    } <= cols
    row = conn.execute(
        "SELECT model_version, holdout_source_set_sha256 FROM curve_preference_models"
    ).fetchone()
    assert row[0] == "m-old"
    assert row[1] == ""

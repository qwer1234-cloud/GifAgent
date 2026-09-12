"""Phase 3 Task 1: Append-only feedback schema with migration support."""

from __future__ import annotations

import sqlite3

# ── helpers ──────────────────────────────────────────────────────────────────


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_columns(
    conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]
) -> None:
    existing = _column_names(conn, table)
    for col, ddl in columns:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


# ── core schema ──────────────────────────────────────────────────────────────


def apply_preference_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS curve_features (
            candidate_id TEXT NOT NULL,
            feature_version TEXT NOT NULL,
            availability TEXT NOT NULL CHECK (availability IN ('AVAILABLE', 'UNAVAILABLE')),
            features_json TEXT,
            missing_reason TEXT,
            extractor_sha256 TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL,
            wall_clock_ms REAL,
            source_video_sha256 TEXT,
            width INTEGER,
            height INTEGER,
            frame_count INTEGER,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            PRIMARY KEY (candidate_id, feature_version)
        );

        CREATE TABLE IF NOT EXISTS curve_preference_models (
            model_version TEXT PRIMARY KEY,
            feature_version TEXT NOT NULL,
            extractor_sha256 TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK(status IN ('DRAFT','PUBLISHED','REJECTED','SUPERSEDED')),
            coefficients_json TEXT NOT NULL,
            intercept REAL NOT NULL,
            feature_mean_json TEXT NOT NULL,
            feature_std_json TEXT NOT NULL,
            feature_names_json TEXT NOT NULL,
            l2_lambda REAL NOT NULL,
            training_event_watermark TEXT NOT NULL,
            training_source_set_sha256 TEXT NOT NULL,
            holdout_source_set_sha256 TEXT NOT NULL DEFAULT '',
            training_manifest_sha256 TEXT NOT NULL DEFAULT '',
            holdout_manifest_sha256 TEXT NOT NULL DEFAULT '',
            training_manifest_json TEXT NOT NULL DEFAULT '{}',
            holdout_manifest_json TEXT NOT NULL DEFAULT '{}',
            holdout_row_count INTEGER NOT NULL,
            metrics_json TEXT NOT NULL,
            curve_model_confidence REAL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            published_at TEXT
        );

        CREATE TABLE IF NOT EXISTS curve_preference_qualification_runs (
            qualification_id TEXT PRIMARY KEY,
            model_version TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('QUALIFIED','NOT_QUALIFIED')),
            input_manifest_sha256 TEXT NOT NULL,
            profile_snapshot_sha256 TEXT NOT NULL,
            baseline_scores_sha256 TEXT NOT NULL,
            proposed_scores_sha256 TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            report_path TEXT NOT NULL,
            report_sha256 TEXT NOT NULL,
            source_db_snapshot_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            qualified_at TEXT,
            FOREIGN KEY(model_version) REFERENCES curve_preference_models(model_version)
        );

        CREATE TABLE IF NOT EXISTS candidate_gifs (
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
            status TEXT NOT NULL DEFAULT 'candidate'
                CHECK(status IN ('candidate','liked','disliked','neutral','promoted','rejected','archived')),
            promoted_media_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_run_id, source_run_candidate_id)
        );

        CREATE TABLE IF NOT EXISTS candidate_vectors (
            candidate_id TEXT NOT NULL,
            vector_type TEXT NOT NULL,
            embedding_model TEXT NOT NULL,
            embedding_dim INTEGER NOT NULL,
            vector_blob BLOB NOT NULL,
            normalized INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            text_schema_version INTEGER NOT NULL DEFAULT 0,
            source_text_hash TEXT,
            PRIMARY KEY(candidate_id, vector_type, embedding_model),
            FOREIGN KEY(candidate_id) REFERENCES candidate_gifs(candidate_id)
        );

        CREATE TABLE IF NOT EXISTS candidate_vector_exclusions (
            candidate_id TEXT NOT NULL,
            embedding_model TEXT NOT NULL DEFAULT 'nomic-embed-text:latest',
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            attempts INTEGER NOT NULL DEFAULT 1,
            error_class TEXT NOT NULL DEFAULT 'permanent',
            PRIMARY KEY (candidate_id, embedding_model),
            FOREIGN KEY(candidate_id) REFERENCES candidate_gifs(candidate_id)
        );

        CREATE TABLE IF NOT EXISTS favorite_gifs (
            favorite_id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL UNIQUE,
            full_path TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(candidate_id) REFERENCES candidate_gifs(candidate_id)
        );

        CREATE INDEX IF NOT EXISTS idx_favorite_gifs_candidate
            ON favorite_gifs(candidate_id);

        CREATE TABLE IF NOT EXISTS preference_events (
            event_id TEXT PRIMARY KEY,
            target_type TEXT NOT NULL CHECK(target_type IN ('media','candidate_gif')),
            target_id TEXT NOT NULL,
            rating TEXT NOT NULL CHECK(rating IN ('like','neutral','dislike','quality_reject','skip','favorite')),
            source_video_sha256 TEXT NOT NULL,
            scenario_keys_json TEXT NOT NULL DEFAULT '[]',
            corrected_tags_json TEXT,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            previous_status TEXT,
            undone_at TEXT,
            undone_reason TEXT,
            event_kind TEXT NOT NULL DEFAULT 'feedback'
                CHECK(event_kind IN ('feedback','correction')),
            supersedes_event_id TEXT
        );

        CREATE TABLE IF NOT EXISTS preference_profile_builds (
            profile_version TEXT PRIMARY KEY,
            event_watermark TEXT NOT NULL,
            embedding_model TEXT NOT NULL,
            embedding_dim INTEGER NOT NULL,
            effective_feedback_count INTEGER NOT NULL,
            source_video_count INTEGER NOT NULL,
            config_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('building','completed','blocked','failed')),
            gate_reasons_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS preference_profiles (
            profile_id TEXT PRIMARY KEY,
            profile_version TEXT NOT NULL,
            scope TEXT NOT NULL CHECK(scope IN ('global','scenario')),
            scenario_key TEXT,
            like_count INTEGER NOT NULL,
            dislike_count INTEGER NOT NULL,
            neutral_count INTEGER NOT NULL,
            confidence REAL NOT NULL,
            liked_centroid_blob BLOB,
            disliked_centroid_blob BLOB,
            tag_weights_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(profile_version) REFERENCES preference_profile_builds(profile_version),
            UNIQUE(profile_version, scope, scenario_key)
        );

        CREATE TABLE IF NOT EXISTS preference_profile_current (
            slot TEXT PRIMARY KEY CHECK(slot = 'current'),
            profile_version TEXT NOT NULL,
            published_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(profile_version) REFERENCES preference_profile_builds(profile_version)
        );

        CREATE TABLE IF NOT EXISTS preference_profile_publications (
            publication_id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_version TEXT NOT NULL,
            previous_profile_version TEXT,
            published_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            config_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(profile_version) REFERENCES preference_profile_builds(profile_version)
        );

        CREATE TABLE IF NOT EXISTS candidate_analysis_records (
            record_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            preexport_key TEXT NOT NULL,
            capture_version TEXT NOT NULL,
            source_video_sha256 TEXT NOT NULL,
            start_us INTEGER NOT NULL,
            end_us INTEGER NOT NULL,
            anchor_us INTEGER NOT NULL,
            anchor_source TEXT NOT NULL,
            capture_status TEXT NOT NULL CHECK(capture_status IN ('CAPTURED', 'FAILED', 'NEEDS_REPAIR')),
            failure_reason TEXT,
            payload_path TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            UNIQUE(run_id, preexport_key, capture_version)
        );

        CREATE TABLE IF NOT EXISTS candidate_analysis_publications (
            publication_id TEXT PRIMARY KEY,
            record_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            gif_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY(record_id) REFERENCES candidate_analysis_records(record_id)
        );

        CREATE INDEX IF NOT EXISTS idx_candidate_analysis_pub_candidate
            ON candidate_analysis_publications(candidate_id);
        CREATE INDEX IF NOT EXISTS idx_candidate_analysis_pub_record
            ON candidate_analysis_publications(record_id);

        CREATE TABLE IF NOT EXISTS candidate_analysis_events (
            event_id TEXT PRIMARY KEY,
            record_id TEXT NOT NULL,
            event_kind TEXT NOT NULL CHECK(event_kind IN ('scored', 'selected', 'not_selected', 'quality_rejected', 'publication_failed')),
            payload_path TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY(record_id) REFERENCES candidate_analysis_records(record_id)
        );

        CREATE INDEX IF NOT EXISTS idx_candidate_analysis_events_record
            ON candidate_analysis_events(record_id, event_kind);

        CREATE TABLE IF NOT EXISTS learning_jobs (
            job_id TEXT PRIMARY KEY,
            job_type TEXT NOT NULL,
            input_snapshot_sha256 TEXT NOT NULL,
            policy_sha256 TEXT NOT NULL,
            stage TEXT NOT NULL CHECK(stage IN ('SNAPSHOT', 'TRAIN', 'QUALIFY', 'COMPLETE')),
            status TEXT NOT NULL CHECK(status IN ('PENDING', 'RUNNING', 'RETRY_WAIT', 'NEEDS_ATTENTION', 'SUCCEEDED', 'CANCELLED')),
            owner TEXT,
            lease_expires_at TEXT,
            lease_expires_epoch INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_retry_at TEXT,
            next_retry_epoch INTEGER,
            result_package_path TEXT,
            result_package_sha256 TEXT,
            error_code TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            UNIQUE(input_snapshot_sha256, policy_sha256)
        );

        CREATE TABLE IF NOT EXISTS curve_model_deployments (
            revision INTEGER PRIMARY KEY AUTOINCREMENT,
            model_version TEXT NOT NULL,
            qualification_id TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            extractor_sha256 TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 0,
            approved_by TEXT NOT NULL,
            approved_at TEXT NOT NULL,
            previous_revision INTEGER,
            qualification_bundle_path TEXT,
            qualification_bundle_sha256 TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS curve_model_deployment_current (
            slot TEXT PRIMARY KEY CHECK(slot = 'current'),
            revision INTEGER NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_by TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_candidate_gifs_status_score
            ON candidate_gifs(status, final_score);
        CREATE INDEX IF NOT EXISTS idx_candidate_gifs_source
            ON candidate_gifs(source_video_sha256, source_run_id);
        CREATE INDEX IF NOT EXISTS idx_preference_events_target
            ON preference_events(target_type, target_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_preference_events_video
            ON preference_events(source_video_sha256, created_at);
        """
    )

    # Phase 2 backward-compat: add columns that older schemas may lack.
    gif_columns = _column_names(conn, "candidate_gifs")
    for column, ddl in (
        ("artifact_id", "TEXT"),
        ("provenance_json", "TEXT"),
    ):
        if column not in gif_columns:
            conn.execute(f"ALTER TABLE candidate_gifs ADD COLUMN {column} {ddl}")

    # Phase 3 migration: add event_kind and supersedes_event_id to
    # preference_events, while replacing the old CHECK constraint with one
    # that includes "favorite".
    _ensure_columns(
        conn,
        "preference_events",
        (
            ("previous_status", "TEXT"),
            ("undone_at", "TEXT"),
            ("undone_reason", "TEXT"),
        ),
    )
    _migrate_preference_events(conn)
    _ensure_columns(
        conn,
        "candidate_vectors",
        (
            ("text_schema_version", "INTEGER NOT NULL DEFAULT 0"),
            ("source_text_hash", "TEXT"),
        ),
    )
    _migrate_vector_exclusions(conn)
    _migrate_curve_model_columns(conn)
    _migrate_learning_job_columns(conn)

    conn.commit()


# ── Phase 3 migration ────────────────────────────────────────────────────────


def _migrate_preference_events(conn: sqlite3.Connection) -> None:
    """Transactional migration of *preference_events* to the Phase 3 schema.

    1. Check whether the migration is already applied (probe for
       ``supersedes_event_id`` column).
    2. Create ``preference_events_new`` with the expanded CHECK constraint.
    3. Copy every existing row, setting ``event_kind='feedback'``.
    4. Validate row counts and foreign-key referents.
    5. Drop old ``preference_events``, rename new table.
    6. Recreate indexes.
    """
    existing = _column_names(conn, "preference_events")

    # Already migrated — nothing to do.
    if "supersedes_event_id" in existing:
        return

    old_count = conn.execute("SELECT COUNT(*) FROM preference_events").fetchone()[0]

    # Use a transaction wrapping individual execute() calls rather than
    # executescript(), because executescript() implicitly commits any
    # pending SAVEPOINT before running, defeating our rollback plan.
    conn.execute("SAVEPOINT preference_events_migration")

    try:
        conn.execute(
            """CREATE TABLE preference_events_new (
                event_id TEXT PRIMARY KEY,
                target_type TEXT NOT NULL
                    CHECK(target_type IN ('media','candidate_gif')),
                target_id TEXT NOT NULL,
                rating TEXT NOT NULL
                    CHECK(rating IN ('like','neutral','dislike','quality_reject','skip','favorite')),
                source_video_sha256 TEXT NOT NULL,
                scenario_keys_json TEXT NOT NULL DEFAULT '[]',
                corrected_tags_json TEXT,
                note TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                previous_status TEXT,
                undone_at TEXT,
                undone_reason TEXT,
                event_kind TEXT NOT NULL DEFAULT 'feedback'
                    CHECK(event_kind IN ('feedback','correction')),
                supersedes_event_id TEXT
            )"""
        )

        conn.execute(
            """INSERT INTO preference_events_new (
                event_id, target_type, target_id, rating,
                source_video_sha256, scenario_keys_json, corrected_tags_json,
                note, created_at, previous_status, undone_at, undone_reason,
                event_kind, supersedes_event_id
            )
            SELECT
                event_id, target_type, target_id, rating,
                source_video_sha256, scenario_keys_json, corrected_tags_json,
                note, created_at, previous_status, undone_at, undone_reason,
                'feedback', NULL
            FROM preference_events"""
        )

        # ---- Validation step ----
        new_count = conn.execute(
            "SELECT COUNT(*) FROM preference_events_new"
        ).fetchone()[0]

        if new_count != old_count:
            raise RuntimeError(
                f"Row-count mismatch during preference_events migration: "
                f"old={old_count} new={new_count}"
            )

        orphan_rows = conn.execute(
            """SELECT COUNT(*) FROM preference_events_new e
               WHERE e.target_type = 'candidate_gif'
                 AND e.target_id NOT IN (
                     SELECT candidate_id FROM candidate_gifs
                 )"""
        ).fetchone()[0]
        if orphan_rows:
            raise RuntimeError(
                f"Migration found {orphan_rows} orphaned candidate_gif "
                f"references — refusing to proceed."
            )

        # Swap tables.
        conn.execute("DROP TABLE preference_events")
        conn.execute("ALTER TABLE preference_events_new RENAME TO preference_events")
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_preference_events_target
               ON preference_events(target_type, target_id, created_at)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_preference_events_video
               ON preference_events(source_video_sha256, created_at)"""
        )

        conn.execute("RELEASE SAVEPOINT preference_events_migration")

    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT preference_events_migration")
        raise


def _migrate_vector_exclusions(conn: sqlite3.Connection) -> None:
    """Rebuild exclusions with a (candidate_id, embedding_model) primary key."""
    existing = _column_names(conn, "candidate_vector_exclusions")
    if (
        "embedding_model" in existing
        and "attempts" in existing
        and "error_class" in existing
    ):
        pk_cols = [
            name
            for _pk, name in sorted(
                (int(row[5]), str(row[1]))
                for row in conn.execute(
                    "PRAGMA table_info(candidate_vector_exclusions)"
                ).fetchall()
                if row[5] > 0
            )
        ]
        if pk_cols == ["candidate_id", "embedding_model"]:
            return

    conn.execute("SAVEPOINT vector_exclusions_migration")
    try:
        conn.execute(
            """CREATE TABLE candidate_vector_exclusions_new (
                candidate_id TEXT NOT NULL,
                embedding_model TEXT NOT NULL DEFAULT 'nomic-embed-text:latest',
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                attempts INTEGER NOT NULL DEFAULT 1,
                error_class TEXT NOT NULL DEFAULT 'permanent',
                PRIMARY KEY (candidate_id, embedding_model),
                FOREIGN KEY (candidate_id) REFERENCES candidate_gifs(candidate_id)
            )"""
        )
        has_model = "embedding_model" in existing
        has_attempts = "attempts" in existing
        has_error_class = "error_class" in existing
        model_expr = (
            "embedding_model" if has_model else "'nomic-embed-text:latest'"
        )
        attempts_expr = "attempts" if has_attempts else "1"
        error_expr = "error_class" if has_error_class else "'permanent'"
        conn.execute(
            f"""INSERT INTO candidate_vector_exclusions_new (
                    candidate_id, embedding_model, reason, created_at,
                    attempts, error_class
                )
                SELECT candidate_id, {model_expr}, reason, created_at,
                       {attempts_expr}, {error_expr}
                FROM candidate_vector_exclusions"""
        )
        conn.execute("DROP TABLE candidate_vector_exclusions")
        conn.execute(
            "ALTER TABLE candidate_vector_exclusions_new "
            "RENAME TO candidate_vector_exclusions"
        )
        conn.execute("RELEASE SAVEPOINT vector_exclusions_migration")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT vector_exclusions_migration")
        raise


# ── learning job epoch-time migration ───────────────────────────────────────


def _migrate_learning_job_columns(conn: sqlite3.Connection) -> None:
    """Ensure learning_jobs gains integer epoch lease/retry columns.

    The legacy ISO-text columns are kept for one-read compatibility only;
    all comparisons now happen on integer epochs.
    """
    if "learning_jobs" not in {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }:
        return
    _ensure_columns(
        conn,
        "learning_jobs",
        (
            ("lease_expires_epoch", "INTEGER"),
            ("next_retry_epoch", "INTEGER"),
        ),
    )
    _ensure_columns(
        conn,
        "curve_model_deployments",
        (
            ("qualification_bundle_path", "TEXT"),
            ("qualification_bundle_sha256", "TEXT"),
        ),
    )


# ── curve model column migration ─────────────────────────────────────────────


def _migrate_curve_model_columns(conn: sqlite3.Connection) -> None:
    """Ensure pre-existing curve model tables gain manifest/holdout columns."""

    if "curve_preference_models" not in {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }:
        return
    _ensure_columns(
        conn,
        "curve_preference_models",
        (
            ("holdout_source_set_sha256", "TEXT NOT NULL DEFAULT ''"),
            ("training_manifest_sha256", "TEXT NOT NULL DEFAULT ''"),
            ("holdout_manifest_sha256", "TEXT NOT NULL DEFAULT ''"),
            ("training_manifest_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("holdout_manifest_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("curve_model_confidence", "REAL"),
            ("published_at", "TEXT"),
        ),
    )

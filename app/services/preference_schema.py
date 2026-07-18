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
            PRIMARY KEY(candidate_id, vector_type, embedding_model),
            FOREIGN KEY(candidate_id) REFERENCES candidate_gifs(candidate_id)
        );

        CREATE TABLE IF NOT EXISTS candidate_vector_exclusions (
            candidate_id TEXT PRIMARY KEY,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
    _migrate_preference_events(conn)

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

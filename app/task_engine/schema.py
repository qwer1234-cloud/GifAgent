from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = "data/task_state.db"
SCHEMA_VERSION = 4

_STATUS_CHECK = (
    "CHECK(status IN ('pending','leased','running','succeeded',"
    "'retry_wait','needs_attention','cancelled'))"
)

_DDL = f"""
CREATE TABLE IF NOT EXISTS task_jobs (
    job_id TEXT PRIMARY KEY,
    directory TEXT NOT NULL,
    directory_key TEXT NOT NULL,
    config_json TEXT NOT NULL,
    job_limit INTEGER NOT NULL DEFAULT 0,
    extensions TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' {_STATUS_CHECK},
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_videos (
    video_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES task_jobs(job_id),
    path TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' {_STATUS_CHECK},
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, path)
);

CREATE TABLE IF NOT EXISTS task_stages (
    stage_id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES task_videos(video_id),
    stage_name TEXT NOT NULL,
    clip_id TEXT,
    input_key TEXT NOT NULL,
    output_key TEXT,
    status TEXT NOT NULL DEFAULT 'pending' {_STATUS_CHECK},
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires_at TEXT,
    retry_at TEXT,
    last_error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_artifacts (
    artifact_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    video_id TEXT NOT NULL,
    stage_name TEXT NOT NULL,
    clip_id TEXT,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    provenance_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_commands (
    command_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES task_jobs(job_id),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- No partial unique index on directory_key — same-directory jobs with
-- different video_paths are allowed (Quality Lab items in shared folders).
-- Application-level conflict detection in TaskRepository.create_job()
-- prevents true duplicates.

CREATE UNIQUE INDEX IF NOT EXISTS uq_stage_identity
ON task_stages(video_id, stage_name, COALESCE(clip_id, ''), input_key);

CREATE INDEX IF NOT EXISTS idx_task_stages_status ON task_stages(status);
CREATE INDEX IF NOT EXISTS idx_task_videos_job ON task_videos(job_id);
"""


def apply_task_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    _migrate_task_schema(conn)
    conn.commit()


def _detect_schema_state(conn: sqlite3.Connection) -> dict:
    """Detect actual schema state from PRAGMA info.

    Returns a dict describing which columns, indexes, and constraints
    actually exist on ``task_artifacts``, independently of migration records.
    """
    try:
        indexes = {r[1] for r in conn.execute("PRAGMA index_list('task_artifacts')").fetchall()}
    except sqlite3.OperationalError:
        indexes = set()
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info('task_artifacts')").fetchall()}
    except sqlite3.OperationalError:
        columns = set()

    return {
        "v4_index": "uq_artifact_stage_identity" in indexes,
        "v3_index": "uq_artifact_stage_kind_clip" in indexes,
        "stage_id": "stage_id" in columns,
        "artifact_kind": "artifact_kind" in columns,
        "lookup_index": "idx_task_artifacts_lookup" in indexes,
    }


def _migrate_task_schema(conn: sqlite3.Connection) -> None:
    """Apply schema migrations in version order.

    Each migration is applied exactly once.  Applied versions are read from
    the ``task_migrations`` table.  Additionally, the actual schema state
    is detected via PRAGMA to handle historical databases where the
    migration record table does not perfectly reflect the schema on disk.

    Compatible with:
    - Fresh databases (no migration records, no task_* tables beyond DDL)
    - Old databases without a ``task_migrations`` table
    - v3 databases (have uq_artifact_stage_kind_clip)
    - v4 databases (have uq_artifact_stage_identity, multi-frame artifacts)
    - Legacy v4 databases where only migration 4 is recorded (no v3)
    """
    # Ensure the migration-tracking columns exist (old DBs may lack them).
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(task_migrations)").fetchall()}
    except sqlite3.OperationalError:
        cols = set()
    for col, ddl in [("migration_id", "TEXT"), ("report_json", "TEXT")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE task_migrations ADD COLUMN {col} {ddl}")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_task_migrations_migration_id
           ON task_migrations(migration_id) WHERE migration_id IS NOT NULL"""
    )

    # Drop the old per-directory unique index (v2 migration).
    _drop_index_if_exists(conn, "uq_active_job_directory")

    # Read which migration versions have already been applied.
    applied = {r["version"] for r in conn.execute(
        "SELECT version FROM task_migrations"
    ).fetchall()}

    # Detect actual schema state on disk.
    state = _detect_schema_state(conn)

    # ── Determine whether v3 DDL has already been applied ───────────
    # The table may already have v3 columns and indexes without migration
    # record 3 (e.g. legacy v4 databases where only v4 was recorded).
    v3_applied_on_disk = (
        state["stage_id"] and state["artifact_kind"]
    )
    v4_applied_on_disk = state["v4_index"]

    # ── v3: artifact identity migration ──────────────────────────────
    if 3 not in applied:
        if v4_applied_on_disk:
            # Schema is already at v4 or beyond — v3 DDL was applied
            # transitively.  Record v3 as already applied and skip DDL
            # to avoid recreating the old UNIQUE index on a table that
            # already has multi-frame artifacts.
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO task_migrations (version, applied_at) VALUES (?, ?)",
                    (3, datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        elif v3_applied_on_disk and not v4_applied_on_disk:
            # v3 columns exist but v4 migration not recorded — record v3
            # and proceed to v4.
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO task_migrations (version, applied_at) VALUES (?, ?)",
                    (3, datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        elif v3_applied_on_disk and v4_applied_on_disk:
            # Fully migrated on disk, just missing records.
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO task_migrations (version, applied_at) VALUES (?, ?)",
                    (3, datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        else:
            # Fresh table — apply v3 DDL.
            conn.execute("BEGIN IMMEDIATE")
            try:
                _migrate_v3_artifact_identity(conn)
                conn.execute(
                    "INSERT OR IGNORE INTO task_migrations (version, applied_at) VALUES (?, ?)",
                    (3, datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # ── v4: multi-frame artifact support ─────────────────────────────
    if 4 not in applied:
        # Re-detect schema state after v3 migration may have been applied.
        state = _detect_schema_state(conn)
        v3_applied_on_disk = (
            state["stage_id"] and state["artifact_kind"]
        )
        v4_applied_on_disk = state["v4_index"]
        if v3_applied_on_disk and not v4_applied_on_disk:
            # v3 is on disk, v4 not yet applied — apply v4.
            conn.execute("BEGIN IMMEDIATE")
            try:
                _migrate_v4_artifact_multi_frame(conn)
                conn.execute(
                    "INSERT OR IGNORE INTO task_migrations (version, applied_at) VALUES (?, ?)",
                    (4, datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        elif v4_applied_on_disk:
            # v4 already on disk, just record it.
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO task_migrations (version, applied_at) VALUES (?, ?)",
                    (4, datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        else:
            # Neither v3 nor v4 applied — this is a contradiction.
            # v4 depends on v3 columns; we should have applied v3 above.
            # If we get here, something is deeply wrong.
            raise RuntimeError(
                "Schema contradiction: migration v4 required but v3 columns "
                "(stage_id, artifact_kind) are not present on task_artifacts. "
                "Manual intervention required."
            )


def _drop_index_if_exists(conn: sqlite3.Connection, index_name: str) -> None:
    """Drop an index if it exists (safe to call on new databases)."""
    conn.execute(f"DROP INDEX IF EXISTS {index_name}")


def _migrate_v3_artifact_identity(conn: sqlite3.Connection) -> None:
    """Add stage_id, artifact_kind columns (v3 migration).

    These are added via ALTER TABLE so existing rows are preserved.
    Note: SQLite ALTER TABLE ADD COLUMN does not reliably enforce
    REFERENCES constraints, so we skip the FOREIGN KEY clause and rely
    on application-level validation in ``insert_artifact_dedup`` and
    ``complete_stage_with_artifacts``.
    """
    art_cols = {r[1] for r in conn.execute("PRAGMA table_info(task_artifacts)").fetchall()}
    if "stage_id" not in art_cols:
        conn.execute("ALTER TABLE task_artifacts ADD COLUMN stage_id TEXT")
    if "artifact_kind" not in art_cols:
        conn.execute("ALTER TABLE task_artifacts ADD COLUMN artifact_kind TEXT NOT NULL DEFAULT 'generic'")

    # Unique constraint: same stage cannot produce the same kind+clip twice.
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_artifact_stage_kind_clip
           ON task_artifacts(stage_id, artifact_kind, COALESCE(clip_id, ''))
           WHERE stage_id IS NOT NULL"""
    )

    # Index for the resolver: lookup by video_id + stage_name + kind + clip.
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_task_artifacts_lookup
           ON task_artifacts(video_id, stage_name, artifact_kind, clip_id)"""
    )


def _migrate_v4_artifact_multi_frame(conn: sqlite3.Connection) -> None:
    """Drop the overly restrictive unique index on (stage_id, kind, clip_id).

    The v3 index ``uq_artifact_stage_kind_clip`` prevented multiple
    artifacts of the same kind from the same stage (e.g. sample frames).
    Replace it with a unique index on (stage_id, artifact_kind,
    COALESCE(clip_id, ''), path) that allows multiple files per stage.
    The primary key ``artifact_id`` already guarantees global uniqueness.
    """
    _drop_index_if_exists(conn, "uq_artifact_stage_kind_clip")

    # New unique constraint: allows multiple files of same kind+clip from
    # the same stage as long as they have different paths.
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_artifact_stage_identity
           ON task_artifacts(stage_id, artifact_kind, COALESCE(clip_id, ''), path)
           WHERE stage_id IS NOT NULL AND stage_id != ''"""
    )

    # Keep the lookup index (might already exist from v3 migration).
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_task_artifacts_lookup
           ON task_artifacts(video_id, stage_name, artifact_kind, clip_id)"""
    )


def connect_task_db(path: str | Path | None = None) -> sqlite3.Connection:
    if path is None:
        path = os.environ.get("GIFAGENT_TASK_DB", DEFAULT_DB_PATH)
    path = str(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    apply_task_schema(conn)
    return conn

"""Phase 0: Schema reopen test — v4 database must survive close+reopen
without hitting UNIQUE constraint on uq_artifact_stage_kind_clip.

This test MUST fail before the Phase 1 schema fix, because the current
_migrate_task_schema() runs v3 (creating uq_artifact_stage_kind_clip) then
v4 (dropping it and creating uq_artifact_stage_identity). When the DB is
reopened, v3 tries to create the old index again — but now there are
multiple sample_frames with same (stage_id, kind, NULL clip), which
violates the old unique constraint.  The error is:

    UNIQUE constraint failed: index 'uq_artifact_stage_kind_clip'
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def test_v4_db_survives_reopen_with_multi_frame_artifacts(tmp_path: Path) -> None:
    """Create a v4 DB with 2 sample_frames, close, reopen — must succeed."""
    from app.task_engine.schema import connect_task_db

    db_path = tmp_path / "task.db"

    # 1. Create temp task DB.
    conn1 = connect_task_db(db_path)

    # 2. Create a sample stage (need job + video + stage first).
    import json
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    conn1.execute(
        "INSERT INTO task_jobs (job_id, directory, directory_key, config_json, status, created_at, updated_at) "
        "VALUES ('j1', '/tmp/d', '/tmp/d', '{}', 'running', ?, ?)",
        (now, now),
    )
    conn1.execute(
        "INSERT INTO task_videos (video_id, job_id, path, fingerprint, status, created_at, updated_at) "
        "VALUES ('v1', 'j1', '/tmp/v.mp4', 'fp', 'running', ?, ?)",
        (now, now),
    )
    stage_id = "s-sample-001"
    conn1.execute(
        "INSERT INTO task_stages (stage_id, video_id, stage_name, clip_id, input_key, status, created_at, updated_at) "
        "VALUES (?, 'v1', 'sample', NULL, 'key1', 'succeeded', ?, ?)",
        (stage_id, now, now),
    )
    # v4 migration is already recorded by apply_task_schema() during connect_task_db().
    # Verify it exists.
    mig = conn1.execute(
        "SELECT version FROM task_migrations WHERE version=4"
    ).fetchone()
    assert mig is not None, "v4 migration should be recorded by apply_task_schema"

    # 3. Insert two different sample_frames.
    from app.task_engine.artifacts import make_artifact_id

    frame1_path = str(tmp_path / "frame1.jpg")
    (tmp_path / "frame1.jpg").write_text("fake-image-data-1")
    art_id_1 = make_artifact_id(
        stage_id=stage_id,
        artifact_kind="sample_frames",
        clip_id=None,
        normalized_path=frame1_path,
    )
    conn1.execute(
        """INSERT INTO task_artifacts
           (artifact_id, job_id, video_id, stage_name, clip_id,
            path, sha256, size_bytes, provenance_json, created_at,
            stage_id, artifact_kind)
           VALUES (?, 'j1', 'v1', 'sample', NULL, ?, 'aabb', 100, '{}', ?, ?, 'sample_frames')""",
        (art_id_1, frame1_path, now, stage_id),
    )

    frame2_path = str(tmp_path / "frame2.jpg")
    (tmp_path / "frame2.jpg").write_text("fake-image-data-2")
    art_id_2 = make_artifact_id(
        stage_id=stage_id,
        artifact_kind="sample_frames",
        clip_id=None,
        normalized_path=frame2_path,
    )
    conn1.execute(
        """INSERT INTO task_artifacts
           (artifact_id, job_id, video_id, stage_name, clip_id,
            path, sha256, size_bytes, provenance_json, created_at,
            stage_id, artifact_kind)
           VALUES (?, 'j1', 'v1', 'sample', NULL, ?, 'bbcc', 200, '{}', ?, ?, 'sample_frames')""",
        (art_id_2, frame2_path, now, stage_id),
    )
    conn1.commit()
    conn1.close()

    # 4. Close and reopen.
    conn2 = connect_task_db(db_path)

    # 5. Assert connection succeeded, both artifacts survive.
    rows = conn2.execute(
        "SELECT artifact_id, path FROM task_artifacts WHERE stage_name='sample' AND artifact_kind='sample_frames'"
    ).fetchall()
    assert len(rows) == 2, f"Expected 2 sample_frames, got {len(rows)}"
    paths = {r["path"] for r in rows}
    assert frame1_path in paths
    assert frame2_path in paths

    conn2.close()

    # 6. Open a third time to confirm repeatability.
    conn3 = connect_task_db(db_path)
    rows3 = conn3.execute(
        "SELECT artifact_id, path FROM task_artifacts WHERE stage_name='sample' AND artifact_kind='sample_frames'"
    ).fetchall()
    assert len(rows3) == 2
    conn3.close()


def test_v4_db_old_index_dropped_new_index_exists(tmp_path: Path) -> None:
    """After migration, uq_artifact_stage_kind_clip must NOT exist,
    and uq_artifact_stage_identity MUST exist."""
    from app.task_engine.schema import connect_task_db

    db_path = tmp_path / "task.db"
    conn = connect_task_db(db_path)

    indexes = {
        r[1]
        for r in conn.execute(
            "SELECT * FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }

    assert (
        "uq_artifact_stage_kind_clip" not in indexes
    ), "Old v3 unique index must be dropped"
    assert (
        "uq_artifact_stage_identity" in indexes
    ), "New v4 unique index must exist"
    assert (
        "idx_task_artifacts_lookup" in indexes
    ), "Lookup index must exist"

    conn.close()


def test_v4_db_integrity_passes(tmp_path: Path) -> None:
    """PRAGMA integrity_check returns 'ok' after migration."""
    from app.task_engine.schema import connect_task_db

    db_path = tmp_path / "task.db"
    conn = connect_task_db(db_path)

    result = conn.execute("PRAGMA integrity_check").fetchone()
    assert result is not None
    assert result[0] == "ok", f"Integrity check failed: {result[0]}"

    conn.close()


def test_v4_db_migrations_table_has_max_one_per_version(tmp_path: Path) -> None:
    """task_migrations has at most one row per version."""
    from app.task_engine.schema import connect_task_db

    db_path = tmp_path / "task.db"
    conn = connect_task_db(db_path)

    counts = conn.execute(
        "SELECT version, COUNT(*) as cnt FROM task_migrations GROUP BY version"
    ).fetchall()
    for row in counts:
        assert row["cnt"] == 1, (
            f"Version {row['version']} has {row['cnt']} entries (expected 1)"
        )

    conn.close()


def test_v3_db_migration_3_only_applies_v4_on_reopen(tmp_path: Path) -> None:
    """migration table only has 3, schema actually at v3: v4 should be applied on reopen."""
    from app.task_engine.schema import connect_task_db

    db_path = tmp_path / "task.db"

    # Create a DB with only v3 migration recorded and v3 schema on disk.
    conn1 = sqlite3.connect(str(db_path), timeout=30)
    conn1.execute("PRAGMA journal_mode=WAL")
    conn1.execute("PRAGMA busy_timeout=30000")
    conn1.execute("PRAGMA foreign_keys=ON")
    conn1.row_factory = sqlite3.Row

    # Create tables manually (as if v3 was applied without v4)
    from app.task_engine.schema import _DDL
    conn1.executescript(_DDL)

    # Manually apply v3: add columns and old index, record migration 3 only.
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn1.execute("ALTER TABLE task_artifacts ADD COLUMN stage_id TEXT")
    conn1.execute("ALTER TABLE task_artifacts ADD COLUMN artifact_kind TEXT NOT NULL DEFAULT 'generic'")
    conn1.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_artifact_stage_kind_clip
           ON task_artifacts(stage_id, artifact_kind, COALESCE(clip_id, ''))
           WHERE stage_id IS NOT NULL"""
    )
    conn1.execute(
        """CREATE INDEX IF NOT EXISTS idx_task_artifacts_lookup
           ON task_artifacts(video_id, stage_name, artifact_kind, clip_id)"""
    )
    conn1.execute(
        "INSERT OR IGNORE INTO task_migrations (version, applied_at) VALUES (?, ?)",
        (3, now),
    )
    conn1.commit()
    conn1.close()

    # Reopen with schema migration — should apply v4, not re-run v3.
    conn2 = connect_task_db(db_path)

    # Verify v3 index was dropped and v4 index created.
    indexes = {
        r[1]
        for r in conn2.execute(
            "SELECT * FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "uq_artifact_stage_kind_clip" not in indexes, "v3 index should be dropped"
    assert "uq_artifact_stage_identity" in indexes, "v4 index should exist"

    # Verify both migration 3 and 4 are recorded.
    versions = {
        r["version"] for r in conn2.execute(
            "SELECT version FROM task_migrations"
        ).fetchall()
    }
    assert 3 in versions
    assert 4 in versions

    conn2.close()


def test_no_migrations_but_v3_columns_exist_upgrades_safely(tmp_path: Path) -> None:
    """migration table is empty but v3 columns already exist: safe upgrade to v4."""
    from app.task_engine.schema import connect_task_db

    db_path = tmp_path / "task.db"

    conn1 = sqlite3.connect(str(db_path), timeout=30)
    conn1.execute("PRAGMA journal_mode=WAL")
    conn1.execute("PRAGMA busy_timeout=30000")
    conn1.execute("PRAGMA foreign_keys=ON")
    conn1.row_factory = sqlite3.Row

    from app.task_engine.schema import _DDL
    conn1.executescript(_DDL)

    # Add v3 columns but NO indexes and NO migration records.
    conn1.execute("ALTER TABLE task_artifacts ADD COLUMN stage_id TEXT")
    conn1.execute("ALTER TABLE task_artifacts ADD COLUMN artifact_kind TEXT NOT NULL DEFAULT 'generic'")
    conn1.commit()
    conn1.close()

    # Reopen — should handle gracefully.
    conn2 = connect_task_db(db_path)

    # Verify both migrations are recorded.
    versions = {
        r["version"] for r in conn2.execute(
            "SELECT version FROM task_migrations"
        ).fetchall()
    }
    assert 3 in versions, "Migration 3 should be recorded"
    assert 4 in versions, "Migration 4 should be recorded"

    indexes = {
        r[1]
        for r in conn2.execute(
            "SELECT * FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "uq_artifact_stage_identity" in indexes, "v4 index should exist"

    conn2.close()


def test_triple_reopen_idempotent(tmp_path: Path) -> None:
    """Opening a migrated DB three times must be idempotent — no errors,
    no duplicate migration records."""
    from app.task_engine.schema import connect_task_db

    db_path = tmp_path / "task.db"

    # First open
    conn1 = connect_task_db(db_path)
    conn1.close()

    # Second open
    conn2 = connect_task_db(db_path)
    conn2.close()

    # Third open — must not crash
    conn3 = connect_task_db(db_path)

    # Verify only one record per version
    counts = conn3.execute(
        "SELECT version, COUNT(*) as cnt FROM task_migrations GROUP BY version"
    ).fetchall()
    for row in counts:
        assert row["cnt"] == 1, (
            f"Version {row['version']} has {row['cnt']} entries (expected 1)"
        )

    # Verify integrity
    result = conn3.execute("PRAGMA integrity_check").fetchone()
    assert result is not None and result[0] == "ok"

    conn3.close()


def test_foreign_key_check_passes_after_migration(tmp_path: Path) -> None:
    """PRAGMA foreign_key_check returns no rows after migration."""
    from app.task_engine.schema import connect_task_db

    db_path = tmp_path / "task.db"
    conn = connect_task_db(db_path)

    # Add valid data to test FK consistency.
    now = "2026-07-18T00:00:00.000+00:00"
    conn.execute(
        "INSERT INTO task_jobs (job_id, directory, directory_key, config_json, status, created_at, updated_at) "
        "VALUES ('j1', '/tmp/d', '/tmp/d', '{}', 'running', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO task_videos (video_id, job_id, path, fingerprint, status, created_at, updated_at) "
        "VALUES ('v1', 'j1', '/tmp/v.mp4', 'fp', 'running', ?, ?)",
        (now, now),
    )
    conn.commit()

    fk_issues = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert len(fk_issues) == 0, f"Foreign key violations: {fk_issues}"

    result = conn.execute("PRAGMA integrity_check").fetchone()
    assert result is not None and result[0] == "ok"

    conn.close()

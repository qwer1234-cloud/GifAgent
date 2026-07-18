from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = "data/quality_lab.db"
SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS benchmark_manifests (
    manifest_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    item_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS benchmark_items (
    item_id TEXT PRIMARY KEY,
    manifest_id TEXT NOT NULL REFERENCES benchmark_manifests(manifest_id),
    source_path TEXT NOT NULL,
    video_fingerprint TEXT NOT NULL,
    duration_bucket TEXT NOT NULL,
    resolution_bucket TEXT NOT NULL,
    pace_bucket TEXT NOT NULL,
    difficulty_tags TEXT NOT NULL DEFAULT '',
    split TEXT NOT NULL CHECK(split IN ('tune','holdout'))
);

CREATE TABLE IF NOT EXISTS experiment_configs (
    config_id TEXT PRIMARY KEY,
    config_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_runs (
    run_id TEXT PRIMARY KEY,
    manifest_id TEXT NOT NULL REFERENCES benchmark_manifests(manifest_id),
    config_id TEXT NOT NULL REFERENCES experiment_configs(config_id),
    split TEXT NOT NULL CHECK(split IN ('tune','holdout')),
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_items (
    item_id TEXT NOT NULL REFERENCES benchmark_items(item_id),
    run_id TEXT NOT NULL REFERENCES experiment_runs(run_id),
    task_job_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    wall_time_seconds REAL,
    vlm_calls INTEGER,
    token_count INTEGER,
    artifact_bytes INTEGER,
    candidate_count INTEGER,
    failure_info TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (item_id, run_id)
);

CREATE TABLE IF NOT EXISTS metric_values (
    metric_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES experiment_runs(run_id),
    metric_name TEXT NOT NULL,
    value REAL NOT NULL,
    item_id TEXT REFERENCES benchmark_items(item_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ab_sessions (
    session_id TEXT PRIMARY KEY,
    run_a TEXT NOT NULL REFERENCES experiment_runs(run_id),
    run_b TEXT NOT NULL REFERENCES experiment_runs(run_id),
    seed INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ab_judgments (
    judgment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES ab_sessions(session_id),
    pair_index INTEGER NOT NULL,
    choice TEXT NOT NULL CHECK(choice IN ('left','right','tie','both_bad')),
    created_at TEXT NOT NULL,
    UNIQUE(session_id, pair_index)
);

CREATE TABLE IF NOT EXISTS ab_pairs (
    pair_index INTEGER NOT NULL,
    session_id TEXT NOT NULL REFERENCES ab_sessions(session_id),
    item_a_id TEXT NOT NULL,
    item_b_id TEXT NOT NULL,
    left_token TEXT NOT NULL,
    right_token TEXT NOT NULL,
    left_is_run_a INTEGER NOT NULL,
    PRIMARY KEY (session_id, pair_index)
);

CREATE TABLE IF NOT EXISTS champion_history (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_id TEXT NOT NULL REFERENCES experiment_configs(config_id),
    action TEXT NOT NULL CHECK(action IN ('promote','rollback')),
    previous_config_id TEXT,
    scorecard_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quality_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_benchmark_items_manifest
ON benchmark_items(manifest_id);

CREATE INDEX IF NOT EXISTS idx_experiment_runs_manifest
ON experiment_runs(manifest_id);

CREATE INDEX IF NOT EXISTS idx_experiment_runs_config
ON experiment_runs(config_id);

CREATE INDEX IF NOT EXISTS idx_experiment_items_run
ON experiment_items(run_id);

CREATE INDEX IF NOT EXISTS idx_metric_values_run
ON metric_values(run_id);

CREATE INDEX IF NOT EXISTS idx_ab_judgments_session
ON ab_judgments(session_id);
"""


def apply_quality_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO quality_migrations (version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def connect_quality_db(path: str | Path | None = None) -> sqlite3.Connection:
    if path is None:
        path = os.environ.get("GIFAGENT_QUALITY_DB", DEFAULT_DB_PATH)
    path = str(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    apply_quality_schema(conn)
    return conn

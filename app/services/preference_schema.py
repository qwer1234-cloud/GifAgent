from __future__ import annotations

import sqlite3


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

        CREATE TABLE IF NOT EXISTS preference_events (
            event_id TEXT PRIMARY KEY,
            target_type TEXT NOT NULL CHECK(target_type IN ('media','candidate_gif')),
            target_id TEXT NOT NULL,
            rating TEXT NOT NULL CHECK(rating IN ('like','neutral','dislike','quality_reject','skip')),
            source_video_sha256 TEXT NOT NULL,
            scenario_keys_json TEXT NOT NULL DEFAULT '[]',
            corrected_tags_json TEXT,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    conn.commit()

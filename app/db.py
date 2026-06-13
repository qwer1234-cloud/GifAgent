import sqlite3
import os
from app.config import get

DB_PATH = get("database.path", "data/library.db")

def get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS media (
            media_id TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            media_type TEXT NOT NULL CHECK(media_type IN ('image','gif','video')),
            film TEXT,
            sha256 TEXT UNIQUE,
            phash TEXT,
            width INTEGER,
            height INTEGER,
            duration REAL,
            frame_count INTEGER,
            created_at TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS frames (
            frame_id TEXT PRIMARY KEY,
            media_id TEXT NOT NULL,
            frame_path TEXT NOT NULL,
            frame_index INTEGER,
            timestamp REAL,
            width INTEGER,
            height INTEGER,
            vlm_status TEXT DEFAULT 'pending'
                CHECK(vlm_status IN ('pending','vlm_processing','text_inferring','done','failed')),
            FOREIGN KEY(media_id) REFERENCES media(media_id)
        );

        CREATE TABLE IF NOT EXISTS annotations (
            annotation_id TEXT PRIMARY KEY,
            media_id TEXT NOT NULL,
            model_name TEXT,
            summary TEXT,
            emotional_core TEXT,
            aesthetic_notes_json TEXT,
            why_i_like_it TEXT,
            tags_json TEXT,
            scene_type TEXT,
            raw_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(media_id) REFERENCES media(media_id)
        );

        CREATE TABLE IF NOT EXISTS frame_annotations (
            annotation_id TEXT PRIMARY KEY,
            frame_id TEXT NOT NULL,
            media_id TEXT NOT NULL,
            model_name TEXT,
            caption TEXT,
            emotional_core TEXT,
            aesthetic_notes_json TEXT,
            why_i_like_it TEXT,
            raw_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(frame_id) REFERENCES frames(frame_id),
            FOREIGN KEY(media_id) REFERENCES media(media_id)
        );

        CREATE TABLE IF NOT EXISTS feedback (
            feedback_id TEXT PRIMARY KEY,
            media_id TEXT NOT NULL,
            user_rating TEXT CHECK(user_rating IN ('like','dislike','neutral')),
            corrected_tags_json TEXT,
            favorite_reason TEXT,
            reviewed_at TEXT NOT NULL,
            FOREIGN KEY(media_id) REFERENCES media(media_id)
        );

        CREATE TABLE IF NOT EXISTS vector_refs (
            vector_id TEXT PRIMARY KEY,
            owner_type TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            vector_type TEXT NOT NULL,
            index_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS video_clips (
            clip_id TEXT PRIMARY KEY,
            video_id TEXT NOT NULL,
            start REAL NOT NULL,
            end REAL NOT NULL,
            duration REAL NOT NULL,
            keyframes_json TEXT,
            score_json TEXT,
            status TEXT DEFAULT 'candidate'
                CHECK(status IN ('candidate','approved','rejected','exported')),
            exported_path TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(video_id) REFERENCES media(media_id)
        );

        CREATE INDEX IF NOT EXISTS idx_media_sha256 ON media(sha256);
        CREATE INDEX IF NOT EXISTS idx_media_phash ON media(phash);
        CREATE INDEX IF NOT EXISTS idx_media_film ON media(film);
        CREATE INDEX IF NOT EXISTS idx_frames_media ON frames(media_id);
        CREATE INDEX IF NOT EXISTS idx_frames_status ON frames(vlm_status);
        CREATE INDEX IF NOT EXISTS idx_annotations_media ON annotations(media_id);
        CREATE INDEX IF NOT EXISTS idx_feedback_media ON feedback(media_id);
    ''')
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")

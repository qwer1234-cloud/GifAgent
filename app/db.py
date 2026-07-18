import sqlite3
import os
from app.config import get

DB_PATH = get("database.path", "data/library.db")

def get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn

def init_db(apply_preference: bool = False):
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
            cluster_id TEXT,
            is_representative INTEGER DEFAULT 0,
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

        CREATE TABLE IF NOT EXISTS processing_checkpoint (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phase TEXT NOT NULL,
            last_media_id TEXT,
            batch_index INTEGER DEFAULT 0,
            total_processed INTEGER DEFAULT 0,
            total_failed INTEGER DEFAULT 0,
            extra_json TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_media_sha256 ON media(sha256);
        CREATE INDEX IF NOT EXISTS idx_media_phash ON media(phash);
        CREATE INDEX IF NOT EXISTS idx_media_film ON media(film);
        CREATE INDEX IF NOT EXISTS idx_frames_media ON frames(media_id);
        CREATE INDEX IF NOT EXISTS idx_frames_status ON frames(vlm_status);
        CREATE INDEX IF NOT EXISTS idx_annotations_media ON annotations(media_id);
        CREATE INDEX IF NOT EXISTS idx_feedback_media ON feedback(media_id);
        CREATE INDEX IF NOT EXISTS idx_frame_annotations_frame ON frame_annotations(frame_id);
        CREATE INDEX IF NOT EXISTS idx_frame_annotations_media ON frame_annotations(media_id);
        CREATE INDEX IF NOT EXISTS idx_video_clips_video ON video_clips(video_id);
        CREATE INDEX IF NOT EXISTS idx_vector_refs_owner ON vector_refs(owner_type, owner_id);
        CREATE INDEX IF NOT EXISTS idx_checkpoint_phase ON processing_checkpoint(phase);
    ''')
    # Migrate existing tables that may lack new columns
    _migrate(conn)
    if apply_preference:
        from app.services.preference_schema import apply_preference_schema

        apply_preference_schema(conn)
    conn.close()

def _migrate(conn):
    """Add columns missing from older schema versions, then add their indexes."""
    # media
    m_cols = {r[1] for r in conn.execute("PRAGMA table_info(media)").fetchall()}
    for col, ddl in [("cluster_id", "TEXT"), ("is_representative", "INTEGER DEFAULT 0")]:
        if col not in m_cols:
            conn.execute(f"ALTER TABLE media ADD COLUMN {col} {ddl}")

    # frames
    f_cols = {r[1] for r in conn.execute("PRAGMA table_info(frames)").fetchall()}
    for col, ddl in [("vlm_attempts", "INTEGER DEFAULT 0"), ("vlm_error", "TEXT")]:
        if col not in f_cols:
            conn.execute(f"ALTER TABLE frames ADD COLUMN {col} {ddl}")

    # frame_annotations
    fa_cols = {r[1] for r in conn.execute("PRAGMA table_info(frame_annotations)").fetchall()}
    for col, ddl in [("quality_status", "TEXT DEFAULT 'unchecked'"), ("quality_errors_json", "TEXT")]:
        if col not in fa_cols:
            conn.execute(f"ALTER TABLE frame_annotations ADD COLUMN {col} {ddl}")

    # annotations
    a_cols = {r[1] for r in conn.execute("PRAGMA table_info(annotations)").fetchall()}
    for col, ddl in [("quality_status", "TEXT DEFAULT 'unchecked'"), ("quality_errors_json", "TEXT")]:
        if col not in a_cols:
            conn.execute(f"ALTER TABLE annotations ADD COLUMN {col} {ddl}")

    # vector_refs
    vr_cols = {r[1] for r in conn.execute("PRAGMA table_info(vector_refs)").fetchall()}
    for col, ddl in [("embedding_model", "TEXT"), ("embedding_dim", "INTEGER"), ("source_hash", "TEXT")]:
        if col not in vr_cols:
            conn.execute(f"ALTER TABLE vector_refs ADD COLUMN {col} {ddl}")

    conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_cluster ON media(cluster_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_representative ON media(is_representative)")
    conn.commit()

def save_checkpoint(phase: str, last_media_id: str = "", batch_index: int = 0,
                    total_processed: int = 0, total_failed: int = 0, extra=None):
    import json
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    extra_json = json.dumps(extra) if extra else None
    conn = get_connection()
    conn.execute(
        """INSERT INTO processing_checkpoint (phase, last_media_id, batch_index, total_processed, total_failed, extra_json, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (phase, last_media_id, batch_index, total_processed, total_failed, extra_json, now),
    )
    conn.commit()

def load_checkpoint(phase: str):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM processing_checkpoint WHERE phase=? ORDER BY id DESC LIMIT 1",
        (phase,),
    ).fetchone()
    if row is None:
        return None
    import json
    result = dict(row)
    if result.get("extra_json"):
        result["extra"] = json.loads(result["extra_json"])
    return result

if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")

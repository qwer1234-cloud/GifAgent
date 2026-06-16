import os
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from PIL import Image

from app.db import get_connection
from app.config import get


def extract_gif_frames(media_id: str) -> list[dict]:
    """Extract keyframes from a GIF and save to data/frames/. Returns list of frame info dicts."""
    conn = get_connection()
    row = conn.execute("SELECT file_path, duration, frame_count FROM media WHERE media_id=?", (media_id,)).fetchone()
    if not row or row["frame_count"] is None:
        return []

    file_path = row["file_path"]
    total_frames = row["frame_count"]
    sample_count = min(get("media.gif_max_sample_frames", 12), total_frames)
    sample_count = max(get("media.gif_sample_frames", 8), sample_count)

    frames_dir = get("paths.frames_dir", "data/frames")
    os.makedirs(frames_dir, exist_ok=True)

    prefix = f"{frames_dir}/{media_id}"
    cmd = [
        "ffmpeg", "-y", "-i", file_path,
        "-vf", f"fps=2,scale=640:-1",
        f"{prefix}_frame_%06d.jpg",
    ]
    subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=60)

    # Discover generated frames
    frame_files = sorted(Path(frames_dir).glob(f"{media_id}_frame_*.jpg"))
    frames = []
    duration = row["duration"] or 0
    for idx, fp in enumerate(frame_files):
        frame_id = f"frame_{uuid.uuid4().hex[:12]}"
        frame_path = str(fp)
        try:
            img = Image.open(frame_path)
            w, h = img.size
            img.close()
        except Exception:
            w, h = None, None

        ts = (idx / len(frame_files)) * duration if duration else None

        conn.execute(
            """INSERT INTO frames (frame_id, media_id, frame_path, frame_index, timestamp, width, height)
               VALUES (?,?,?,?,?,?,?)""",
            (frame_id, media_id, frame_path, idx + 1, ts, w, h),
        )
        frames.append({"frame_id": frame_id, "frame_path": frame_path, "frame_index": idx + 1, "timestamp": ts})
    conn.commit()
    return frames


def generate_thumbnail(media_id: str) -> Optional[str]:
    """Generate a 320px thumbnail for preview. Returns path or None."""
    conn = get_connection()
    row = conn.execute("SELECT file_path, media_type FROM media WHERE media_id=?", (media_id,)).fetchone()
    if not row:
        return None

    thumbs_dir = get("paths.thumbs_dir", "data/thumbs")
    os.makedirs(thumbs_dir, exist_ok=True)
    thumb_path = f"{thumbs_dir}/{media_id}_thumb.jpg"

    if row["media_type"] == "gif":
        cmd = ["ffmpeg", "-y", "-i", row["file_path"], "-vf", "scale=320:-1", "-vframes", "1", thumb_path]
    else:
        cmd = ["ffmpeg", "-y", "-i", row["file_path"], "-vf", "scale=320:-1", thumb_path]
    result = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    if result.returncode == 0 and os.path.exists(thumb_path):
        return thumb_path
    return None


def preprocess_all(progress_callback=None) -> dict:
    """Extract frames for all unprocessed GIFs. Returns stats."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT media_id FROM media WHERE media_type='gif' AND media_id NOT IN "
        "(SELECT DISTINCT media_id FROM frames)"
    ).fetchall()

    stats = {"total": len(rows), "processed": 0, "frames_extracted": 0, "failed": 0}
    for i, row in enumerate(rows):
        media_id = row["media_id"]
        try:
            frames = extract_gif_frames(media_id)
            stats["processed"] += 1
            stats["frames_extracted"] += len(frames)
            if progress_callback:
                progress_callback(i, len(rows), media_id, "done")
        except Exception as e:
            stats["failed"] += 1
            if progress_callback:
                progress_callback(i, len(rows), media_id, f"failed: {e}")
    return stats


def get_pending_frame_count() -> int:
    conn = get_connection()
    cur = conn.execute("SELECT COUNT(*) as cnt FROM frames WHERE vlm_status='pending'")
    return cur.fetchone()["cnt"]

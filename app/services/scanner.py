import hashlib
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List

from PIL import Image
import imagehash

from app.db import get_connection
from app.config import get

MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mkv", ".webm", ".mov", ".avi"}


def compute_sha256(file_path: str) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_phash(image_path: str) -> Optional[str]:
    try:
        img = Image.open(image_path)
        return str(imagehash.phash(img))
    except Exception:
        return None


def extract_film_name(file_path: str) -> Optional[str]:
    stem = Path(file_path).stem
    m = re.match(r"^(.+?)[\s_\-\.]+.*$", stem)
    if m:
        name = m.group(1).strip()
        if len(name) >= 2 and not name.isdigit():
            return name
    return stem or None


def get_media_type(ext: str) -> str:
    ext = ext.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return "image"
    elif ext == ".gif":
        return "gif"
    return "video"


def is_sha256_duplicate(sha256: str) -> bool:
    conn = get_connection()
    cur = conn.execute("SELECT 1 FROM media WHERE sha256=?", (sha256,))
    return cur.fetchone() is not None


def is_phash_duplicate(phash: str) -> bool:
    conn = get_connection()
    cur = conn.execute("SELECT media_id, phash FROM media WHERE phash IS NOT NULL")
    threshold = get("dedup.phash_hamming_threshold", 5)
    for row in cur:
        if row["phash"]:
            try:
                existing = imagehash.hex_to_hash(row["phash"])
                current = imagehash.hex_to_hash(phash)
                if abs(existing - current) <= threshold:
                    return True
            except Exception:
                continue
    return False


def scan_directory(root_dir: str) -> List[dict]:
    results = []
    for dirpath, _, filenames in os.walk(root_dir):
        for fname in filenames:
            ext = Path(fname).suffix.lower()
            if ext not in MEDIA_EXTENSIONS:
                continue
            file_path = os.path.join(dirpath, fname)
            results.append({
                "file_path": file_path,
                "ext": ext,
                "media_type": get_media_type(ext),
            })
    return results


def register_media(file_path: str) -> Optional[str]:
    """Register a single media file. Returns media_id or None if skipped."""
    ext = Path(file_path).suffix.lower()
    if ext not in MEDIA_EXTENSIONS:
        return None

    sha256 = compute_sha256(file_path)
    if is_sha256_duplicate(sha256):
        return None

    media_type = get_media_type(ext)
    media_id = f"media_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    film = extract_film_name(file_path)

    phash = None
    width, height = None, None
    duration = None
    frame_count = None

    if media_type in ("image", "gif"):
        try:
            img = Image.open(file_path)
            width, height = img.size
            phash = str(imagehash.phash(img))
            if media_type == "gif":
                frame_count = getattr(img, "n_frames", 0)
                try:
                    duration = img.info.get("duration", 0) / 1000.0
                except Exception:
                    duration = None
            img.close()
        except Exception:
            pass

    if phash and is_phash_duplicate(phash):
        return None

    conn = get_connection()
    conn.execute(
        """INSERT INTO media (media_id, file_path, media_type, film, sha256, phash,
           width, height, duration, frame_count, created_at, indexed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (media_id, file_path, media_type, film, sha256, phash,
         width, height, duration, frame_count,
         datetime.fromtimestamp(os.path.getctime(file_path), tz=timezone.utc).isoformat(),
         now),
    )
    conn.commit()
    return media_id


def scan_and_register(root_dir: str, progress_callback=None) -> dict:
    files = scan_directory(root_dir)
    stats = {"scanned": len(files), "registered": 0, "skipped_sha256": 0, "skipped_phash": 0, "skipped_ext": 0}

    sha256_seen: set = set()
    for i, f in enumerate(files):
        file_path = f["file_path"]
        sha256 = compute_sha256(file_path)
        if sha256 in sha256_seen or is_sha256_duplicate(sha256):
            stats["skipped_sha256"] += 1
            if progress_callback:
                progress_callback(i, len(files), file_path, "skipped_sha256")
            continue
        sha256_seen.add(sha256)

        mid = register_media(file_path)
        if mid:
            stats["registered"] += 1
        else:
            stats["skipped_phash"] += 1

        if progress_callback:
            progress_callback(i, len(files), file_path, "registered" if mid else "skipped_phash")

    return stats

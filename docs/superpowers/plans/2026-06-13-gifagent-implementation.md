# GifAgent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local movie-scene GIF/VLM agent that scans 9000+ film GIFs, auto-labels them for aesthetic quality, indexes them with FAISS, and provides a Gradio review UI.

**Architecture:** Python monolith with FastAPI core, Ollama-backed VLM (llava:13b) for visual understanding and 9B text model for label synthesis. Model scheduler swaps between the two since 16GB VRAM can't hold both. SQLite for metadata, FAISS for vector search, ffmpeg for media processing, Gradio for the review interface.

**Tech Stack:** Python 3.11+, FastAPI, Gradio, SQLite, FAISS, Ollama API, ffmpeg, Pillow, imagehash, watchdog

---

### Task 1: Project Scaffold and Environment

**Files:**
- Create: `app/__init__.py`
- Create: `app/config.py`
- Create: `configs/models.yaml`
- Create: `requirements.txt`
- Create: `scripts/setup.bat`

- [ ] **Step 1: Create project directory structure**

```bash
mkdir -p app/models app/services app/workers app/ui
mkdir -p data/frames data/thumbs data/faiss data/exports
mkdir -p configs docs scripts
```

- [ ] **Step 2: Write requirements.txt**

```text
fastapi==0.115.6
uvicorn[standard]==0.34.0
gradio==5.9.0
Pillow==11.1.0
imagehash==4.3.1
numpy==2.2.0
faiss-cpu==1.9.0
watchdog==6.0.0
httpx==0.28.1
pydantic==2.10.3
python-multipart==0.0.18
opencv-python-headless==4.10.0.84
```

- [ ] **Step 3: Write configs/models.yaml**

```yaml
media:
  source_dir: "E:/data/originals"
  image_max_side: 1344
  gif_sample_frames: 8
  gif_max_sample_frames: 12
  video_sample_fps: 1

vlm:
  provider: "ollama"
  model: "llava:13b"
  base_url: "http://localhost:11434"
  batch_size: 50
  max_batch_size: 100

llm:
  provider: "ollama"
  model: "fredrezones55/Qwen3.5-Uncensored-HauhauCS-Aggressive:9b"
  base_url: "http://localhost:11434"
  require_json: true

embedding:
  provider: "ollama"
  image_model: "llava:13b"
  text_model: "fredrezones55/Qwen3.5-Uncensored-HauhauCS-Aggressive:9b"
  base_url: "http://localhost:11434"

scheduler:
  model_switch_wait: 10
  max_retries: 3

scoring:
  visual_similarity_weight: 0.50
  text_similarity_weight: 0.25
  emotional_match_weight: 0.15
  cluster_confidence_weight: 0.10
  high_match_threshold: 0.80
  review_threshold: 0.60

dedup:
  phash_hamming_threshold: 5

database:
  path: "data/library.db"

paths:
  frames_dir: "data/frames"
  thumbs_dir: "data/thumbs"
  faiss_dir: "data/faiss"
  exports_dir: "data/exports"
```

- [ ] **Step 4: Write app/__init__.py**

```python
"""GifAgent - Local movie-scene GIF auto-tagging and preference agent."""
```

- [ ] **Step 5: Write app/config.py**

```python
import yaml
import os
from pathlib import Path
from typing import Any

_config: dict[str, Any] = {}

def load_config(path: str = "configs/models.yaml") -> dict[str, Any]:
    global _config
    with open(path, "r") as f:
        _config = yaml.safe_load(f)
    return _config

def get(key: str, default: Any = None) -> Any:
    keys = key.split(".")
    val: Any = _config
    for k in keys:
        if isinstance(val, dict):
            val = val.get(k)
        else:
            return default
    return val if val is not None else default

# Auto-load on import
_config_path = os.environ.get("GIFAGENT_CONFIG", "configs/models.yaml")
if Path(_config_path).exists():
    load_config(_config_path)
```

- [ ] **Step 6: Write scripts/setup.bat**

```batch
@echo off
echo === GifAgent Setup ===

echo [1/4] Creating virtual environment...
python -m venv venv
call venv\Scripts\activate.bat

echo [2/4] Installing Python dependencies...
pip install -r requirements.txt

echo [3/4] Verifying ffmpeg...
where ffmpeg >nul 2>&1
if %errorlevel% neq 0 (
    echo ffmpeg not found! Install from https://ffmpeg.org/download.html
    echo Make sure ffmpeg.exe is in PATH
    exit /b 1
)
echo ffmpeg found.

echo [4/4] Pulling llava:13b for visual understanding...
ollama pull llava:13b

echo === Setup complete ===
echo Run: venv\Scripts\activate
echo Then: python app/main.py
```

- [ ] **Step 7: Install dependencies and verify environment**

```bash
python -m venv venv && venv/Scripts/activate && pip install -r requirements.txt
```
Verify: All packages install without error.

- [ ] **Step 8: Commit**

```bash
git add app/__init__.py app/config.py configs/models.yaml requirements.txt scripts/setup.bat
git commit -m "feat: project scaffold with config, requirements, and setup script"
```

---

### Task 2: Database Layer

**Files:**
- Create: `app/db.py`

- [ ] **Step 1: Write app/db.py**

```python
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
```

- [ ] **Step 2: Run db.py to create the database**

```bash
python app/db.py
```
Expected: `Database initialized at data/library.db`

- [ ] **Step 3: Verify schema**

```bash
python -c "from app.db import get_connection; conn=get_connection(); cur=conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\"); print([r[0] for r in cur.fetchall()])"
```
Expected: `['media', 'frames', 'annotations', 'frame_annotations', 'feedback', 'vector_refs', 'video_clips']`

- [ ] **Step 4: Commit**

```bash
git add app/db.py
git commit -m "feat: SQLite database layer with all tables and indexes"
```

---

### Task 3: Media Scanner and Deduplication

**Files:**
- Create: `app/services/__init__.py`
- Create: `app/services/scanner.py`

- [ ] **Step 1: Write app/services/__init__.py**

```python
"""Services for GifAgent."""
```

- [ ] **Step 2: Write app/services/scanner.py**

```python
import hashlib
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image
import imagehash

from app.db import get_connection
from app.config import get

MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mkv", ".webm", ".mov", ".avi"}
HASH_ALG = "sha256"

def compute_sha256(file_path: str) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def compute_phash(image_path: str) -> str | None:
    try:
        img = Image.open(image_path)
        return str(imagehash.phash(img))
    except Exception:
        return None

def extract_film_name(file_path: str) -> str | None:
    stem = Path(file_path).stem
    # Match leading words before a separator: "Film Name" from "Film Name_suffix.gif"
    m = re.match(r"^(.+?)[\s_\-\.]+.*$", stem)
    if m:
        name = m.group(1).strip()
        # Only return if it looks like a title (2+ chars, not pure digits)
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

def scan_directory(root_dir: str) -> list[dict]:
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

def register_media(file_path: str) -> str | None:
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

    # Compute phash for image/gif types
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

    # phash dedup
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
    """Full scan and register. Returns stats dict."""
    files = scan_directory(root_dir)
    stats = {"scanned": len(files), "registered": 0, "skipped_sha256": 0, "skipped_phash": 0}
    sha256_seen: set[str] = set()

    for i, f in enumerate(files):
        file_path = f["file_path"]
        ext = f["ext"]
        media_type = f["media_type"]

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
```

- [ ] **Step 3: Test scanner on a small sample**

Create a test script:

```bash
python -c "
from app.db import init_db; init_db()
from app.services.scanner import scan_and_register
stats = scan_and_register('E:/data/originals', progress_callback=lambda i, total, path, status: print(f'[{i+1}/{total}] {status}: {path}'))
print('Stats:', stats)
"
```
Expected: Files start registering, no crashes.

- [ ] **Step 4: Commit**

```bash
git add app/services/__init__.py app/services/scanner.py
git commit -m "feat: media scanner with sha256+phash dedup and film name extraction"
```

---

### Task 4: Media Preprocessing (GIF Frame Extraction)

**Files:**
- Create: `app/services/preprocess.py`

- [ ] **Step 1: Write app/services/preprocess.py**

```python
import os
import subprocess
import uuid
from pathlib import Path

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
    subprocess.run(cmd, capture_output=True, text=True, timeout=60)

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

        # Approximate timestamp
        ts = (idx / len(frame_files)) * duration if duration else None

        conn.execute(
            """INSERT INTO frames (frame_id, media_id, frame_path, frame_index, timestamp, width, height)
               VALUES (?,?,?,?,?,?,?)""",
            (frame_id, media_id, frame_path, idx + 1, ts, w, h),
        )
        frames.append({"frame_id": frame_id, "frame_path": frame_path, "frame_index": idx + 1, "timestamp": ts})
    conn.commit()
    return frames

def generate_thumbnail(media_id: str) -> str | None:
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
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
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
```

- [ ] **Step 2: Test frame extraction on a single GIF**

```bash
python -c "
from app.db import init_db; init_db()
from app.services.preprocess import extract_gif_frames
frames = extract_gif_frames('media_<replace_with_real_id>')
print(f'Extracted {len(frames)} frames')
"
```

- [ ] **Step 3: Commit**

```bash
git add app/services/preprocess.py
git commit -m "feat: GIF frame extraction and thumbnail generation via ffmpeg"
```

---

### Task 5: Model Scheduler (llava:13b ↔ 9B Swap)

**Files:**
- Create: `app/services/scheduler.py`

- [ ] **Step 1: Write app/services/scheduler.py**

```python
import time
import subprocess
import httpx
from app.config import get

class ModelScheduler:
    """Manages ollama model lifecycle for VLM <-> text model swaps."""

    def __init__(self):
        self.vlm_model = get("vlm.model", "llava:13b")
        self.llm_model = get("llm.model")
        self.base_url = get("vlm.base_url", "http://localhost:11434")
        self.switch_wait = get("scheduler.model_switch_wait", 10)
        self.max_retries = get("scheduler.max_retries", 3)

    def _run_ollama(self, cmd: list[str]) -> tuple[int, str, str]:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return result.returncode, result.stdout, result.stderr

    def _check_model_running(self, model: str) -> bool:
        ret, stdout, _ = self._run_ollama(["ollama", "ps"])
        return model in stdout

    def _wait_for_model(self, model: str, timeout: int = 30) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = httpx.post(
                    f"{self.base_url}/api/generate",
                    json={"model": model, "prompt": "ping", "stream": False},
                    timeout=10,
                )
                if resp.status_code == 200:
                    return True
            except Exception:
                pass
            time.sleep(2)
        return False

    def stop_model(self, model: str) -> bool:
        if not self._check_model_running(model):
            return True
        ret, _, _ = self._run_ollama(["ollama", "stop", model])
        time.sleep(self.switch_wait)
        return ret == 0

    def switch_to_vlm(self) -> bool:
        """Stop LLM, load VLM. Returns True if VLM is ready."""
        self.stop_model(self.llm_model)
        for attempt in range(self.max_retries):
            if self._wait_for_model(self.vlm_model, timeout=10):
                return True
        return False

    def switch_to_llm(self) -> bool:
        """Stop VLM, load LLM. Returns True if LLM is ready."""
        self.stop_model(self.vlm_model)
        for attempt in range(self.max_retries):
            if self._wait_for_model(self.llm_model, timeout=10):
                return True
        return False

    def process_batch(self, frames: list[dict], progress_callback=None) -> list[dict]:
        """
        Full batch processing cycle:
        1. Switch to VLM
        2. Process all frames with llava:13b
        3. Switch to LLM
        4. Synthesize frame annotations with 9B
        Returns list of annotation results.
        """
        batch_size = get("vlm.batch_size", 50)
        results = []

        # Phase 1: VLM frame analysis
        for i in range(0, len(frames), batch_size):
            chunk = frames[i:i + batch_size]

            for attempt in range(self.max_retries):
                try:
                    chunk_results = self._vlm_analyze_frames(chunk)
                    results.extend(chunk_results)
                    break
                except Exception as e:
                    if attempt == self.max_retries - 1:
                        for f in chunk:
                            results.append({"frame_id": f["frame_id"], "error": str(e), "status": "failed"})
                    else:
                        time.sleep(5)

            if progress_callback:
                progress_callback(min(i + batch_size, len(frames)), len(frames))

        return results

    def _vlm_analyze_frames(self, frames: list[dict]) -> list[dict]:
        """Send frames to llava:13b for aesthetic analysis."""
        from app.services.vision import analyze_frame
        results = []
        for f in frames:
            try:
                annotation = analyze_frame(f["frame_id"], f["frame_path"], f.get("media_id", ""))
                results.append({"frame_id": f["frame_id"], "status": "done", "annotation": annotation})
            except Exception as e:
                results.append({"frame_id": f["frame_id"], "error": str(e), "status": "failed"})
        return results

    def process_pending_frames(self, progress_callback=None) -> dict:
        """Process all pending frames through the VLM->LLM pipeline."""
        from app.db import get_connection
        conn = get_connection()
        rows = conn.execute(
            "SELECT f.frame_id, f.frame_path, f.media_id FROM frames f WHERE f.vlm_status='pending'"
        ).fetchall()

        frames = [{"frame_id": r["frame_id"], "frame_path": r["frame_path"], "media_id": r["media_id"]} for r in rows]
        total = len(frames)

        if total == 0:
            return {"total": 0, "processed": 0, "failed": 0}

        # Phase 1: VLM
        if not self.switch_to_vlm():
            raise RuntimeError("Failed to start VLM model")

        batch_size = get("vlm.batch_size", 50)
        vlm_results = []
        for i in range(0, len(frames), batch_size):
            chunk = frames[i:i + batch_size]
            chunk_results = self._vlm_analyze_frames(chunk)
            vlm_results.extend(chunk_results)
            if progress_callback:
                progress_callback(min(i + batch_size, total), total, "vlm")

        # Phase 2: Switch to LLM for synthesis
        if not self.switch_to_llm():
            raise RuntimeError("Failed to start LLM model")

        from app.services.llm import synthesize_media_annotation
        # Group results by media_id for synthesis
        media_frame_map: dict[str, list] = {}
        for r in vlm_results:
            if r["status"] == "done":
                mid = r["annotation"].get("media_id", "") if "annotation" in r else ""
                if mid:
                    media_frame_map.setdefault(mid, []).append(r)

        stats = {"total": total, "processed": 0, "failed": 0}
        for idx, (media_id, frame_results) in enumerate(media_frame_map.items()):
            try:
                synthesize_media_annotation(media_id, frame_results)
                stats["processed"] += len(frame_results)
            except Exception:
                stats["failed"] += len(frame_results)
            if progress_callback:
                progress_callback(idx, len(media_frame_map), "llm_synthesis")

        return stats
```

- [ ] **Step 2: Commit**

```bash
git add app/services/scheduler.py
git commit -m "feat: model scheduler for llava:13b <-> 9B swap with batch processing"
```

---

### Task 6: VLM Vision Service

**Files:**
- Create: `app/services/vision.py`
- Create: `configs/prompts.yaml`

- [ ] **Step 1: Write configs/prompts.yaml**

```yaml
vlm_frame_analysis: |
  You are analyzing individual frames from a movie/TV show GIF.
  Focus on the CINEMATIC and AESTHETIC qualities, not just listing objects.

  Output JSON only, no markdown, no explanation:
  {
    "caption": "concise description of the scene, composition, and what makes it visually striking",
    "emotional_core": "tension | melancholy | awe | joy | sadness | catharsis | serenity | excitement | dread | nostalgia | admiration | other",
    "aesthetic_notes": ["specific cinematic qualities: lighting, color palette, depth of field, framing, texture, movement"],
    "why_i_like_it": "a personal, subjective reason this frame is compelling - think like a cinephile"
  }

llm_media_synthesis: |
  You are an AI that synthesizes frame-by-frame analysis into a cohesive movie scene annotation.

  Film title hint (from filename): {film_name}

  Given multiple frame analyses from the same GIF, output a single JSON only, no markdown:
  {
    "summary": "one cohesive sentence describing the full moment captured in this GIF",
    "emotional_core": "the dominant emotion carried through the scene",
    "aesthetic_notes": ["consolidated list of the most significant cinematic qualities across all frames"],
    "why_i_like_it": "an eloquent, personal reason this moment is worth saving - what makes it cinematically special",
    "tags": ["film_title", "character_name", "actor_name", "scene_type", "notable_keywords"],
    "scene_type": "close-up | dialogue | action | transition | reaction | establishing | montage | other"
  }
```

- [ ] **Step 2: Write app/services/vision.py**

```python
import json
import re
import uuid
from datetime import datetime, timezone

import httpx

from app.db import get_connection
from app.config import get

VLM_BASE = get("vlm.base_url", "http://localhost:11434")
VLM_MODEL = get("vlm.model", "llava:13b")
FRAME_PROMPT = """You are analyzing individual frames from a movie/TV show GIF.
Focus on the CINEMATIC and AESTHETIC qualities, not just listing objects.

Output JSON only, no markdown, no explanation:
{
  "caption": "concise description of the scene, composition, and what makes it visually striking",
  "emotional_core": "tension | melancholy | awe | joy | sadness | catharsis | serenity | excitement | dread | nostalgia | admiration | other",
  "aesthetic_notes": ["specific cinematic qualities: lighting, color palette, depth of field, framing, texture, movement"],
  "why_i_like_it": "a personal, subjective reason this frame is compelling - think like a cinephile"
}"""


def _parse_json_response(text: str) -> dict:
    """Try strict parse, then regex extraction, then fallback."""
    text = text.strip()
    # Remove markdown code fences if present
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to extract first JSON object
    m = re.search(r"\{[^{}]*\{[^{}]*\}[^{}]*\}|\{[^{}]*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    return {"_parse_error": True, "_raw": text[:500]}


def analyze_frame(frame_id: str, image_path: str, media_id: str) -> dict:
    """Call llava:13b to analyze a single frame. Returns annotation dict."""
    with open(image_path, "rb") as f:
        image_bytes = f.read()

    # Ollama generate with image
    resp = httpx.post(
        f"{VLM_BASE}/api/generate",
        json={
            "model": VLM_MODEL,
            "prompt": FRAME_PROMPT,
            "images": [image_bytes.hex()],  # Ollama expects hex-encoded images
            "stream": False,
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    response_text = data.get("response", "")

    parsed = _parse_json_response(response_text)
    if parsed.get("_parse_error"):
        print(f"[WARN] JSON parse failed for frame {frame_id}: {parsed.get('_raw', '')[:200]}")

    annotation_id = f"ann_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()

    conn = get_connection()
    conn.execute(
        """INSERT INTO frame_annotations
           (annotation_id, frame_id, media_id, model_name, caption, emotional_core,
            aesthetic_notes_json, why_i_like_it, raw_json, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            annotation_id,
            frame_id,
            media_id,
            VLM_MODEL,
            parsed.get("caption", ""),
            parsed.get("emotional_core", ""),
            json.dumps(parsed.get("aesthetic_notes", [])),
            parsed.get("why_i_like_it", ""),
            json.dumps(parsed, ensure_ascii=False),
            now,
        ),
    )
    conn.execute("UPDATE frames SET vlm_status='done' WHERE frame_id=?", (frame_id,))
    conn.commit()

    return {**parsed, "annotation_id": annotation_id, "frame_id": frame_id, "media_id": media_id}
```

- [ ] **Step 3: Commit**

```bash
git add app/services/vision.py configs/prompts.yaml
git commit -m "feat: VLM vision service with llava:13b frame analysis and JSON parsing"
```

---

### Task 7: LLM Text Synthesis Service

**Files:**
- Create: `app/services/llm.py`

- [ ] **Step 1: Write app/services/llm.py**

```python
import json
import re
import uuid
from datetime import datetime, timezone

import httpx

from app.db import get_connection
from app.config import get

LLM_BASE = get("llm.base_url", "http://localhost:11434")
LLM_MODEL = get("llm.model")


def _parse_json_response(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[^{}]*\{[^{}]*\}[^{}]*\}|\{[^{}]*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {"_parse_error": True, "_raw": text[:500]}


def build_synthesis_prompt(film_name: str, frame_analyses: list[dict]) -> str:
    analyses_text = "\n\n".join(
        f"Frame {i+1}:\n"
        f"  Caption: {fa.get('caption', '')}\n"
        f"  Emotional core: {fa.get('emotional_core', '')}\n"
        f"  Aesthetic notes: {fa.get('aesthetic_notes', [])}\n"
        f"  Why compelling: {fa.get('why_i_like_it', '')}"
        for i, fa in enumerate(frame_analyses)
    )

    return f"""You are an AI that synthesizes frame-by-frame analysis into a cohesive movie scene annotation.

Film title hint (from filename): {film_name}

Given multiple frame analyses from the same GIF, output a single JSON only, no markdown:
{{
  "summary": "one cohesive sentence describing the full moment captured in this GIF",
  "emotional_core": "the dominant emotion carried through the scene",
  "aesthetic_notes": ["consolidated list of the most significant cinematic qualities across all frames"],
  "why_i_like_it": "an eloquent, personal reason this moment is worth saving - what makes it cinematically special",
  "tags": ["film_title", "character_name", "actor_name", "scene_type", "notable_keywords"],
  "scene_type": "close-up | dialogue | action | transition | reaction | establishing | montage | other"
}}

Frame analyses:
{analyses_text}"""


def synthesize_media_annotation(media_id: str, vlm_results: list[dict]) -> dict:
    """Take VLM frame analyses and synthesize a cohesive media-level annotation using the 9B model."""
    conn = get_connection()
    media = conn.execute("SELECT film FROM media WHERE media_id=?", (media_id,)).fetchone()
    if not media:
        raise ValueError(f"media_id {media_id} not found")

    film_name = media["film"] or "Unknown"

    # Extract frame analyses from vlm_results
    frame_analyses = []
    for r in vlm_results:
        ann = r.get("annotation", {})
        frame_analyses.append(ann)

    if not frame_analyses:
        return {}

    prompt = build_synthesis_prompt(film_name, frame_analyses)

    resp = httpx.post(
        f"{LLM_BASE}/api/generate",
        json={"model": LLM_MODEL, "prompt": prompt, "stream": False},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    response_text = data.get("response", "")

    parsed = _parse_json_response(response_text)
    if parsed.get("_parse_error"):
        print(f"[WARN] JSON parse failed for media {media_id}: {parsed.get('_raw', '')[:200]}")

    annotation_id = f"ann_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """INSERT INTO annotations
           (annotation_id, media_id, model_name, summary, emotional_core,
            aesthetic_notes_json, why_i_like_it, tags_json, scene_type, raw_json, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            annotation_id,
            media_id,
            LLM_MODEL,
            parsed.get("summary", ""),
            parsed.get("emotional_core", ""),
            json.dumps(parsed.get("aesthetic_notes", [])),
            parsed.get("why_i_like_it", ""),
            json.dumps(parsed.get("tags", [])),
            parsed.get("scene_type", ""),
            json.dumps(parsed, ensure_ascii=False),
            now,
        ),
    )
    conn.commit()

    return {**parsed, "annotation_id": annotation_id, "media_id": media_id}
```

- [ ] **Step 2: Commit**

```bash
git add app/services/llm.py
git commit -m "feat: LLM synthesis service for media-level annotation from frame analyses"
```

---

### Task 8: Embedding Service with Ollama

**Files:**
- Create: `app/services/embedding.py`

- [ ] **Step 1: Write app/services/embedding.py**

```python
import json
from typing import Any

import numpy as np
import httpx

from app.db import get_connection
from app.config import get

EMBED_BASE = get("embedding.base_url", "http://localhost:11434")
EMBED_MODEL = get("embedding.text_model")
VLM_MODEL = get("vlm.model", "llava:13b")


def _ollama_embed(text: str, model: str = None) -> list[float]:
    model = model or EMBED_MODEL
    resp = httpx.post(
        f"{EMBED_BASE}/api/embeddings",
        json={"model": model, "prompt": text},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def compute_text_embedding(text: str) -> list[float]:
    """Generate embedding for text using Ollama."""
    return _ollama_embed(text)


def compute_image_embedding(image_path: str) -> list[float] | None:
    """Generate embedding for an image using Ollama (via VLM model's embedding capability).
    Falls back to text embedding of a generic description if image embedding not available."""
    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()

        resp = httpx.post(
            f"{EMBED_BASE}/api/embeddings",
            json={
                "model": EMBED_MODEL,
                "prompt": "Describe this image for embedding purposes",
                "images": [image_bytes.hex()],
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]
    except Exception:
        return None


def compute_media_embedding(media_id: str) -> list[float] | None:
    """Compute aggregated embedding for a media item from its frames."""
    conn = get_connection()
    frame_rows = conn.execute(
        "SELECT frame_path FROM frames WHERE media_id=? ORDER BY frame_index", (media_id,)
    ).fetchall()

    embeddings = []
    for fr in frame_rows:
        emb = compute_image_embedding(fr["frame_path"])
        if emb:
            embeddings.append(emb)

    if not embeddings:
        return None

    # Average pooling
    avg_embedding = np.mean(embeddings, axis=0).tolist()
    return avg_embedding


def compute_text_summary_embedding(media_id: str) -> list[float] | None:
    """Compute text embedding from annotation summary + tags."""
    conn = get_connection()
    row = conn.execute(
        "SELECT summary, emotional_core, tags_json, why_i_like_it FROM annotations WHERE media_id=?",
        (media_id,),
    ).fetchone()

    if not row:
        return None

    text_parts = []
    if row["summary"]:
        text_parts.append(row["summary"])
    if row["emotional_core"]:
        text_parts.append(row["emotional_core"])
    if row["why_i_like_it"]:
        text_parts.append(row["why_i_like_it"])
    if row["tags_json"]:
        try:
            tags = json.loads(row["tags_json"])
            text_parts.extend(tags)
        except json.JSONDecodeError:
            pass

    text = " ".join(text_parts)
    if not text.strip():
        return None

    return _ollama_embed(text)
```

- [ ] **Step 2: Commit**

```bash
git add app/services/embedding.py
git commit -m "feat: embedding service using Ollama API for image and text vectors"
```

---

### Task 9: FAISS Indexing and Similarity Search

**Files:**
- Create: `app/services/indexer.py`

- [ ] **Step 1: Write app/services/indexer.py**

```python
import json
import os
import uuid
from datetime import datetime, timezone

import numpy as np
import faiss

from app.db import get_connection
from app.config import get

FAISS_DIR = get("paths.faiss_dir", "data/faiss")
INDEX_FILE = os.path.join(FAISS_DIR, "media_index.faiss")
ID_MAP_FILE = os.path.join(FAISS_DIR, "id_map.json")


class MediaIndex:
    def __init__(self, dim: int = 768):
        self.dim = dim
        os.makedirs(FAISS_DIR, exist_ok=True)
        if os.path.exists(INDEX_FILE):
            self.index = faiss.read_index(INDEX_FILE)
        else:
            self.index = faiss.IndexFlatIP(self.dim)  # Inner product for cosine similarity

    def _load_id_map(self) -> dict[int, str]:
        if os.path.exists(ID_MAP_FILE):
            with open(ID_MAP_FILE) as f:
                return json.load(f)
        return {}

    def _save_id_map(self, id_map: dict[int, str]):
        with open(ID_MAP_FILE, "w") as f:
            json.dump(id_map, f)

    def add(self, vector: list[float], media_id: str, vector_type: str = "media_global") -> str:
        vec = np.array([vector], dtype=np.float32)
        faiss.normalize_L2(vec)
        idx = self.index.ntotal
        self.index.add(vec)
        faiss.write_index(self.index, INDEX_FILE)

        id_map = self._load_id_map()
        id_map[str(idx)] = media_id
        self._save_id_map(id_map)

        vector_id = f"vec_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection()
        conn.execute(
            "INSERT INTO vector_refs VALUES (?,?,?,?,?,?)",
            (vector_id, "media", media_id, vector_type, "media_index", now),
        )
        conn.commit()
        return vector_id

    def search(self, vector: list[float], top_k: int = 10) -> list[dict]:
        if self.index.ntotal == 0:
            return []

        vec = np.array([vector], dtype=np.float32)
        faiss.normalize_L2(vec)
        distances, indices = self.index.search(vec, min(top_k, self.index.ntotal))

        id_map = self._load_id_map()
        conn = get_connection()
        results = []
        for dist, idx in zip(distances[0], indices[0]):
            if idx < 0:
                continue
            media_id = id_map.get(str(idx))
            if not media_id:
                continue
            row = conn.execute(
                """SELECT m.file_path, m.film, a.summary, a.emotional_core, a.tags_json
                   FROM media m LEFT JOIN annotations a ON m.media_id=a.media_id
                   WHERE m.media_id=?""",
                (media_id,),
            ).fetchone()
            if row:
                results.append({
                    "media_id": media_id,
                    "score": float(dist),
                    "film": row["film"],
                    "summary": row["summary"],
                    "emotional_core": row["emotional_core"],
                    "tags": json.loads(row["tags_json"]) if row["tags_json"] else [],
                    "file_path": row["file_path"],
                })
        return results

    @property
    def count(self) -> int:
        return self.index.ntotal


# Global index instance
_media_index: MediaIndex | None = None


def get_index() -> MediaIndex:
    global _media_index
    if _media_index is None:
        _media_index = MediaIndex()
    return _media_index


def index_all_annotated() -> dict:
    """Build FAISS index from all media that have both annotation and frame embeddings."""
    from app.services.embedding import compute_media_embedding

    conn = get_connection()
    rows = conn.execute(
        """SELECT DISTINCT m.media_id FROM media m
           INNER JOIN annotations a ON m.media_id=a.media_id
           INNER JOIN frames f ON m.media_id=f.media_id
           WHERE m.media_id NOT IN (SELECT owner_id FROM vector_refs WHERE vector_type='media_global')"""
    ).fetchall()

    idx = get_index()
    stats = {"total": len(rows), "indexed": 0, "failed": 0}
    for row in rows:
        try:
            emb = compute_media_embedding(row["media_id"])
            if emb:
                idx.add(emb, row["media_id"], "media_global")
                stats["indexed"] += 1
            else:
                stats["failed"] += 1
        except Exception:
            stats["failed"] += 1
    return stats
```

- [ ] **Step 2: Commit**

```bash
git add app/services/indexer.py
git commit -m "feat: FAISS indexer with cosine similarity search and id mapping"
```

---

### Task 10: Preference Scoring Service

**Files:**
- Create: `app/services/scorer.py`

- [ ] **Step 1: Write app/services/scorer.py**

```python
import json

from app.db import get_connection
from app.config import get
from app.services.indexer import get_index
from app.services.embedding import compute_media_embedding


VISUAL_W = get("scoring.visual_similarity_weight", 0.50)
TEXT_W = get("scoring.text_similarity_weight", 0.25)
EMOTION_W = get("scoring.emotional_match_weight", 0.15)
CLUSTER_W = get("scoring.cluster_confidence_weight", 0.10)
HIGH_THRESHOLD = get("scoring.high_match_threshold", 0.80)
REVIEW_THRESHOLD = get("scoring.review_threshold", 0.60)


def score_media(media_id: str) -> dict | None:
    """Score a single media item against the preference index."""
    idx = get_index()
    if idx.count == 0:
        return {"media_id": media_id, "final_score": 0.0, "decision": "unknown", "needs_review": True}

    emb = compute_media_embedding(media_id)
    if not emb:
        return None

    similar = idx.search(emb, top_k=10)

    # visual_similarity from top match
    visual_similarity = similar[0]["score"] if similar else 0.0

    # text_similarity: compare tags/emotional_core overlap
    conn = get_connection()
    annotation = conn.execute("SELECT emotional_core, tags_json FROM annotations WHERE media_id=?", (media_id,)).fetchone()
    text_similarity = 0.0
    if annotation and annotation["emotional_core"] and similar:
        query_emo = annotation["emotional_core"]
        for s in similar:
            if s.get("emotional_core") == query_emo:
                text_similarity = max(text_similarity, s["score"])

    # emotional_match: if the dominant emotion appears in top results
    emotional_match = 0.0
    if annotation and annotation["emotional_core"]:
        emo = annotation["emotional_core"]
        matches = [s for s in similar if s.get("emotional_core") == emo]
        if matches:
            emotional_match = sum(s["score"] for s in matches) / len(matches)

    # cluster_confidence: fraction of top-k above threshold
    cluster_confidence = sum(1 for s in similar if s["score"] >= REVIEW_THRESHOLD) / max(len(similar), 1)

    final_score = (
        VISUAL_W * visual_similarity
        + TEXT_W * text_similarity
        + EMOTION_W * emotional_match
        + CLUSTER_W * cluster_confidence
    )

    if final_score >= HIGH_THRESHOLD:
        decision = "high_match"
        needs_review = False
    elif final_score >= REVIEW_THRESHOLD:
        decision = "maybe"
        needs_review = False
    elif final_score >= REVIEW_THRESHOLD * 0.5:
        decision = "review"
        needs_review = True
    else:
        decision = "unknown"
        needs_review = True

    return {
        "media_id": media_id,
        "similarity_to_saved_items": visual_similarity,
        "text_similarity": text_similarity,
        "emotional_match": emotional_match,
        "cluster_confidence": cluster_confidence,
        "final_score": round(final_score, 4),
        "decision": decision,
        "needs_review": needs_review,
        "nearest_items": similar[:5],
    }


def score_all_unscored() -> dict:
    """Score all indexed media that don't have a score yet."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT media_id FROM media WHERE media_type IN ('gif','image')"
    ).fetchall()

    stats = {"total": len(rows), "scored": 0, "skipped": 0}
    results = []
    for row in rows:
        s = score_media(row["media_id"])
        if s:
            results.append(s)
            stats["scored"] += 1
        else:
            stats["skipped"] += 1
    return {"scored": results, "stats": stats}
```

- [ ] **Step 2: Commit**

```bash
git add app/services/scorer.py
git commit -m "feat: preference scoring with weighted similarity and decision tiers"
```

---

### Task 11: FastAPI Main Application

**Files:**
- Create: `app/main.py`

- [ ] **Step 1: Write app/main.py**

```python
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from app.db import init_db, get_connection
from app.config import get, load_config
from app.services.scanner import scan_and_register
from app.services.preprocess import preprocess_all, get_pending_frame_count
from app.services.scheduler import ModelScheduler
from app.services.indexer import index_all_annotated, get_index
from app.services.scorer import score_all_unscored, score_media

_scheduler = ModelScheduler()
_bg_processing = threading.Event()

@asynccontextmanager
async def lifespan(app: FastAPI):
    load_config()
    init_db()
    yield

app = FastAPI(title="GifAgent", lifespan=lifespan)


@app.get("/api/status")
def status():
    conn = get_connection()
    media_count = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
    frame_count = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    pending = get_pending_frame_count()
    annotated = conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0]
    indexed = get_index().count
    return {
        "media_count": media_count,
        "frame_count": frame_count,
        "frames_pending": pending,
        "annotated_media": annotated,
        "indexed_vectors": indexed,
    }


@app.post("/api/scan")
def api_scan():
    root = get("media.source_dir", "E:/data/originals")
    stats = scan_and_register(root)
    return {"status": "ok", "stats": stats}


@app.post("/api/preprocess")
def api_preprocess():
    stats = preprocess_all()
    return {"status": "ok", "stats": stats}


@app.get("/api/processing-progress")
def api_progress():
    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM frames WHERE vlm_status='done'").fetchone()[0]
    failed = conn.execute("SELECT COUNT(*) FROM frames WHERE vlm_status='failed'").fetchone()[0]
    pending = total - done - failed
    return {"total": total, "done": done, "failed": failed, "pending": pending}


@app.post("/api/process-frames")
def api_process_frames():
    if _bg_processing.is_set():
        return {"status": "error", "message": "Processing already in progress"}

    def _process():
        _bg_processing.set()
        try:
            _scheduler.process_pending_frames()
        finally:
            _bg_processing.clear()

    threading.Thread(target=_process, daemon=True).start()
    return {"status": "started"}


@app.post("/api/build-index")
def api_build_index():
    stats = index_all_annotated()
    return {"status": "ok", "stats": stats}


@app.post("/api/score-all")
def api_score_all():
    result = score_all_unscored()
    return result


@app.get("/api/media/{media_id}/score")
def api_score_media(media_id: str):
    result = score_media(media_id)
    if result is None:
        return JSONResponse({"error": "Cannot score media - no embedding available"}, status_code=400)
    return result


@app.get("/api/media/{media_id}")
def api_get_media(media_id: str):
    conn = get_connection()
    media = conn.execute("SELECT * FROM media WHERE media_id=?", (media_id,)).fetchone()
    if not media:
        return JSONResponse({"error": "Not found"}, status_code=404)
    annotation = conn.execute("SELECT * FROM annotations WHERE media_id=?", (media_id,)).fetchone()
    frames = conn.execute("SELECT * FROM frames WHERE media_id=? ORDER BY frame_index", (media_id,)).fetchall()
    return {
        "media": dict(media),
        "annotation": dict(annotation) if annotation else None,
        "frames": [dict(f) for f in frames],
    }


@app.post("/api/feedback")
def api_save_feedback(media_id: str, rating: str = Query(...), tags: str = "", reason: str = ""):
    import json, uuid
    from datetime import datetime, timezone

    feedback_id = f"fb_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    tags_json = json.dumps([t.strip() for t in tags.split(",") if t.strip()]) if tags else "[]"

    conn = get_connection()
    conn.execute(
        "INSERT INTO feedback VALUES (?,?,?,?,?,?)",
        (feedback_id, media_id, rating, tags_json, reason, now),
    )
    conn.commit()
    return {"status": "ok", "feedback_id": feedback_id}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
```

- [ ] **Step 2: Commit**

```bash
git add app/main.py
git commit -m "feat: FastAPI application with all endpoints and background processing"
```

---

### Task 12: Gradio Review UI

**Files:**
- Create: `app/ui/__init__.py`
- Create: `app/ui/review.py`

- [ ] **Step 1: Write app/ui/__init__.py**

```python
"""Gradio UI module."""
```

- [ ] **Step 2: Write app/ui/review.py**

```python
import json
import webbrowser
from pathlib import Path

import gradio as gr
import httpx

API_BASE = "http://127.0.0.1:8000"


def load_next_for_review():
    """Fetch the next media item that needs review or is unscored."""
    try:
        resp = httpx.get(f"{API_BASE}/api/status", timeout=5)
        status = resp.json()
    except Exception:
        return None, None, 0, 0, [], "Cannot connect to API", None

    # Try to get a media item with 'review' or 'unknown' decision
    conn_info = f"Media: {status['media_count']} | Frames: {status['frame_count']} | Annotated: {status['annotated_media']} | Index: {status['indexed_vectors']}"

    # For now, grab first annotated media
    return None, None, 0, 0, [], conn_info, None


def rate(media_id: str, rating: str, tags: str, reason: str):
    """Save user feedback."""
    if not media_id:
        return "No media to rate"
    try:
        resp = httpx.post(
            f"{API_BASE}/api/feedback",
            params={"media_id": media_id, "rating": rating, "tags": tags, "reason": reason},
            timeout=5,
        )
        return f"Saved: {rating}"
    except Exception as e:
        return f"Error: {e}"


def build_ui():
    with gr.Blocks(title="GifAgent - Review") as demo:
        gr.Markdown("# GifAgent - Movie Scene Review")

        status_text = gr.Textbox(label="Status", interactive=False)

        with gr.Row():
            with gr.Column(scale=2):
                preview = gr.Image(label="Preview", interactive=False)
                similar_gallery = gr.Gallery(label="Similar Scenes")

            with gr.Column(scale=1):
                media_id_state = gr.State("")
                summary = gr.Textbox(label="Summary", interactive=False)
                emotional_core = gr.Textbox(label="Emotional Core", interactive=False)
                aesthetic = gr.Textbox(label="Aesthetic Notes", interactive=False)
                why = gr.Textbox(label="Why I Like It", interactive=False)
                tags = gr.Textbox(label="Tags (comma-separated)")
                reason = gr.Textbox(label="Your reason (optional)")

        with gr.Row():
            like_btn = gr.Button("👍 Like (A)", variant="primary")
            neutral_btn = gr.Button("😐 Neutral (S)")
            dislike_btn = gr.Button("👎 Dislike (D)")
            refresh_btn = gr.Button("🔄 Next")

        result = gr.Textbox(label="Action Result")

        refresh_btn.click(
            load_next_for_review,
            outputs=[preview, media_id_state, gr.State(0), summary, similar_gallery, status_text, emotional_core],
        )

        def like_action(media_id, t, r):
            return rate(media_id, "like", t, r)
        def neutral_action(media_id, t, r):
            return rate(media_id, "neutral", t, r)
        def dislike_action(media_id, t, r):
            return rate(media_id, "dislike", t, r)

        like_btn.click(like_action, inputs=[media_id_state, tags, reason], outputs=[result])
        neutral_btn.click(neutral_action, inputs=[media_id_state, tags, reason], outputs=[result])
        dislike_btn.click(dislike_action, inputs=[media_id_state, tags, reason], outputs=[result])

        # Keyboard shortcuts
        demo.load(
            None, None, None,
            js="""
            document.addEventListener('keydown', function(e) {
                if (e.key === 'a' || e.key === 'A') document.querySelector('button:has-text("Like")')?.click();
                if (e.key === 's' || e.key === 'S') document.querySelector('button:has-text("Neutral")')?.click();
                if (e.key === 'd' || e.key === 'D') document.querySelector('button:has-text("Dislike")')?.click();
            });
            """
        )

    return demo


if __name__ == "__main__":
    ui = build_ui()
    ui.launch(server_name="127.0.0.1", server_port=7860)
```

- [ ] **Step 3: Commit**

```bash
git add app/ui/__init__.py app/ui/review.py
git commit -m "feat: Gradio review UI with keyboard shortcuts and feedback saving"
```

---

### Task 13: Integration Test and Final Assembly

**Files:**
- Create: `scripts/index_library.py`

- [ ] **Step 1: Write scripts/index_library.py**

```python
#!/usr/bin/env python3
"""Full indexing pipeline: scan -> preprocess -> annotate -> index -> score."""
import sys
sys.path.insert(0, '.')

from app.db import init_db
from app.config import load_config
from app.services.scanner import scan_and_register
from app.services.preprocess import preprocess_all
from app.services.scheduler import ModelScheduler
from app.services.indexer import index_all_annotated, get_index
from app.services.scorer import score_all_unscored

def main():
    load_config()
    init_db()

    print("=== Phase 1: Scan and register ===")
    stats = scan_and_register("E:/data/originals",
        progress_callback=lambda i, t, p, s: print(f"  [{i+1}/{t}] {s}: {p}"))
    print(f"  Registered: {stats['registered']}, Skipped SHA256: {stats['skipped_sha256']}, Skipped pHash: {stats['skipped_phash']}")

    print("\n=== Phase 2: Extract frames ===")
    pp_stats = preprocess_all(
        progress_callback=lambda i, t, mid, s: print(f"  [{i+1}/{t}] {s}: {mid}"))
    print(f"  Processed: {pp_stats['processed']}, Frames: {pp_stats['frames_extracted']}, Failed: {pp_stats['failed']}")

    print("\n=== Phase 3: VLM analysis + LLM synthesis ===")
    scheduler = ModelScheduler()
    proc_stats = scheduler.process_pending_frames(
        progress_callback=lambda i, t, phase: print(f"  [{i}/{t}] {phase}"))
    print(f"  Processed: {proc_stats.get('processed', 0)}, Failed: {proc_stats.get('failed', 0)}")

    print("\n=== Phase 4: Build FAISS index ===")
    idx_stats = index_all_annotated()
    print(f"  Indexed: {idx_stats['indexed']}, Total: {idx_stats['total']}, Failed: {idx_stats['failed']}")

    print("\n=== Phase 5: Score all ===")
    score_result = score_all_unscored()
    print(f"  Scored: {score_result['stats']['scored']}, Skipped: {score_result['stats']['skipped']}")

    print(f"\n=== Pipeline complete. Index has {get_index().count} vectors. ===")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify the full stack starts**

```bash
# Start the FastAPI server in background
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 &

# Check status endpoint
curl http://127.0.0.1:8000/api/status
```
Expected: JSON response with counts.

- [ ] **Step 3: Commit**

```bash
git add scripts/index_library.py
git commit -m "feat: full indexing pipeline script and integration assembly"
```

---

### Verification Checklist

After all tasks, run this end-to-end check:

```bash
# 1. Database exists and is initialized
python -c "from app.db import init_db; init_db(); print('DB OK')"

# 2. Scanner can list files in test directory
python -c "from app.services.scanner import scan_directory; files = scan_directory('E:/data/originals'); print(f'Found {len(files)} files')"

# 3. Single-file registration works (on one test GIF)
# Replace with actual test file path
python -c "
from app.db import init_db; init_db()
from app.services.scanner import register_media
mid = register_media('E:/data/originals/test.gif')
print(f'Registered: {mid}')
"

# 4. Frame extraction works (on the test GIF)
python -c "
from app.services.preprocess import extract_gif_frames
frames = extract_gif_frames('<test_media_id>')
print(f'Frames: {len(frames)}')
"

# 5. Ollama is reachable
curl http://localhost:11434/api/tags

# 6. FastAPI server starts
python -c "from app.main import app; print('Server app loaded OK')"

# 7. Gradio UI loads (import check)
python -c "from app.ui.review import build_ui; print('UI module loaded OK')"
```

---

### File Map (Complete)

```
GifAgent/
├── app/
│   ├── __init__.py              # Package marker
│   ├── main.py                  # FastAPI application + all API endpoints
│   ├── config.py                # YAML config loader with dot-path accessor
│   ├── db.py                    # SQLite connection, init_db, all CREATE TABLE
│   ├── models/                  # (reserved for Pydantic models if needed)
│   ├── services/
│   │   ├── __init__.py
│   │   ├── scanner.py           # File scan, sha256/phash compute, dedup, register
│   │   ├── preprocess.py        # ffmpeg GIF frame extraction, thumbnails
│   │   ├── scheduler.py         # Model lifecycle: llava:13b <-> 9B swap, batch orchestration
│   │   ├── vision.py            # llava:13b frame analysis, JSON parse, frame_annotations INSERT
│   │   ├── llm.py               # 9B media synthesis, annotations INSERT
│   │   ├── embedding.py         # Ollama embedding API wrapper for image/text vectors
│   │   ├── indexer.py           # FAISS IndexFlatIP, add/search, id_map persistence
│   │   └── scorer.py            # Weighted preference scoring, decision tiers
│   ├── workers/                 # (reserved for background task modules)
│   └── ui/
│       ├── __init__.py
│       └── review.py            # Gradio review interface with A/S/D keybinds
├── configs/
│   ├── models.yaml              # All configuration: models, paths, thresholds
│   └── prompts.yaml             # VLM and LLM prompt templates
├── data/
│   ├── library.db               # SQLite database (auto-created)
│   ├── faiss/                   # FAISS index files (auto-created)
│   ├── frames/                  # Extracted frame JPEGs (auto-created)
│   ├── thumbs/                  # Thumbnails (auto-created)
│   └── exports/                 # GIF exports (auto-created)
├── scripts/
│   ├── setup.bat                # Environment setup: venv, pip, ffmpeg check, ollama pull
│   └── index_library.py         # Full 5-phase pipeline script
├── docs/
│   └── 初版构建方案.md           # Design spec (revised)
├── requirements.txt             # Python dependencies
└── venv/                        # Virtual environment (gitignored)
```

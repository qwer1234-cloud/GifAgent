import threading
from contextlib import asynccontextmanager
import json

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
def api_save_feedback(media_id: str = Query(...), rating: str = Query(...), tags: str = "", reason: str = ""):
    import uuid
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

#!/usr/bin/env python3
"""
Robust VLM processing loop — auto-resume, checkpoint, log to file.

Usage:
  .venv\Scripts\python.exe -u scripts\vlm_loop.py

Features:
  - Processes 200 frames per batch with checkpoint auto-resume
  - Logs progress to data/vlm_loop.log with timestamps
  - Auto-restarts VLM model every 50 batches to prevent slowdown
  - Stops on 3 consecutive batch failures
"""
import sys, os, time, json, re, uuid, base64, io, subprocess
from datetime import datetime, timezone

# Fix Windows GBK encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import httpx
sys.path.insert(0, '.')
from app.db import init_db, get_connection
from app.services.json_guard import parse_json_response
from app.services.quality import validate_frame_analysis, normalize_emotional_core

OLLAMA_BASE = "http://localhost:11434"
VLM_MODEL = "llava:13b"
LOG_FILE = "data/vlm_loop.log"
BATCH_SIZE = 200
MODEL_RESTART_EVERY = 50  # restart VLM every N batches to prevent slowdown

FRAME_PROMPT = (
    "Analyze this film frame. Focus on cinematic and aesthetic qualities.\n"
    "Output ONLY a valid JSON object with real, specific content. No placeholder text, no template values.\n\n"
    '{"caption":"describe actual visible subjects and composition","emotional_core":"one lowercase word",'
    '"aesthetic_notes":["2-4 concrete visual observations"],"why_i_like_it":"one personal cinephile reason"}\n\n'
    "CRITICAL: emotional_core = EXACTLY ONE lowercase word from: "
    "tension|melancholy|awe|joy|sadness|catharsis|serenity|excitement|dread|"
    "nostalgia|admiration|intimacy|vulnerability|longing|desire|other\n"
    "NEVER output 'what you see', 'one reason', '2-3 observations', pipe-delimited emotions, or markdown fences."
)
VALID_EMOTIONS = {
    "tension","melancholy","awe","joy","sadness","catharsis","serenity",
    "excitement","dread","nostalgia","admiration","intimacy","vulnerability",
    "longing","desire","other"
}

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def parse_json(text):
    text = text.strip()
    if "</think>" in text: text = text.split("</think>")[-1].strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try: return json.loads(text)
    except: pass
    m = re.search(r"\{[^{}]*\{[^{}]*\}[^{}]*\}|\{[^{}]*\}", text, re.DOTALL)
    if m:
        try: return json.loads(m.group(0))
        except: pass
    return {"_parse_error": True, "_raw": text[:500]}

def restart_model():
    log("Restarting VLM to prevent slowdown...")
    subprocess.run(["wsl", "ollama", "stop", "llava:13b"], capture_output=True, timeout=30)
    time.sleep(15)
    # Ping to reload
    try:
        httpx.post(f"{OLLAMA_BASE}/api/generate",
                   json={"model": VLM_MODEL, "prompt": "ping", "stream": False}, timeout=60)
    except Exception:
        pass
    time.sleep(10)

def main():
    init_db()
    log("=" * 50)
    log("VLM Loop started")

    batch = 0
    total = 0
    fails = 0

    while True:
        conn = get_connection()
        pending = conn.execute("SELECT COUNT(*) FROM frames WHERE vlm_status='pending'").fetchone()[0]
        done = conn.execute("SELECT COUNT(*) FROM frames WHERE vlm_status='done'").fetchone()[0]
        failed_count = conn.execute("SELECT COUNT(*) FROM frames WHERE vlm_status='failed'").fetchone()[0]

        if pending == 0:
            log(f"COMPLETE! {done} done, {failed_count} failed, {total} this session")
            log("Triggering Stage 2 pipeline (LLM synthesis + FAISS rebuild)...")
            try:
                subprocess.run([
                    sys.executable, "-u", "scripts/pipeline_stage2.py"
                ], cwd=".", check=False)
            except Exception as e:
                log(f"Stage 2 launch failed: {e} — run manually: uv run python scripts/pipeline_stage2.py")
            break

        batch += 1
        log(f"Batch {batch}: {pending} pending / {done} done / {failed_count} failed")

        if batch > 1 and batch % MODEL_RESTART_EVERY == 0:
            restart_model()

        frames = conn.execute(
            "SELECT f.frame_id, f.frame_path, f.media_id FROM frames f "
            "WHERE f.vlm_status='pending' ORDER BY f.frame_id LIMIT ?",
            (BATCH_SIZE,)
        ).fetchall()

        if not frames:
            time.sleep(60)
            continue

        t0 = time.time()
        done_count = 0
        batch_failed = 0

        for f in frames:
            try:
                with open(f["frame_path"], "rb") as fh:
                    img_b64 = base64.b64encode(fh.read()).decode("utf-8")
            except Exception:
                conn.execute("UPDATE frames SET vlm_status='failed' WHERE frame_id=?", (f["frame_id"],))
                conn.commit()
                batch_failed += 1
                continue

            for attempt in range(3):
                try:
                    resp = httpx.post(
                        f"{OLLAMA_BASE}/api/generate",
                        json={"model": VLM_MODEL, "prompt": FRAME_PROMPT, "images": [img_b64], "stream": False},
                        timeout=120,
                    )
                    resp.raise_for_status()
                    raw = resp.json().get("response", "")

                    parse_result = parse_json_response(raw)
                    if not parse_result.ok:
                        if attempt < 2:
                            continue
                        conn.execute("UPDATE frames SET vlm_status='failed', vlm_error=? WHERE frame_id=?",
                                    (f"JSON parse: {parse_result.error}", f["frame_id"]))
                        conn.commit()
                        batch_failed += 1
                        break

                    parsed = parse_result.data
                    cleaned, quality_errors = validate_frame_analysis(parsed)

                    if quality_errors and attempt < 2:
                        conn.execute("UPDATE frames SET vlm_attempts=vlm_attempts+1 WHERE frame_id=?", (f["frame_id"],))
                        conn.commit()
                        continue

                    fa_id = f"fa_{uuid.uuid4().hex[:12]}"
                    now = datetime.now(timezone.utc).isoformat()
                    q_status = "passed" if not quality_errors else "quality_failed"
                    conn.execute(
                        "INSERT INTO frame_annotations (annotation_id, frame_id, media_id, model_name, "
                        "caption, emotional_core, aesthetic_notes_json, why_i_like_it, raw_json, "
                        "quality_status, quality_errors_json, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (fa_id, f["frame_id"], f["media_id"], VLM_MODEL,
                         cleaned.get("caption", ""), cleaned.get("emotional_core", ""),
                         json.dumps(cleaned.get("aesthetic_notes", [])),
                         cleaned.get("why_i_like_it", ""),
                         json.dumps(parsed, ensure_ascii=False),
                         q_status,
                         json.dumps(quality_errors) if quality_errors else None,
                         now),
                    )
                    conn.execute("UPDATE frames SET vlm_status='done', vlm_attempts=vlm_attempts+1 WHERE frame_id=?",
                                (f["frame_id"],))
                    conn.commit()
                    done_count += 1
                    break
                except Exception:
                    if attempt == 2:
                        conn.execute("UPDATE frames SET vlm_status='failed', vlm_attempts=vlm_attempts+1 WHERE frame_id=?",
                                    (f["frame_id"],))
                        conn.commit()
                        batch_failed += 1
                    time.sleep(2)

        elapsed = time.time() - t0
        total += done_count

        if done_count == 0:
            fails += 1
            log(f"  {done_count}/{len(frames)} processed! Consecutive fails: {fails}/3")
            if fails >= 3:
                log("  3 consecutive failures, aborting.")
                break
        else:
            fails = 0
            avg = elapsed / max(done_count + batch_failed, 1)
            eta_h = pending * avg / 3600
            log(f"  {done_count}/{len(frames)} in {elapsed:.0f}s ({avg:.1f}s/frame) ETA {eta_h:.1f}h")

        time.sleep(3)

    log(f"VLM Loop finished. Session: {total}, DB: {done} done")

if __name__ == "__main__":
    main()

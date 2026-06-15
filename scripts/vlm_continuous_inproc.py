#!/usr/bin/env python3
"""Continuous in-process VLM runner - single Python process, all frames, with checkpoint support."""
import sys, json, re, uuid, time, base64
from datetime import datetime, timezone
import httpx

sys.path.insert(0, '.')
from app.db import init_db, get_connection

init_db()

OLLAMA_BASE = "http://localhost:11434"
VLM_MODEL = "llava:13b"

FRAME_PROMPT = (
    "Analyze this frame. Output ONLY JSON with real content:\n"
    '{"caption":"what you see","emotional_core":"one word",'
    '"aesthetic_notes":["2-3 observations"],"why_i_like_it":"one reason"}\n'
    "CRITICAL: emotional_core = exactly one lowercase word from: "
    "tension|melancholy|awe|joy|sadness|catharsis|serenity|excitement|dread|nostalgia|admiration|intimacy|vulnerability|longing|desire|other"
)

VALID_EMOTIONS = {"tension","melancholy","awe","joy","sadness","catharsis","serenity",
                  "excitement","dread","nostalgia","admiration","intimacy","vulnerability",
                  "longing","desire","other"}

def parse_json(text):
    text = text.strip()
    if "</think>" in text:
        text = text.split("</think>")[-1].strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
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

def process_batch(batch_size=200):
    """Process one batch and return count processed."""
    conn = get_connection()
    pending = conn.execute(
        "SELECT f.frame_id, f.frame_path, f.media_id FROM frames f "
        "INNER JOIN media m ON f.media_id=m.media_id "
        "WHERE m.is_representative=1 AND f.vlm_status='pending' LIMIT ?",
        (batch_size,)
    ).fetchall()

    if not pending:
        conn.close()
        return 0

    print(f"Batch: {len(pending)} frames at {time.strftime('%H:%M:%S')}")
    t0 = time.time()

    for i, f in enumerate(pending):
        try:
            with open(f["frame_path"], "rb") as fh:
                img_b64 = base64.b64encode(fh.read()).decode("utf-8")
        except Exception as e:
            conn.execute("UPDATE frames SET vlm_status='failed' WHERE frame_id=?", (f["frame_id"],))
            conn.commit()
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
                parsed = parse_json(raw)

                raw_emotion = (parsed.get("emotional_core") or "").strip().lower()
                if raw_emotion and raw_emotion not in VALID_EMOTIONS:
                    parts = [p.strip() for p in raw_emotion.replace("|", ",").split(",")]
                    found = next((p for p in parts if p in VALID_EMOTIONS), None)
                    parsed["emotional_core"] = found if found else "other"

                raw_cap = (parsed.get("caption") or "").strip()
                if raw_cap.startswith("describe what") or raw_cap.startswith("concise"):
                    parsed["caption"] = ""

                fa_id = f"fa_{uuid.uuid4().hex[:12]}"
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "INSERT INTO frame_annotations (annotation_id, frame_id, media_id, model_name, caption, emotional_core, aesthetic_notes_json, why_i_like_it, raw_json, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (fa_id, f["frame_id"], f["media_id"], VLM_MODEL,
                     parsed.get("caption", ""), parsed.get("emotional_core", ""),
                     json.dumps(parsed.get("aesthetic_notes", [])),
                     parsed.get("why_i_like_it", ""),
                     json.dumps(parsed, ensure_ascii=False), now),
                )
                conn.execute("UPDATE frames SET vlm_status='done' WHERE frame_id=?", (f["frame_id"],))
                conn.commit()
                break
            except Exception as e:
                if attempt == 2:
                    conn.execute("UPDATE frames SET vlm_status='failed' WHERE frame_id=?", (f["frame_id"],))
                    conn.commit()
                time.sleep(2)

        if (i + 1) % 25 == 0:
            elapsed = time.time() - t0
            eta = elapsed / (i + 1) * (len(pending) - i - 1)
            print(f"  [{i+1}/{len(pending)}] {elapsed:.0f}s elapsed, ETA {eta:.0f}s")

    elapsed = time.time() - t0
    done = conn.execute("SELECT COUNT(*) FROM frame_annotations").fetchone()[0]
    conn.close()
    print(f"  Done in {elapsed:.0f}s ({elapsed/len(pending):.1f}s/frame). Total annotations: {done}")
    return len(pending)


# Main loop
total_batches = 0
total_processed = 0
t_start = time.time()

while True:
    conn = get_connection()
    remaining = conn.execute("SELECT COUNT(*) FROM frames f INNER JOIN media m ON f.media_id=m.media_id WHERE m.is_representative=1 AND f.vlm_status='pending'").fetchone()[0]
    conn.close()

    if remaining == 0:
        break

    total_batches += 1
    n = process_batch(200)
    total_processed += n

    if n == 0:
        break

    # Checkpoint every 5 batches
    if total_batches % 5 == 0:
        elapsed = time.time() - t_start
        hours = elapsed / 3600
        rate = total_processed / elapsed if elapsed > 0 else 0
        eta_total = remaining / rate if rate > 0 else 0
        print(f"CHECKPOINT: {total_processed} frames in {total_batches} batches, "
              f"{hours:.1f}h elapsed, {rate:.2f} frames/s, "
              f"ETA {eta_total/3600:.1f}h, {remaining} remaining")

elapsed_total = time.time() - t_start
print(f"\nVLM processing complete: {total_processed} frames in {total_batches} batches, "
      f"{elapsed_total/3600:.1f} hours total")

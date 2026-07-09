#!/usr/bin/env python3
"""
Phase 3: Process representative GIFs with VLM+LLM, with checkpoint/resume support.

Usage:
  python scripts/process_representatives.py           # Fresh start or resume
  python scripts/process_representatives.py --reset   # Clear checkpoint, start fresh
  python scripts/process_representatives.py --status  # Show progress only

Checkpoint behavior:
  - After every batch (10 GIFs), saves checkpoint to processing_checkpoint table
  - On restart, resumes from last checkpoint
  - Failed frames are marked in DB and skipped on resume
  - No duplicate processing
"""
import sys, os, time, json, re, uuid, base64
from datetime import datetime, timezone

import httpx

sys.path.insert(0, '.')
from app.db import init_db, get_connection, save_checkpoint, load_checkpoint
from app.config import load_config, get
from app.services.llm_client import generate_llm_text, is_local_llm, llm_model_name, wait_for_llm
from app.services.preprocess import extract_gif_frames

load_config()
init_db()

OLLAMA_BASE = get("vlm.base_url", "http://127.0.0.1:11434")
VLM_MODEL = get("vlm.model", "llava:13b")
LLM_MODEL = llm_model_name()
BATCH_SIZE = 10  # GIFs per batch (not frames)
FRAMES_DIR = get("paths.frames_dir", "data/frames")
PHASE = "vlm_llm_representatives"

# ── Helpers ───────────────────────────────────────────────────────────

def parse_json(text):
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

FRAME_PROMPT = """You are analyzing a single frame from a movie or TV show. Focus on CINEMATIC and AESTHETIC qualities.

Output ONLY a valid JSON object with real, specific content. No placeholder text, no template values, no markdown fencing.

{
  "caption": "describe what you actually see in this specific frame",
  "emotional_core": "intimacy",
  "aesthetic_notes": ["warm amber lighting", "shallow depth of field"],
  "why_i_like_it": "the vulnerability draws you into their private world"
}

CRITICAL RULES:
- emotional_core MUST be EXACTLY ONE lowercase word. Choose from: tension, melancholy, awe, joy, sadness, catharsis, serenity, excitement, dread, nostalgia, admiration, intimacy, vulnerability, longing, desire.
- NEVER output multiple emotions joined with "|" or commas. Pick the single strongest one.
- aesthetic_notes: 2-4 concrete visual observations you actually see.
- caption and why_i_like_it MUST contain real descriptions, not the instruction text itself."""

def ollama_generate(model, prompt, images=None, temperature=0.3):
    payload = {"model": model, "prompt": prompt, "stream": False, "options": {"temperature": temperature, "num_think": 0}}
    if images:
        payload["images"] = images
    resp = httpx.post(f"{OLLAMA_BASE}/api/generate", json=payload, timeout=120)
    resp.raise_for_status()
    return resp.json().get("response", "")

def ollama_stop(model):
    import subprocess
    subprocess.run(["wsl", "ollama", "stop", model], capture_output=True, timeout=60)
    time.sleep(10)

def wait_model(model, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.post(f"{OLLAMA_BASE}/api/generate",
                          json={"model": model, "prompt": "ping", "stream": False}, timeout=10)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False

def build_synthesis_prompt(frame_analyses):
    text = "\n\n".join(
        f"Frame {i+1}: caption={fa.get('caption','')}, emotion={fa.get('emotional_core','')}, "
        f"aesthetic={fa.get('aesthetic_notes',[])}, why={fa.get('why_i_like_it','')}"
        for i, fa in enumerate(frame_analyses)
    )
    return (
        "Synthesize these frame analyses into one cohesive annotation.\n"
        "IMPORTANT: Output ONLY a valid JSON object. Start with {\"summary\".\n\n"
        "{\n"
        '  "summary": "one sentence describing the visual style",\n'
        '  "emotional_core": "one dominant emotion",\n'
        '  "aesthetic_notes": ["2-4 cinematographic qualities"],\n'
        '  "why_i_like_it": "one cinephile-level reason",\n'
        '  "tags": ["3-5 keywords"]\n'
        "}\n\n"
        "Frame analyses:\n" + text
    )

# ── CLI ───────────────────────────────────────────────────────────────

if "--status" in sys.argv:
    conn = get_connection()
    total = conn.execute(
        "SELECT COUNT(*) FROM media WHERE is_representative=1"
    ).fetchone()[0]
    done = conn.execute(
        "SELECT COUNT(DISTINCT m.media_id) FROM media m "
        "INNER JOIN annotations a ON m.media_id=a.media_id "
        "WHERE m.is_representative=1"
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT COUNT(*) FROM frames WHERE vlm_status='pending'"
    ).fetchone()[0]
    ckpt = load_checkpoint(PHASE)
    print(f"Representatives: {total} total, {done} annotated, {total - done} remaining")
    print(f"Pending frames: {pending}")
    if ckpt:
        print(f"Last checkpoint: batch={ckpt['batch_index']}, processed={ckpt['total_processed']}, "
              f"failed={ckpt['total_failed']}, media={ckpt['last_media_id']}")
    else:
        print("No checkpoint found")
    sys.exit(0)

if "--reset" in sys.argv:
    conn = get_connection()
    conn.execute("DELETE FROM processing_checkpoint WHERE phase=?", (PHASE,))
    conn.commit()
    print("Checkpoint cleared. Starting fresh.")

# ── Main processing loop ──────────────────────────────────────────────

ckpt = load_checkpoint(PHASE)
start_batch = (ckpt["batch_index"] + 1) if ckpt else 0
total_processed = ckpt["total_processed"] if ckpt else 0
total_failed = ckpt["total_failed"] if ckpt else 0
last_media_id = ckpt["last_media_id"] if ckpt else ""

conn = get_connection()

# Get all representatives that haven't been annotated yet
# Resume after last_media_id if checkpoint exists
query = """
    SELECT m.media_id, m.file_path FROM media m
    WHERE m.is_representative=1
      AND m.media_type='gif'
      AND m.media_id NOT IN (SELECT DISTINCT media_id FROM annotations)
"""
if last_media_id:
    query += f" AND m.media_id > '{last_media_id}'"
query += " ORDER BY m.media_id"

rows = conn.execute(query).fetchall()
print(f"\n{'='*60}")
print(f"Phase 3: VLM+LLM Processing")
print(f"{'='*60}")
print(f"  Representatives to process: {len(rows)}")
print(f"  Starting from batch: {start_batch}")
print(f"  Batch size: {BATCH_SIZE} GIFs")
print(f"  Already processed: {total_processed}, Failed: {total_failed}")
print(f"  VLM: {VLM_MODEL}, LLM: {LLM_MODEL}")
print(f"{'='*60}\n")

if len(rows) == 0:
    print("All representatives already processed!")
    sys.exit(0)

batch_idx = start_batch
for i in range(0, len(rows), BATCH_SIZE):
    batch = rows[i:i + BATCH_SIZE]
    batch_start = time.time()
    print(f"\n── Batch {batch_idx} ({i+1}-{min(i+BATCH_SIZE, len(rows))}/{len(rows)}) ──")

    # Step 1: Extract frames for this batch (parallel-able but sequential for now)
    print(f"  Extracting frames...")
    batch_frame_map = {}  # media_id -> [{"frame_id": ..., "frame_path": ...}, ...]
    for row in batch:
        media_id = row["media_id"]
        # Check if frames already extracted
        existing = conn.execute(
            "SELECT COUNT(*) FROM frames WHERE media_id=?", (media_id,)
        ).fetchone()[0]
        if existing == 0:
            try:
                frames = extract_gif_frames(media_id)
                batch_frame_map[media_id] = [
                    {"frame_id": f["frame_id"], "frame_path": f["frame_path"]}
                    for f in frames
                ]
            except Exception as e:
                print(f"    Frame extraction failed for {media_id}: {e}")
                conn.execute("UPDATE media SET is_representative=-1 WHERE media_id=?", (media_id,))
                conn.commit()
                total_failed += 1
                continue
        else:
            frame_rows = conn.execute(
                "SELECT frame_id, frame_path FROM frames WHERE media_id=? AND vlm_status='pending'",
                (media_id,)
            ).fetchall()
            batch_frame_map[media_id] = [{"frame_id": r["frame_id"], "frame_path": r["frame_path"]} for r in frame_rows]

    if not batch_frame_map:
        print(f"  No frames to process in this batch, saving checkpoint...")
        batch_idx += 1
        last_media_id = batch[-1]["media_id"]
        save_checkpoint(PHASE, last_media_id, batch_idx, total_processed, total_failed)
        continue

    # Step 2: VLM analyze all frames
    print(f"  VLM analysis ({len(batch_frame_map)} GIFs, ~{sum(len(v) for v in batch_frame_map.values())} frames)...")

    # Ensure VLM is running
    if is_local_llm():
        ollama_stop(LLM_MODEL.split("/")[-1].split(":")[0])
    if not wait_model(VLM_MODEL):
        print("  ERROR: VLM model not responding, saving checkpoint...")
        save_checkpoint(PHASE, last_media_id or batch[0]["media_id"], batch_idx, total_processed, total_failed)
        sys.exit(1)

    frame_annotations = {}  # media_id -> [annotation_dict, ...]
    vlm_t_start = time.time()
    total_frames = sum(len(v) for v in batch_frame_map.values())
    frames_done = 0
    for mid, frames in batch_frame_map.items():
        mid_annotations = []
        for f in frames:
            with open(f["frame_path"], "rb") as fh:
                img_b64 = base64.b64encode(fh.read()).decode("utf-8")
            for attempt in range(3):
                try:
                    raw = ollama_generate(VLM_MODEL, FRAME_PROMPT, images=[img_b64])
                    parsed = parse_json(raw)
                    if parsed.get("_parse_error"):
                        if attempt < 2:
                            continue

                    # Post-process: clean up emotional_core (model may copy option list)
                    VALID_EMOTIONS = {"tension", "melancholy", "awe", "joy", "sadness", "catharsis",
                                      "serenity", "excitement", "dread", "nostalgia", "admiration",
                                      "intimacy", "vulnerability", "longing", "desire", "other"}
                    raw_emotion = (parsed.get("emotional_core") or "").strip().lower()
                    if raw_emotion and raw_emotion not in VALID_EMOTIONS:
                        parts = [p.strip() for p in raw_emotion.replace("|", ",").split(",")]
                        found = next((p for p in parts if p in VALID_EMOTIONS), None)
                        parsed["emotional_core"] = found if found else "other"

                    # Post-process: discard template text as caption
                    raw_cap = (parsed.get("caption") or "").strip()
                    if not raw_cap or raw_cap.startswith("concise") or raw_cap.startswith("describe what"):
                        parsed["caption"] = ""

                    # Save frame annotation
                    fa_id = f"fa_{uuid.uuid4().hex[:12]}"
                    now = datetime.now(timezone.utc).isoformat()
                    conn.execute(
                        """INSERT INTO frame_annotations (annotation_id, frame_id, media_id, model_name, caption,
                           emotional_core, aesthetic_notes_json, why_i_like_it, raw_json, created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (fa_id, f["frame_id"], mid, VLM_MODEL,
                         parsed.get("caption", ""), parsed.get("emotional_core", ""),
                         json.dumps(parsed.get("aesthetic_notes", [])),
                         parsed.get("why_i_like_it", ""),
                         json.dumps(parsed, ensure_ascii=False), now),
                    )
                    conn.execute("UPDATE frames SET vlm_status='done' WHERE frame_id=?", (f["frame_id"],))
                    conn.commit()
                    mid_annotations.append(parsed)
                    frames_done += 1
                    print(f"    [{frames_done}/{total_frames}] {mid[:14]} frame {f['frame_id'][:10]}: "
                          f"{parsed.get('caption','?')[:40]}...")
                    break
                except Exception as e:
                    if attempt == 2:
                        print(f"    [{frames_done+1}/{total_frames}] {mid[:14]} FAILED: {e}")
                        conn.execute("UPDATE frames SET vlm_status='failed' WHERE frame_id=?", (f["frame_id"],))
                        conn.commit()
        if mid_annotations:
            frame_annotations[mid] = mid_annotations

    vlm_elapsed = time.time() - vlm_t_start
    print(f"  VLM done in {vlm_elapsed:.0f}s ({vlm_elapsed/max(total_frames,1):.0f}s/frame)")

    # Step 3: Switch to LLM and synthesize
    print(f"  Switching to LLM...")
    ollama_stop(VLM_MODEL.split("/")[-1].split(":")[0])
    if not wait_for_llm(timeout_s=30):
        print("  ERROR: LLM model not responding, saving checkpoint...")
        save_checkpoint(PHASE, last_media_id or batch[0]["media_id"], batch_idx, total_processed, total_failed)
        sys.exit(1)

    print(f"  LLM synthesis ({len(frame_annotations)} GIFs)...")
    llm_t_start = time.time()
    for mid, annotations in frame_annotations.items():
        prompt = build_synthesis_prompt(annotations)
        for attempt in range(3):
            try:
                raw = generate_llm_text(prompt, temperature=0.3, timeout=120)
                parsed = parse_json(raw)
                if parsed.get("_parse_error"):
                    if attempt < 2:
                        prompt += "\n\nYour last response was not valid JSON. Output ONLY the JSON object."
                        continue
                ann_id = f"ann_{uuid.uuid4().hex[:12]}"
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """INSERT INTO annotations (annotation_id, media_id, model_name, summary,
                       emotional_core, aesthetic_notes_json, why_i_like_it, tags_json, raw_json, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (ann_id, mid, LLM_MODEL,
                     parsed.get("summary", ""), parsed.get("emotional_core", ""),
                     json.dumps(parsed.get("aesthetic_notes", [])),
                     parsed.get("why_i_like_it", ""),
                     json.dumps(parsed.get("tags", [])),
                     json.dumps(parsed, ensure_ascii=False), now),
                )
                conn.commit()
                total_processed += 1
                print(f"    [{total_processed}] {mid[:14]}: {parsed.get('emotional_core','?')}")
                break
            except Exception as e:
                if attempt == 2:
                    print(f"    [{total_processed+1}] {mid[:14]} LLM FAILED: {e}")
                    total_failed += 1
    llm_elapsed = time.time() - llm_t_start
    print(f"  LLM done in {llm_elapsed:.0f}s ({llm_elapsed/max(len(frame_annotations),1):.0f}s/GIF)")

    # Step 4: Save checkpoint
    last_media_id = batch[-1]["media_id"]
    batch_elapsed = time.time() - batch_start
    save_checkpoint(PHASE, last_media_id, batch_idx, total_processed, total_failed,
                    extra={"batch_elapsed_s": int(batch_elapsed)})
    print(f"  Checkpoint saved. Batch elapsed: {batch_elapsed:.0f}s")
    batch_idx += 1

# ── Done ──────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"Phase 3 complete!")
print(f"  Total processed: {total_processed}")
print(f"  Total failed: {total_failed}")
print(f"  Success rate: {total_processed/(total_processed+total_failed)*100:.1f}%"
      if (total_processed + total_failed) > 0 else "  N/A")
print(f"{'='*60}")

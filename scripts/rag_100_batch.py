#!/usr/bin/env python3
"""Process 100 RAG seed GIFs: extract frames, VLM, LLM synth, build FAISS."""
import sys, os, json, re, uuid, time, base64, subprocess
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
EMBED_MODEL = get("embedding.text_model", "nomic-embed-text:latest")
PHASE = "rag_100_vlm"

N_TOTAL = 100  # Process all 100

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

VALID_EMOTIONS = {"tension", "melancholy", "awe", "joy", "sadness", "catharsis",
                  "serenity", "excitement", "dread", "nostalgia", "admiration",
                  "intimacy", "vulnerability", "longing", "desire", "other"}

FRAME_PROMPT = """Analyze this frame. Output ONLY JSON:
{"caption":"what you see","emotional_core":"one word","aesthetic_notes":["2-3 observations"],"why_i_like_it":"one reason"}
CRITICAL: emotional_core = EXACTLY ONE lowercase word. Never pipe-delimited."""

conn = get_connection()

# ── Step 1: Extract frames ───────────────────────────────────────────
print("=" * 60)
print("Step 1: Extract frames for 100 RAG seeds")
print("=" * 60)

reps = conn.execute(
    "SELECT media_id FROM media WHERE is_representative=1 AND media_type='gif' ORDER BY RANDOM() LIMIT ?",
    (N_TOTAL,)
).fetchall()

total_extracted = 0
for i, (mid,) in enumerate(reps):
    existing = conn.execute("SELECT COUNT(*) FROM frames WHERE media_id=?", (mid,)).fetchone()[0]
    if existing == 0:
        try:
            frames = extract_gif_frames(mid)
            total_extracted += len(frames)
            if (i + 1) % 20 == 0:
                print(f"  [{i+1}/{N_TOTAL}] frames extracted...")
        except Exception as e:
            print(f"  [{i+1}] FAILED: {mid[:14]} - {e}")
    else:
        fc = conn.execute("SELECT COUNT(*) FROM frames WHERE media_id=?", (mid,)).fetchone()[0]
        total_extracted += fc
print(f"  Done. Total frames: {total_extracted}")

# ── Step 2: VLM analysis ─────────────────────────────────────────────
print(f"\n{'='*60}")
print("Step 2: VLM analysis (llava:13b)")
print(f"{'='*60}")

# Stop LLM, wait for VLM
if is_local_llm():
    subprocess.run(["wsl", "ollama", "stop", LLM_MODEL.split("/")[-1].split(":")[0]], capture_output=True, timeout=30)
time.sleep(10)

# Verify VLM is ready
for attempt in range(5):
    try:
        r = httpx.post(f"{OLLAMA_BASE}/api/generate",
                       json={"model": VLM_MODEL, "prompt": "ping", "stream": False}, timeout=10)
        if r.status_code == 200:
            print("  VLM ready")
            break
    except Exception:
        pass
    if attempt == 4:
        print("  ERROR: VLM not responding")
        sys.exit(1)
    time.sleep(5)

# Process pending frames
ckpt = load_checkpoint(PHASE)
start_from = (ckpt["batch_index"] + 1) if ckpt else 0

pending = conn.execute(
    "SELECT f.frame_id, f.frame_path, f.media_id FROM frames f "
    "INNER JOIN media m ON f.media_id=m.media_id "
    "WHERE m.is_representative=1 AND f.vlm_status='pending' ORDER BY f.frame_id"
).fetchall()

if start_from > 0:
    pending = pending[start_from * 10:]  # Skip processed batches
    print(f"  Resuming from batch {start_from} ({len(pending)} frames remaining)")

total_frames = len(pending)
print(f"  Frames to process: {total_frames}")

processed = 0
batch_size = 100  # frames per checkpoint save
for i in range(0, len(pending), batch_size):
    chunk = pending[i:i + batch_size]
    for f in chunk:
        with open(f["frame_path"], "rb") as fh:
            img_b64 = base64.b64encode(fh.read()).decode("utf-8")

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

                # Cleanup emotional_core
                raw_emotion = (parsed.get("emotional_core") or "").strip().lower()
                if raw_emotion and raw_emotion not in VALID_EMOTIONS:
                    parts = [p.strip() for p in raw_emotion.replace("|", ",").split(",")]
                    found = next((p for p in parts if p in VALID_EMOTIONS), None)
                    parsed["emotional_core"] = found if found else "other"

                # Skip template captions
                raw_cap = (parsed.get("caption") or "").strip()
                if raw_cap.startswith("describe what") or raw_cap.startswith("concise"):
                    parsed["caption"] = ""

                # Save
                fa_id = f"fa_{uuid.uuid4().hex[:12]}"
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """INSERT INTO frame_annotations (annotation_id, frame_id, media_id, model_name,
                       caption, emotional_core, aesthetic_notes_json, why_i_like_it, raw_json, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (fa_id, f["frame_id"], f["media_id"], VLM_MODEL,
                     parsed.get("caption", ""), parsed.get("emotional_core", ""),
                     json.dumps(parsed.get("aesthetic_notes", [])),
                     parsed.get("why_i_like_it", ""),
                     json.dumps(parsed, ensure_ascii=False), now),
                )
                conn.execute("UPDATE frames SET vlm_status='done' WHERE frame_id=?", (f["frame_id"],))
                conn.commit()
                processed += 1
                if processed % 50 == 0:
                    print(f"  [{processed}/{total_frames}] VLM done, emotion={parsed.get('emotional_core', '?')}")
                break
            except Exception as e:
                if attempt == 2:
                    conn.execute("UPDATE frames SET vlm_status='failed' WHERE frame_id=?", (f["frame_id"],))
                    conn.commit()

    # Save checkpoint after each batch
    batch_idx = start_from + (i // batch_size) + 1
    last_mid = chunk[-1]["media_id"] if chunk else ""
    save_checkpoint(PHASE, last_mid, batch_idx, processed, 0)
print(f"  VLM complete. Processed: {processed}")

# ── Step 3: LLM synthesis ────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"Step 3: LLM synthesis ({LLM_MODEL})")
print(f"{'='*60}")

# Stop VLM, switch to LLM
subprocess.run(["wsl", "ollama", "stop", "llava"], capture_output=True, timeout=30)
time.sleep(10)

if not wait_for_llm(timeout_s=30):
    print("  ERROR: LLM not responding")
    sys.exit(1)
print("  LLM ready")

# Get media with frames done but no annotation
media_to_synth = conn.execute("""
    SELECT DISTINCT m.media_id, m.file_path
    FROM media m
    INNER JOIN frame_annotations fa ON m.media_id = fa.media_id
    WHERE m.is_representative = 1
      AND m.media_id NOT IN (SELECT media_id FROM annotations)
""").fetchall()
print(f"  Media to synthesize: {len(media_to_synth)}")

SYNTH_PROMPT_TEMPLATE = (
    "Synthesize these frame analyses. Output ONLY JSON:\n"
    '{"summary":"one sentence","emotional_core":"one word","aesthetic_notes":["2-4 items"],'
    '"why_i_like_it":"one reason","tags":["3-5 keywords"]}\n\n'
    "Frame analyses:\n{analyses}"
)

synth_done = 0
for mid, fpath in media_to_synth:
    fas = conn.execute(
        "SELECT caption, emotional_core, aesthetic_notes_json, why_i_like_it FROM frame_annotations WHERE media_id=? LIMIT 8",
        (mid,)
    ).fetchall()
    if not fas:
        continue

    analyses = "\n".join(
        f"F{i+1}: {fa['caption']}, emotion={fa['emotional_core']}, notes={fa['aesthetic_notes_json']}, why={fa['why_i_like_it']}"
        for i, fa in enumerate(fas)
    )
    prompt = SYNTH_PROMPT_TEMPLATE.format(analyses=analyses)

    for attempt in range(3):
        try:
            raw = generate_llm_text(prompt, temperature=0.3, timeout=120)
            parsed = parse_json(raw)
            if parsed.get("_parse_error"):
                if attempt < 2:
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
            synth_done += 1
            if synth_done % 20 == 0:
                print(f"  [{synth_done}/{len(media_to_synth)}] synthesized")
            break
        except Exception as e:
            if attempt == 2:
                print(f"  SYNTH FAILED {mid[:14]}: {e}")
            time.sleep(3)

print(f"  Synthesis done: {synth_done}")

# ── Step 4: Build FAISS index ────────────────────────────────────────
print(f"\n{'='*60}")
print("Step 4: Build FAISS index")
print(f"{'='*60}")

from app.services.embedding import compute_text_summary_embedding
from app.services.indexer import get_index

idx = get_index()
to_index = conn.execute("""
    SELECT DISTINCT m.media_id FROM media m
    INNER JOIN annotations a ON m.media_id = a.media_id
    WHERE m.media_id NOT IN (SELECT owner_id FROM vector_refs WHERE vector_type='media_global')
""").fetchall()
print(f"  Media to index: {len(to_index)}")

# Load embedding model
if is_local_llm():
    subprocess.run(["wsl", "ollama", "stop", LLM_MODEL.split("/")[-1].split(":")[0]], capture_output=True, timeout=30)
time.sleep(5)

indexed = 0
for mid, in to_index:
    try:
        emb = compute_text_summary_embedding(mid)
        if emb:
            idx.add(emb, mid, "media_global")
            indexed += 1
            if indexed % 20 == 0:
                print(f"  [{indexed}/{len(to_index)}] indexed")
    except Exception as e:
        if indexed < 5:
            print(f"  Index err: {e}")

print(f"  Indexed: {indexed}, FAISS size: {idx.count}")

# ── Done ─────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"RAG 100 batch complete! FAISS index: {idx.count} vectors")
print(f"{'='*60}")

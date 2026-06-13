#!/usr/bin/env python3
"""
Two-pass RAG test on JUR-639.mp4:
  Pass 1: VLM bare analysis (no RAG) → per-frame captions + emotions
  Pass 2: Embed each caption → FAISS search → inject per-frame RAG context → LLM synthesis
"""
import sys, os, subprocess, json, re, base64, time
from datetime import datetime, timezone
import httpx
from PIL import Image

sys.path.insert(0, '.')
from app.db import init_db, get_connection
from app.config import load_config
from app.services.embedding import compute_text_embedding
from app.services.indexer import get_index

load_config()
init_db()

VIDEO_PATH = "C:/Users/sunhao/Desktop/ToWatch/JUR-639.mp4"
OLLAMA_BASE = "http://localhost:11434"
VLM_MODEL = "llava:13b"
LLM_MODEL = "hf.co/unsloth/Qwen3-14B-GGUF:Q4_K_M"
FRAMES_DIR = "data/frames/rag_v2_test"
EXPORT_DIR = "data/exports/rag_v2_test"
os.makedirs(FRAMES_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)

print("=" * 60)
print("GifAgent RAG v2 Test — JUR-639.mp4")
print("=" * 60)

# ── Helpers ───────────────────────────────────────────────────────────

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

NO_RAG_PROMPT = (
    "Analyze this film frame. Focus on cinematic and aesthetic qualities.\n"
    "Output ONLY a JSON object with real, specific content:\n"
    '{"caption":"what you see in this frame","emotional_core":"one word",'
    '"aesthetic_notes":["2-3 visual observations"],"why_i_like_it":"one reason"}\n'
    "CRITICAL: emotional_core = EXACTLY ONE lowercase word from: "
    "tension|melancholy|awe|joy|sadness|catharsis|serenity|excitement|dread|nostalgia|admiration|intimacy|vulnerability|longing|desire|other"
)

def stop_model(name_part):
    subprocess.run(["wsl", "ollama", "stop", name_part], capture_output=True, timeout=30)

def wait_model(model_name, timeout_s=60):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = httpx.post(f"{OLLAMA_BASE}/api/generate",
                          json={"model": model_name, "prompt": "ping", "stream": False}, timeout=10)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(3)
    return False

# ── Phase 1: Extract I-frames ─────────────────────────────────────────
print("\n[1/5] Extracting I-frames...")
subprocess.run([
    "ffmpeg", "-y", "-i", VIDEO_PATH, "-t", "5400",
    "-vf", "select='eq(pict_type,I)',scale=640:-1",
    "-vsync", "vfr", f"{FRAMES_DIR}/scene_%06d.jpg"
], capture_output=True, timeout=300)

frame_files = sorted([f for f in os.listdir(FRAMES_DIR) if f.endswith('.jpg')])
print(f"  I-frames: {len(frame_files)}")

# Dark-frame filter
good_frames = []
for fname in frame_files:
    fpath = os.path.join(FRAMES_DIR, fname)
    try:
        img = Image.open(fpath).convert('L')
        brightness = sum(img.getdata()) / max(1, img.width * img.height)
        if brightness > 30:
            # Extract timestamp from filename index
            idx = int(fname.replace("scene_", "").replace(".jpg", ""))
            good_frames.append({"path": fpath, "name": fname, "idx": idx})
        img.close()
    except Exception:
        pass
print(f"  After dark filter: {len(good_frames)}")

# ── Phase 2: Pass 1 — VLM bare analysis (no RAG) ──────────────────────
N_FRAMES = 50
# Spread sample across the video for diversity
step = max(1, len(good_frames) // N_FRAMES)
sample_frames = good_frames[::step][:N_FRAMES]
print(f"\n[2/5] Pass 1: VLM bare analysis ({len(sample_frames)} frames)...")

stop_model("Qwen3-14B-GGUF")
stop_model("nomic-embed-text")
time.sleep(5)
if not wait_model(VLM_MODEL):
    print("  ERROR: VLM not responding")
    sys.exit(1)

vlm_results = []
for fi, finfo in enumerate(sample_frames):
    with open(finfo["path"], "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")

    for attempt in range(3):
        try:
            resp = httpx.post(
                f"{OLLAMA_BASE}/api/generate",
                json={"model": VLM_MODEL, "prompt": NO_RAG_PROMPT, "images": [img_b64], "stream": False},
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

            raw_cap = (parsed.get("caption") or "").strip()
            if raw_cap.startswith("describe what") or raw_cap.startswith("concise"):
                parsed["caption"] = ""

            parsed["frame_name"] = finfo["name"]
            vlm_results.append(parsed)
            print(f"  [{fi+1}/{len(sample_frames)}] {finfo['name'][:12]}: {parsed.get('emotional_core','?')}")
            break
        except Exception as e:
            if attempt == 2:
                print(f"  [{fi+1}] FAILED: {e}")
            time.sleep(2)

print(f"  VLM done: {len(vlm_results)} frames")
# Show emotion distribution
emo_counts = {}
for r in vlm_results:
    e = r.get("emotional_core", "?")
    emo_counts[e] = emo_counts.get(e, 0) + 1
print(f"  Emotion distribution: {dict(sorted(emo_counts.items(), key=lambda x: -x[1]))}")

# ── Phase 3: Pass 2 — Per-frame RAG search + LLM synthesis ────────────
print(f"\n[3/5] Pass 2: Per-frame RAG search with VLM captions...")

# Switch to LLM model
stop_model("llava")
time.sleep(10)
if not wait_model(LLM_MODEL, timeout_s=180):
    print("  ERROR: LLM not responding")
    sys.exit(1)

idx = get_index()
if idx.count == 0:
    print("  WARNING: FAISS index is empty, skipping RAG")

# For each frame, embed caption → search FAISS → attach similar GIFs
for fi, result in enumerate(vlm_results):
    caption = result.get("caption", "")
    if caption:
        try:
            emb = compute_text_embedding(caption)
            similar = idx.search(emb, top_k=5)
            result["rag_similar"] = [
                {"media_id": s["media_id"], "score": s["score"],
                 "emotional_core": s.get("emotional_core", ""),
                 "tags": s.get("tags", [])[:3]}
                for s in similar
            ]
        except Exception as e:
            result["rag_similar"] = []
    else:
        result["rag_similar"] = []

    if (fi + 1) % 10 == 0:
        print(f"  [{fi+1}/{len(vlm_results)}] RAG search done")

# Show RAG diversity
rag_emotions = set()
for r in vlm_results:
    for s in r.get("rag_similar", []):
        rag_emotions.add(s.get("emotional_core", ""))
print(f"  Unique RAG emotions retrieved: {sorted(rag_emotions)}")

# Build per-frame RAG context
rag_frames = []
for r in vlm_results:
    rag_context = ""
    if r.get("rag_similar"):
        rag_context = "User's similar saved GIFs (as style reference):\n" + "\n".join(
            f"  - {s['emotional_core']} | {', '.join(s.get('tags', [])[:3])}"
            for s in r["rag_similar"][:5]
        )
    rag_frames.append({**r, "rag_context": rag_context})

# LLM synthesis with per-frame RAG
print(f"\n[4/5] LLM synthesis with per-frame RAG context...")

analyses_text = "\n\n".join(
    f"Frame {i+1}: caption={r.get('caption','')}, emotion={r.get('emotional_core','')}, "
    f"aesthetic={r.get('aesthetic_notes',[])}, why={r.get('why_i_like_it','')}"
    for i, r in enumerate(rag_frames)
)

# Collect unique RAG tags for global context
all_rag_tags = set()
for r in rag_frames:
    for s in r.get("rag_similar", []):
        for t in s.get("tags", []):
            all_rag_tags.add(t.lower())
rag_tag_context = ", ".join(sorted(all_rag_tags)[:20])

synth_prompt = (
    "Synthesize these film frame analyses into ONE cohesive annotation.\n"
    f"User's stylistic preferences (from saved collection): {rag_tag_context}\n\n"
    "Output ONLY JSON:\n"
    '{"summary":"one sentence describing visual style",'
    '"emotional_core":"one dominant emotion","aesthetic_notes":["2-4 qualities"],'
    '"why_i_like_it":"one cinephile reason","tags":["3-5 keywords"],'
    '"scene_type":"close-up|dialogue|action|transition|reaction|establishing|montage|other"}\n\n'
    "Frame analyses:\n" + analyses_text
)

synthesis = {"_parse_error": True, "_raw": ""}
for attempt in range(3):
    try:
        resp = httpx.post(
            f"{OLLAMA_BASE}/api/generate",
            json={"model": LLM_MODEL, "prompt": synth_prompt, "stream": False, "options": {"temperature": 0.3}},
            timeout=180,
        )
        resp.raise_for_status()
        resp_data = resp.json()
        raw = resp_data.get("response", "") or resp_data.get("thinking", "")
        synthesis = parse_json(raw)
        if not synthesis.get("_parse_error"):
            print(f"  summary: {synthesis.get('summary', '?')}")
            print(f"  emotional_core: {synthesis.get('emotional_core', '?')}")
            print(f"  tags: {synthesis.get('tags', [])}")
            break
        else:
            print(f"  Attempt {attempt+1}: JSON parse failed, retrying...")
    except Exception as e:
        print(f"  Attempt {attempt+1}: {e}")
        time.sleep(5)

# ── Phase 5: Export top 10 GIF clips ──────────────────────────────────
print(f"\n[5/5] Exporting GIF clips...")

ranked = sorted(rag_frames, key=lambda x: len(x.get("aesthetic_notes") or []), reverse=True)
top_frames = ranked[:50]

for i, fa in enumerate(top_frames):
    frame_idx = fa.get("idx", fa.get("frame_name", "").replace("scene_", "").replace(".jpg", ""))
    try:
        est_ts = int(frame_idx) * 5400 / max(len(frame_files), 1)
    except (ValueError, TypeError):
        est_ts = i * 540

    start = max(0, est_ts - 1.5)
    dur = 3.5
    out_gif = f"{EXPORT_DIR}/jur639_v2_scene_{i+1}.gif"
    palette = f"{EXPORT_DIR}/v2_palette_{i+1}.png"

    subprocess.run([
        "ffmpeg", "-y", "-ss", str(start), "-t", str(dur), "-i", VIDEO_PATH,
        "-vf", "fps=10,scale=480:-1:flags=lanczos,palettegen", palette
    ], capture_output=True, timeout=30)

    subprocess.run([
        "ffmpeg", "-y", "-ss", str(start), "-t", str(dur), "-i", VIDEO_PATH,
        "-i", palette,
        "-filter_complex", "fps=10,scale=480:-1:flags=lanczos[x];[x][1:v]paletteuse",
        out_gif
    ], capture_output=True, timeout=30)

    if os.path.exists(out_gif):
        sz = os.path.getsize(out_gif)
        print(f"  Scene {i+1} (t~{int(est_ts)}s): {sz//1024}KB - {fa.get('emotional_core','?')}")

# ── Save results ──────────────────────────────────────────────────────
output = {
    "video": VIDEO_PATH,
    "rag_index_size": idx.count,
    "frames_analyzed": len(vlm_results),
    "vlm_emotion_distribution": emo_counts,
    "rag_unique_emotions": sorted(rag_emotions),
    "synthesis": synthesis,
    "top_scenes": [
        {
            "rank": i+1,
            "frame_name": fa.get("frame_name"),
            "caption": fa.get("caption"),
            "emotional_core": fa.get("emotional_core"),
            "aesthetic_notes": fa.get("aesthetic_notes"),
            "why_i_like_it": fa.get("why_i_like_it"),
            "rag_similar": fa.get("rag_similar", [])[:3],
        }
        for i, fa in enumerate(top_frames)
    ],
}

out_file = "data/test_jur639_rag_v2_result.json"
with open(out_file, "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f"\n{'='*60}")
print("RAG v2 Test Complete!")
print(f"  Frames: {len(vlm_results)}")
print(f"  Emotion distribution: {emo_counts}")
print(f"  GIFs exported: {len(top_frames)}")
print(f"  Synthesis: {'OK' if not synthesis.get('_parse_error') else 'FAILED'}")
print(f"  Results: {out_file}")
print(f"{'='*60}")

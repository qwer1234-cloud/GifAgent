#!/usr/bin/env python3
"""Test JUR-639.mp4 with RAG-enhanced VLM analysis using existing annotations."""
import sys, os, subprocess, json, re, base64, time
from datetime import datetime, timezone
import httpx

sys.path.insert(0, '.')
from app.db import init_db, get_connection
from app.config import load_config
from app.services.embedding import compute_text_summary_embedding, compute_text_embedding
from app.services.indexer import get_index

load_config()
init_db()

VIDEO_PATH = "C:/Users/sunhao/Desktop/ToWatch/JUR-639.mp4"
OLLAMA_BASE = "http://localhost:11434"
VLM_MODEL = "llava:13b"
LLM_MODEL = "fredrezones55/Qwen3.5-Uncensored-HauhauCS-Aggressive:9b"
EXPORT_DIR = "data/exports/rag_test"
FRAMES_DIR = "data/frames/rag_test"
os.makedirs(FRAMES_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)

print("=" * 60)
print("GifAgent RAG Test — JUR-639.mp4")
print("=" * 60)

# ── Phase 1: Scene-detect and extract frames ──────────────────────────
print("\n[1/5] Scene detection + frame extraction...")

# Extract I-frames (keyframes) for fast scene sampling
subprocess.run([
    "ffmpeg", "-y", "-i", VIDEO_PATH, "-t", "5400",
    "-vf", "select='eq(pict_type,I)',scale=640:-1",
    "-vsync", "vfr", f"{FRAMES_DIR}/scene_%06d.jpg"
], capture_output=True, timeout=300)

frame_files = sorted([
    f for f in os.listdir(FRAMES_DIR) if f.endswith('.jpg')
])
print(f"  Scenes detected: {len(frame_files)} frames")

# Filter dark frames (brightness < 30)
from PIL import Image
good_frames = []
for fname in frame_files:
    fpath = os.path.join(FRAMES_DIR, fname)
    try:
        img = Image.open(fpath).convert('L')
        brightness = sum(img.getdata()) / (img.width * img.height)
        if brightness > 30:
            good_frames.append({"path": fpath, "name": fname})
        img.close()
    except Exception:
        pass
print(f"  After dark-frame filter: {len(good_frames)} frames")

# ── Phase 2: RAG search for each frame ───────────────────────────────
print(f"\n[2/5] RAG retrieval for each frame...")

idx = get_index()
conn = get_connection()
rag_results = []

for fi, finfo in enumerate(good_frames[:30]):  # Limit to 30 frames for test
    # Get similar GIFs from FAISS
    # Use a generic embedding of "cinematic movie scene" as query
    query_emb = compute_text_embedding("cinematic movie scene intimate moment tension drama")
    similar = idx.search(query_emb, top_k=3)

    if similar:
        rag_context = "\n".join(
            f"Similar saved GIF {i+1}: {s['emotional_core']} | "
            f"tags: {', '.join(s.get('tags', []))} | summary: {s.get('summary', '?')[:80]}"
            for i, s in enumerate(similar)
        )
    else:
        rag_context = "No similar GIFs found in your collection."

    finfo["rag_context"] = rag_context
    finfo["similar_gifs"] = similar
    rag_results.append(finfo)
    if (fi + 1) % 10 == 0:
        print(f"  [{fi+1}/{min(30, len(good_frames))}] RAG ready")

print(f"  RAG context prepared for {len(rag_results)} frames")

# ── Phase 3: VLM analyze with RAG context ─────────────────────────────
print(f"\n[3/5] VLM analysis with RAG context ({len(rag_results)} frames)...")

FRAME_PROMPT_RAG = """You are analyzing a single frame from a movie or TV show. Focus on CINEMATIC and AESTHETIC qualities.

The user has saved similar GIFs with these styles (use as reference for style consistency):
{rag_context}

Output ONLY a valid JSON object with real, specific content:
{{
  "caption": "describe what you actually see in this specific frame",
  "emotional_core": "ONE lowercase emotion: tension, melancholy, awe, joy, sadness, catharsis, serenity, excitement, dread, nostalgia, admiration, intimacy, vulnerability, longing, desire",
  "aesthetic_notes": ["2-4 concrete visual observations"],
  "why_i_like_it": "one cinephile reason, referencing the visual style"
}}

CRITICAL: emotional_core MUST be EXACTLY ONE word. NEVER pipe-delimited."""

def parse_json(text):
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r'\{[^{}]*\{[^{}]*\}[^{}]*\}|\{[^{}]*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {"_parse_error": True, "_raw": text[:500]}

VALID_EMOTIONS = {"tension", "melancholy", "awe", "joy", "sadness", "catharsis",
                  "serenity", "excitement", "dread", "nostalgia", "admiration",
                  "intimacy", "vulnerability", "longing", "desire", "other"}

vlm_results = []
for fi, finfo in enumerate(rag_results):
    print(f"  [{fi+1}/{len(rag_results)}] {finfo['name']}...")
    with open(finfo["path"], "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")

    prompt = FRAME_PROMPT_RAG.format(rag_context=finfo["rag_context"])

    for attempt in range(3):
        try:
            resp = httpx.post(
                f"{OLLAMA_BASE}/api/generate",
                json={"model": VLM_MODEL, "prompt": prompt, "images": [img_b64], "stream": False},
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

            parsed["frame_name"] = finfo["name"]
            parsed["rag_similar"] = [s["media_id"] for s in finfo.get("similar_gifs", [])]
            vlm_results.append(parsed)
            print(f"    -> emotion={parsed.get('emotional_core', '?')}")
            break
        except Exception as e:
            if attempt == 2:
                print(f"    FAILED: {e}")
            time.sleep(3)

print(f"  VLM analyzed: {len(vlm_results)}/{len(rag_results)}")

# ── Phase 4: LLM synthesis ────────────────────────────────────────────
print(f"\n[4/5] LLM synthesis...")

analyses_text = "\n\n".join(
    f"Frame {i+1}: caption={fa.get('caption','')}, emotion={fa.get('emotional_core','')}, "
    f"aesthetic={fa.get('aesthetic_notes',[])}, why={fa.get('why_i_like_it','')}"
    for i, fa in enumerate(vlm_results)
)

synth_prompt = (
    "Synthesize frame analyses into one cohesive film annotation. Output ONLY JSON:\n"
    "{\n"
    '  "summary": "one sentence describing visual style",\n'
    '  "emotional_core": "one dominant emotion",\n'
    '  "aesthetic_notes": ["2-4 cinematographic qualities"],\n'
    '  "why_i_like_it": "one cinephile reason",\n'
    '  "tags": ["3-5 keywords"],\n'
    '  "scene_type": "close-up | dialogue | action | transition | reaction | establishing | montage | other"\n'
    "}\n\n"
    "Frame analyses:\n" + analyses_text
)

for attempt in range(3):
    try:
        resp = httpx.post(
            f"{OLLAMA_BASE}/api/generate",
            json={"model": LLM_MODEL, "prompt": synth_prompt, "stream": False, "options": {"temperature": 0.3, "num_think": 0}},
            timeout=120,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "")
        synthesis = parse_json(raw)
        if not synthesis.get("_parse_error"):
            break
    except Exception as e:
        if attempt == 2:
            synthesis = {"error": str(e)}
        time.sleep(3)

print(f"  summary: {synthesis.get('summary', '?')}")
print(f"  emotional_core: {synthesis.get('emotional_core', '?')}")
print(f"  tags: {synthesis.get('tags', [])}")

# ── Phase 5: Export top 10 GIF clips ──────────────────────────────────
print(f"\n[5/5] Exporting GIF clips...")

# Pick frames with best aesthetic_notes length as most informative
ranked = sorted(vlm_results, key=lambda x: sum(len(n) for n in (x.get("aesthetic_notes") or ["",""])), reverse=True)
top_frames = ranked[:10]

for i, fa in enumerate(top_frames):
    # Extract timestamp from frame name (scene_XXXXXX.jpg) - estimate from index
    frame_idx = int(fa["frame_name"].replace("scene_", "").replace(".jpg", "")) if fa["frame_name"] else 0
    # Approximate: each scene detected is roughly evenly spaced
    est_ts = frame_idx * 5400 / len(frame_files) if frame_files else 0

    start = max(0, est_ts - 1.5)
    dur = 3.5
    out_gif = f"{EXPORT_DIR}/jur639_rag_scene_{i+1}.gif"

    palette = f"{EXPORT_DIR}/rag_palette_{i+1}.png"
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
        print(f"  Scene {i+1} (t~{int(est_ts)}s): {sz//1024}KB - {fa.get('emotional_core', '?')}")

# ── Save results ──────────────────────────────────────────────────────
output = {
    "video": VIDEO_PATH,
    "rag_index_size": idx.count,
    "frames_analyzed": len(vlm_results),
    "synthesis": synthesis,
    "top_scenes": [
        {
            "rank": i+1,
            "frame_name": fa.get("frame_name"),
            "caption": fa.get("caption"),
            "emotional_core": fa.get("emotional_core"),
            "aesthetic_notes": fa.get("aesthetic_notes"),
            "why_i_like_it": fa.get("why_i_like_it"),
            "similar_gifs": fa.get("rag_similar"),
        }
        for i, fa in enumerate(top_frames)
    ],
}

out_file = "data/test_jur639_rag_result.json"
with open(out_file, "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f"\n{'='*60}")
print(f"RAG Test Complete!")
print(f"  Frames analyzed: {len(vlm_results)}")
print(f"  GIFs exported: {len(top_frames)}")
print(f"  Synthesis: {'OK' if not synthesis.get('_parse_error') else 'FAILED'}")
print(f"  Results: {out_file}")
print(f"{'='*60}")

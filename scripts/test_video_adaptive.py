#!/usr/bin/env python3
"""
Two-pass adaptive GIF extraction:
  Pass 1: coarse sample every N seconds → VLM scores
  Pass 2: around high-score regions, re-sample at finer intervals
  Adjacent high-score frames are merged into longer clips.
  Top-50 ranked by gif_worthiness.
"""
import sys, os, subprocess, json, re, base64, time
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
EXPORT_DIR = "data/exports/adaptive_test"
FRAMES_DIR = "data/frames/adaptive_test"
os.makedirs(FRAMES_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)

# ── Config ────────────────────────────────────────────────────────────
SAMPLE_INTERVAL = 20       # seconds between coarse samples (dense for multi-per-minute)
REFINE_INTERVAL = 10       # seconds for fine sampling around high-score regions
REFINE_RADIUS = 20         # ±seconds around high-score frame to re-sample
REFINE_THRESHOLD = 0.5     # score above which we do fine sampling
MAX_DURATION = 5.0         # max GIF duration (high quality)
MIN_DURATION = 1.5         # min GIF duration (low quality)
WORTHINESS_THRESHOLD = 0.4 # below this, skip entirely
MERGE_GAP = 10             # max seconds between frames to merge (shorter = more independent GIFs)
EMBED_SIM_THRESHOLD = 0.95 # cosine similarity threshold for embedding dedup (higher = stricter)
OUTPUT_RATIO = 0.5         # fraction of total extracted clips to keep as final output
MAX_OUTPUT = 500           # absolute cap on output count (0 = no cap)
GIF_MAX_WIDTH = 1920       # max output width (0 = use source resolution)

print("=" * 60)
print(f"Adaptive GIF Extraction — {SAMPLE_INTERVAL}s intervals, ratio={OUTPUT_RATIO}, cap={MAX_OUTPUT}")
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
    try: return json.loads(text)
    except: pass
    m = re.search(r"\{[^{}]*\{[^{}]*\}[^{}]*\}|\{[^{}]*\}", text, re.DOTALL)
    if m:
        try: return json.loads(m.group(0))
        except: pass
    return {"_parse_error": True, "_raw": text[:500]}

VALID_EMOTIONS = {"tension","melancholy","awe","joy","sadness","catharsis",
                  "serenity","excitement","dread","nostalgia","admiration",
                  "intimacy","vulnerability","longing","desire","other"}

SCORE_PROMPT = (
    "You are evaluating a film frame for GIF potential. Be DECISIVE - use the full 0.0-1.0 scale.\n"
    "Output ONLY JSON:\n"
    '{"caption":"what you see","emotional_core":"one word","gif_worthiness":0.5,'
    '"aesthetic_notes":["2-3 observations"],"reason":"why this works as a GIF (or why not)"}\n\n'
    "gif_worthiness scale - SPREAD YOUR SCORES:\n"
    "  0.0-0.2: BAD - static, dark, blurry, nothing happening, empty frame, skip.\n"
    "  0.2-0.4: BELOW AVERAGE - barely interesting, single person standing, generic background.\n"
    "  0.4-0.6: AVERAGE - some emotion visible, decent composition, could work as context GIF.\n"
    "  0.6-0.8: GOOD - clear emotion or action, cinematic framing, would save to collection.\n"
    "  0.8-1.0: EXCELLENT - iconic shot, peak drama, beautiful lighting, perfect reaction GIF material.\n\n"
    "IMPORTANT: Do NOT give everything 0.5-0.7. Use the extremes. Bad frames get 0.1. Great frames get 0.9.\n"
    "CRITICAL: emotional_core = EXACTLY ONE lowercase word from: "
    "tension|melancholy|awe|joy|sadness|catharsis|serenity|excitement|dread|nostalgia|admiration|intimacy|vulnerability|longing|desire|other"
)

def stop_model(name):
    subprocess.run(["wsl","ollama","stop",name], capture_output=True, timeout=30)

def wait_model(name, timeout_s=120):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = httpx.post(f"{OLLAMA_BASE}/api/generate",
                          json={"model":name,"prompt":"ping","stream":False}, timeout=10)
            if r.status_code == 200: return True
        except: pass
        time.sleep(3)
    return False

# ── Phase 1: Probe video + sample frames ──────────────────────────────
print("\n[1/4] Probing video + extracting samples...")

# Get duration
probe = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
    "-of","default=noprint_wrappers=1:nokey=1", VIDEO_PATH], capture_output=True, text=True)
total_duration = float(probe.stdout.strip())
print(f"  Duration: {total_duration:.0f}s ({total_duration/60:.0f} min)")

# Generate sample timestamps
timestamps = list(range(SAMPLE_INTERVAL, int(total_duration) - int(MAX_DURATION), SAMPLE_INTERVAL))
print(f"  Sampling {len(timestamps)} timestamps")

# Extract one frame per timestamp
sample_frames = []
for i, ts in enumerate(timestamps):
    out_path = f"{FRAMES_DIR}/ts_{ts:06d}.jpg"
    subprocess.run([
        "ffmpeg","-y","-ss",str(ts),"-i",VIDEO_PATH,
        "-vf","scale=640:-1","-vframes","1",out_path
    ], capture_output=True, timeout=15)

    if os.path.exists(out_path) and os.path.getsize(out_path) > 500:
        # Quick brightness check
        try:
            img = Image.open(out_path).convert('L')
            brightness = sum(img.getdata()) / max(1, img.width * img.height)
            img.close()
            if brightness > 25:
                sample_frames.append({"path": out_path, "timestamp": ts})
        except: pass

    if (i+1) % 50 == 0:
        print(f"  [{i+1}/{len(timestamps)}] extracted, {len(sample_frames)} kept")

print(f"  Frames after dark filter: {len(sample_frames)}")

# ── Phase 2: VLM scoring ─────────────────────────────────────────────
print(f"\n[2/4] VLM scoring ({len(sample_frames)} frames)...")

stop_model("Qwen3-14B-GGUF")
stop_model("nomic-embed-text")
time.sleep(5)
if not wait_model(VLM_MODEL):
    print("ERROR: VLM not responding"); sys.exit(1)

scored = []
for fi, sf in enumerate(sample_frames):
    with open(sf["path"], "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")

    for attempt in range(3):
        try:
            resp = httpx.post(f"{OLLAMA_BASE}/api/generate",
                json={"model":VLM_MODEL,"prompt":SCORE_PROMPT,"images":[img_b64],"stream":False}, timeout=120)
            resp.raise_for_status()
            raw = resp.json().get("response","")
            parsed = parse_json(raw)

            raw_emotion = (parsed.get("emotional_core") or "").strip().lower()
            if raw_emotion and raw_emotion not in VALID_EMOTIONS:
                parts = [p.strip() for p in raw_emotion.replace("|",",").split(",")]
                found = next((p for p in parts if p in VALID_EMOTIONS), None)
                parsed["emotional_core"] = found if found else "other"

            raw_cap = (parsed.get("caption") or "").strip()
            if raw_cap.startswith("describe what") or raw_cap.startswith("concise"):
                parsed["caption"] = ""

            worth = float(parsed.get("gif_worthiness", 0.5))
            parsed["gif_worthiness"] = max(0.0, min(1.0, worth))
            parsed["timestamp"] = sf["timestamp"]

            if parsed["gif_worthiness"] >= WORTHINESS_THRESHOLD:
                scored.append(parsed)

            if (fi+1) % 30 == 0:
                avg = sum(s["gif_worthiness"] for s in scored) / max(1, len(scored))
                print(f"  [{fi+1}/{len(sample_frames)}] scored={len(scored)} kept, avg_worth={avg:.2f}")
            break
        except Exception as e:
            if attempt == 2: print(f"  [{fi+1}] FAILED: {e}")
            time.sleep(2)

print(f"  Scored: {len(scored)} frames kept (threshold={WORTHINESS_THRESHOLD})")

# Show distribution
bins = {"0.0-0.3":0,"0.3-0.5":0,"0.5-0.7":0,"0.7-0.9":0,"0.9-1.0":0}
for s in scored:
    w = s["gif_worthiness"]
    if w < 0.3: bins["0.0-0.3"] += 1
    elif w < 0.5: bins["0.3-0.5"] += 1
    elif w < 0.7: bins["0.5-0.7"] += 1
    elif w < 0.9: bins["0.7-0.9"] += 1
    else: bins["0.9-1.0"] += 1
print(f"  Worthiness distribution: {bins}")

# ── Phase 2.5: Boundary refinement ────────────────────────────────────
print(f"\n[2.5/4] Boundary refinement around high-score regions...")

# Find timestamps that scored above refine threshold
high_ts = {r["timestamp"] for r in scored if r["gif_worthiness"] >= REFINE_THRESHOLD}
refine_ts = set()  # default, may be populated below

# Generate refinement timestamps
refine_ts = set()
for ts in high_ts:
    for offset in range(-REFINE_RADIUS, REFINE_RADIUS + REFINE_INTERVAL, REFINE_INTERVAL):
        new_ts = ts + offset
        if 0 <= new_ts <= total_duration - 1 and new_ts not in {r["timestamp"] for r in scored}:
            refine_ts.add(new_ts)

# Remove duplicates with existing samples
existing_ts = {r["timestamp"] for r in scored}
refine_ts -= existing_ts

print(f"  High-score regions: {len(high_ts)}, new frames to sample: {len(refine_ts)}")

if refine_ts:
    refine_frames = []
    for ts in sorted(refine_ts):
        out_path = f"{FRAMES_DIR}/ts_{ts:06d}.jpg"
        subprocess.run([
            "ffmpeg","-y","-ss",str(ts),"-i",VIDEO_PATH,
            "-vf","scale=640:-1","-vframes","1",out_path
        ], capture_output=True, timeout=15)

        if os.path.exists(out_path) and os.path.getsize(out_path) > 500:
            try:
                img = Image.open(out_path).convert('L')
                brightness = sum(img.getdata()) / max(1, img.width * img.height)
                img.close()
                if brightness > 25:
                    refine_frames.append({"path": out_path, "timestamp": ts})
            except: pass

    print(f"  Refinement frames after filter: {len(refine_frames)}")

    # Score refinement frames
    for fi, rf in enumerate(refine_frames):
        with open(rf["path"], "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")

        for attempt in range(3):
            try:
                resp = httpx.post(f"{OLLAMA_BASE}/api/generate",
                    json={"model":VLM_MODEL,"prompt":SCORE_PROMPT,"images":[img_b64],"stream":False}, timeout=120)
                resp.raise_for_status()
                raw = resp.json().get("response","")
                parsed = parse_json(raw)

                raw_emotion = (parsed.get("emotional_core") or "").strip().lower()
                if raw_emotion and raw_emotion not in VALID_EMOTIONS:
                    parts = [p.strip() for p in raw_emotion.replace("|",",").split(",")]
                    found = next((p for p in parts if p in VALID_EMOTIONS), None)
                    parsed["emotional_core"] = found if found else "other"

                worth = float(parsed.get("gif_worthiness", 0.5))
                parsed["gif_worthiness"] = max(0.0, min(1.0, worth))
                parsed["timestamp"] = rf["timestamp"]

                if parsed["gif_worthiness"] >= WORTHINESS_THRESHOLD:
                    scored.append(parsed)
                break
            except Exception as e:
                if attempt == 2:
                    print(f"  refine [{fi+1}] FAILED: {e}")
                time.sleep(2)

    print(f"  After refinement: {len(scored)} total scored frames")

# ── Merge adjacent high-score frames into clip groups ─────────────────
print(f"\n[2.6/4] Merging adjacent frames into clips...")

# Sort by timestamp
scored.sort(key=lambda x: x["timestamp"])

# Group frames that are close together
clips = []
current_group = [scored[0]]

for r in scored[1:]:
    gap = r["timestamp"] - current_group[-1]["timestamp"]
    if gap <= MERGE_GAP:
        current_group.append(r)
    else:
        # Finalize current group: use best frame's worthiness as group score
        best = max(current_group, key=lambda x: x["gif_worthiness"])
        clips.append({
            "start_ts": current_group[0]["timestamp"],
            "end_ts": current_group[-1]["timestamp"],
            "best_frame": best,
            "frame_count": len(current_group),
            "gif_worthiness": best["gif_worthiness"],
            "emotional_core": best.get("emotional_core","?"),
        })
        current_group = [r]

# Don't forget the last group
if current_group:
    best = max(current_group, key=lambda x: x["gif_worthiness"])
    clips.append({
        "start_ts": current_group[0]["timestamp"],
        "end_ts": current_group[-1]["timestamp"],
        "best_frame": best,
        "frame_count": len(current_group),
        "gif_worthiness": best["gif_worthiness"],
        "emotional_core": best.get("emotional_core","?"),
    })

print(f"  Merged into {len(clips)} clips (merge_gap={MERGE_GAP}s)")
multi_frame = sum(1 for c in clips if c["frame_count"] > 1)
print(f"  Multi-frame clips (crossing boundaries): {multi_frame}")
single_frame = sum(1 for c in clips if c["frame_count"] == 1)
print(f"  Single-frame clips: {single_frame}")

# ── Phase 2.7: Embedding-based dedup (Plan D) ──────────────────────────
# After time-based merging, deduplicate by caption similarity
print(f"\n[2.7/4] Embedding dedup (threshold={EMBED_SIM_THRESHOLD})...")

# Compute text embeddings for each clip's best frame caption
import numpy as np
clip_texts = [c["best_frame"].get("caption", "") or f"frame_{c['start_ts']}" for c in clips]

# Compute embeddings in batch (using FAISS index's embed function)
clip_embs = []
for i, ct in enumerate(clip_texts):
    try:
        emb = compute_text_embedding(ct)
        clip_embs.append(emb)
    except Exception:
        clip_embs.append(None)
    if (i + 1) % 100 == 0:
        print(f"  [{i+1}/{len(clips)}] embeddings computed")

# Greedy clustering by cosine similarity
from collections import defaultdict
clusters = []  # list of {"center_emb": [...], "members": [clip_index, ...]}

for i, emb in enumerate(clip_embs):
    if emb is None:
        clusters.append({"center_emb": None, "members": [i]})
        continue

    vec = np.array(emb, dtype=np.float32)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm  # normalize

    assigned = False
    for c in clusters:
        if c["center_emb"] is not None:
            sim = float(np.dot(vec, c["center_emb"]))
            if sim >= EMBED_SIM_THRESHOLD:
                c["members"].append(i)
                assigned = True
                break
    if not assigned:
        clusters.append({"center_emb": vec, "members": [i]})

print(f"  {len(clips)} clips → {len(clusters)} clusters (dedup ratio: {1-len(clusters)/len(clips):.1%})")

# Per cluster: keep top-1 by worthiness, plus top-2 if cluster > 5 members (preserve variety)
deduped_clips = []
for c in clusters:
    members_sorted = sorted(c["members"], key=lambda idx: clips[idx]["gif_worthiness"], reverse=True)
    keep_count = 3 if len(members_sorted) > 10 else (2 if len(members_sorted) > 3 else 1)
    for idx in members_sorted[:keep_count]:
        deduped_clips.append(clips[idx])

print(f"  After dedup: {len(deduped_clips)} clips kept")

# ── Phase 3: RAG + LLM synthesis ──────────────────────────────────────
print(f"\n[3/4] RAG + LLM synthesis...")

# Switch to LLM
stop_model("llava")
time.sleep(10)
if not wait_model(LLM_MODEL, timeout_s=180):
    print("ERROR: LLM not responding"); sys.exit(1)

# Per-frame RAG using FAISS
idx = get_index()
for r in scored:
    caption = r.get("caption","")
    if caption and idx.count > 0:
        try:
            emb = compute_text_embedding(caption)
            similar = idx.search(emb, top_k=3)
            r["rag_similar"] = [{"mid":s["media_id"],"score":s["score"],
                "emo":s.get("emotional_core",""),"tags":s.get("tags",[])[:3]} for s in similar]
        except: r["rag_similar"] = []
    else: r["rag_similar"] = []

# LLM synthesis with deduped clips
top_for_synth = sorted(deduped_clips, key=lambda x: x["gif_worthiness"], reverse=True)[:20]
analyses = "\n\n".join(
    f"Frame {i+1} (t={c['best_frame']['timestamp']}s, worth={c['gif_worthiness']:.2f}): "
    f"caption={c['best_frame'].get('caption','')}, emotion={c['best_frame'].get('emotional_core','')}"
    for i,c in enumerate(top_for_synth)
)

synth_prompt = (
    "Synthesize scene analyses from a film. Output ONLY JSON:\n"
    '{"summary":"one sentence about visual style","emotional_core":"one dominant emotion",'
    '"aesthetic_notes":["2-4 qualities"],"tags":["3-5 keywords"],'
    '"scene_type":"close-up|dialogue|action|transition|reaction|establishing|montage|other"}\n\n'
    "Scene analyses:\n" + analyses
)

synthesis = {"_parse_error": True}
for attempt in range(3):
    try:
        resp = httpx.post(f"{OLLAMA_BASE}/api/generate",
            json={"model":LLM_MODEL,"prompt":synth_prompt,"stream":False,"options":{"temperature":0.3}}, timeout=180)
        resp.raise_for_status()
        raw = resp.json().get("response","") or resp.json().get("thinking","")
        synthesis = parse_json(raw)
        if not synthesis.get("_parse_error"):
            print(f"  summary: {synthesis.get('summary','?')}")
            print(f"  emotional_core: {synthesis.get('emotional_core','?')}")
            print(f"  tags: {synthesis.get('tags',[])}")
            break
    except Exception as e:
        print(f"  Attempt {attempt+1}: {e}")
        time.sleep(5)

# ── Phase 4: Export adaptive-duration GIFs ─────────────────────────────
# Determine output count: ratio of total deduped clips, capped at MAX_OUTPUT
output_count = int(len(deduped_clips) * OUTPUT_RATIO)
if MAX_OUTPUT > 0:
    output_count = min(output_count, MAX_OUTPUT)
output_count = max(1, output_count)

print(f"\n[4/4] Exporting {output_count}/{len(deduped_clips)} GIFs (4K) "
      f"({OUTPUT_RATIO*100:.0f}% ratio, cap={MAX_OUTPUT})...")

# Rank clips by gif_worthiness, take top N
ranked_clips = sorted(deduped_clips, key=lambda x: x["gif_worthiness"], reverse=True)[:output_count]

for i, clip in enumerate(ranked_clips):
    worth = clip["gif_worthiness"]
    r = clip["best_frame"]

    # Use the clip's natural span if multi-frame, otherwise adaptive duration
    if clip["frame_count"] > 1:
        duration = min(clip["end_ts"] - clip["start_ts"] + 3.0, MAX_DURATION + 2.0)
    else:
        duration = MIN_DURATION + (MAX_DURATION - MIN_DURATION) * worth

    # Center the clip around the best frame
    ts = r["timestamp"]
    start = max(0, ts - duration * 0.4)  # bias towards starting before the peak
    start = min(start, total_duration - duration)  # don't exceed video end

    out_gif = f"{EXPORT_DIR}/adapt_{i+1:03d}_w{worth:.2f}_t{int(ts)}s.gif"
    palette = f"{EXPORT_DIR}/pal_{i+1:03d}.png"

    fps = 10 if worth > 0.6 else 8  # 10fps for 4K to keep file sizes reasonable

    subprocess.run([
        "ffmpeg","-y","-ss",str(start),"-t",str(duration),"-i",VIDEO_PATH,
        "-vf",f"fps={fps},scale={GIF_MAX_WIDTH}:-1:flags=lanczos,palettegen",palette
    ], capture_output=True, timeout=60)

    subprocess.run([
        "ffmpeg","-y","-ss",str(start),"-t",str(duration),"-i",VIDEO_PATH,
        "-i",palette,
        "-filter_complex",f"fps={fps},scale={GIF_MAX_WIDTH}:-1:flags=lanczos[x];[x][1:v]paletteuse",
        out_gif
    ], capture_output=True, timeout=60)

    # Clean up palette PNG immediately after GIF generation
    if os.path.exists(palette):
        os.remove(palette)

    if os.path.exists(out_gif):
        sz = os.path.getsize(out_gif)
        merged = "merged" if clip["frame_count"] > 1 else "single"
        print(f"  #{i+1:2d} w={worth:.2f} dur={duration:.1f}s [{merged}:{clip['frame_count']}fr] "
              f"{sz//1024:4d}KB t={int(ts)}s {r.get('emotional_core','?')}")

# ── Save results ──────────────────────────────────────────────────────
output = {
    "video": VIDEO_PATH,
    "sample_interval": SAMPLE_INTERVAL,
    "total_samples": len(sample_frames),
    "scored_kept": len(scored),
    "worthiness_distribution": bins,
    "synthesis": synthesis,
    "sample_interval": SAMPLE_INTERVAL,
    "merge_gap": MERGE_GAP,
    "refine_radius": REFINE_RADIUS,
    "refine_interval": REFINE_INTERVAL,
    "output_ratio": OUTPUT_RATIO,
    "max_output": MAX_OUTPUT,
    "embed_dedup_threshold": EMBED_SIM_THRESHOLD,
    "total_clips": len(clips),
    "deduped_clips": len(deduped_clips),
    "clusters_after_dedup": len(clusters),
    "output_count": output_count,
    "multi_frame_clips": sum(1 for c in clips if c["frame_count"] > 1),
    "top_clips": [
        {"rank":i+1,"timestamp":clip["best_frame"]["timestamp"],
         "start_ts":clip["start_ts"],"end_ts":clip["end_ts"],
         "gif_worthiness":clip["gif_worthiness"],
         "duration":min(clip["end_ts"]-clip["start_ts"]+3.0, MAX_DURATION+2.0) if clip["frame_count"]>1 else MIN_DURATION+(MAX_DURATION-MIN_DURATION)*clip["gif_worthiness"],
         "frame_count":clip["frame_count"],"merged":clip["frame_count"]>1,
         "caption":clip["best_frame"].get("caption"),
         "emotional_core":clip["best_frame"].get("emotional_core"),
         "aesthetic_notes":clip["best_frame"].get("aesthetic_notes"),
         "reason":clip["best_frame"].get("reason")}
        for i,clip in enumerate(ranked_clips)
    ],
}

with open("data/adaptive_test_result.json","w",encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

# Stats
durations = [min(c["end_ts"]-c["start_ts"]+3.0, MAX_DURATION+2.0) if c["frame_count"]>1
             else MIN_DURATION+(MAX_DURATION-MIN_DURATION)*c["gif_worthiness"]
             for c in ranked_clips]
emotions = {}
for c in ranked_clips: e=c["best_frame"].get("emotional_core","?"); emotions[e]=emotions.get(e,0)+1
merged_count = sum(1 for c in ranked_clips if c["frame_count"]>1)

print(f"\n{'='*60}")
print(f"Two-pass adaptive extraction complete!")
print(f"  Sampling: every {SAMPLE_INTERVAL}s, refine {REFINE_RADIUS}s radius @ {REFINE_INTERVAL}s")
print(f"  Pass 1: {len(sample_frames)} coarse frames scored")
print(f"  Pass 2: {len(refine_ts)} refinement frames around {len(high_ts)} high-score regions")
print(f"  Clips: {len(clips)} total ({multi_frame} merged, merge_gap={MERGE_GAP}s)")
print(f"  Plan D dedup: {len(clips)} → {len(deduped_clips)} clips ({len(clusters)} clusters)")
print(f"  Output: {output_count} GIFs @ max {GIF_MAX_WIDTH}px (ratio={OUTPUT_RATIO}, cap={MAX_OUTPUT})")
print(f"  Duration: {min(durations):.1f}s - {max(durations):.1f}s")
print(f"  Worthiness: {min(c['gif_worthiness'] for c in ranked_clips):.2f} - {max(c['gif_worthiness'] for c in ranked_clips):.2f}")
print(f"  Emotions: {dict(sorted(emotions.items(), key=lambda x:-x[1]))}")
print(f"  Export: {EXPORT_DIR}/")
print(f"{'='*60}")

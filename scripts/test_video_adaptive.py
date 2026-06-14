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
from app.db import init_db, get_connection, load_config
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
SAMPLE_INTERVAL = 60       # seconds between coarse samples
REFINE_INTERVAL = 15       # seconds for fine sampling around high-score regions
REFINE_RADIUS = 30         # ±seconds around high-score frame to re-sample
REFINE_THRESHOLD = 0.5     # score above which we do fine sampling
MAX_DURATION = 5.0         # max GIF duration (high quality)
MIN_DURATION = 1.5         # min GIF duration (low quality)
WORTHINESS_THRESHOLD = 0.4 # below this, skip entirely
MERGE_GAP = 45             # max seconds between high-score frames to merge into one clip
TOP_K = 50                 # number to return

print("=" * 60)
print(f"Adaptive GIF Extraction — {SAMPLE_INTERVAL}s intervals, top {TOP_K}")
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
    "You are evaluating a frame from a film for GIF potential. Output ONLY JSON:\n"
    '{"caption":"what you see","emotional_core":"one word","gif_worthiness":0.5,'
    '"aesthetic_notes":["2-3 observations"],"reason":"why this would make a good GIF (or not)"}\n\n'
    "gif_worthiness: 0.0 to 1.0 scale.\n"
    "  0.0-0.3: static shot, nothing happening, dark/blurry, skip.\n"
    "  0.3-0.5: mildly interesting, single character, basic composition.\n"
    "  0.5-0.7: good scene, clear emotion or action, cinematic framing.\n"
    "  0.7-0.9: excellent moment, strong emotion/movement, beautiful composition.\n"
    "  0.9-1.0: iconic shot, peak drama, would go viral as a reaction GIF.\n"
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
timestamps = list(range(SAMPLE_INTERVAL, int(total_duration) - MAX_DURATION, SAMPLE_INTERVAL))
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

# LLM synthesis with scored frames
top_for_synth = sorted(scored, key=lambda x: x["gif_worthiness"], reverse=True)[:20]
analyses = "\n\n".join(
    f"Frame {i+1} (t={r['timestamp']}s, worth={r['gif_worthiness']:.2f}): "
    f"caption={r.get('caption','')}, emotion={r.get('emotional_core','')}"
    for i,r in enumerate(top_for_synth)
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
print(f"\n[4/4] Exporting top {TOP_K} GIFs (adaptive boundaries)...")

# Rank clips by gif_worthiness, take top K
ranked_clips = sorted(clips, key=lambda x: x["gif_worthiness"], reverse=True)[:TOP_K]

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

    fps = 12 if worth > 0.6 else 8

    subprocess.run([
        "ffmpeg","-y","-ss",str(start),"-t",str(duration),"-i",VIDEO_PATH,
        "-vf",f"fps={fps},scale=480:-1:flags=lanczos,palettegen",palette
    ], capture_output=True, timeout=30)

    subprocess.run([
        "ffmpeg","-y","-ss",str(start),"-t",str(duration),"-i",VIDEO_PATH,
        "-i",palette,
        "-filter_complex",f"fps={fps},scale=480:-1:flags=lanczos[x];[x][1:v]paletteuse",
        out_gif
    ], capture_output=True, timeout=30)

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
    "merge_gap": MERGE_GAP,
    "refine_radius": REFINE_RADIUS,
    "refine_interval": REFINE_INTERVAL,
    "merged_clips": len(clips),
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
print(f"  Pass 1: {len(sample_frames)} coarse frames → {len(scored)-len(refine_ts)} scored")
print(f"  Pass 2: {len(refine_ts)} refinement frames around {len(high_ts)} high-score regions")
print(f"  Merged into {len(clips)} clips ({multi_frame} crossing time boundaries)")
print(f"  Top {TOP_K}: {merged_count} merged, {TOP_K-merged_count} single, dur {min(durations):.1f}s-{max(durations):.1f}s")
print(f"  Worthiness range: {min(c['gif_worthiness'] for c in ranked_clips):.2f}-{max(c['gif_worthiness'] for c in ranked_clips):.2f}")
print(f"  Emotions: {dict(sorted(emotions.items(), key=lambda x:-x[1]))}")
print(f"  Export: {EXPORT_DIR}/")
print(f"{'='*60}")

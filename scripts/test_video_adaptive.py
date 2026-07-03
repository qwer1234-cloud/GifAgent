#!/usr/bin/env python3
"""
Two-pass adaptive GIF extraction:
  Pass 1: coarse sample every N seconds → VLM scores
  Pass 2: around high-score regions, re-sample at finer intervals
  Adjacent high-score frames are merged into longer clips.
  Top-50 ranked by gif_worthiness.
"""
import sys, os, subprocess, json, re, base64, time, argparse
import httpx
from PIL import Image

sys.path.insert(0, '.')
from app.db import init_db, get_connection
from app.config import load_config, get
from app.services.embedding import compute_text_embedding
from app.services.indexer import get_index
from app.services.json_guard import parse_json_response
from app.services.llm_client import generate_llm_text, is_local_llm, llm_model_name, wait_for_llm
from app.services.quality import validate_frame_analysis, normalize_emotional_core

load_config()
init_db()

# ── CLI ──────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Adaptive GIF extraction from video")
parser.add_argument("--video", default=None, help="Video file path")
parser.add_argument("--export-dir", default=None, help="Export directory for GIFs")
args_cli, _ = parser.parse_known_args()

if args_cli.video:
    VIDEO_PATH = args_cli.video
else:
    VIDEO_PATH = "C:/Users/sunhao/Desktop/ToWatch/JUR-639.mp4"

OLLAMA_BASE = "http://localhost:11434"
VLM_MODEL = "llava:13b"
LLM_MODEL = llm_model_name()

video_name = os.path.splitext(os.path.basename(VIDEO_PATH))[0]
if args_cli.export_dir:
    EXPORT_DIR = os.path.join(args_cli.export_dir, video_name)
else:
    EXPORT_DIR = "data/exports/adaptive_test"

FRAMES_DIR = f"data/frames/adaptive_test/{video_name}"
os.makedirs(FRAMES_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)
print(f"Video: {os.path.basename(VIDEO_PATH)}")
print(f"Export: {EXPORT_DIR}")

# ── Config ────────────────────────────────────────────────────────────
# Read from configs/models.yaml [adaptive] section, fall back to defaults
_adaptive = get("adaptive", {}) or {}
SAMPLE_INTERVAL = int(_adaptive.get("sample_interval", 10))
REFINE_INTERVAL = int(_adaptive.get("refine_interval", 10))
REFINE_RADIUS = int(_adaptive.get("refine_radius", 20))
REFINE_THRESHOLD = float(_adaptive.get("refine_threshold", 0.5))
MAX_DURATION = 5.0
MIN_DURATION = 1.5
WORTHINESS_THRESHOLD = float(_adaptive.get("worthiness_threshold", 0.2))
MERGE_GAP = int(_adaptive.get("merge_gap", 12))
MERGE_SCORE_THRESHOLD = float(_adaptive.get("merge_score_threshold", 0.55))
EMBED_SIM_THRESHOLD = 0.95
OUTPUT_RATIO = float(_adaptive.get("output_ratio", 1.0))
MAX_OUTPUT = int(_adaptive.get("max_output", 0))
GIF_MAX_WIDTH = 1920

VLM_OPTIONS = {
    "temperature": float(_adaptive.get("vlm_temperature", 0.65)),
    "top_p": float(_adaptive.get("vlm_top_p", 0.95)),
    "top_k": int(_adaptive.get("vlm_top_k", 60)),
    "num_think": 0,
}

print("=" * 60)
print(f"Adaptive GIF Extraction — {SAMPLE_INTERVAL}s intervals, ratio={OUTPUT_RATIO}, cap={MAX_OUTPUT}")
print("=" * 60)

# ── Helpers ───────────────────────────────────────────────────────────

def parse_vlm_response(raw_text: str) -> dict:
    """Parse VLM response through quality gate, return cleaned dict."""
    result = parse_json_response(raw_text)
    if not result.ok:
        return {"_parse_error": True, "_raw": raw_text[:500]}
    cleaned, errors = validate_frame_analysis(result.data)
    if errors:
        cleaned["_quality_errors"] = errors
    return cleaned

SCORE_PROMPT = (
    "Evaluate this film frame for GIF potential. Use the full 0.0-1.0 scale.\n"
    "Output ONLY valid JSON with real, specific content. No template text.\n\n"
    '{"caption":"describe actual visible subjects, lighting, and composition",'
    '"emotional_core":"one lowercase word","gif_worthiness":0.5,'
    '"aesthetic_notes":["2-3 concrete visual observations"],'
    '"reason":"why this specific moment works as a GIF (or why not)"}\n\n'
    "gif_worthiness scale:\n"
    "  0.0-0.2: BAD - static, dark, blurry, nothing happening. Skip.\n"
    "  0.3-0.5: AVERAGE - some emotion, decent composition.\n"
    "  0.6-0.8: GOOD - clear emotion/action, cinematic framing.\n"
    "  0.9-1.0: EXCELLENT - iconic moment, beautiful lighting, peak drama.\n\n"
    "CRITICAL: emotional_core = EXACTLY ONE lowercase word from: "
    "tension|melancholy|awe|joy|sadness|catharsis|serenity|excitement|dread|nostalgia|admiration|intimacy|vulnerability|longing|desire|other\n"
    "NEVER output 'what you see', '2-3 observations', or pipe-delimited emotions."
)

def safe_worth(value):
    """Parse gif_worthiness robustly — VLM sometimes returns text labels instead of numbers."""
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, str):
        lowered = value.strip().lower()
        try:
            return max(0.0, min(1.0, float(lowered)))
        except ValueError:
            pass
        if "excellent" in lowered: return 0.9
        if "good" in lowered: return 0.7
        if "average" in lowered: return 0.4
        if "bad" in lowered: return 0.15
    return 0.5  # fallback

def stop_model(name):
    """Stop an Ollama model and wait until it's fully unloaded from GPU."""
    for attempt in range(3):
        subprocess.run(["wsl","ollama","stop",name], capture_output=True, timeout=30)
        time.sleep(5)
        # Verify it's actually unloaded
        try:
            r = httpx.get(f"{OLLAMA_BASE}/api/ps", timeout=5)
            loaded = {m.get("name","") for m in r.json().get("models",[])}
            # Check if any matching model is still loaded
            still_loaded = any(name.split(":")[0] in m for m in loaded)
            if not still_loaded:
                return True
        except Exception:
            pass
        time.sleep(10)
    return False

def wait_model(name, timeout_s=120):
    """Wait for an Ollama model to be ready, loading it if needed."""
    deadline = time.time() + timeout_s
    load_triggered = False
    while time.time() < deadline:
        try:
            r = httpx.post(f"{OLLAMA_BASE}/api/generate",
                          json={"model":name,"prompt":"ping","stream":False}, timeout=30)
            if r.status_code == 200:
                return True
            # If server is busy loading, keep waiting
            if r.status_code == 503:
                time.sleep(10)
                continue
        except Exception:
            pass
        # Trigger model load on first attempt
        if not load_triggered:
            try:
                httpx.post(f"{OLLAMA_BASE}/api/generate",
                    json={"model":name,"prompt":"ping","stream":False,"options":{"num_predict":1}}, timeout=5)
            except Exception:
                pass
            load_triggered = True
        time.sleep(5)
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

if is_local_llm():
    stop_model(LLM_MODEL.split("/")[-1].split(":")[0])
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
                json={"model":VLM_MODEL,"prompt":SCORE_PROMPT,"images":[img_b64],"stream":False,"options":VLM_OPTIONS}, timeout=120)
            resp.raise_for_status()
            raw = resp.json().get("response","")
            parsed = parse_vlm_response(raw)

            worth = safe_worth(parsed.get("gif_worthiness", 0.5))
            parsed["gif_worthiness"] = worth
            parsed["timestamp"] = sf["timestamp"]

            if worth >= WORTHINESS_THRESHOLD:
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
                    json={"model":VLM_MODEL,"prompt":SCORE_PROMPT,"images":[img_b64],"stream":False,"options":VLM_OPTIONS}, timeout=120)
                resp.raise_for_status()
                raw = resp.json().get("response","")
                parsed = parse_vlm_response(raw)

                worth = safe_worth(parsed.get("gif_worthiness", 0.5))
                parsed["gif_worthiness"] = worth
                parsed["timestamp"] = rf["timestamp"]

                if worth >= WORTHINESS_THRESHOLD:
                    scored.append(parsed)
                break
            except Exception as e:
                if attempt == 2:
                    print(f"  refine [{fi+1}] FAILED: {e}")
                time.sleep(2)

        if (fi+1) % 50 == 0:
            print(f"  refine [{fi+1}/{len(refine_frames)}] done, scored={len(scored)}")

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
    both_good = (r["gif_worthiness"] >= MERGE_SCORE_THRESHOLD
                 and current_group[-1]["gif_worthiness"] >= MERGE_SCORE_THRESHOLD)
    if gap <= MERGE_GAP and both_good:
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

# ── Phase 2.7: Skip embedding dedup, pass all clips through ────────────
deduped_clips = clips
clusters = [{"center_emb": None, "members": [i]} for i in range(len(clips))]
print(f"\n[2.7/4] Dedup skipped — {len(deduped_clips)} clips passed through")

# ── Phase 3: RAG + LLM synthesis (non-fatal — skip if LLM unavailable) ──
print(f"\n[3/4] RAG + LLM synthesis...")

# Switch to LLM
stop_model("llava")
time.sleep(10)
if not wait_for_llm(timeout_s=180):
    print("WARNING: LLM not responding — skipping synthesis, proceeding to export")
    synthesis = {"_parse_error": True}

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
llm_available = wait_for_llm(timeout_s=180)
if llm_available:
    for attempt in range(3):
        try:
            raw = generate_llm_text(synth_prompt, temperature=0.3, timeout=180)
            result = parse_json_response(raw)
            if result.ok:
                synthesis = result.data
                print(f"  summary: {synthesis.get('summary','?')}")
                print(f"  emotional_core: {synthesis.get('emotional_core','?')}")
                print(f"  tags: {synthesis.get('tags',[])}")
                break
            else:
                synthesis = {"_parse_error": True, "_raw": raw[:500]}
                print(f"  Attempt {attempt+1}: JSON parse failed")
        except Exception as e:
            print(f"  Attempt {attempt+1}: {e}")
            time.sleep(5)
else:
    print("  Skipping LLM synthesis — model unavailable, proceeding to GIF export")

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

# Optional: Preference Memory reranking
if get("preference_memory.enabled", False):
    from app.services.reranker import PreferenceReranker
    reranker = PreferenceReranker(get_connection())
    for clip in ranked_clips:
        caption = clip["best_frame"].get("caption", "")
        if caption:
            try:
                vec = compute_text_embedding(caption)
                if vec is not None:
                    emo = clip["best_frame"].get("emotional_core", "")
                    scenario_keys = [f"emotion:{emo}"] if (emo and emo != "?") else []
                    breakdown = reranker.score(
                        candidate_vector=vec,
                        base_rag_similarity=clip["gif_worthiness"],
                        scenario_keys=scenario_keys,
                        profile_version=None,
                        enabled=True,
                    )
                    clip["final_score"] = breakdown["final_score"]
                    clip["profile_score"] = breakdown.get("profile_score")
                    clip["score_profile_version"] = breakdown.get("preference_profile_version")
            except Exception:
                pass  # reranking is best-effort, not critical path


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

    start_ts = int(start)
    end_ts = int(start + duration)
    video_name = os.path.splitext(os.path.basename(VIDEO_PATH))[0]
    out_gif = f"{EXPORT_DIR}/{video_name}@@@{i+1:03d}_{start_ts}s-{end_ts}s.gif"
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

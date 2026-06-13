#!/usr/bin/env python3
"""Test run: index a single video end-to-end and analyze with VLM+LLM."""
import sys, os, subprocess, uuid, json, hashlib, base64, re, time
from datetime import datetime, timezone

import httpx

# === Config ===
VIDEO_PATH = "C:/Users/sunhao/Desktop/ToWatch/JUR-639.mp4"
OLLAMA_BASE = "http://localhost:11434"
VLM_MODEL = "llava:13b"
LLM_MODEL = "fredrezones55/Qwen3.5-Uncensored-HauhauCS-Aggressive:9b"
FRAMES_DIR = "data/frames/test_jur639"
THUMBS_DIR = "data/thumbs"
os.makedirs(FRAMES_DIR, exist_ok=True)
os.makedirs(THUMBS_DIR, exist_ok=True)

print("=" * 60)
print("GifAgent Test Run — JUR-639.mp4")
print("=" * 60)

# === Phase 1: Extract frames from video ===
print("\n[1/5] Extracting test frames...")

timestamps = [600, 1200, 1800, 2400, 3000, 3600]  # 10min, 20min, ..., 60min
frame_files = []
for i, ts in enumerate(timestamps):
    out_path = f"{FRAMES_DIR}/frame_{i+1:02d}_ts{ts}.jpg"
    cmd = [
        "ffmpeg", "-y", "-ss", str(ts), "-i", VIDEO_PATH,
        "-vf", "scale=640:-1", "-vframes", "1", out_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode == 0 and os.path.exists(out_path):
        sz = os.path.getsize(out_path)
        frame_files.append({"path": out_path, "timestamp": ts, "index": i + 1})
        print(f"  Frame {i+1}: t={ts}s ({ts//60}min), {sz//1024}KB")

print(f"  Extracted {len(frame_files)}/{len(timestamps)} frames")

# === Phase 2: VLM analyze each frame ===
print(f"\n[2/5] Calling llava:13b to analyze each frame...")

FRAME_PROMPT = """You are analyzing individual frames from a movie/TV show.
Focus on the CINEMATIC and AESTHETIC qualities, not just listing objects.

Output JSON only, no markdown, no explanation:
{
  "caption": "concise description of the scene and composition",
  "emotional_core": "tension | melancholy | awe | joy | sadness | catharsis | serenity | excitement | dread | nostalgia | admiration | other",
  "aesthetic_notes": ["specific cinematic qualities: lighting, color palette, depth of field, framing, texture, movement"],
  "why_i_like_it": "a personal, subjective reason this frame is compelling - think like a cinephile"
}"""


def parse_json_response(text):
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


frame_analyses = []
for fi, finfo in enumerate(frame_files):
    print(f"  Analyzing frame {fi+1}/{len(frame_files)} (t={finfo['timestamp']}s)...")
    with open(finfo["path"], "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")

    for attempt in range(3):
        try:
            resp = httpx.post(
                f"{OLLAMA_BASE}/api/generate",
                json={"model": VLM_MODEL, "prompt": FRAME_PROMPT, "images": [img_b64], "stream": False},
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            parsed = parse_json_response(data.get("response", ""))
            if parsed.get("_parse_error"):
                print(f"    WARN: JSON parse issue: {parsed.get('_raw', '')[:100]}")
            parsed["timestamp"] = finfo["timestamp"]
            parsed["frame_index"] = fi + 1
            frame_analyses.append(parsed)
            print(f"    caption: {parsed.get('caption', '?')[:80]}...")
            print(f"    emotional_core: {parsed.get('emotional_core', '?')}")
            break
        except Exception as e:
            if attempt < 2:
                print(f"    Retry {attempt+2}/3: {e}")
                time.sleep(5)
            else:
                print(f"    FAILED: {e}")

print(f"  Analyzed {len(frame_analyses)}/{len(frame_files)} frames successfully")

# === Phase 3: LLM synthesize ===
print(f"\n[3/5] Calling 9B LLM to synthesize annotations...")

analyses_text = "\n\n".join(
    f"Frame {fa.get('frame_index', i+1)} (t={fa.get('timestamp', 0)}s):\n"
    f"  Caption: {fa.get('caption', '')}\n"
    f"  Emotional core: {fa.get('emotional_core', '')}\n"
    f"  Aesthetic notes: {fa.get('aesthetic_notes', [])}\n"
    f"  Why compelling: {fa.get('why_i_like_it', '')}"
    for i, fa in enumerate(frame_analyses)
)

SYNTHESIS_PROMPT = (
    "You are an AI that synthesizes frame-by-frame film analyses into a cohesive annotation.\n\n"
    "IMPORTANT: Respond with ONLY a valid JSON object. Start with {\"summary\". No markdown, no other text.\n\n"
    "{\n"
    '  "summary": "one sentence describing the film\'s visual style based on these frames",\n'
    '  "emotional_core": "the dominant emotion across these frames",\n'
    '  "aesthetic_notes": ["consolidated cinematographic qualities"],\n'
    '  "why_i_like_it": "an eloquent, personal reason",\n'
    '  "tags": ["genre_guess", "visual_style", "notable_elements"],\n'
    '  "scene_type": "close-up | dialogue | action | transition | reaction | establishing | montage | other"\n'
    "}\n\n"
    "Frame analyses:\n" + analyses_text
)

synthesis = {"_parse_error": True, "_raw": ""}
for attempt in range(3):
    try:
        resp = httpx.post(
            f"{OLLAMA_BASE}/api/generate",
            json={"model": LLM_MODEL, "prompt": SYNTHESIS_PROMPT, "stream": False, "options": {"temperature": 0.3}},
            timeout=120,
        )
        resp.raise_for_status()
        synthesis_data = resp.json()
        raw_text = synthesis_data.get("response", "")
        if not raw_text or not raw_text.strip():
            print(f"  Attempt {attempt+1}: empty response, retrying...")
            continue
        synthesis = parse_json_response(raw_text)
        if synthesis.get("_parse_error"):
            print(f"  Attempt {attempt+1}: JSON parse failed, raw: {raw_text[:100]}...")
            SYNTHESIS_PROMPT += "\n\nYour last response was not valid JSON. Output ONLY the JSON object."
            continue
        print(f"  summary: {synthesis.get('summary', '?')}")
        print(f"  emotional_core: {synthesis.get('emotional_core', '?')}")
        print(f"  aesthetic_notes: {synthesis.get('aesthetic_notes', [])}")
        print(f"  why_i_like_it: {synthesis.get('why_i_like_it', '?')}")
        print(f"  tags: {synthesis.get('tags', [])}")
        print(f"  scene_type: {synthesis.get('scene_type', '?')}")
        break
    except Exception as e:
        if attempt < 2:
            print(f"  Attempt {attempt+1}: {e}, retrying...")
            time.sleep(5)
        else:
            synthesis = {"error": str(e), "_parse_error": True}

# === Phase 4: Save results ===
print(f"\n[4/5] Saving results...")

output = {
    "video": VIDEO_PATH,
    "frames_analyzed": len(frame_analyses),
    "frame_analyses": [
        {
            "frame_index": fa.get("frame_index"),
            "timestamp": fa.get("timestamp"),
            "caption": fa.get("caption"),
            "emotional_core": fa.get("emotional_core"),
            "aesthetic_notes": fa.get("aesthetic_notes"),
            "why_i_like_it": fa.get("why_i_like_it"),
        }
        for fa in frame_analyses
    ],
    "synthesis": synthesis,
    "analyzed_at": datetime.now(timezone.utc).isoformat(),
}

out_file = "data/test_jur639_result.json"
with open(out_file, "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f"  Results saved to {out_file}")

# === Phase 5: Summary ===
print(f"\n[5/5] Test Complete!")
print(f"  Frames analyzed: {len(frame_analyses)}")
print(f"  Synthesis: {'OK' if synthesis and not synthesis.get('_parse_error') else 'FAILED'}")
print(f"  Emotional core: {synthesis.get('emotional_core')}")
print(f"  Tags: {synthesis.get('tags')}")

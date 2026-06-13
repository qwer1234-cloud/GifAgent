#!/usr/bin/env python3
"""Step 2: Extract a high-interest GIF clip from JUR-639.mp4 based on VLM frame analysis."""
import subprocess, os, json

VIDEO = "C:/Users/sunhao/Desktop/ToWatch/JUR-639.mp4"
EXPORT_DIR = "data/exports"
os.makedirs(EXPORT_DIR, exist_ok=True)

# Load the VLM synthesis to pick best timestamp
with open("data/test_jur639_result.json") as f:
    data = json.load(f)

print("Synthesis result:")
print(f"  Summary: {data['synthesis'].get('summary', 'N/A')}")
print(f"  Emotional core: {data['synthesis'].get('emotional_core', 'N/A')}")
print(f"  Tags: {data['synthesis'].get('tags', [])}")

# Find the frame with the most detailed aesthetic notes as best candidate
best_frame = None
best_len = 0
for fa in data["frame_analyses"]:
    notes = fa.get("aesthetic_notes") or []
    notes_len = sum(len(n) for n in notes)
    if notes_len > best_len and fa.get("caption"):
        best_len = notes_len
        best_frame = fa

if not best_frame:
    print("No good frame found, falling back to frame 3")
    best_frame = data["frame_analyses"][2]

print(f"\nBest frame: #{best_frame['frame_index']} @ {best_frame['timestamp']}s")
print(f"  Caption: {best_frame['caption']}")
print(f"  Emotional: {best_frame['emotional_core']}")

# Extract a 3.5-second GIF clip around the best timestamp
ts = best_frame["timestamp"]
start = max(0, ts - 1.5)
duration = 3.5

print(f"\nExtracting GIF: start={start}s, duration={duration}s...")

# Two-pass palette generation for quality
palette = f"{EXPORT_DIR}/jur639_palette.png"
gif_out = f"{EXPORT_DIR}/jur639_best_{int(ts)}s.gif"
mp4_out = f"{EXPORT_DIR}/jur639_best_{int(ts)}s.mp4"

# Pass 1: palette
cmd1 = [
    "ffmpeg", "-y", "-ss", str(start), "-t", str(duration), "-i", VIDEO,
    "-vf", "fps=12,scale=480:-1:flags=lanczos,palettegen",
    palette
]
r1 = subprocess.run(cmd1, capture_output=True, text=True, timeout=60)

# Pass 2: GIF
cmd2 = [
    "ffmpeg", "-y", "-ss", str(start), "-t", str(duration), "-i", VIDEO,
    "-i", palette,
    "-filter_complex", "fps=12,scale=480:-1:flags=lanczos[x];[x][1:v]paletteuse",
    gif_out
]
r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=60)

# Also export MP4 for comparison
cmd3 = [
    "ffmpeg", "-y", "-ss", str(start), "-t", str(duration), "-i", VIDEO,
    "-vf", "scale=480:-1", "-an",
    mp4_out
]
r3 = subprocess.run(cmd3, capture_output=True, text=True, timeout=60)

# Check results
for path in [palette, gif_out, mp4_out]:
    if os.path.exists(path):
        sz = os.path.getsize(path)
        print(f"  {os.path.basename(path)}: {sz//1024}KB")

print(f"\nExported to {EXPORT_DIR}/")

# Also generate GIFs for all 6 frames with context
print(f"\nGenerating GIFs for all 6 scenes...")
for fa in data["frame_analyses"]:
    ts = fa["timestamp"]
    if ts is None:
        continue
    start = max(0, ts - 1.0)
    dur = 2.5
    idx = fa["frame_index"]

    palette_i = f"{EXPORT_DIR}/jur639_scene{idx}_palette.png"
    gif_i = f"{EXPORT_DIR}/jur639_scene{idx}_{int(ts)}s.gif"

    subprocess.run([
        "ffmpeg", "-y", "-ss", str(start), "-t", str(dur), "-i", VIDEO,
        "-vf", "fps=10,scale=480:-1:flags=lanczos,palettegen",
        palette_i
    ], capture_output=True, timeout=30)

    subprocess.run([
        "ffmpeg", "-y", "-ss", str(start), "-t", str(dur), "-i", VIDEO,
        "-i", palette_i,
        "-filter_complex", "fps=10,scale=480:-1:flags=lanczos[x];[x][1:v]paletteuse",
        gif_i
    ], capture_output=True, timeout=30)

    if os.path.exists(gif_i):
        sz = os.path.getsize(gif_i)
        print(f"  Scene {idx} (t={int(ts)}s): {sz//1024}KB")

print("\nAll GIFs generated successfully!")

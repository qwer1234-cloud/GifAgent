#!/usr/bin/env python3
"""
Re-encode all GIFs under a directory to a target max width (default 720px).

Uses ffmpeg palette two-pass for quality. Skips GIFs already at or below
the target width. Processes in-place via temp file + atomic rename.

Usage:
  uv run python scripts/resize_gifs.py
  uv run python scripts/resize_gifs.py --dir data/exports/adaptive_test --width 720
  uv run python scripts/resize_gifs.py --dry-run   # preview only, no changes
"""
import argparse
import os
import subprocess
import sys
import tempfile


def get_gif_width(gif_path: str) -> int | None:
    """Return GIF width in pixels, or None if ffprobe fails."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width", "-of", "default=noprint_wrappers=1:nokey=1",
             gif_path],
            capture_output=True, text=True, timeout=30,
        )
        return int(r.stdout.strip())
    except Exception:
        return None


def reencode_gif(gif_path: str, target_width: int) -> bool:
    """Re-encode a GIF to target width using palette two-pass. Returns True on success."""
    # Two-pass: palettegen → paletteuse, preserves original fps
    palette_fd, palette_path = tempfile.mkstemp(suffix=".png")
    os.close(palette_fd)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".gif", dir=os.path.dirname(gif_path))
    os.close(tmp_fd)

    try:
        # Pass 1: generate palette
        r1 = subprocess.run(
            ["ffmpeg", "-y", "-i", gif_path,
             "-vf", f"scale={target_width}:-1:flags=lanczos,palettegen",
             palette_path],
            capture_output=True, timeout=120,
        )
        if r1.returncode != 0 or not os.path.exists(palette_path):
            return False

        # Pass 2: apply palette
        r2 = subprocess.run(
            ["ffmpeg", "-y", "-i", gif_path, "-i", palette_path,
             "-filter_complex", f"scale={target_width}:-1:flags=lanczos[x];[x][1:v]paletteuse",
             tmp_path],
            capture_output=True, timeout=120,
        )
        if r2.returncode != 0 or not os.path.exists(tmp_path):
            return False

        # Atomic replace
        os.replace(tmp_path, gif_path)
        return True
    finally:
        for p in (palette_path, tmp_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description="Batch re-encode GIFs to target width")
    parser.add_argument("--dir", default="data/exports/adaptive_test",
                        help="Root directory to scan for GIFs")
    parser.add_argument("--width", type=int, default=720,
                        help="Target max width in pixels (default: 720)")
    parser.add_argument("--dry-run", action="store_true",
                        help="List files that would be re-encoded without changing them")
    args = parser.parse_args()

    if not os.path.isdir(args.dir):
        print(f"Directory not found: {args.dir}")
        return 1

    # Walk recursively, find all .gif files
    gifs = []
    for root, dirs, files in os.walk(args.dir):
        for f in files:
            if f.lower().endswith(".gif"):
                gifs.append(os.path.join(root, f))

    print(f"Found {len(gifs)} GIFs under {args.dir}")
    if not gifs:
        return 0

    skipped = 0
    resized = 0
    failed = 0
    total_before = 0
    total_after = 0

    for i, gif in enumerate(gifs):
        width = get_gif_width(gif)
        size_before = os.path.getsize(gif)

        if width is None:
            print(f"  [{i+1}/{len(gifs)}] SKIP (ffprobe failed): {os.path.basename(gif)[:60]}")
            failed += 1
            continue

        if width <= args.width:
            skipped += 1
            if args.dry_run or (i + 1) % 50 == 0:
                print(f"  [{i+1}/{len(gifs)}] skip ({width}px ≤ {args.width}): {os.path.basename(gif)[:50]}")
            continue

        if args.dry_run:
            print(f"  [{i+1}/{len(gifs)}] WOULD RESIZE ({width}px → {args.width}px, {size_before//1024}KB): {os.path.basename(gif)[:50]}")
            continue

        print(f"  [{i+1}/{len(gifs)}] Resizing {width}px → {args.width}px ({size_before//1024}KB): {os.path.basename(gif)[:50]}", end="", flush=True)

        if reencode_gif(gif, args.width):
            size_after = os.path.getsize(gif)
            total_before += size_before
            total_after += size_after
            ratio = (1 - size_after / size_before) * 100 if size_before else 0
            print(f" → {size_after//1024}KB (-{ratio:.0f}%)")
            resized += 1
        else:
            print(f" FAILED")
            failed += 1

    print(f"\n{'='*60}")
    print(f"Done: {resized} resized, {skipped} skipped (already ≤{args.width}px), {failed} failed")
    if total_before > 0:
        print(f"Size: {total_before//1024//1024}MB → {total_after//1024//1024}MB "
              f"(saved {(total_before-total_after)//1024//1024}MB, {(1-total_after/total_before)*100:.0f}%)")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

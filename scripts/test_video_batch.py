#!/usr/bin/env python3
"""
Batch adaptive GIF extraction — process all videos in a directory.

Usage:
  uv run python scripts/test_video_batch.py --dir "C:/Users/sunhao/Desktop/ToWatch/CumForKate"
  uv run python scripts/test_video_batch.py --dir <path> --limit 5
  uv run python scripts/test_video_batch.py --dir <path> --dry-run   # list videos only
"""
import sys, os, subprocess, json, time, glob, argparse

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True, help="Directory containing video files")
    parser.add_argument("--limit", type=int, default=0, help="Max videos to process (0=all)")
    parser.add_argument("--dry-run", action="store_true", help="List videos without processing")
    parser.add_argument("--extensions", default=".mp4,.mkv,.avi,.mov,.webm", help="Video extensions")
    args = parser.parse_args()

    exts = [e.strip() for e in args.extensions.split(",")]
    videos = []
    for ext in exts:
        videos.extend(glob.glob(os.path.join(args.dir, f"*{ext}")))
        videos.extend(glob.glob(os.path.join(args.dir, f"*{ext.upper()}")))

    videos = sorted(set(videos))

    if not videos:
        print(f"No videos found in {args.dir}")
        return 1

    print(f"Found {len(videos)} videos in {args.dir}")

    if args.dry_run:
        for i, v in enumerate(videos):
            name = os.path.splitext(os.path.basename(v))[0]
            print(f"  [{i+1}] {name}")
        return 0

    if args.limit and args.limit < len(videos):
        videos = videos[:args.limit]
        print(f"Limited to {args.limit} videos")

    total_start = time.time()
    succeeded = 0
    failed = 0

    for idx, video in enumerate(videos):
        video_name = os.path.splitext(os.path.basename(video))[0]
        print(f"\n{'='*60}")
        print(f"[{idx+1}/{len(videos)}] Processing: {video_name}")
        print(f"{'='*60}")

        video_start = time.time()
        try:
            result = subprocess.run([
                sys.executable, "-u", "scripts/test_video_adaptive.py",
                "--video", video,
            ], cwd=".", capture_output=False, timeout=14400)  # 4h max per video

            if result.returncode == 0:
                succeeded += 1
                elapsed = time.time() - video_start
                print(f"  [{idx+1}/{len(videos)}] OK ({elapsed:.0f}s)")
            else:
                failed += 1
                print(f"  [{idx+1}/{len(videos)}] FAILED (exit {result.returncode})")
        except subprocess.TimeoutExpired:
            failed += 1
            print(f"  [{idx+1}/{len(videos)}] TIMEOUT (>4h)")

        total_elapsed = time.time() - total_start
        avg = total_elapsed / (idx + 1)
        eta = avg * (len(videos) - idx - 1)
        print(f"  Progress: {succeeded} ok / {failed} failed | ETA: {eta/3600:.1f}h")

    print(f"\n{'='*60}")
    print(f"Batch complete: {succeeded} succeeded, {failed} failed in {total_elapsed/3600:.1f}h")
    return 0 if failed == 0 else 1

if __name__ == "__main__":
    raise SystemExit(main())

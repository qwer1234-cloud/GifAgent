#!/usr/bin/env python3
"""
Batch adaptive GIF extraction — process all videos in a directory with checkpoint resume.

Usage:
  uv run python scripts/test_video_batch.py --dir "C:/Users/sunhao/Desktop/ToWatch/CumForKate"
  uv run python scripts/test_video_batch.py --dir <path> --limit 5
  uv run python scripts/test_video_batch.py --dir <path> --dry-run   # list videos only
"""
import sys, os, subprocess, json, time, glob, argparse
from datetime import datetime

CHECKPOINT_FILE = "data/batch_checkpoint.json"


def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"completed": {}, "started_at": None, "updated_at": None}


def save_checkpoint(cp):
    cp["updated_at"] = datetime.now().isoformat()
    os.makedirs(os.path.dirname(CHECKPOINT_FILE), exist_ok=True)
    with open(CHECKPOINT_FILE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(cp, f, ensure_ascii=False, indent=2)
    os.replace(CHECKPOINT_FILE + ".tmp", CHECKPOINT_FILE)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True, help="Directory containing video files")
    parser.add_argument("--limit", type=int, default=0, help="Max videos to process (0=all)")
    parser.add_argument("--dry-run", action="store_true", help="List videos without processing")
    parser.add_argument("--extensions", default=".mp4,.mkv,.avi,.mov,.webm", help="Video extensions")
    parser.add_argument("--force", action="store_true", help="Re-process completed videos")
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

    # ── Load checkpoint ──────────────────────────────────────────────────
    cp = load_checkpoint()
    if cp["started_at"] is None:
        cp["started_at"] = datetime.now().isoformat()
        save_checkpoint(cp)

    pending = []
    skipped = 0
    for v in videos:
        vname = os.path.splitext(os.path.basename(v))[0]
        if vname in cp["completed"] and not args.force:
            skipped += 1
        else:
            pending.append(v)

    print(f"Checkpoint: {skipped} already done, {len(pending)} pending")

    if args.limit and args.limit < len(pending):
        pending = pending[:args.limit]
        print(f"Limited to {args.limit} videos (this run)")

    if not pending:
        print("All videos already processed. Use --force to re-run.")
        return 0

    # ── Process ──────────────────────────────────────────────────────────
    total_start = time.time()
    succeeded = 0
    failed = 0

    for idx, video in enumerate(pending):
        video_name = os.path.splitext(os.path.basename(video))[0]
        print(f"\n{'='*60}")
        print(f"[{idx+1}/{len(pending)}] {video_name}")
        print(f"{'='*60}")

        video_start = time.time()
        try:
            result = subprocess.run([
                sys.executable, "-u", "scripts/test_video_adaptive.py",
                "--video", video,
                "--export-dir", "data/exports/adaptive_test",
            ], cwd=".", timeout=14400)

            if result.returncode == 0:
                succeeded += 1
                cp["completed"][video_name] = {
                    "status": "ok",
                    "elapsed_s": int(time.time() - video_start),
                    "finished_at": datetime.now().isoformat(),
                }
                print(f"  [{idx+1}/{len(pending)}] OK ({time.time()-video_start:.0f}s)")
            else:
                failed += 1
                cp["completed"][video_name] = {
                    "status": "failed",
                    "exit_code": result.returncode,
                    "finished_at": datetime.now().isoformat(),
                }
                print(f"  [{idx+1}/{len(pending)}] FAILED (exit {result.returncode})")
        except subprocess.TimeoutExpired:
            failed += 1
            cp["completed"][video_name] = {
                "status": "timeout",
                "finished_at": datetime.now().isoformat(),
            }
            print(f"  [{idx+1}/{len(pending)}] TIMEOUT (>4h)")

        save_checkpoint(cp)

        # Show overall progress
        total_done = len(cp["completed"])
        total_elapsed = time.time() - total_start
        avg = total_elapsed / (idx + 1)
        eta = avg * (len(pending) - idx - 1)
        print(f"  Progress: {total_done}/{len(videos)} total | ETA: {eta/3600:.1f}h")

    total_elapsed = time.time() - total_start
    print(f"\n{'='*60}")
    print(f"Batch done: {succeeded} ok / {failed} failed in {total_elapsed/3600:.1f}h")
    print(f"Checkpoint: {CHECKPOINT_FILE}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

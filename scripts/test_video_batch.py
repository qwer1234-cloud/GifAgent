#!/usr/bin/env python3
"""
Batch adaptive GIF extraction — process all videos in a directory with checkpoint resume.

Usage:
  uv run python scripts/test_video_batch.py --dir "C:/Users/sunhao/Desktop/ToWatch/CumForKate"
  uv run python scripts/test_video_batch.py --dir <path> --limit 5
  uv run python scripts/test_video_batch.py --dir <path> --dry-run   # list videos only
"""
import sys, os, subprocess, json, time, argparse
from datetime import datetime
from pathlib import Path
from typing import Callable

# Windows console defaults to GBK — reconfigure to handle Unicode filenames (💦💢💗 etc.)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

CHECKPOINT_FILE = "data/batch_checkpoint.json"
REUSABLE_CHECKPOINT_STATUSES = {"ok", "dedup_skipped"}
RETRYABLE_CHECKPOINT_STATUSES = {"failed", "timeout"}

from app.services.video_fingerprint import compute_fingerprint, find_duplicate_in_checkpoint
from app.services.batch_queue import (
    load_queue,
    load_queue_state,
    pending_jobs,
    save_queue_state,
    update_job_state,
)


DEFAULT_EXTENSIONS = ".mp4,.mkv,.avi,.mov,.webm,.ts"


def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, encoding="utf-8-sig") as f:
            return normalize_checkpoint_for_resume(json.load(f))
    return normalize_checkpoint_for_resume({"completed": {}, "started_at": None, "updated_at": None})


def save_checkpoint(cp):
    cp["updated_at"] = datetime.now().isoformat()
    os.makedirs(os.path.dirname(CHECKPOINT_FILE), exist_ok=True)
    with open(CHECKPOINT_FILE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(cp, f, ensure_ascii=False, indent=2)
    os.replace(CHECKPOINT_FILE + ".tmp", CHECKPOINT_FILE)


def checkpoint_entry_can_be_reused(entry: dict | None) -> bool:
    if not isinstance(entry, dict):
        return False
    return entry.get("status") in REUSABLE_CHECKPOINT_STATUSES


def discover_videos(video_dir: str, extensions: str) -> list[str]:
    wanted = {
        ext.strip().lower() if ext.strip().startswith(".") else f".{ext.strip().lower()}"
        for ext in extensions.split(",")
        if ext.strip()
    }
    if not wanted:
        return []
    root = Path(video_dir)
    return sorted(str(path) for path in root.iterdir() if path.is_file() and path.suffix.lower() in wanted)


def normalize_checkpoint_for_resume(cp: dict) -> dict:
    cp.setdefault("completed", {})
    cp.setdefault("retryable", {})
    cp.setdefault("last_run", None)

    for video_name, info in list(cp["completed"].items()):
        if checkpoint_entry_can_be_reused(info):
            continue
        if isinstance(info, dict) and info.get("status") in RETRYABLE_CHECKPOINT_STATUSES:
            cp["retryable"][video_name] = info
        cp["completed"].pop(video_name, None)
    return cp


def update_last_run(cp: dict, **updates):
    run = cp.setdefault("last_run", {}) or {}
    run.update(updates)
    run["updated_at"] = datetime.now().isoformat()
    cp["last_run"] = run


def run_single_directory(video_dir: str, limit: int, extensions: str, force: bool) -> int:
    videos = discover_videos(video_dir, extensions)

    if not videos:
        print(f"No videos found in {video_dir}")
        return 1

    print(f"Found {len(videos)} videos in {video_dir}")

    # ── Load checkpoint ──────────────────────────────────────────────────
    cp = load_checkpoint()
    if cp["started_at"] is None:
        cp["started_at"] = datetime.now().isoformat()
    save_checkpoint(cp)

    pending = []
    skipped = 0
    dedup_skipped = 0
    retrying = 0
    for v in videos:
        vname = os.path.splitext(os.path.basename(v))[0]
        existing = cp["completed"].get(vname)
        if existing and not force:
            if checkpoint_entry_can_be_reused(existing):
                skipped += 1
                continue
            retrying += 1

        # Content-based dedup: skip if a different-named video with same content was already processed
        if not force:
            fp = compute_fingerprint(v)
            if fp:
                dup_of = find_duplicate_in_checkpoint(fp, cp)
                if dup_of:
                    dedup_skipped += 1
                    cp["completed"][vname] = {
                        "status": "dedup_skipped",
                        "duplicate_of": dup_of,
                        "fingerprint": fp,
                        "finished_at": datetime.now().isoformat(),
                    }
                    cp["retryable"].pop(vname, None)
                    save_checkpoint(cp)
                    print(f"  [dedup] {vname[:60]} == {dup_of[:60]} (skipped)")
                    continue
        pending.append(v)

    print(f"Checkpoint: {skipped} reusable, {retrying} retrying, {dedup_skipped} dedup-skipped, {len(pending)} pending")

    if limit and limit < len(pending):
        pending = pending[:limit]
        print(f"Limited to {limit} videos (this run)")

    update_last_run(
        cp,
        status="running" if pending else "complete",
        started_at=datetime.now().isoformat(),
        dir=video_dir,
        limit=limit,
        planned=len(pending),
        processed=0,
        succeeded=0,
        failed=0,
        dedup_skipped=dedup_skipped,
        skipped_reusable=skipped,
        retrying_backlog=retrying,
        current_video="",
    )
    save_checkpoint(cp)

    if not pending:
        print("All videos already processed. Use --force to re-run.")
        return 0

    # Derive input folder name so outputs are grouped: adaptive_test/{folder}/{video}/
    input_folder = os.path.basename(os.path.normpath(video_dir))
    base_export_dir = os.path.join("data/exports/adaptive_test", input_folder) if input_folder else "data/exports/adaptive_test"

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
        # When frozen (exe), use the exe itself with --run-script flag.
        # When running from source, use sys.executable (python) directly.
        if getattr(sys, "frozen", False):
            adaptive_script = os.path.join(sys._MEIPASS, "scripts", "test_video_adaptive.py")
            cmd = [sys.executable, "--run-script", adaptive_script, "--video", video]
        else:
            adaptive_script = "scripts/test_video_adaptive.py"
            cmd = [sys.executable, "-u", adaptive_script, "--video", video]
        cmd.extend(["--export-dir", base_export_dir])
        try:
            result = subprocess.run(cmd, cwd=".", timeout=14400)

            if result.returncode == 0:
                succeeded += 1
                cp["completed"][video_name] = {
                    "status": "ok",
                    "elapsed_s": int(time.time() - video_start),
                    "finished_at": datetime.now().isoformat(),
                    "fingerprint": compute_fingerprint(video),
                }
                cp["retryable"].pop(video_name, None)
                print(f"  [{idx+1}/{len(pending)}] OK ({time.time()-video_start:.0f}s)")
            else:
                failed += 1
                cp["completed"].pop(video_name, None)
                cp["retryable"][video_name] = {
                    "status": "failed",
                    "exit_code": result.returncode,
                    "finished_at": datetime.now().isoformat(),
                }
                print(f"  [{idx+1}/{len(pending)}] FAILED (exit {result.returncode})")
        except subprocess.TimeoutExpired:
            failed += 1
            cp["completed"].pop(video_name, None)
            cp["retryable"][video_name] = {
                "status": "timeout",
                "finished_at": datetime.now().isoformat(),
            }
            print(f"  [{idx+1}/{len(pending)}] TIMEOUT (>4h)")

        update_last_run(
            cp,
            processed=idx + 1,
            succeeded=succeeded,
            failed=failed,
            current_video=video_name,
        )
        save_checkpoint(cp)

        # Show overall progress
        total_done = len(cp["completed"]) + failed
        total_elapsed = time.time() - total_start
        avg = total_elapsed / (idx + 1)
        eta = avg * (len(pending) - idx - 1)
        print(f"  Progress: {total_done}/{len(videos)} total | ETA: {eta/3600:.1f}h")

    total_elapsed = time.time() - total_start
    print(f"\n{'='*60}")
    print(f"Batch done: {succeeded} ok / {failed} failed in {total_elapsed/3600:.1f}h")
    print(f"Checkpoint: {CHECKPOINT_FILE}")
    update_last_run(
        cp,
        status="complete" if failed == 0 else "completed_with_failures",
        processed=len(pending),
        succeeded=succeeded,
        failed=failed,
        current_video="",
    )
    save_checkpoint(cp)
    return 0 if failed == 0 else 1


def build_single_batch_command(video_dir: str, limit: int, extensions: str) -> list[str]:
    if getattr(sys, "frozen", False):
        script = os.path.join(sys._MEIPASS, "scripts", "test_video_batch.py")
        command = [sys.executable, "--run-script", script]
    else:
        command = [sys.executable, "-u", "scripts/test_video_batch.py"]
    return command + [
        "--dir",
        video_dir,
        "--limit",
        str(limit),
        "--extensions",
        extensions,
    ]


def _queue_state_path(queue_file: str) -> Path:
    queue_path = Path(queue_file)
    return queue_path.with_name(f"{queue_path.stem}_state{queue_path.suffix}")


def run_queue(
    queue_file: str,
    process_job: Callable[[dict], int] | None = None,
) -> int:
    state_path = _queue_state_path(queue_file)
    failed = False

    while True:
        queue = load_queue(queue_file)
        state = load_queue_state(state_path)
        jobs = pending_jobs(queue, state)
        if not jobs:
            state["status"] = "draining"
            state["current_job_id"] = None
            save_queue_state(state, state_path)
            queue = load_queue(queue_file)
            state = load_queue_state(state_path)
            if pending_jobs(queue, state):
                state["status"] = "running"
                save_queue_state(state, state_path)
                continue
            state["status"] = "idle"
            state["current_job_id"] = None
            state.pop("worker_pid", None)
            save_queue_state(state, state_path)
            return 1 if failed else 0

        job = jobs[0]
        job_id = job["job_id"]
        update_job_state(state, job_id, "running", started_at=datetime.now().isoformat())
        state["status"] = "running"
        state["current_job_id"] = job_id
        save_queue_state(state, state_path)
        print(f"[QUEUE] Processing folder: {job['directory']}", flush=True)

        if process_job is None:
            command = build_single_batch_command(
                job["directory"],
                job.get("limit", 0),
                job.get("extensions") or DEFAULT_EXTENSIONS,
            )
            result = subprocess.run(command, cwd=".").returncode
        else:
            result = process_job(job)

        state = load_queue_state(state_path)
        status = "completed" if result == 0 else "failed"
        update_job_state(state, job_id, status, finished_at=datetime.now().isoformat())
        state["status"] = "running"
        state["current_job_id"] = None
        save_queue_state(state, state_path)
        failed = failed or result != 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", help="Directory containing video files")
    parser.add_argument("--queue-file", help="Queue file containing batch-folder jobs")
    parser.add_argument("--limit", type=int, default=0, help="Max videos to process (0=all)")
    parser.add_argument("--dry-run", action="store_true", help="List videos without processing")
    parser.add_argument("--extensions", default=DEFAULT_EXTENSIONS, help="Video extensions")
    parser.add_argument("--force", action="store_true", help="Re-process completed videos")
    args = parser.parse_args()

    if not args.dir and not args.queue_file:
        parser.error("one of --dir or --queue-file is required")
    if args.dir and args.queue_file:
        parser.error("--dir and --queue-file cannot be used together")
    if args.queue_file:
        return run_queue(args.queue_file)

    if args.dry_run:
        videos = discover_videos(args.dir, args.extensions)
        if not videos:
            print(f"No videos found in {args.dir}")
            return 1
        print(f"Found {len(videos)} videos in {args.dir}")
        for i, video in enumerate(videos):
            print(f"  [{i + 1}] {os.path.splitext(os.path.basename(video))[0]}")
        return 0

    return run_single_directory(args.dir, args.limit, args.extensions, args.force)


if __name__ == "__main__":
    raise SystemExit(main())

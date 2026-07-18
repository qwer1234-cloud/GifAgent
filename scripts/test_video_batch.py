#!/usr/bin/env python3
"""
Batch adaptive GIF extraction — process all videos in a directory with checkpoint resume.

Usage:
  uv run python scripts/test_video_batch.py --dir "C:/Users/sunhao/Desktop/ToWatch/CumForKate"
  uv run python scripts/test_video_batch.py --dir <path> --limit 5
  uv run python scripts/test_video_batch.py --dir <path> --dry-run   # list videos only
"""
import sys, os, subprocess, json, time, argparse, tempfile, threading, uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

# Windows console defaults to GBK — reconfigure to handle Unicode filenames (💦💢💗 etc.)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

CHECKPOINT_FILE = "data/batch_checkpoint.json"
PID_FILE = "data/batch_pid.txt"
REUSABLE_CHECKPOINT_STATUSES = {"ok", "dedup_skipped"}
RETRYABLE_CHECKPOINT_STATUSES = {"failed", "timeout"}
WORKER_BUSY_EXIT_CODE = 3
LAUNCH_REJECTED_EXIT_CODE = 4
QUEUE_LEASE_RETRY_INTERVAL_SECONDS = 0.05

from app.services.video_fingerprint import compute_fingerprint, find_duplicate_in_checkpoint
from app.services.batch_queue import (
    DEFAULT_WORKER_LEASE_FILE,
    WorkerLease,
    WorkerLeaseBusyError,
    load_queue,
    load_queue_state,
    pending_jobs,
    queue_state_lock,
    queue_state_transaction,
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


def normalized_source_path(video_path: str) -> str:
    """Return the stable source identity used by new checkpoint entries."""
    return os.path.normcase(os.path.normpath(os.path.abspath(video_path)))


def checkpoint_key(video_path: str) -> str:
    return f"path:{normalized_source_path(video_path)}"


def _claim_mapping_entry_for_source(mapping: dict, video_path: str) -> tuple[str, dict | None]:
    key = checkpoint_key(video_path)
    existing = mapping.get(key)
    if isinstance(existing, dict):
        return key, existing

    legacy_key = os.path.splitext(os.path.basename(video_path))[0]
    legacy = mapping.get(legacy_key)
    if not isinstance(legacy, dict):
        return key, None
    source_path = normalized_source_path(video_path)
    bound_source = legacy.get("source_path")
    if bound_source and normalized_source_path(bound_source) != source_path:
        return key, None

    migrated = dict(legacy)
    migrated["source_path"] = source_path
    migrated.setdefault("display_name", legacy_key)
    mapping[key] = migrated
    mapping.pop(legacy_key, None)
    return key, migrated


def claim_checkpoint_entry_for_source(cp: dict, video_path: str) -> tuple[str, dict | None]:
    """Bind one legacy basename entry to its first concrete source path."""
    cp.setdefault("completed", {})
    return _claim_mapping_entry_for_source(cp["completed"], video_path)


def _format_video_event(
    video_path: str,
    status: str,
    *,
    outcome: str = "",
    reason: str = "",
) -> str:
    fields = [f"[VIDEO] status={status}", f"path={video_path}"]
    if outcome:
        fields.append(f"outcome={outcome}")
    if reason:
        fields.append(f"reason={str(reason).replace(chr(10), ' ')}")
    return " ".join(fields)


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

    pending: list[tuple[str, str]] = []
    skipped = 0
    dedup_skipped = 0
    skipped_limit = 0
    retrying = 0
    prescan_failed = 0
    for v in videos:
        print(_format_video_event(v, "START"), flush=True)
        vname = os.path.splitext(os.path.basename(v))[0]
        key, existing = claim_checkpoint_entry_for_source(cp, v)
        _, retryable = _claim_mapping_entry_for_source(cp["retryable"], v)
        if existing and not force:
            if checkpoint_entry_can_be_reused(existing):
                skipped += 1
                print(
                    _format_video_event(v, "OK", outcome="SKIPPED", reason="reusable checkpoint"),
                    flush=True,
                )
                continue
            retrying += 1
        elif retryable and not force:
            retrying += 1

        # Content-based dedup: skip if a different-named video with same content was already processed
        if not force:
            try:
                fp = compute_fingerprint(v)
            except Exception as exc:
                prescan_failed += 1
                cp["completed"].pop(key, None)
                cp["retryable"][key] = {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "source_path": normalized_source_path(v),
                    "display_name": vname,
                    "finished_at": datetime.now().isoformat(),
                }
                save_checkpoint(cp)
                print(
                    _format_video_event(v, "FAILED", reason=f"{type(exc).__name__}: {exc}"),
                    flush=True,
                )
                continue
            if fp:
                dup_of = find_duplicate_in_checkpoint(fp, cp)
                if dup_of:
                    dedup_skipped += 1
                    cp["completed"][key] = {
                        "status": "dedup_skipped",
                        "duplicate_of": dup_of,
                        "fingerprint": fp,
                        "source_path": normalized_source_path(v),
                        "display_name": vname,
                        "finished_at": datetime.now().isoformat(),
                    }
                    cp["retryable"].pop(key, None)
                    save_checkpoint(cp)
                    print(f"  [dedup] {v} == {dup_of} (skipped)")
                    print(
                        _format_video_event(v, "OK", outcome="DEDUP_SKIPPED", reason=f"duplicate_of={dup_of}"),
                        flush=True,
                    )
                    continue
        pending.append((v, key))

    print(f"Checkpoint: {skipped} reusable, {retrying} retrying, {dedup_skipped} dedup-skipped, {len(pending)} pending")

    if limit and limit < len(pending):
        skipped_limit = len(pending) - limit
        for skipped_video, _key in pending[limit:]:
            print(
                _format_video_event(skipped_video, "OK", outcome="SKIPPED", reason="run limit"),
                flush=True,
            )
        pending = pending[:limit]
        print(f"Limited to {limit} videos (this run)")

    update_last_run(
        cp,
        status=(
            "running"
            if pending
            else ("completed_with_failures" if prescan_failed else "complete")
        ),
        started_at=datetime.now().isoformat(),
        dir=video_dir,
        limit=limit,
        planned=len(videos),
        processed=prescan_failed + skipped + dedup_skipped + skipped_limit,
        succeeded=0,
        failed=prescan_failed,
        dedup_skipped=dedup_skipped,
        skipped_reusable=skipped,
        skipped_limit=skipped_limit,
        retrying_backlog=retrying,
        current_video="",
    )
    save_checkpoint(cp)

    if not pending:
        print("All videos already processed. Use --force to re-run.")
        return 1 if prescan_failed else 0

    # Derive input folder name so outputs are grouped: adaptive_test/{folder}/{video}/
    input_folder = os.path.basename(os.path.normpath(video_dir))
    base_export_dir = os.path.join("data/exports/adaptive_test", input_folder) if input_folder else "data/exports/adaptive_test"

    # ── Process ──────────────────────────────────────────────────────────
    total_start = time.time()
    succeeded = 0
    failed = prescan_failed

    for idx, (video, video_key) in enumerate(pending):
        video_name = os.path.splitext(os.path.basename(video))[0]
        print(f"\n{'='*60}")
        print(f"[{idx+1}/{len(pending)}] {video_name}")
        print(f"{'='*60}")

        video_start = time.time()
        update_last_run(cp, current_video=video)
        save_checkpoint(cp)
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
                fingerprint = compute_fingerprint(video)
                cp["completed"][video_key] = {
                    "status": "ok",
                    "elapsed_s": int(time.time() - video_start),
                    "finished_at": datetime.now().isoformat(),
                    "fingerprint": fingerprint,
                    "source_path": normalized_source_path(video),
                    "display_name": video_name,
                }
                cp["retryable"].pop(video_key, None)
                save_checkpoint(cp)
                succeeded += 1
                print(f"  [{idx+1}/{len(pending)}] OK ({time.time()-video_start:.0f}s)")
                print(_format_video_event(video, "OK", outcome="PROCESSED"), flush=True)
            else:
                failed += 1
                cp["completed"].pop(video_key, None)
                cp["retryable"][video_key] = {
                    "status": "failed",
                    "exit_code": result.returncode,
                    "source_path": normalized_source_path(video),
                    "display_name": video_name,
                    "finished_at": datetime.now().isoformat(),
                }
                print(f"  [{idx+1}/{len(pending)}] FAILED (exit {result.returncode})")
                print(
                    _format_video_event(video, "FAILED", reason=f"exit_code={result.returncode}"),
                    flush=True,
                )
        except subprocess.TimeoutExpired:
            failed += 1
            cp["completed"].pop(video_key, None)
            cp["retryable"][video_key] = {
                "status": "timeout",
                "source_path": normalized_source_path(video),
                "display_name": video_name,
                "finished_at": datetime.now().isoformat(),
            }
            print(f"  [{idx+1}/{len(pending)}] TIMEOUT (>4h)")
            print(_format_video_event(video, "FAILED", reason="timeout >4h"), flush=True)
        except OSError as exc:
            failed += 1
            cp["completed"].pop(video_key, None)
            cp["retryable"][video_key] = {
                "status": "failed",
                "error": str(exc),
                "source_path": normalized_source_path(video),
                "display_name": video_name,
                "finished_at": datetime.now().isoformat(),
            }
            print(_format_video_event(video, "FAILED", reason=f"OSError: {exc}"), flush=True)
        except Exception as exc:
            failed += 1
            cp["completed"].pop(video_key, None)
            cp["retryable"][video_key] = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "source_path": normalized_source_path(video),
                "display_name": video_name,
                "finished_at": datetime.now().isoformat(),
            }
            print(
                _format_video_event(video, "FAILED", reason=f"{type(exc).__name__}: {exc}"),
                flush=True,
            )

        update_last_run(
            cp,
            processed=min(
                len(videos),
                prescan_failed
                + skipped
                + dedup_skipped
                + skipped_limit
                + idx
                + 1,
            ),
            succeeded=succeeded,
            failed=failed,
            current_video="",
        )
        save_checkpoint(cp)

        # Show overall progress
        total_done = (
            prescan_failed
            + skipped
            + dedup_skipped
            + skipped_limit
            + idx
            + 1
        )
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
        processed=len(videos),
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


def _default_worker_lease_path(queue_file: str) -> Path:
    return Path(queue_file).with_name("batch_worker.lock")


def _default_pid_path(queue_file: str) -> Path:
    return Path(queue_file).with_name("batch_pid.txt")


def _atomic_write_pid(path: str | Path, pid: int) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(str(pid))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _remove_pid_if_owned(path: str | Path, pid: int) -> None:
    target = Path(path)
    try:
        if int(target.read_text(encoding="ascii").strip()) == int(pid):
            target.unlink()
    except (FileNotFoundError, OSError, ValueError):
        pass


def _record_launch_failure(state_path: Path, launch_token: str | None, error: str) -> None:
    if not launch_token:
        return
    with queue_state_lock(state_path):
        state = load_queue_state(state_path)
        if state.get("launch_token") != launch_token:
            return
        if state.get("status") != "starting":
            return
        state["status"] = "idle"
        state["current_job_id"] = None
        state["failed_launch_token"] = launch_token
        state["last_error"] = error
        for key in (
            "worker_pid",
            "spawned_pid",
            "launcher_pid",
            "launch_token",
            "cleanup_pending",
        ):
            state.pop(key, None)
        save_queue_state(state, state_path)


def _claim_queue_worker(
    state_path: Path,
    *,
    launch_token: str | None,
    pid_file: str | Path,
) -> str | None:
    pid = os.getpid()
    with queue_state_lock(state_path):
        state = load_queue_state(state_path)
        if launch_token is not None:
            if (
                state.get("status") != "starting"
                or state.get("launch_token") != launch_token
            ):
                return None
            token = launch_token
        else:
            token = uuid.uuid4().hex
        state["status"] = "running"
        state["current_job_id"] = None
        state["worker_pid"] = pid
        state["launch_token"] = token
        state.pop("launcher_pid", None)
        state.pop("spawned_pid", None)
        state.pop("cleanup_pending", None)
        state.pop("failed_launch_token", None)
        state.pop("completed_launch_token", None)
        state.pop("previous_worker_pid", None)
        save_queue_state(state, state_path)

    try:
        _atomic_write_pid(pid_file, pid)
    except OSError as exc:
        with queue_state_lock(state_path):
            state = load_queue_state(state_path)
            if state.get("worker_pid") == pid and state.get("launch_token") == token:
                state["cleanup_pending"] = True
                state["last_error"] = f"PID persistence failed: {exc}"
                save_queue_state(state, state_path)
    return token


def _owned_queue_state(state_path: Path, token: str, pid: int) -> dict | None:
    state = load_queue_state(state_path)
    if state.get("launch_token") != token or state.get("worker_pid") != pid:
        return None
    return state


def _finish_queue_worker(
    queue_file: str | Path,
    state_path: Path,
    *,
    token: str,
    pid: int,
    error: str = "",
) -> str:
    """Commit idle only after an atomic last queue/state inspection."""
    with queue_state_transaction(queue_file, state_path):
        state = _owned_queue_state(state_path, token, pid)
        if state is None:
            return "lost"
        if pending_jobs(load_queue(queue_file), state):
            state["status"] = "running"
            state["current_job_id"] = None
            save_queue_state(state, state_path)
            return "pending"
        state["status"] = "idle"
        state["current_job_id"] = None
        state["previous_worker_pid"] = pid
        state["completed_launch_token"] = token
        state.pop("worker_pid", None)
        state.pop("launch_token", None)
        state.pop("launcher_pid", None)
        state.pop("spawned_pid", None)
        state.pop("cleanup_pending", None)
        if error:
            state["last_error"] = error
        save_queue_state(state, state_path)
        return "finished"


def _acquire_queue_lease(
    lease: WorkerLease,
    state_path: Path,
    launch_token: str | None,
) -> bool | None:
    """Wait for a Control-launched queue child to inherit the worker lease."""
    waited = False
    while True:
        try:
            lease.acquire()
            return True
        except WorkerLeaseBusyError as exc:
            if launch_token is None:
                message = str(exc)
                print(
                    f"[QUEUE] status=FAILED folder={state_path} error={message}",
                    flush=True,
                )
                _record_launch_failure(state_path, launch_token, message)
                return False
            try:
                state = load_queue_state(state_path)
            except Exception:
                state = {}
            if (
                state.get("status") != "starting"
                or state.get("launch_token") != launch_token
            ):
                return None
            if not waited:
                print(f"[QUEUE] waiting for worker lease: {exc}", flush=True)
                waited = True
            # Use an independent wait primitive here.  Legacy UI tests and
            # embedders may temporarily replace ``time.sleep`` while probing
            # handoff behavior; sharing that replacement with the queue child
            # can spin through state transitions before the lease owner gets a
            # chance to release the file lock.
            threading.Event().wait(QUEUE_LEASE_RETRY_INTERVAL_SECONDS)


def run_queue(
    queue_file: str,
    process_job: Callable[[dict], int] | None = None,
    *,
    worker_lease_file: str | Path | None = None,
    pid_file: str | Path | None = None,
    launch_token: str | None = None,
) -> int:
    state_path = _queue_state_path(queue_file)
    lease_path = Path(worker_lease_file or _default_worker_lease_path(queue_file))
    worker_pid_file = Path(pid_file or _default_pid_path(queue_file))
    worker_pid = os.getpid()
    failed = False
    lease = WorkerLease(lease_path, mode="queue")
    lease_result = _acquire_queue_lease(lease, state_path, launch_token)
    if lease_result is False:
        return WORKER_BUSY_EXIT_CODE
    if lease_result is None:
        return LAUNCH_REJECTED_EXIT_CODE

    token = None
    try:
        token = _claim_queue_worker(
            state_path, launch_token=launch_token, pid_file=worker_pid_file
        )
        if token is None:
            return LAUNCH_REJECTED_EXIT_CODE

        while True:
            with queue_state_lock(state_path):
                state = _owned_queue_state(state_path, token, worker_pid)
                if state is None:
                    return 1
                queue = load_queue(queue_file)
                jobs = pending_jobs(queue, state)
                if jobs:
                    job = jobs[0]
                    job_id = job["job_id"]
                    update_job_state(
                        state,
                        job_id,
                        "running",
                        started_at=datetime.now().isoformat(),
                    )
                    state["status"] = "running"
                    state["current_job_id"] = job_id
                    save_queue_state(state, state_path)
                else:
                    state["status"] = "draining"
                    state["current_job_id"] = None
                    save_queue_state(state, state_path)
                    job = None

            if job is None:
                finish_result = _finish_queue_worker(
                    queue_file,
                    state_path,
                    token=token,
                    pid=worker_pid,
                )
                if finish_result == "pending":
                    continue
                if finish_result == "lost":
                    return 1
                return 1 if failed else 0

            directory = job["directory"]
            print(f"[QUEUE] status=START folder={directory}", flush=True)
            job_error = ""
            try:
                if process_job is None:
                    result = run_single_directory(
                        directory,
                        job.get("limit", 0),
                        job.get("extensions") or DEFAULT_EXTENSIONS,
                        False,
                    )
                else:
                    result = process_job(job)
            except Exception as exc:
                result = 1
                job_error = f"{type(exc).__name__}: {exc}"

            with queue_state_lock(state_path):
                state = _owned_queue_state(state_path, token, worker_pid)
                if state is None:
                    return 1
                status = "completed" if result == 0 else "failed"
                updates = {"finished_at": datetime.now().isoformat()}
                if job_error:
                    updates["error"] = job_error
                    state["last_error"] = f"Folder {directory}: {job_error}"
                elif result != 0:
                    updates["error"] = f"exit code {result}"
                    state["last_error"] = f"Folder {directory}: exit code {result}"
                update_job_state(state, job_id, status, **updates)
                state["status"] = "running"
                state["current_job_id"] = None
                save_queue_state(state, state_path)
            if result == 0:
                print(f"[QUEUE] status=OK folder={directory}", flush=True)
            else:
                detail = job_error or f"exit code {result}"
                print(
                    f"[QUEUE] status=FAILED folder={directory} error={detail}",
                    flush=True,
                )
                failed = True
    except Exception as exc:
        error = f"Queue worker error: {type(exc).__name__}: {exc}"
        if token is not None:
            _finish_queue_worker(
                queue_file,
                state_path,
                token=token,
                pid=worker_pid,
                error=error,
            )
        print(f"[QUEUE] status=FAILED folder={queue_file} error={error}", flush=True)
        return 1
    finally:
        _remove_pid_if_owned(worker_pid_file, worker_pid)
        lease.release()


def run_direct(
    video_dir: str,
    limit: int,
    extensions: str,
    force: bool,
    *,
    worker_lease_file: str | Path = DEFAULT_WORKER_LEASE_FILE,
    pid_file: str | Path = PID_FILE,
    process_directory: Callable[[str, int, str, bool], int] = run_single_directory,
) -> int:
    """Run direct mode under the same lease used by the queue orchestrator."""
    lease = WorkerLease(worker_lease_file, mode="direct")
    try:
        lease.acquire()
    except WorkerLeaseBusyError as exc:
        print(f"[BATCH] status=FAILED folder={video_dir} error={exc}", flush=True)
        return WORKER_BUSY_EXIT_CODE
    pid = os.getpid()
    try:
        _atomic_write_pid(pid_file, pid)
        return process_directory(video_dir, limit, extensions, force)
    except OSError as exc:
        print(f"[BATCH] status=FAILED folder={video_dir} error={exc}", flush=True)
        return 1
    finally:
        _remove_pid_if_owned(pid_file, pid)
        lease.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", help="Directory containing video files")
    parser.add_argument("--queue-file", help="Queue file containing batch-folder jobs")
    parser.add_argument("--limit", type=int, default=0, help="Max videos to process (0=all)")
    parser.add_argument("--dry-run", action="store_true", help="List videos without processing")
    parser.add_argument("--extensions", default=DEFAULT_EXTENSIONS, help="Video extensions")
    parser.add_argument("--force", action="store_true", help="Re-process completed videos")
    parser.add_argument("--launch-token", help="Queue launch claim token from the Control UI")
    parser.add_argument(
        "--worker-lease-file",
        default=str(DEFAULT_WORKER_LEASE_FILE),
        help="Cross-process lease shared by direct and queue workers",
    )
    parser.add_argument("--pid-file", default=PID_FILE, help="Worker PID handshake file")
    args = parser.parse_args()

    if not args.dir and not args.queue_file:
        parser.error("one of --dir or --queue-file is required")
    if args.dir and args.queue_file:
        parser.error("--dir and --queue-file cannot be used together")
    if args.queue_file:
        return run_queue(
            args.queue_file,
            worker_lease_file=args.worker_lease_file,
            pid_file=args.pid_file,
            launch_token=args.launch_token,
        )

    if args.dry_run:
        videos = discover_videos(args.dir, args.extensions)
        if not videos:
            print(f"No videos found in {args.dir}")
            return 1
        print(f"Found {len(videos)} videos in {args.dir}")
        for i, video in enumerate(videos):
            print(f"  [{i + 1}] {os.path.splitext(os.path.basename(video))[0]}")
        return 0

    return run_direct(
        args.dir,
        args.limit,
        args.extensions,
        args.force,
        worker_lease_file=args.worker_lease_file,
        pid_file=args.pid_file,
    )


if __name__ == "__main__":
    raise SystemExit(main())

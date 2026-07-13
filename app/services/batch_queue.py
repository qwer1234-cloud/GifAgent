"""Persistent ordered queue for batch-folder jobs."""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_QUEUE_FILE = Path("data/batch_queue.json")
DEFAULT_STATE_FILE = Path("data/batch_queue_state.json")
DEFAULT_WORKER_LEASE_FILE = Path("data/batch_worker.lock")
TERMINAL_QUEUE_JOB_STATUSES = {"completed", "failed"}


class BatchQueueFormatError(ValueError):
    """Raised when a queue or state file cannot be read as its expected shape."""


class DuplicateQueueJobError(ValueError):
    """Raised when an active job already owns the requested directory."""

    def __init__(self, directory: str, existing_job: dict):
        self.directory = directory
        self.existing_job = dict(existing_job)
        job_id = self.existing_job.get("job_id", "unknown")
        super().__init__(f"Directory already queued: {directory} (job {job_id})")


class WorkerLeaseBusyError(RuntimeError):
    """Raised when another direct or queue batch worker owns the process lease."""


class InterProcessFileLock:
    """Small advisory file lock implemented with stdlib Windows/POSIX APIs."""

    def __init__(self, path: str | Path, *, timeout: float = 5.0):
        self.path = Path(path)
        self.timeout = timeout
        self._stream = None

    def _try_lock(self) -> None:
        assert self._stream is not None
        self._stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def acquire(self, *, blocking: bool = True) -> "InterProcessFileLock":
        if self._stream is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b", buffering=0)
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            os.fsync(stream.fileno())
        self._stream = stream
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._try_lock()
                return self
            except (BlockingIOError, OSError):
                if not blocking or time.monotonic() >= deadline:
                    self._stream.close()
                    self._stream = None
                    raise WorkerLeaseBusyError(f"Lock is already held: {self.path}")
                time.sleep(0.01)

    def write_metadata(self, payload: dict) -> None:
        if self._stream is None:
            raise RuntimeError("Cannot write metadata before acquiring the lock")
        encoded = (json.dumps(payload, ensure_ascii=True) + "\n").encode("ascii")
        self._stream.seek(0)
        self._stream.truncate()
        self._stream.write(encoded)
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self._stream.seek(0)

    def release(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None

    def __enter__(self) -> "InterProcessFileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


class WorkerLease:
    """Exclusive cross-process lease shared by queue and direct batch modes."""

    def __init__(self, path: str | Path = DEFAULT_WORKER_LEASE_FILE, *, mode: str):
        self.path = Path(path)
        self.mode = mode
        self._lock = InterProcessFileLock(self.path, timeout=0)

    def acquire(self) -> "WorkerLease":
        try:
            self._lock.acquire(blocking=False)
        except WorkerLeaseBusyError as exc:
            raise WorkerLeaseBusyError(
                f"Another batch worker already owns {self.path}"
            ) from exc
        self._lock.write_metadata(
            {"pid": os.getpid(), "mode": self.mode, "acquired_at": _timestamp()}
        )
        return self

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> "WorkerLease":
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


def _lock_path(path: str | Path, purpose: str) -> Path:
    target = Path(path)
    return target.with_name(f".{target.name}.{purpose}.lock")


@contextmanager
def queue_state_lock(path: str | Path = DEFAULT_STATE_FILE):
    """Serialize queue-state read/modify/write transactions across GUI processes."""
    lock = InterProcessFileLock(_lock_path(path, "state"))
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


@contextmanager
def queue_state_transaction(
    queue_path: str | Path = DEFAULT_QUEUE_FILE,
    state_path: str | Path = DEFAULT_STATE_FILE,
):
    """Atomically coordinate queue append and worker state transitions.

    Every caller acquires the queue lock before the state lock.  The final
    worker drain check and a GUI append therefore cannot observe different
    halves of the handoff.
    """
    queue_lock = InterProcessFileLock(_lock_path(queue_path, "queue"))
    state_lock = InterProcessFileLock(_lock_path(state_path, "state"))
    with queue_lock:
        with state_lock:
            yield


def queue_state_path_for_queue(path: str | Path) -> Path:
    """Return the sibling state path used by a queue file."""
    queue_path = Path(path)
    return queue_path.with_name(f"{queue_path.stem}_state{queue_path.suffix}")


def _read_json(path: str | Path) -> Any:
    try:
        with Path(path).open(encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        if isinstance(exc, OSError) and not Path(path).exists():
            return None
        raise BatchQueueFormatError(f"Invalid queue file: {path}") from exc


def _atomic_write(payload: dict, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_directory(directory: str | Path) -> str:
    """Return a stable, case-normalized identity for a directory path."""
    value = os.fspath(directory).strip()
    if not value:
        return ""
    return os.path.normcase(os.path.realpath(os.path.abspath(value)))


def queue_job_directory_key(job: dict) -> str:
    """Read a new or legacy queue job's canonical directory identity."""
    stored_key = job.get("directory_key")
    if stored_key:
        return os.path.normcase(str(stored_key))
    return canonical_directory(job.get("directory", ""))


def _queue_job_status(job: dict, state: dict) -> str:
    return state.get("jobs", {}).get(job.get("job_id"), {}).get("status", "pending")


def deduplicate_queue_jobs(queue: dict, state: dict | None = None) -> tuple[dict, list[dict]]:
    """Remove duplicate active jobs while retaining terminal history entries."""
    state = state or {"jobs": {}}
    seen_active: set[str] = set()
    kept: list[dict] = []
    removed: list[dict] = []
    for job in queue.get("jobs", []):
        key = queue_job_directory_key(job)
        status = _queue_job_status(job, state)
        if not key or status in TERMINAL_QUEUE_JOB_STATUSES or key not in seen_active:
            kept.append(job)
            if key and status not in TERMINAL_QUEUE_JOB_STATUSES:
                seen_active.add(key)
        else:
            removed.append(job)
    cleaned = dict(queue)
    cleaned["jobs"] = kept
    return cleaned, removed


def deduplicate_queue_file(
    path: str | Path = DEFAULT_QUEUE_FILE,
    *,
    state_path: str | Path | None = None,
) -> list[dict]:
    """Persist removal of duplicate active jobs and return removed jobs."""
    queue_path = Path(path)
    transaction_state_path = (
        Path(state_path) if state_path is not None else queue_state_path_for_queue(queue_path)
    )
    with queue_state_transaction(queue_path, transaction_state_path):
        queue = load_queue(queue_path)
        state = load_queue_state(transaction_state_path)
        cleaned, removed = deduplicate_queue_jobs(queue, state)
        if removed:
            save_queue(cleaned, queue_path)
            removed_ids = {job.get("job_id") for job in removed}
            state["jobs"] = {
                job_id: job_state
                for job_id, job_state in state.get("jobs", {}).items()
                if job_id not in removed_ids
            }
            save_queue_state(state, transaction_state_path)
    return removed


def load_queue(path: str | Path = DEFAULT_QUEUE_FILE) -> dict:
    payload = _read_json(path)
    if payload is None:
        return {"jobs": [], "updated_at": None}
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise BatchQueueFormatError(f"Invalid queue format: {path}")
    return {"jobs": payload["jobs"], "updated_at": payload.get("updated_at")}


def save_queue(queue: dict, path: str | Path = DEFAULT_QUEUE_FILE) -> None:
    if not isinstance(queue, dict) or not isinstance(queue.get("jobs"), list):
        raise BatchQueueFormatError("Queue must contain a list of jobs")
    payload = dict(queue)
    payload["updated_at"] = _timestamp()
    _atomic_write(payload, path)


def append_queue_job(
    directory: str,
    limit: int = 0,
    extensions: str = "",
    path: str | Path = DEFAULT_QUEUE_FILE,
    *,
    state_path: str | Path | None = None,
) -> dict:
    queue_path = Path(path)
    transaction_state_path = Path(state_path) if state_path is not None else queue_state_path_for_queue(queue_path)
    with queue_state_transaction(queue_path, transaction_state_path):
        queue = load_queue(path)
        state = load_queue_state(transaction_state_path)
        queue, _removed = deduplicate_queue_jobs(queue, state)
        if _removed:
            save_queue(queue, path)
        requested_key = canonical_directory(directory)
        existing_job = next(
            (
                job
                for job in queue["jobs"]
                if queue_job_directory_key(job) == requested_key
                and _queue_job_status(job, state) not in TERMINAL_QUEUE_JOB_STATUSES
            ),
            None,
        )
        if existing_job is not None:
            raise DuplicateQueueJobError(directory, existing_job)
        job = {
            "job_id": str(uuid.uuid4()),
            "directory": directory,
            "directory_key": requested_key,
            "limit": limit,
            "extensions": extensions,
            "created_at": _timestamp(),
        }
        queue["jobs"].append(job)
        save_queue(queue, path)
    return job


def load_queue_state(path: str | Path = DEFAULT_STATE_FILE) -> dict:
    payload = _read_json(path)
    if payload is None:
        return {"status": "idle", "current_job_id": None, "jobs": {}}
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("jobs"), dict)
    ):
        raise BatchQueueFormatError(f"Invalid queue state format: {path}")
    state = dict(payload)
    state.setdefault("status", "idle")
    state.setdefault("current_job_id", None)
    state["jobs"] = payload["jobs"]
    return state


def save_queue_state(state: dict, path: str | Path = DEFAULT_STATE_FILE) -> None:
    if not isinstance(state, dict) or not isinstance(state.get("jobs"), dict):
        raise BatchQueueFormatError("Queue state must contain a jobs mapping")
    payload = dict(state)
    payload.setdefault("status", "idle")
    payload.setdefault("current_job_id", None)
    payload["jobs"] = state["jobs"]
    _atomic_write(payload, path)


def pending_jobs(queue: dict, state: dict) -> list[dict]:
    states = state.get("jobs", {})
    return [
        job
        for job in queue.get("jobs", [])
        if states.get(job.get("job_id"), {}).get("status") not in {"completed", "failed"}
    ]


def update_job_state(state: dict, job_id: str, status: str, **updates) -> dict:
    state.setdefault("jobs", {})
    job_state = dict(state["jobs"].get(job_id, {}))
    job_state.update(updates)
    job_state["status"] = status
    state["jobs"][job_id] = job_state
    return state


def format_queue_status(queue: dict, state: dict) -> str:
    statuses = state.get("jobs", {})
    jobs = queue.get("jobs", [])
    lines = [
        f"Batch queue ({len(jobs)} jobs)",
        f"Worker: {state.get('status', 'idle')}",
        f"PID: {state.get('worker_pid') or 'N/A'}",
    ]
    if state.get("cleanup_pending"):
        lines.append("Cleanup pending: YES")
    if state.get("last_error"):
        lines.append(f"Last error: {state['last_error']}")
    for index, job in enumerate(jobs, 1):
        status = statuses.get(job.get("job_id"), {}).get("status", "pending")
        lines.append(f"{index}. [{status}] {job.get('directory', '')}")
    return "\n".join(lines)

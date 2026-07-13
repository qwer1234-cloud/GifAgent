"""Persistent ordered queue for batch-folder jobs."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_QUEUE_FILE = Path("data/batch_queue.json")
DEFAULT_STATE_FILE = Path("data/batch_queue_state.json")


class BatchQueueFormatError(ValueError):
    """Raised when a queue or state file cannot be read as its expected shape."""


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
) -> dict:
    queue = load_queue(path)
    job = {
        "job_id": str(uuid.uuid4()),
        "directory": directory,
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
    state = {
        "status": payload.get("status", "idle"),
        "current_job_id": payload.get("current_job_id"),
        "jobs": payload["jobs"],
    }
    for key in ("worker_pid", "cleanup_pending", "last_error"):
        if key in payload:
            state[key] = payload[key]
    return state


def save_queue_state(state: dict, path: str | Path = DEFAULT_STATE_FILE) -> None:
    if not isinstance(state, dict) or not isinstance(state.get("jobs"), dict):
        raise BatchQueueFormatError("Queue state must contain a jobs mapping")
    payload = {
        "status": state.get("status", "idle"),
        "current_job_id": state.get("current_job_id"),
        "jobs": state["jobs"],
    }
    for key in ("worker_pid", "cleanup_pending", "last_error"):
        if key in state:
            payload[key] = state[key]
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
    lines = [f"Batch queue ({len(jobs)} jobs)"]
    for index, job in enumerate(jobs, 1):
        status = statuses.get(job.get("job_id"), {}).get("status", "pending")
        lines.append(f"{index}. [{status}] {job.get('directory', '')}")
    return "\n".join(lines)

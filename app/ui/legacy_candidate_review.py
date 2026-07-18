"""Compatibility surface for historical ``app.ui.candidate_review`` callers.

The implementation delegates to the modular Workbench tabs.  Queue lifecycle
helpers are translated to the task HTTP API when legacy PID mode is disabled.
"""

from __future__ import annotations

import os

from app.ui.api_client import GifAgentApiClient
from app.ui.components.common import _format_api_error
from app.ui.tabs.control import get_process_command_line, is_batch_process
from app.ui.tabs.profile import (
    build_profile,
    build_profile_and_refresh,
    get_profile_status,
    load_profile_publish_choices,
    publish_profile_and_refresh,
)
from app.ui.tabs.review import (
    _candidate_display_path,
    _ensure_candidate_thumbnail,
    _folder_label,
    _safe_float,
    load_candidates,
    select_candidate,
    selection_values,
    submit_review_action,
    undo_and_refresh,
)
from app.ui.tabs.settings import (
    config_checkbox,
    config_field_name,
    config_textbox,
    load_config,
    save_config,
    test_llm_connection,
)
from app.ui.workbench import _build_gradio_allowed_paths as _allowed_paths


def _build_gradio_allowed_paths() -> list[str]:
    return list(_allowed_paths())


def _active_jobs(client: GifAgentApiClient) -> list[dict]:
    payload = client.list_tasks()
    if not isinstance(payload, list):
        return []
    active = {"pending", "leased", "running", "retry_wait", "needs_attention"}
    return [job for job in payload if job.get("status") in active]


def get_batch_status() -> dict:
    """Return the old status shape backed by the task API."""
    jobs = _active_jobs(GifAgentApiClient())
    current = jobs[0] if jobs else {}
    return {
        "running": bool(jobs),
        "pid": None,
        "completed": 0,
        "failed": sum(job.get("status") == "needs_attention" for job in jobs),
        "total": int(current.get("video_count", 0) or 0),
        "current_video": "",
        "gpu_model": "",
        "job_id": current.get("job_id"),
    }


def start_batch(video_dir: str, limit: int = 0, extensions: str = "") -> str:
    """Create a task-engine job through the historical function signature."""
    if not video_dir or not os.path.isdir(video_dir):
        return f"Invalid directory: {video_dir}"
    result = GifAgentApiClient().create_task(video_dir, int(limit or 0), extensions or "")
    if not isinstance(result, dict):
        return f"Failed to start: unexpected API response {result!r}"
    if result.get("error"):
        detail = result.get("detail") or result["error"]
        return f"Failed to start: {detail}"
    return f"Batch queued (job {result.get('job_id', 'unknown')}) - dir: {video_dir}"


def stop_batch() -> str:
    """Cancel active task-engine jobs through the historical no-argument API."""
    client = GifAgentApiClient()
    jobs = _active_jobs(client)
    if not jobs:
        return "No batch process running."
    cancelled = 0
    errors: list[str] = []
    for job in jobs:
        result = client.cancel_task(job["job_id"])
        if isinstance(result, dict) and result.get("error"):
            errors.append(f"{job['job_id']}: {result['error']}")
        else:
            cancelled += 1
    if errors:
        return f"Cancelled {cancelled} job(s); errors: {'; '.join(errors)}"
    return f"Cancelled {cancelled} active job(s)."


__all__ = [name for name in globals() if not name.startswith("__")]

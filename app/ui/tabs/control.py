"""Control tab — task queue management panel.

``build_control_tab(client)`` should be called from inside a
``gr.Blocks`` context (usually within ``with gr.Tab("Control"):``).
It creates all Gradio components and wires their events internally,
returning a dict of component references so the caller can attach
additional listeners if needed.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import gradio as gr

from app.ui.api_client import GifAgentApiClient

PID_FILE = "data/batch_pid.txt"
CHECKPOINT_FILE = "data/batch_checkpoint.json"

_ACTIVE_STATUSES = frozenset({"pending", "running", "leased", "retry_wait"})


# ---------------------------------------------------------------------------
# Legacy fallback helpers (used when the API server is unreachable)
# ---------------------------------------------------------------------------

def _legacy_read_status() -> dict:
    """Read PID file and checkpoint directly — mirrors ``get_batch_status``."""
    status = {
        "running": False,
        "pid": None,
        "completed": 0,
        "failed": 0,
        "total": 0,
        "current_video": "",
    }

    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                pid = int(f.read().strip())
            # Quick process-exists check
            if os.name == "nt":
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                    capture_output=True, text=True, timeout=3,
                )
                alive = str(pid) in result.stdout
            else:
                result = subprocess.run(
                    ["kill", "-0", str(pid)], capture_output=True, timeout=3,
                )
                alive = result.returncode == 0
            if alive:
                status["running"] = True
                status["pid"] = pid
            else:
                try:
                    os.remove(PID_FILE)
                except OSError:
                    pass
        except (ValueError, OSError, subprocess.TimeoutExpired):
            try:
                os.remove(PID_FILE)
            except OSError:
                pass

    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, encoding="utf-8-sig") as f:
                cp = json.load(f)
            run = cp.get("last_run")
            if isinstance(run, dict):
                status["completed"] = int(run.get("succeeded", 0)) + int(
                    run.get("dedup_skipped", 0)
                )
                status["failed"] = int(run.get("failed", 0))
                status["total"] = int(run.get("planned", 0))
                status["current_video"] = run.get("current_video", "") or ""
            else:
                completed = 0
                for info in cp.get("completed", {}).values():
                    item_status = (
                        info.get("status") if isinstance(info, dict) else None
                    )
                    if item_status in {"ok", "dedup_skipped"}:
                        completed += 1
                status["completed"] = completed
                status["total"] = completed
        except Exception:
            pass

    return status


# ---------------------------------------------------------------------------
# Tab builder
# ---------------------------------------------------------------------------

def build_control_tab(client: GifAgentApiClient) -> dict:
    """Build the Gradio Control tab components inside the current Blocks context.

    Parameters
    ----------
    client : GifAgentApiClient
        An API client wired to the task-engine server.

    Returns
    -------
    dict
        All Gradio components keyed by name so the caller can attach
        additional event handlers if needed.
    """
    gr.Markdown("## Task Queue Control")

    # ---- Job queue + summary ------------------------------------------------
    with gr.Row():
        with gr.Column(scale=2):
            job_table = gr.Dataframe(
                headers=[
                    "Job ID",
                    "Folder",
                    "Status",
                    "Videos",
                    "Stages",
                    "Clips",
                    "Created",
                ],
                label="Job Queue",
                interactive=False,
            )
        with gr.Column(scale=1):
            summary_text = gr.Textbox(
                label="Status Summary", interactive=False, lines=4
            )

    # ---- Create job panel ---------------------------------------------------
    with gr.Group():
        gr.Markdown("### Create Job")
        dir_input = gr.Textbox(
            label="Video Directory",
            value="",
            placeholder="Path to video directory...",
        )
        with gr.Row():
            limit_input = gr.Number(label="Limit (0=all)", value=0, precision=0)
            ext_input = gr.Textbox(
                label="Extensions",
                value=".mp4,.mkv,.avi,.mov,.webm,.ts",
                placeholder=".mp4,.mkv,.avi,.mov,.webm,.ts",
            )
        with gr.Row():
            start_btn = gr.Button("Start", variant="primary")
            cancel_btn = gr.Button("Cancel Selected", variant="stop")
            retry_btn = gr.Button("Retry Selected")
        control_output = gr.Textbox(label="Result", interactive=False)

    # ---- Job selector (used by cancel / retry) ------------------------------
    job_id_input = gr.Textbox(
        label="Job ID",
        placeholder="Click a row in the table above or paste a job ID",
    )

    # ---- Event log ----------------------------------------------------------
    event_log = gr.Textbox(label="Event Log", interactive=False, lines=8)

    # ---- Manual refresh -----------------------------------------------------
    refresh_btn = gr.Button("Refresh")

    # ---- State variables ----------------------------------------------------
    jobs_state = gr.State([])

    # ---- Timer --------------------------------------------------------------
    timer = gr.Timer(10)

    # ---- Internal helpers ---------------------------------------------------

    def _format_jobs(jobs: list | dict) -> list[list]:
        """Convert job dicts into rows for the Dataframe."""
        if not jobs or (isinstance(jobs, dict) and "error" in jobs):
            return []
        rows: list[list] = []
        for job in jobs:
            rows.append(
                [
                    str(job.get("job_id", ""))[:12],
                    str(job.get("folder", "")),
                    str(job.get("status", "")),
                    str(job.get("video_count", 0)),
                    str(job.get("stage_count", 0)),
                    str(job.get("clip_count", 0)),
                    str(job.get("created_at", ""))[:19],
                ]
            )
        return rows

    def _build_summary(jobs: list | dict) -> str:
        """Build a one-line status summary string."""
        if isinstance(jobs, dict) and "error" in jobs:
            return f"API unavailable — {jobs.get('error', 'unknown error')}"
        total = len(jobs)
        active = sum(1 for j in jobs if j.get("status") in _ACTIVE_STATUSES)
        succeeded = sum(1 for j in jobs if j.get("status") == "succeeded")
        attention = sum(1 for j in jobs if j.get("status") == "needs_attention")
        cancelled = sum(1 for j in jobs if j.get("status") == "cancelled")
        pending = sum(1 for j in jobs if j.get("status") == "pending")
        return (
            f"Total: {total} | Active: {active} | Pending: {pending} | "
            f"Succeeded: {succeeded} | Needs Attention: {attention} | "
            f"Cancelled: {cancelled}"
        )

    def _format_events(events: list | dict) -> str:
        """Convert events to a readable log string (most recent last)."""
        if isinstance(events, dict) and "error" in events:
            return f"Events unavailable: {events.get('error', '')}"
        lines: list[str] = []
        for ev in events if isinstance(events, list) else []:
            kind = ev.get("kind", "?")
            ts = str(ev.get("created_at", ""))[:19]
            lines.append(f"[{ts}] {kind}")
        return "\n".join(lines[-50:])  # keep last 50 lines

    def _try_legacy() -> tuple[list[list], str, str]:
        """Fallback: read legacy PID/checkpoint files when API is down."""
        s = _legacy_read_status()
        rows = [
            [
                s.get("pid") and f"PID-{s['pid']}" or "",
                "legacy",
                "running" if s.get("running") else "stopped",
                str(s.get("completed", 0)),
                "",
                "",
                "",
            ]
        ]
        summary = (
            f"Legacy mode | Running: {'YES' if s['running'] else 'NO'} | "
            f"Completed: {s['completed']} | Failed: {s['failed']} | "
            f"Total: {s['total']}"
        )
        log_text = f"[legacy] PID: {s['pid'] or 'N/A'}, running: {s['running']}"
        return rows, summary, log_text

    # ---- Refresh all --------------------------------------------------------

    def _refresh_all():
        """Fetch latest jobs + events from the API (with legacy fallback)."""
        jobs = client.list_tasks()
        events = client.task_events(after_id=0)

        # If API is unreachable, fall back to legacy file reads
        if isinstance(jobs, dict) and "error" in jobs:
            return *_try_legacy(), []

        rows = _format_jobs(jobs)
        summary = _build_summary(jobs)
        event_text = _format_events(events)
        return rows, summary, event_text, jobs

    # ---- Action handlers ----------------------------------------------------

    def _do_create(directory: str, limit: int, extensions: str):
        """Create a new task and refresh the display."""
        if not directory or not directory.strip():
            return "Error: Directory is required", *_try_legacy(), []

        limit = int(limit) if limit is not None else 0

        result = client.create_task(directory.strip(), limit, extensions)
        if isinstance(result, dict) and "error" in result:
            if "existing_job_id" in str(result.get("detail", {})):
                eid = result["detail"].get("existing_job_id", "?")
                msg = f"Active job already exists: {eid}"
            else:
                msg = f"Error: {result['error']}"
        else:
            msg = (
                f"Created job {str(result.get('job_id', ''))[:12]} for "
                f"{directory} (limit={limit}, ext={extensions!r})"
            )

        # Refresh after action
        jobs = client.list_tasks()
        events = client.task_events(after_id=0)
        if isinstance(jobs, dict) and "error" in jobs:
            fallback_rows, fallback_summary, fallback_log = _try_legacy()
            return msg, fallback_rows, fallback_summary, fallback_log, []

        rows = _format_jobs(jobs)
        summary = _build_summary(jobs)
        event_text = _format_events(events)
        return msg, rows, summary, event_text, jobs

    def _do_cancel(job_id: str):
        """Cancel the selected job and refresh."""
        if not job_id or not job_id.strip():
            return "No job selected", gr.update(), gr.update(), gr.update(), []

        result = client.cancel_task(job_id.strip())
        if isinstance(result, dict) and "error" in result:
            msg = f"Cancel error: {result['error']}"
        else:
            msg = f"Cancelled job {job_id.strip()[:12]}"

        jobs = client.list_tasks()
        events = client.task_events(after_id=0)
        if isinstance(jobs, dict) and "error" in jobs:
            fallback_rows, fallback_summary, fallback_log = _try_legacy()
            return msg, fallback_rows, fallback_summary, fallback_log, []

        rows = _format_jobs(jobs)
        summary = _build_summary(jobs)
        event_text = _format_events(events)
        return msg, rows, summary, event_text, jobs

    def _do_retry(job_id: str):
        """Retry the selected job and refresh."""
        if not job_id or not job_id.strip():
            return "No job selected", gr.update(), gr.update(), gr.update(), []

        result = client.retry_task(job_id.strip())
        if isinstance(result, dict) and "error" in result:
            msg = f"Retry error: {result['error']}"
        else:
            msg = f"Retry requested for job {job_id.strip()[:12]}"

        jobs = client.list_tasks()
        events = client.task_events(after_id=0)
        if isinstance(jobs, dict) and "error" in jobs:
            fallback_rows, fallback_summary, fallback_log = _try_legacy()
            return msg, fallback_rows, fallback_summary, fallback_log, []

        rows = _format_jobs(jobs)
        summary = _build_summary(jobs)
        event_text = _format_events(events)
        return msg, rows, summary, event_text, jobs

    def _select_job(evt: gr.SelectData, jobs: list):
        """Handle row selection in the job table."""
        if not jobs:
            return ""
        row = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
        if 0 <= row < len(jobs):
            job = jobs[row]
            if isinstance(job, dict):
                return str(job.get("job_id", ""))
        return ""

    # ---- Wire events --------------------------------------------------------

    refresh_outputs = [
        job_table,
        summary_text,
        event_log,
        jobs_state,
    ]

    timer.tick(fn=_refresh_all, outputs=refresh_outputs)

    refresh_btn.click(fn=_refresh_all, outputs=refresh_outputs)

    start_btn.click(
        fn=_do_create,
        inputs=[dir_input, limit_input, ext_input],
        outputs=[control_output, *refresh_outputs],
    )

    cancel_btn.click(
        fn=_do_cancel,
        inputs=[job_id_input],
        outputs=[control_output, *refresh_outputs],
    )

    retry_btn.click(
        fn=_do_retry,
        inputs=[job_id_input],
        outputs=[control_output, *refresh_outputs],
    )

    job_table.select(
        fn=_select_job,
        inputs=[jobs_state],
        outputs=[job_id_input],
    )

    # Return all components so the caller can wire additional events.
    return {
        "job_table": job_table,
        "summary_text": summary_text,
        "dir_input": dir_input,
        "limit_input": limit_input,
        "ext_input": ext_input,
        "start_btn": start_btn,
        "cancel_btn": cancel_btn,
        "retry_btn": retry_btn,
        "job_id_input": job_id_input,
        "control_output": control_output,
        "event_log": event_log,
        "refresh_btn": refresh_btn,
        "timer": timer,
        "jobs_state": jobs_state,
    }


# ---------------------------------------------------------------------------
# Legacy queue lifecycle functions (moved from candidate_review.py)
# ---------------------------------------------------------------------------


def summarize_checkpoint_status(cp: dict) -> dict:
    """Summarize checkpoint data into a flat status dict."""
    run = cp.get("last_run")
    if isinstance(run, dict):
        completed = sum(
            int(run.get(field, 0) or 0)
            for field in (
                "succeeded",
                "dedup_skipped",
                "skipped_reusable",
                "skipped_limit",
            )
        )
        failed = int(run.get("failed", 0) or 0)
        processed = int(run.get("processed", 0) or 0)
        planned = int(run.get("planned", 0) or 0)
        return {
            "completed": completed,
            "failed": failed,
            "total": max(planned, processed, completed + failed),
            "current_video": run.get("current_video", "") or "",
        }

    completed = 0
    for info in cp.get("completed", {}).values():
        item_status = info.get("status") if isinstance(info, dict) else None
        if item_status in {"ok", "dedup_skipped"}:
            completed += 1
    return {
        "completed": completed,
        "failed": 0,
        "total": completed,
        "current_video": "",
    }


def is_batch_command_line(command_line: str | None) -> bool:
    if not command_line:
        return False
    normalized = command_line.replace("\\", "/").lower()
    return "test_video_batch.py" in normalized


def get_process_command_line(pid: int) -> str | None:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None

    try:
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=flags,
            )
        else:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "args="],
                capture_output=True,
                text=True,
                timeout=3,
            )
    except Exception:
        return None

    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def is_batch_process(pid: int) -> bool:
    return is_batch_command_line(get_process_command_line(pid))


# Conditionally-compiled legacy PID management
if os.environ.get("GIFAGENT_LEGACY_QUEUE_UI"):

    def get_batch_status():  # type: ignore[misc]
        """Check current batch processing status (legacy PID mode)."""
        import signal
        import time

        status = {
            "running": False,
            "pid": None,
            "completed": 0,
            "failed": 0,
            "total": 0,
            "current_video": "",
            "gpu_model": "",
        }

        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE) as f:
                    pid = int(f.read().strip())
                if is_batch_process(pid):
                    status["running"] = True
                    status["pid"] = pid
                else:
                    os.remove(PID_FILE)
            except (ValueError, OSError, ProcessLookupError):
                status["running"] = False
                try:
                    os.remove(PID_FILE)
                except OSError:
                    pass

        if os.path.exists(CHECKPOINT_FILE):
            try:
                with open(CHECKPOINT_FILE, encoding="utf-8-sig") as f:
                    cp = json.load(f)
                status.update(summarize_checkpoint_status(cp))
            except Exception:
                pass

        try:
            r = httpx.get("http://127.0.0.1:11434/api/ps", timeout=5)
            models = r.json().get("models", [])
            if models:
                status["gpu_model"] = models[0].get("name", "?")
        except Exception:
            status["gpu_model"] = "ollama offline"

        return status

    def stop_batch():  # type: ignore[misc]
        """Stop running batch process (legacy PID mode)."""
        import signal
        import time

        status = get_batch_status()
        if not status["running"]:
            return "No batch process running."

        pid = status["pid"]
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        time.sleep(2)

        if is_batch_process(pid):
            return f"WARNING: Process {pid} may still be running. Try manual kill."
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        return f"Batch stopped (PID {pid}). Checkpoint saved at {CHECKPOINT_FILE}"

    def start_batch(video_dir: str, limit: int = 0, extensions: str = ""):  # type: ignore[misc]
        """Start batch processing in background (legacy PID mode)."""
        status = get_batch_status()
        if status["running"]:
            return f"Batch already running (PID {status['pid']}). Stop it first."

        if not video_dir or not os.path.isdir(video_dir):
            return f"Invalid directory: {video_dir}"

        import sys

        if getattr(sys, "frozen", False):
            script_path = os.path.join(sys._MEIPASS, "scripts", "test_video_batch.py")
            cmd = [sys.executable, "--run-script", script_path, "--dir", video_dir]
        else:
            cmd = ["uv", "run", "python", "-u", "scripts/test_video_batch.py", "--dir", video_dir]
        if limit > 0:
            cmd.extend(["--limit", str(limit)])
        if extensions and extensions.strip():
            cmd.extend(["--extensions", extensions.strip()])

        os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
        log_path = os.path.join(os.path.dirname(PID_FILE), "batch_subprocess.log")
        log_file = open(log_path, "w", encoding="utf-8", errors="replace")

        try:
            proc = subprocess.Popen(
                cmd, cwd=".",
                stdout=log_file, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            with open(PID_FILE, "w") as f:
                f.write(str(proc.pid))
            return (
                f"Batch started (PID {proc.pid}) - dir: {video_dir}"
                + (f" limit: {limit}" if limit > 0 else "")
                + (f" ext: {extensions}" if extensions else "")
                + f" | log: {log_path}"
            )
        except Exception as e:
            log_file.close()
            return f"Failed to start: {e}"

else:

    def get_batch_status():  # type: ignore[misc]
        return {
            "running": False, "pid": None, "completed": 0, "failed": 0,
            "total": 0, "current_video": "", "gpu_model": "legacy disabled",
        }

    def stop_batch():  # type: ignore[misc]
        return "Legacy PID mode is disabled. Use the API-based Control tab."

    def start_batch(video_dir, limit=0, extensions=""):  # type: ignore[misc]
        return "Legacy PID mode is disabled. Use the API-based Control tab."

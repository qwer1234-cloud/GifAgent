"""
Gradio UI - candidate GIF review + batch process control panel.
"""
import html, json, os, subprocess, signal, sys, time
from pathlib import Path

import gradio as gr
import httpx
import yaml
from PIL import Image

from app.db import get_connection
from app.services.batch_logging import read_batch_log
from app.services.batch_queue import (
    append_queue_job,
    format_queue_status,
    load_queue,
    load_queue_state,
    pending_jobs,
)
from app.services.candidate_vectors import backfill_candidate_vectors
from app.services.embedding import compute_text_embedding

API_BASE = "http://127.0.0.1:8000"
PID_FILE = "data/batch_pid.txt"
CHECKPOINT_FILE = "data/batch_checkpoint.json"
BATCH_QUEUE_FILE = "data/batch_queue.json"
BATCH_QUEUE_STATE_FILE = "data/batch_queue_state.json"
BATCH_LOG_FILE = "data/batch_subprocess.log"
QUEUE_WORKER_EXIT_GRACE_SECONDS = 0.1
CONFIG_FILE = "configs/models.yaml"
PAGE_SIZE = 12
THUMB_DIR = "data/thumbs/candidates"
STATIC_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
DEFAULT_SAMPLE_ROOT = os.path.abspath(os.path.join("data", "exports", "adaptive_test"))


def _build_gradio_allowed_paths() -> list[str]:
    paths = [
        os.getcwd(),
        os.path.abspath("data/exports"),
        os.path.abspath("data/thumbs"),
        os.path.abspath("data/frames"),
    ]
    allowed: list[str] = []
    seen: set[str] = set()
    for path in paths:
        for candidate in (path, os.path.realpath(path)):
            key = os.path.normcase(os.path.normpath(candidate))
            if key not in seen:
                allowed.append(candidate)
                seen.add(key)
    return allowed


GRADIO_ALLOWED_PATHS = _build_gradio_allowed_paths()


def summarize_checkpoint_status(cp: dict) -> dict:
    run = cp.get("last_run")
    if isinstance(run, dict):
        return {
            "completed": int(run.get("succeeded", 0)) + int(run.get("dedup_skipped", 0)),
            "failed": int(run.get("failed", 0)),
            "total": int(run.get("planned", 0)),
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


# Process manager
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


def get_batch_status():
    """Check current batch processing status."""
    status = {
        "running": False,
        "pid": None,
        "completed": 0,
        "failed": 0,
        "total": 0,
        "current_video": "",
        "current_folder": "",
        "queue_completed": 0,
        "queue_failed": 0,
        "queue_total": 0,
        "gpu_model": "",
    }

    # Check PID file
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

    # Check checkpoint
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, encoding="utf-8-sig") as f:
                cp = json.load(f)
            status.update(summarize_checkpoint_status(cp))
            last_run = cp.get("last_run")
            if isinstance(last_run, dict):
                status["current_folder"] = last_run.get("dir", "") or ""
        except Exception:
            pass

    try:
        queue = load_queue()
        queue_state = load_queue_state()
        queue_jobs = queue.get("jobs", [])
        job_states = queue_state.get("jobs", {})
        current_job_id = queue_state.get("current_job_id")
        current_job = next(
            (job for job in queue_jobs if job.get("job_id") == current_job_id), None
        )
        if current_job:
            status["current_folder"] = current_job.get("directory", "")
        status["queue_total"] = len(queue_jobs)
        status["queue_completed"] = sum(
            job_states.get(job.get("job_id"), {}).get("status") == "completed"
            for job in queue_jobs
        )
        status["queue_failed"] = sum(
            job_states.get(job.get("job_id"), {}).get("status") == "failed"
            for job in queue_jobs
        )
    except Exception:
        pass

    # Check Ollama GPU
    try:
        r = httpx.get("http://127.0.0.1:11434/api/ps", timeout=5)
        models = r.json().get("models", [])
        if models:
            status["gpu_model"] = models[0].get("name", "?")
    except Exception:
        status["gpu_model"] = "ollama offline"

    return status


def format_batch_status(status: dict) -> str:
    """Format the fixed Control-tab summary without detailed log output."""
    return "\n".join([
        f"Running: {'YES' if status.get('running') else 'NO'}",
        f"PID: {status.get('pid') or 'N/A'}",
        f"Current Folder: {status.get('current_folder') or 'N/A'}",
        f"Current Video: {status.get('current_video') or 'N/A'}",
        f"Video: {status.get('completed', 0)}/{status.get('total', 0)}",
        f"Video Failed: {status.get('failed', 0)}",
        f"Queue: {status.get('queue_completed', 0)}/{status.get('queue_total', 0)}",
        f"Queue Failed: {status.get('queue_failed', 0)}",
        f"GPU Model: {status.get('gpu_model') or 'N/A'}",
    ])


def refresh_batch_status() -> tuple[str, str, str]:
    """Refresh the independent Control summary, queue, and detailed log."""
    status = get_batch_status()
    try:
        queue_text = format_queue_status(load_queue(), load_queue_state())
    except Exception as exc:
        queue_text = f"Queue unavailable: {exc}"
    try:
        log_text = read_batch_log(BATCH_LOG_FILE)
    except OSError as exc:
        log_text = f"Detailed output log unavailable: {exc}"
    return format_batch_status(status), queue_text, log_text


def stop_batch():
    """Stop running batch process."""
    status = get_batch_status()
    if not status["running"]:
        return "No batch process running."

    pid = status["pid"]
    try:
        # Kill process tree on Windows
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=10)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    time.sleep(2)

    # Verify stopped
    if is_batch_process(pid):
        return f"WARNING: Process {pid} may still be running. Try manual kill."
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)
    return f"Batch stopped (PID {pid}). Checkpoint saved at {CHECKPOINT_FILE}"


def start_batch(video_dir: str, limit: int = 0, extensions: str = ""):
    """Start batch processing in background.

    extensions: comma-separated video extensions (e.g. ".ts,.mp4"). Empty = default.
    """
    status = get_batch_status()
    if status["running"]:
        return f"Batch already running (PID {status['pid']}). Stop it first."

    if not video_dir or not os.path.isdir(video_dir):
        return f"Invalid directory: {video_dir}"

    # When frozen (exe), use the exe itself with --run-script flag (PyInstaller
    # can't run arbitrary .py files via sys.executable directly).
    # When running from source, use uv run + relative path.
    if getattr(sys, "frozen", False):
        script_path = os.path.join(sys._MEIPASS, "scripts", "test_video_batch.py")
        cmd = [sys.executable, "--run-script", script_path, "--dir", video_dir]
    else:
        cmd = ["uv", "run", "python", "-u", "scripts/test_video_batch.py", "--dir", video_dir]
    if limit > 0:
        cmd.extend(["--limit", str(limit)])
    if extensions and extensions.strip():
        cmd.extend(["--extensions", extensions.strip()])

    # Redirect subprocess output to a log file so failures are diagnosable
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    log_path = BATCH_LOG_FILE
    log_file = open(log_path, "a", encoding="utf-8", errors="replace")

    try:
        proc = subprocess.Popen(
            cmd, cwd=".",
            stdout=log_file, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        with open(PID_FILE, "w") as f:
            f.write(str(proc.pid))
        return f"Batch started (PID {proc.pid}) - dir: {video_dir}" + \
               (f" limit: {limit}" if limit > 0 else "") + \
               (f" ext: {extensions}" if extensions else "") + \
               f" | log: {log_path}"
    except Exception as e:
        log_file.close()
        return f"Failed to start: {e}"


def start_batch_queue():
    """Start the persistent folder queue without replacing a valid worker."""
    status = get_batch_status()
    if status["running"]:
        return f"Batch already running (PID {status['pid']})."

    try:
        if not pending_jobs(load_queue(), load_queue_state()):
            return "No queued folders to process."
    except Exception as exc:
        return f"Queue unavailable: {exc}"

    if getattr(sys, "frozen", False):
        script_path = os.path.join(sys._MEIPASS, "scripts", "test_video_batch.py")
        cmd = [sys.executable, "--run-script", script_path, "--queue-file", BATCH_QUEUE_FILE]
    else:
        cmd = ["uv", "run", "python", "-u", "scripts/test_video_batch.py", "--queue-file", BATCH_QUEUE_FILE]

    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    log_file = open(BATCH_LOG_FILE, "a", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(
            cmd, cwd=".", stdout=log_file, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        with open(PID_FILE, "w") as f:
            f.write(str(proc.pid))
        return f"Batch queue started (PID {proc.pid})."
    except Exception as exc:
        log_file.close()
        return f"Failed to start queue: {exc}"


def append_batch_directory(
    video_dir: str,
    limit: int = 0,
    extensions: str = "",
) -> tuple[str, str]:
    """Append a folder and ensure an idle queue worker starts afterwards."""
    directory = (video_dir or "").strip()
    if not directory or not os.path.isdir(directory):
        return f"Invalid directory: {video_dir}", refresh_batch_status()[1]

    get_batch_status()
    append_queue_job(directory, int(limit or 0), (extensions or "").strip())
    queue_text = format_queue_status(load_queue(), load_queue_state())

    worker_running = get_batch_status().get("running")
    if worker_running:
        time.sleep(QUEUE_WORKER_EXIT_GRACE_SECONDS)
        worker_running = get_batch_status().get("running")
    if not worker_running:
        return f"Queued: {directory}. {start_batch_queue()}", queue_text
    return f"Queued: {directory}", queue_text


# Candidate review functions
def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ensure_candidate_thumbnail(candidate_id: str, artifact_path: str) -> str | None:
    if not candidate_id or not artifact_path or not os.path.exists(artifact_path):
        return None

    thumb_path = os.path.join(THUMB_DIR, f"{candidate_id}.jpg")
    if os.path.exists(thumb_path):
        return thumb_path

    try:
        os.makedirs(THUMB_DIR, exist_ok=True)
        with Image.open(artifact_path) as img:
            img.seek(0)
            frame = img.convert("RGB")
            frame.thumbnail((360, 240), Image.Resampling.LANCZOS)
            frame.save(thumb_path, "JPEG", quality=82, optimize=True)
        return thumb_path
    except Exception:
        return None


def _candidate_display_path(candidate: dict) -> str:
    preview_path = candidate.get("preview_path") or ""
    artifact_path = candidate.get("artifact_path") or ""

    for path in (preview_path, artifact_path):
        if path and Path(path).suffix.lower() in STATIC_IMAGE_EXTS and os.path.exists(path):
            return path

    thumb_path = _ensure_candidate_thumbnail(
        candidate.get("candidate_id", ""),
        artifact_path,
    )
    return thumb_path or candidate.get("display_path") or preview_path or artifact_path


def _format_api_error(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail", resp.text)
        if isinstance(detail, dict):
            message = detail.get("message") or detail.get("error") or str(detail)
            count = detail.get("count")
            suffix = f" ({count} item(s))" if count else ""
            return f"{message}{suffix}"
        return str(detail)
    except Exception:
        return resp.text or f"HTTP {resp.status_code}"


def _folder_label(folder: dict) -> str:
    relative = folder.get("relative_folder") or "."
    depth = 0 if relative == "." else relative.count("/") + 1
    indent = "  " * max(0, depth - 1)
    missing = folder.get("missing_count") or 0
    unmaterialized = folder.get("unmaterialized_count") or 0
    details = []
    if unmaterialized:
        details.append(f"{unmaterialized} new")
    if missing:
        details.append(f"{missing} missing")
    suffix = f", {', '.join(details)}" if details else ""
    return f"{indent}{relative} ({folder.get('count', 0)}{suffix})"


def load_folder_choices(root_dir: str):
    if not root_dir or not root_dir.strip():
        return gr.update(choices=[], value=None), "Select a data folder first.", []

    try:
        resp = httpx.get(
            f"{API_BASE}/api/candidates/folders",
            params={"root": root_dir.strip(), "status": "all"},
            timeout=15,
        )
        if resp.status_code != 200:
            return gr.update(choices=[], value=None), f"Folder error: {_format_api_error(resp)}", []

        data = resp.json()
        folders = data.get("folders", [])
        # Filter out folders where every candidate is already rated
        # (status != "candidate"). Keep folders with at least one unrated.
        reviewable = [
            folder for folder in folders
            if (folder.get("status_counts", {}).get("candidate", 0) > 0
                or folder.get("unmaterialized_count", 0) > 0)
        ]
        fully_rated = len(folders) - len(reviewable)
        choices = [(_folder_label(folder), folder["folder"]) for folder in reviewable]
        if not choices:
            extra = f" ({fully_rated} folder(s) fully rated, hidden)" if fully_rated else ""
            return (
                gr.update(choices=[], value=None),
                f"No reviewable folders under {data.get('root', root_dir)}{extra}.",
                [],
            )
        extra = f" ({fully_rated} fully rated, hidden)" if fully_rated else ""
        return (
            gr.update(choices=choices, value=None),
            f"Found {len(choices)} reviewable folder(s){extra}. Choose a folder to review.",
            reviewable,
        )
    except Exception as e:
        return gr.update(choices=[], value=None), f"Folder error: {e}", []


def load_candidates(
    page: int,
    page_size: int = PAGE_SIZE,
    filter_status: str = "candidate",
    folder: str | None = None,
):
    if not folder:
        return [], {"error": "Choose a folder before loading candidates."}

    try:
        params = {
            "limit": page_size,
            "offset": max(0, page) * page_size,
            "status": filter_status or "candidate",
            "folder": folder,
        }
        resp = httpx.get(f"{API_BASE}/api/candidates", params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("candidates", []), data
        return [], {"error": _format_api_error(resp)}
    except Exception:
        return [], {"error": "API unavailable"}


RATING_ICON = {
    "candidate": "todo",
    "favorited": "favorite",
    "liked": "like",
    "disliked": "dislike",
    "neutral": "neutral",
    "rejected": "reject",
    "promoted": "promoted",
    "archived": "archived",
}


def load_candidate_page(
    page: int,
    page_size: int = PAGE_SIZE,
    filter_status: str = "candidate",
    folder: str | None = None,
):
    if not folder:
        return [], "Choose a data folder to review.", gr.update(value=0, maximum=1), []

    candidates, meta = load_candidates(page, page_size, filter_status, folder)
    if meta.get("error"):
        return [], f"Error: {meta['error']}", gr.update(value=0, maximum=1), []

    total = int(meta.get("total", len(candidates)))
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))

    expected_offset = page * page_size
    if meta and meta.get("offset") != expected_offset:
        candidates, meta = load_candidates(page, page_size, filter_status, folder)

    status_counts = meta.get("status_counts", {})
    counts_str = " | ".join(
        f"{RATING_ICON.get(k, k)} {v}" for k, v in sorted(status_counts.items())
    ) or "no candidates"

    gallery = []
    page_items = []
    for candidate in candidates:
        path = _candidate_display_path(candidate)
        cid = candidate.get("candidate_id", "")
        status = candidate.get("status", "candidate")
        icon = RATING_ICON.get(status, "?")
        start_s = _safe_float(candidate.get("start_sec"), 0.0)
        end_s = _safe_float(candidate.get("end_sec"), 0.0)
        label = f"{icon} [{status}] {start_s:.0f}s-{end_s:.0f}s | {cid[:16]}"
        if path:
            gallery.append((path, label))
        page_items.append(candidate)

    folder_name = os.path.basename(folder.rstrip("\\/")) or folder
    info = f"Folder: {folder_name} | Page {page + 1}/{total_pages} | {counts_str} | Showing: {total}"
    slider_update = gr.update(value=page, maximum=max(1, total_pages - 1))
    return gallery, info, slider_update, page_items


def selection_values(item: dict):
    """Return component values for one candidate, regardless of selection source."""
    cid = item.get("candidate_id", "")
    src = item.get("source_run_candidate_id", "?")
    artifact_path = item.get("artifact_path") or ""
    preview = artifact_path or item.get("display_path") or item.get("preview_path")
    return cid, f"Selected: {src[:40]}", preview, artifact_path


def select_candidate(evt: gr.SelectData, page_items: list[dict]):
    idx = evt.index
    if 0 <= idx < len(page_items):
        return selection_values(page_items[idx])
    return "", "Selection error", None, ""


def select_first_candidate(page_items: list[dict]):
    """Select the first refreshed candidate so the next GIF preview is visible."""
    if page_items:
        return selection_values(page_items[0])
    return "", "", None, ""


def load_folder_page(folder: str | None, filter_status: str = "candidate"):
    """Load folder page zero and select its first GIF for immediate preview."""
    gallery, info, page_update, page_items = load_candidate_page(
        0, filter_status=filter_status, folder=folder
    )
    return gallery, info, page_update, page_items, *select_first_candidate(page_items)


def next_reviewable_folder(
    previous_folders: list[dict],
    refreshed_folders: list[dict],
    current_folder: str | None,
) -> str | None:
    """Choose the next remaining folder in the loaded order, wrapping if needed."""
    remaining = {folder.get("folder") for folder in refreshed_folders if folder.get("folder")}
    if not remaining:
        return None

    previous_paths = [folder.get("folder") for folder in previous_folders if folder.get("folder")]
    try:
        current_index = previous_paths.index(current_folder)
    except ValueError:
        current_index = -1

    ordered_paths = previous_paths[current_index + 1:] + previous_paths[:current_index + 1]
    for path in ordered_paths:
        if path in remaining and path != current_folder:
            return path
    for folder in refreshed_folders:
        path = folder.get("folder")
        if path in remaining and path != current_folder:
            return path
    return None


def rate_candidate(candidate_id: str, rating: str, note: str = "", expected_artifact_path: str = ""):
    if not candidate_id or not candidate_id.strip():
        return "Error: No candidate selected"
    try:
        cid = candidate_id.strip()
        payload = {"rating": rating, "note": note}
        if expected_artifact_path:
            payload["expected_artifact_path"] = expected_artifact_path
        resp = httpx.post(
            f"{API_BASE}/api/candidates/{cid}/feedback",
            json=payload,
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            return f"Rated: {data['status']}"
        return f"Error: {resp.status_code} - {_format_api_error(resp)}"
    except Exception as e:
        return f"Error: {e}"


def favorite_candidate(candidate_id: str, expected_artifact_path: str = ""):
    if not candidate_id or not candidate_id.strip():
        return "Error: No candidate selected"
    try:
        payload = {}
        if expected_artifact_path:
            payload["expected_artifact_path"] = expected_artifact_path
        resp = httpx.post(
            f"{API_BASE}/api/candidates/{candidate_id.strip()}/favorite",
            json=payload,
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            return f"Rated: {data['status']}"
        return f"Error: {resp.status_code} - {_format_api_error(resp)}"
    except Exception as e:
        return f"Error: {e}"


def undo_last_action():
    try:
        resp = httpx.post(f"{API_BASE}/api/candidates/undo-last", json={}, timeout=10)
        if resp.status_code == 200:
            return f"Undo: {resp.json().get('status', 'unknown')}"
        return f"Error: {resp.status_code} - {_format_api_error(resp)}"
    except Exception as e:
        return f"Error: {e}"


def submit_review_action(candidate_id: str, action: str, note: str = "", expected_artifact_path: str = ""):
    if action == "favorite":
        return favorite_candidate(candidate_id, expected_artifact_path)
    return rate_candidate(candidate_id, action, note, expected_artifact_path)


def rate_and_advance(
    candidate_id: str,
    rating: str,
    note: str,
    expected_artifact_path: str,
    page: int,
    filter_status: str,
    folder: str | None,
    root_dir: str,
    previous_folders: list[dict],
):
    """Rate a GIF, select the next item, and advance folders when necessary."""
    result = submit_review_action(candidate_id, rating, note, expected_artifact_path)
    if not result.startswith("Rated:"):
        return (
            result, gr.update(), gr.update(), gr.update(), gr.update(),
            candidate_id, "Rating failed; selection kept", expected_artifact_path or None,
            expected_artifact_path, gr.update(), previous_folders,
        )

    gallery, info, page_update, page_items = load_candidate_page(
        int(page), filter_status=filter_status, folder=folder
    )
    if page_items:
        cid, label, preview, artifact_path = select_first_candidate(page_items)
        return (
            result, gallery, info, page_update, page_items,
            cid, label, preview, artifact_path,
            gr.update(value=folder), previous_folders,
        )

    _folder_update, folder_info, refreshed_folders = load_folder_choices(root_dir)
    next_folder = next_reviewable_folder(previous_folders, refreshed_folders, folder)
    folder_choices = [(_folder_label(item), item["folder"]) for item in refreshed_folders]
    if next_folder:
        gallery, next_info, page_update, page_items = load_candidate_page(
            0, filter_status=filter_status, folder=next_folder
        )
        cid, label, preview, artifact_path = select_first_candidate(page_items)
        return (
            result, gallery, f"Auto-advanced to next folder. {next_info}", page_update, page_items,
            cid, label, preview, artifact_path,
            gr.update(choices=folder_choices, value=next_folder), refreshed_folders,
        )

    return (
        result, [], folder_info, page_update, [], "", "All reviewable folders are complete.", None, "",
        gr.update(choices=folder_choices, value=None), refreshed_folders,
    )


def undo_and_refresh(page: int, filter_status: str, folder: str | None):
    result = undo_last_action()
    if result != "Undo: undone":
        return result, gr.update(), gr.update(), gr.update(), gr.update(), "", "", None, ""
    gallery, info, page_update, page_items = load_candidate_page(
        int(page), filter_status=filter_status, folder=folder
    )
    return result, gallery, info, page_update, page_items, *select_first_candidate(page_items)


def get_profile_status():
    try:
        resp = httpx.get(f"{API_BASE}/api/preference/profiles", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            current = data.get("current")
            builds = data.get("profiles", [])
            if current:
                return f"Current: {current['profile_version'][:20]}... | Builds: {len(builds)}"
            return f"No published profile | Builds: {len(builds)}"
    except Exception:
        pass
    return "API unavailable"


def profile_publish_choices(payload: dict) -> tuple[list[str], str | None]:
    profiles = payload.get("profiles", []) or []
    choices = [
        profile["profile_version"]
        for profile in profiles
        if profile.get("status") in {"completed", "built"}
    ]
    return choices, (choices[0] if choices else None)


def load_profile_publish_choices():
    try:
        resp = httpx.get(f"{API_BASE}/api/preference/profiles", timeout=10)
        if resp.status_code != 200:
            return gr.update(choices=[], value=None), f"Error: {resp.status_code} - {_format_api_error(resp)}"
        choices, value = profile_publish_choices(resp.json())
        status = get_profile_status()
        return gr.update(choices=choices, value=value), status
    except Exception as e:
        return gr.update(choices=[], value=None), f"API unavailable: {e}"


def build_profile():
    try:
        resp = httpx.post(
            f"{API_BASE}/api/preference/profiles/build",
            json={"dry_run": False},
            timeout=30,
        )
        return json.dumps(resp.json(), indent=2)
    except Exception as e:
        return str(e)


def build_profile_and_refresh():
    result = build_profile()
    dropdown, status = load_profile_publish_choices()
    return result, dropdown, status


def publish_profile_version(profile_version: str | None):
    if not profile_version:
        return "Select a completed profile_version first."
    try:
        resp = httpx.post(
            f"{API_BASE}/api/preference/profiles/{profile_version}/publish",
            timeout=30,
        )
        if resp.status_code == 200:
            return json.dumps(resp.json(), indent=2)
        return f"Error: {resp.status_code} - {_format_api_error(resp)}"
    except Exception as e:
        return str(e)


def publish_profile_and_refresh(profile_version: str | None):
    result = publish_profile_version(profile_version)
    dropdown, status = load_profile_publish_choices()
    return result, dropdown, status


def backfill_profile_vectors():
    """Create missing vectors only for candidates with effective feedback."""
    conn = None
    try:
        conn = get_connection()
        result = backfill_candidate_vectors(
            conn,
            embed_fn=compute_text_embedding,
            only_feedback=True,
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"}, indent=2)
    finally:
        if conn is not None:
            conn.close()


# Config editor
CONFIG_FIELD_KEYS = (
    "llm.provider",
    "llm.model",
    "llm.api_key_env",
    "llm.base_url",
    "llm.temperature",
    "llm.max_tokens",
    "llm.timeout_s",
    "vlm.model",
    "vlm.base_url",
    "adaptive.sample_interval",
    "adaptive.merge_gap",
    "adaptive.merge_score_threshold",
    "adaptive.worthiness_threshold",
    "adaptive.refine_threshold",
    "adaptive.max_duration",
    "adaptive.vlm_temperature",
    "adaptive.output_ratio",
    "adaptive.max_output",
    "adaptive.gif_fps",
    "preference_memory.enabled",
    "preference_memory.base_score_weight",
    "preference_memory.preference_score_weight",
)

CONFIG_FIELD_HELP = {
    "llm.provider": "文本合成使用的模型服务类型，例如 openai_compatible。",
    "llm.model": "用于生成摘要、标签和描述的语言模型名称。",
    "llm.api_key_env": "从环境变量读取云端模型 API Key 的变量名。",
    "llm.base_url": "语言模型兼容 API 的服务地址。",
    "llm.temperature": "文本生成随机性；数值越高越有变化，越低越稳定。",
    "llm.max_tokens": "单次文本生成允许输出的最大 token 数。",
    "llm.timeout_s": "等待语言模型响应的最长时间，单位为秒。",
    "vlm.model": "用于分析视频帧和评分的视觉语言模型名称。",
    "vlm.base_url": "视觉语言模型服务的访问地址。",
    "adaptive.sample_interval": "粗采样相邻帧的时间间隔，单位为秒；越小越密集。",
    "adaptive.merge_gap": "相邻高分帧允许合并的最大时间间隔，单位为秒。",
    "adaptive.merge_score_threshold": "只有两帧评分都达到此值时才允许合并。",
    "adaptive.worthiness_threshold": "帧被认为值得导出为 GIF 的最低评分。",
    "adaptive.refine_threshold": "达到此评分的帧会触发周边时间段的细采样。",
    "adaptive.max_duration": "单个导出 GIF 的最长时长，单位为秒。",
    "adaptive.vlm_temperature": "视觉模型评分时的随机性；较低值通常更稳定。",
    "adaptive.output_ratio": "从去重后的候选片段中导出的比例，范围通常为 0 到 1。",
    "adaptive.max_output": "每个视频最多导出的 GIF 数量；填写 0 表示不设上限。",
    "adaptive.gif_fps": "导出 GIF 的播放帧率，单位为每秒帧数。",
    "preference_memory.enabled": "是否启用基于用户反馈构建偏好画像并参与后续排序。",
    "preference_memory.base_score_weight": "导出排序中原始 VLM gif_worthiness 评分的权重；与偏好权重按比例归一化。",
    "preference_memory.preference_score_weight": "导出排序中已发布偏好画像评分的权重；与原始评分权重按比例归一化。",
}

CONFIG_FIELD_LABELS = {
    "adaptive.sample_interval": "sample_interval (s)",
    "adaptive.merge_gap": "merge_gap (s)",
    "adaptive.max_duration": "max_duration (s)",
    "adaptive.max_output": "max_output (0=no cap)",
    "adaptive.gif_fps": "gif_fps (frames/s)",
}

CONFIG_TOOLTIP_CSS = """
.config-field-label {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    min-height: 1.35rem;
    margin: 0 0 0.2rem 0;
    color: var(--body-text-color);
    font-size: var(--text-sm);
    font-weight: 500;
}
.config-tooltip-icon {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1rem;
    height: 1rem;
    border: 1px solid var(--border-color-primary);
    border-radius: 50%;
    color: var(--body-text-color-subdued);
    cursor: help;
    font-size: 0.72rem;
    font-weight: 700;
    line-height: 1;
}
.preference-tooltip-icon {
    margin-left: 0.35rem;
}
"""

REVIEW_LAYOUT_CSS = """
#candidate-gallery .grid-wrap {
    display: flex;
    justify-content: center;
}
#candidate-gallery img {
    object-fit: contain;
    object-position: center;
    margin: auto;
}
#selected-gif-preview {
    display: flex !important;
    align-items: center;
    justify-content: center;
    width: 100%;
    min-height: 340px;
}
#selected-gif-preview .image-container {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 100%;
    min-height: 300px;
}
#selected-gif-preview img {
    display: block;
    max-width: 100%;
    max-height: 300px;
    margin: auto;
    object-fit: contain;
    object-position: center;
}
"""


def config_field_name(key: str) -> str:
    return CONFIG_FIELD_LABELS.get(key, key.rsplit(".", 1)[-1])


def config_tooltip_icon(key: str) -> str:
    """Render the shared accessible hover tooltip icon."""
    help_text = html.escape(CONFIG_FIELD_HELP[key], quote=True)
    return f'<span class="config-tooltip-icon" tabindex="0" title="{help_text}" aria-label="{help_text}">?</span>'


def config_field_label(key: str) -> str:
    """Render a non-persistent label with an accessible hover tooltip icon."""
    name = html.escape(config_field_name(key))
    return f'<div class="config-field-label"><span>{name}</span>{config_tooltip_icon(key)}</div>'


def config_field_kwargs(key: str) -> dict[str, str | bool]:
    """Hide Gradio's persistent help text in favor of the HTML tooltip icon."""
    return {"label": config_field_name(key), "show_label": False}


def config_checkbox_kwargs(key: str) -> dict[str, str | bool]:
    """Keep a Checkbox's native, clickable label visible beside the tooltip."""
    return {
        "label": config_field_name(key),
        "container": False,
        "elem_id": "preference-memory-enabled",
    }


def config_textbox(key: str, **kwargs):
    gr.HTML(config_field_label(key), sanitize_html=False)
    return gr.Textbox(**config_field_kwargs(key), **kwargs)


def config_checkbox(key: str, **kwargs):
    return gr.Checkbox(**config_checkbox_kwargs(key), **kwargs)


CONFIG_TOOLTIP_JS = f"""
(() => {{
    const attach = () => {{
        const label = document.querySelector('#preference-memory-enabled label');
        if (!label || label.querySelector('.preference-tooltip-icon')) return;
        const icon = document.createElement('span');
        icon.className = 'config-tooltip-icon preference-tooltip-icon';
        icon.tabIndex = 0;
        icon.textContent = '?';
        icon.title = {json.dumps(CONFIG_FIELD_HELP['preference_memory.enabled'], ensure_ascii=False)};
        icon.setAttribute('aria-label', icon.title);
        label.append(icon);
    }};
    requestAnimationFrame(attach);
    setTimeout(attach, 250);
    setTimeout(attach, 1000);
}})();
"""


REVIEW_SHORTCUTS_JS = """
(() => {
    const buttonByKey = {
        '1': 'like-btn',
        '2': 'neutral-btn',
        '3': 'dislike-btn',
        '4': 'favorite-btn',
    };
    document.addEventListener('keydown', (event) => {
        const active = document.activeElement;
        if (['INPUT', 'TEXTAREA', 'SELECT'].includes(active?.tagName) || active?.isContentEditable) return;
        if (event.ctrlKey && event.key.toLowerCase() === 'z') {
            const undoButton = document.querySelector('#undo-btn button') || document.querySelector('#undo-btn');
            if (undoButton) {
                event.preventDefault();
                undoButton.click();
            }
            return;
        }
        const elemId = buttonByKey[event.key];
        if (!elemId) return;
        const button = document.querySelector(`#${elemId} button`) || document.querySelector(`#${elemId}`);
        if (!button) return;
        event.preventDefault();
        button.click();
    });
})();
"""


def launch_kwargs() -> dict:
    return {
        "server_name": "127.0.0.1",
        "server_port": 7861,
        "allowed_paths": GRADIO_ALLOWED_PATHS,
        "theme": gr.themes.Soft(),
        "css": CONFIG_TOOLTIP_CSS + REVIEW_LAYOUT_CSS,
        "js": CONFIG_TOOLTIP_JS + REVIEW_SHORTCUTS_JS,
    }


def load_config():
    """Load configs/models.yaml, return (llm_fields, vlm_fields, adaptive_fields, preference_field, raw_text)."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        return ([str(e)] * 7, [str(e)] * 2, [str(e)] * 10, [False, "0.50", "0.50"], "")

    llm = cfg.get("llm", {}) or {}
    vlm = cfg.get("vlm", {}) or {}
    adaptive = cfg.get("adaptive", {}) or {}
    pm = cfg.get("preference_memory", {}) or {}

    llm_fields = [
        llm.get("provider", ""),
        llm.get("model", ""),
        llm.get("api_key_env", ""),
        llm.get("base_url", ""),
        str(llm.get("temperature", 0.3)),
        str(llm.get("max_tokens", 2048)),
        str(llm.get("timeout_s", 120)),
    ]
    vlm_fields = [
        vlm.get("model", ""),
        vlm.get("base_url", ""),
    ]
    adaptive_fields = [
        str(adaptive.get("sample_interval", 10)),
        str(adaptive.get("merge_gap", 12)),
        str(adaptive.get("merge_score_threshold", 0.55)),
        str(adaptive.get("worthiness_threshold", 0.2)),
        str(adaptive.get("refine_threshold", 0.5)),
        str(adaptive.get("max_duration", 10)),
        str(adaptive.get("vlm_temperature", 0.65)),
        str(adaptive.get("output_ratio", 1.0)),
        str(adaptive.get("max_output", 0)),
        str(adaptive.get("gif_fps", 24)),
    ]
    pm_fields = [
        bool(pm.get("enabled", False)),
        str(pm.get("base_score_weight", 0.50)),
        str(pm.get("preference_score_weight", 0.50)),
    ]
    raw_text = yaml.dump(cfg, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return llm_fields, vlm_fields, adaptive_fields, pm_fields, raw_text


def save_config(llm_provider, llm_model, llm_api_key_env, llm_base_url,
                llm_temperature, llm_max_tokens, llm_timeout,
                vlm_model, vlm_base_url,
                ad_sample_interval, ad_merge_gap, ad_merge_score_threshold,
                ad_worthiness_threshold, ad_refine_threshold,
                ad_max_duration,
                ad_vlm_temperature, ad_output_ratio, ad_max_output, ad_gif_fps,
                pm_enabled, pm_base_score_weight, pm_preference_score_weight, raw_text):
    """Save edited fields back to configs/models.yaml, preserving other sections."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}

    cfg.setdefault("llm", {})
    cfg["llm"]["provider"] = llm_provider
    cfg["llm"]["model"] = llm_model
    cfg["llm"]["api_key_env"] = llm_api_key_env
    cfg["llm"]["base_url"] = llm_base_url
    cfg["llm"]["temperature"] = float(llm_temperature)
    cfg["llm"]["max_tokens"] = int(llm_max_tokens)
    cfg["llm"]["timeout_s"] = int(llm_timeout)

    cfg.setdefault("vlm", {})
    cfg["vlm"]["model"] = vlm_model
    cfg["vlm"]["base_url"] = vlm_base_url

    cfg.setdefault("adaptive", {})
    cfg["adaptive"]["sample_interval"] = int(ad_sample_interval)
    cfg["adaptive"]["merge_gap"] = int(ad_merge_gap)
    cfg["adaptive"]["merge_score_threshold"] = float(ad_merge_score_threshold)
    cfg["adaptive"]["worthiness_threshold"] = float(ad_worthiness_threshold)
    cfg["adaptive"]["refine_threshold"] = float(ad_refine_threshold)
    cfg["adaptive"]["max_duration"] = float(ad_max_duration)
    cfg["adaptive"]["vlm_temperature"] = float(ad_vlm_temperature)
    cfg["adaptive"]["output_ratio"] = float(ad_output_ratio)
    cfg["adaptive"]["max_output"] = int(ad_max_output)
    cfg["adaptive"]["gif_fps"] = int(ad_gif_fps)

    cfg.setdefault("preference_memory", {})
    cfg["preference_memory"]["enabled"] = bool(pm_enabled)
    cfg["preference_memory"]["base_score_weight"] = float(pm_base_score_weight)
    cfg["preference_memory"]["preference_score_weight"] = float(pm_preference_score_weight)

    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    new_raw = yaml.dump(cfg, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return "Saved to " + CONFIG_FILE, new_raw


def test_llm_connection():
    """Quick ping to the configured LLM to verify connectivity."""
    try:
        resp = httpx.post(f"{API_BASE}/api/status", timeout=5)
        if resp.status_code != 200:
            return f"API server not running (status {resp.status_code})"
    except Exception:
        return "API server not running at " + API_BASE

    try:
        from app.services.llm_client import generate_llm_text, get_llm_settings
        s = get_llm_settings()
        out = generate_llm_text("Reply OK", max_tokens=16, timeout=30)
        return f"OK - provider={s.provider}, model={s.model}, response={out[:50]!r}"
    except Exception as e:
        return f"FAIL - {type(e).__name__}: {e}"


# UI
with gr.Blocks(title="GifAgent") as app:
    gr.Markdown("# GifAgent - Preference Memory")

    with gr.Tab("Review"):
        with gr.Row():
            with gr.Column(scale=1):
                with gr.Row():
                    review_root_input = gr.Textbox(
                        label="Data Folder",
                        value=DEFAULT_SAMPLE_ROOT,
                        placeholder="Folder containing exported candidate GIF folders...",
                    )
                    load_folders_btn = gr.Button("Load Folders", variant="primary")
                folder_dropdown = gr.Dropdown(
                    choices=[],
                    value=None,
                    label="Folder to Review",
                    interactive=True,
                )
                gallery = gr.Gallery(
                    label="Candidate GIFs - liked | disliked | unrated - click to select",
                    columns=2, height=600, object_fit="contain", allow_preview=True,
                    elem_id="candidate-gallery")
                with gr.Row():
                    filter_dropdown = gr.Dropdown(
                        choices=["candidate", "favorited", "all", "liked", "disliked", "neutral", "rejected"],
                        value="candidate", label="Filter by status")
                    page_slider = gr.Slider(minimum=0, maximum=1, value=0, step=1, label="Page")

            with gr.Column(scale=3):
                gr.Markdown("## Rate")
                selected_label = gr.Textbox(label="Selected", interactive=False)
                candidate_id_input = gr.Textbox(label="Candidate ID", placeholder="Click GIF to select...")
                selected_preview = gr.Image(
                    label="Selected GIF",
                    interactive=False,
                    type="filepath",
                    height=300,
                    elem_id="selected-gif-preview",
                )
                with gr.Row():
                    like_btn = gr.Button("Like", variant="primary", elem_id="like-btn")
                    neutral_btn = gr.Button("Neutral", elem_id="neutral-btn")
                    dislike_btn = gr.Button("Dislike", variant="stop", elem_id="dislike-btn")
                    skip_btn = gr.Button("Favorite", elem_id="favorite-btn")
                note_input = gr.Textbox(label="Note (optional)")
                feedback_output = gr.Textbox(label="Result")
                undo_btn = gr.Button("Undo Last (Ctrl+Z)", elem_id="undo-btn")

        info_text = gr.Markdown("")
        page_items_state = gr.State([])
        folder_choices_state = gr.State([])
        selected_artifact_path_state = gr.State("")
        status_timer = gr.Timer(10)

        # Review events
        def clear_review_message():
            return [], gr.update(), gr.update(value=0, maximum=1), [], "", "", None, ""

        load_folders_btn.click(
            fn=load_folder_choices,
            inputs=[review_root_input],
            outputs=[folder_dropdown, info_text, folder_choices_state],
        ).then(
            fn=clear_review_message,
            outputs=[
                gallery, info_text, page_slider, page_items_state,
                candidate_id_input, selected_label, selected_preview,
                selected_artifact_path_state,
            ],
        )

        def refresh_page(page, filtr, folder):
            gal, info, p, page_items = load_candidate_page(int(page), filter_status=filtr, folder=folder)
            return gal, info, p, page_items, *select_first_candidate(page_items)

        page_slider.change(fn=refresh_page, inputs=[page_slider, filter_dropdown, folder_dropdown],
                           outputs=[
                               gallery, info_text, page_slider, page_items_state,
                               candidate_id_input, selected_label, selected_preview,
                               selected_artifact_path_state,
                           ])
        filter_dropdown.change(fn=lambda f, folder: refresh_page(0, f, folder),
                               inputs=[filter_dropdown, folder_dropdown],
                               outputs=[
                                   gallery, info_text, page_slider, page_items_state,
                                   candidate_id_input, selected_label, selected_preview,
                                   selected_artifact_path_state,
                               ])
        folder_dropdown.change(fn=lambda folder, f: load_folder_page(folder, f),
                               inputs=[folder_dropdown, filter_dropdown],
                               outputs=[
                                   gallery, info_text, page_slider, page_items_state,
                                   candidate_id_input, selected_label, selected_preview,
                                   selected_artifact_path_state,
                               ])
        gallery.select(fn=select_candidate, inputs=[page_items_state],
                       outputs=[
                           candidate_id_input, selected_label, selected_preview,
                           selected_artifact_path_state,
                       ])

        undo_btn.click(
            fn=undo_and_refresh,
            inputs=[page_slider, filter_dropdown, folder_dropdown],
            outputs=[
                feedback_output, gallery, info_text, page_slider,
                page_items_state, candidate_id_input, selected_label,
                selected_preview, selected_artifact_path_state,
            ],
        )

        like_btn.click(fn=lambda c, n, ep, p, f, folder, root, folders: rate_and_advance(c, "like", n, ep, p, f, folder, root, folders),
                       inputs=[
                           candidate_id_input, note_input, selected_artifact_path_state,
                           page_slider, filter_dropdown, folder_dropdown, review_root_input, folder_choices_state,
                       ],
                       outputs=[
                           feedback_output, gallery, info_text, page_slider,
                           page_items_state, candidate_id_input, selected_label,
                           selected_preview, selected_artifact_path_state, folder_dropdown, folder_choices_state,
                       ])
        neutral_btn.click(fn=lambda c, n, ep, p, f, folder, root, folders: rate_and_advance(c, "neutral", n, ep, p, f, folder, root, folders),
                          inputs=[
                              candidate_id_input, note_input, selected_artifact_path_state,
                              page_slider, filter_dropdown, folder_dropdown, review_root_input, folder_choices_state,
                          ],
                          outputs=[
                              feedback_output, gallery, info_text, page_slider,
                              page_items_state, candidate_id_input, selected_label,
                              selected_preview, selected_artifact_path_state, folder_dropdown, folder_choices_state,
                          ])
        dislike_btn.click(fn=lambda c, n, ep, p, f, folder, root, folders: rate_and_advance(c, "dislike", n, ep, p, f, folder, root, folders),
                          inputs=[
                              candidate_id_input, note_input, selected_artifact_path_state,
                              page_slider, filter_dropdown, folder_dropdown, review_root_input, folder_choices_state,
                          ],
                          outputs=[
                              feedback_output, gallery, info_text, page_slider,
                              page_items_state, candidate_id_input, selected_label,
                              selected_preview, selected_artifact_path_state, folder_dropdown, folder_choices_state,
                          ])
        skip_btn.click(fn=lambda c, n, ep, p, f, folder, root, folders: rate_and_advance(c, "favorite", n, ep, p, f, folder, root, folders),
                       inputs=[
                           candidate_id_input, note_input, selected_artifact_path_state,
                           page_slider, filter_dropdown, folder_dropdown, review_root_input, folder_choices_state,
                       ],
                       outputs=[
                           feedback_output, gallery, info_text, page_slider,
                           page_items_state, candidate_id_input, selected_label,
                           selected_preview, selected_artifact_path_state, folder_dropdown, folder_choices_state,
                       ])
        app.load(
            fn=lambda: ([], "Choose a data folder to review.", gr.update(value=0, maximum=1), []),
            outputs=[gallery, info_text, page_slider, page_items_state],
        )

    with gr.Tab("Profile"):
        gr.Markdown("## Preference Profile")
        profile_status = gr.Textbox(label="Status", value="Loading...", interactive=False)
        with gr.Row():
            build_btn = gr.Button("Build Profile", variant="primary")
            backfill_vectors_btn = gr.Button("Backfill Missing Vectors")
            refresh_profiles_btn = gr.Button("Refresh Profiles")
        publish_profile_dropdown = gr.Dropdown(
            choices=[],
            value=None,
            label="Profile Version to Publish",
            interactive=True,
        )
        publish_btn = gr.Button("Publish Selected Profile")
        build_output = gr.Textbox(label="Build Result")
        backfill_vectors_output = gr.Textbox(label="Vector Backfill", interactive=False)
        publish_output = gr.Textbox(label="Publish Result")

        build_btn.click(
            fn=build_profile_and_refresh,
            outputs=[build_output, publish_profile_dropdown, profile_status],
        )
        backfill_vectors_btn.click(
            fn=backfill_profile_vectors,
            outputs=[backfill_vectors_output],
        ).then(
            fn=load_profile_publish_choices,
            outputs=[publish_profile_dropdown, profile_status],
        )
        refresh_profiles_btn.click(
            fn=load_profile_publish_choices,
            outputs=[publish_profile_dropdown, profile_status],
        )
        publish_btn.click(
            fn=publish_profile_and_refresh,
            inputs=[publish_profile_dropdown],
            outputs=[publish_output, publish_profile_dropdown, profile_status],
        )
        app.load(
            fn=load_profile_publish_choices,
            outputs=[publish_profile_dropdown, profile_status],
        )
        profile_status_timer = gr.Timer(10)
        profile_status_timer.tick(fn=get_profile_status, outputs=[profile_status])
    # Control Panel Tab
    with gr.Tab("Control"):
        gr.Markdown("## Batch Processing Control")

        with gr.Row():
            with gr.Column(scale=2):
                with gr.Group():
                    gr.Markdown("### Folder Queue")
                    dir_input = gr.Textbox(
                        label="Video Directory",
                        value="C:/Users/sunhao/Desktop/ToWatch/CumForKate",
                        placeholder="Path to video directory...")
                    limit_input = gr.Number(label="Limit (0=all)", value=0, precision=0)
                    ext_input = gr.Textbox(
                        label="Extensions (comma-separated)",
                        value=".mp4,.mkv,.avi,.mov,.webm,.ts",
                        placeholder=".mp4,.mkv,.avi,.mov,.webm,.ts")
                    with gr.Row():
                        append_folder_btn = gr.Button("Append Folder", variant="primary")
                        start_queue_btn = gr.Button("Start Queue")
                        stop_btn = gr.Button("Stop", variant="stop")
                    control_output = gr.Textbox(label="Result", interactive=False)

            with gr.Column(scale=1):
                with gr.Group():
                    gr.Markdown("### Status")
                    status_text = gr.Textbox(label="Batch Status", interactive=False, lines=9,
                                             elem_id="batch-status", value="Loading...")
                    queue_text = gr.Textbox(label="Folder Queue", interactive=False, lines=7)
                    refresh_btn = gr.Button("Refresh")

        with gr.Group():
            gr.Markdown("### Detailed Output")
            batch_log_text = gr.Textbox(
                label="Detailed Output Log", interactive=False, lines=18,
                elem_id="batch-log",
            )

        status_timer2 = gr.Timer(10)
        status_timer2.tick(
            fn=refresh_batch_status,
            outputs=[status_text, queue_text, batch_log_text],
        )

        append_folder_btn.click(
            fn=append_batch_directory,
            inputs=[dir_input, limit_input, ext_input],
            outputs=[control_output, queue_text],
        ).then(fn=refresh_batch_status, outputs=[status_text, queue_text, batch_log_text])
        start_queue_btn.click(fn=start_batch_queue, outputs=[control_output])\
                .then(fn=refresh_batch_status, outputs=[status_text, queue_text, batch_log_text])
        stop_btn.click(fn=stop_batch, outputs=[control_output])\
                .then(fn=refresh_batch_status, outputs=[status_text, queue_text, batch_log_text])
        refresh_btn.click(fn=refresh_batch_status, outputs=[status_text, queue_text, batch_log_text])

        app.load(fn=refresh_batch_status, outputs=[status_text, queue_text, batch_log_text])
    # Config Tab
    with gr.Tab("Config"):
        gr.Markdown("## Configuration Editor\nEdit values and click **Save**. Changes write to `configs/models.yaml`.")

        with gr.Row():
            with gr.Column():
                with gr.Group():
                    gr.Markdown("### LLM (text synthesis)")
                    llm_provider = config_textbox("llm.provider", value="")
                    llm_model = config_textbox("llm.model", value="")
                    llm_api_key_env = config_textbox("llm.api_key_env", value="")
                    llm_base_url = config_textbox("llm.base_url", value="")
                    with gr.Row():
                        with gr.Column(min_width=160):
                            llm_temperature = config_textbox("llm.temperature", value="")
                        with gr.Column(min_width=160):
                            llm_max_tokens = config_textbox("llm.max_tokens", value="")
                        with gr.Column(min_width=160):
                            llm_timeout = config_textbox("llm.timeout_s", value="")
                    test_llm_btn = gr.Button("Test LLM Connection")
                    test_llm_output = gr.Textbox(label="LLM Test", interactive=False)

            with gr.Column():
                with gr.Group():
                    gr.Markdown("### VLM (vision analysis)")
                    vlm_model = config_textbox("vlm.model", value="")
                    vlm_base_url = config_textbox("vlm.base_url", value="")

                with gr.Group():
                    gr.Markdown("### Adaptive Sampling")
                    ad_sample_interval = config_textbox("adaptive.sample_interval", value="")
                    ad_merge_gap = config_textbox("adaptive.merge_gap", value="")
                    ad_merge_score_threshold = config_textbox("adaptive.merge_score_threshold", value="")
                    ad_worthiness_threshold = config_textbox("adaptive.worthiness_threshold", value="")
                    ad_refine_threshold = config_textbox("adaptive.refine_threshold", value="")
                    ad_max_duration = config_textbox("adaptive.max_duration", value="")
                    ad_vlm_temperature = config_textbox("adaptive.vlm_temperature", value="")
                    with gr.Row():
                        with gr.Column(min_width=160):
                            ad_output_ratio = config_textbox("adaptive.output_ratio", value="")
                        with gr.Column(min_width=160):
                            ad_max_output = config_textbox("adaptive.max_output", value="")
                    ad_gif_fps = config_textbox("adaptive.gif_fps", value="")

                with gr.Group():
                    gr.Markdown("### Preference Memory")
                    pm_enabled = config_checkbox("preference_memory.enabled", value=False)
                    with gr.Row():
                        with gr.Column(min_width=180):
                            pm_base_score_weight = config_textbox("preference_memory.base_score_weight", value="0.50")
                        with gr.Column(min_width=180):
                            pm_preference_score_weight = config_textbox("preference_memory.preference_score_weight", value="0.50")

        with gr.Row():
            save_btn = gr.Button("Save Config", variant="primary")
            reload_btn = gr.Button("Reload from File")
        config_status = gr.Textbox(label="Status", interactive=False)
        raw_yaml = gr.Textbox(label="Raw YAML (read-only preview)", lines=15, interactive=False)

        def _reload():
            llm_f, vlm_f, ad_f, pm_f, raw = load_config()
            return [*llm_f, *vlm_f, *ad_f, *pm_f, raw, "Loaded from " + CONFIG_FILE]

        all_inputs = [llm_provider, llm_model, llm_api_key_env, llm_base_url,
                      llm_temperature, llm_max_tokens, llm_timeout,
                      vlm_model, vlm_base_url,
                      ad_sample_interval, ad_merge_gap, ad_merge_score_threshold,
                      ad_worthiness_threshold, ad_refine_threshold,
                      ad_max_duration, ad_vlm_temperature, ad_output_ratio, ad_max_output, ad_gif_fps,
                      pm_enabled, pm_base_score_weight, pm_preference_score_weight, raw_yaml]
        save_btn.click(fn=save_config, inputs=all_inputs, outputs=[config_status, raw_yaml])
        reload_btn.click(fn=_reload, outputs=all_inputs + [config_status])
        test_llm_btn.click(fn=test_llm_connection, outputs=[test_llm_output])
        app.load(fn=_reload, outputs=all_inputs + [config_status])

if __name__ == "__main__":
    app.launch(**launch_kwargs())

"""
Gradio UI — candidate GIF review + batch process control panel.
"""
import json, os, subprocess, signal, sys, time
from pathlib import Path

import gradio as gr
import httpx
import yaml
from PIL import Image

API_BASE = "http://127.0.0.1:8000"
PID_FILE = "data/batch_pid.txt"
CHECKPOINT_FILE = "data/batch_checkpoint.json"
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


# ═══════════════════════════════════════════════════════════════════════
# Process manager
# ═══════════════════════════════════════════════════════════════════════

def get_batch_status():
    """Check current batch processing status."""
    status = {
        "running": False,
        "pid": None,
        "completed": 0,
        "total": 0,
        "current_video": "",
        "gpu_model": "",
    }

    # Check PID file
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # signal 0 just checks existence
            status["running"] = True
            status["pid"] = pid
        except (ValueError, OSError, ProcessLookupError):
            status["running"] = False

    # Check checkpoint
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, encoding="utf-8") as f:
                cp = json.load(f)
            status["completed"] = len(cp.get("completed", {}))
            status["total"] = status["completed"]  # estimate
        except Exception:
            pass

    # Check Ollama GPU
    try:
        r = httpx.get("http://localhost:11434/api/ps", timeout=5)
        models = r.json().get("models", [])
        if models:
            status["gpu_model"] = models[0].get("name", "?")
    except Exception:
        status["gpu_model"] = "ollama offline"

    return status


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
    try:
        os.kill(pid, 0)
        return f"WARNING: Process {pid} may still be running. Try manual kill."
    except OSError:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        return f"Batch stopped (PID {pid}). Checkpoint saved at {CHECKPOINT_FILE}"


def start_batch(video_dir: str, limit: int = 0):
    """Start batch processing in background."""
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

    try:
        proc = subprocess.Popen(cmd, cwd=".", creationflags=subprocess.CREATE_NO_WINDOW)
        os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
        with open(PID_FILE, "w") as f:
            f.write(str(proc.pid))
        return f"Batch started (PID {proc.pid}) — dir: {video_dir}" + \
               (f" limit: {limit}" if limit > 0 else "")
    except Exception as e:
        return f"Failed to start: {e}"


# ═══════════════════════════════════════════════════════════════════════
# Candidate review functions
# ═══════════════════════════════════════════════════════════════════════

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
        choices = [(_folder_label(folder), folder["folder"]) for folder in folders]
        if not choices:
            return (
                gr.update(choices=[], value=None),
                f"No candidate GIFs found under {data.get('root', root_dir)}.",
                [],
            )
        return (
            gr.update(choices=choices, value=None),
            f"Found {len(choices)} folder(s). Choose a folder to review.",
            folders,
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


def select_candidate(evt: gr.SelectData, page_items: list[dict]):
    idx = evt.index
    if 0 <= idx < len(page_items):
        item = page_items[idx]
        cid = item.get("candidate_id", "")
        src = item.get("source_run_candidate_id", "?")
        artifact_path = item.get("artifact_path") or ""
        preview = artifact_path or item.get("display_path") or item.get("preview_path")
        return cid, f"Selected: {src[:40]}", preview, artifact_path
    return "", "Selection error", None, ""


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


def build_profile():
    try:
        resp = httpx.post(f"{API_BASE}/api/preference/profiles/build", timeout=30)
        return json.dumps(resp.json(), indent=2)
    except Exception as e:
        return str(e)


# ═══════════════════════════════════════════════════════════════════════
# Config editor
# ═══════════════════════════════════════════════════════════════════════

def load_config():
    """Load configs/models.yaml, return (llm_fields, vlm_fields, adaptive_fields, preference_field, raw_text)."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        return ([str(e)] * 6, [str(e)] * 2, [str(e)] * 9, False, "")

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
        str(adaptive.get("vlm_temperature", 0.65)),
        str(adaptive.get("output_ratio", 1.0)),
        str(adaptive.get("max_output", 0)),
        str(adaptive.get("gif_fps", 24)),
    ]
    pm_enabled = bool(pm.get("enabled", False))
    raw_text = yaml.dump(cfg, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return llm_fields, vlm_fields, adaptive_fields, pm_enabled, raw_text


def save_config(llm_provider, llm_model, llm_api_key_env, llm_base_url,
                llm_temperature, llm_max_tokens, llm_timeout,
                vlm_model, vlm_base_url,
                ad_sample_interval, ad_merge_gap, ad_merge_score_threshold,
                ad_worthiness_threshold, ad_refine_threshold,
                ad_vlm_temperature, ad_output_ratio, ad_max_output, ad_gif_fps,
                pm_enabled, raw_text):
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
    cfg["adaptive"]["vlm_temperature"] = float(ad_vlm_temperature)
    cfg["adaptive"]["output_ratio"] = float(ad_output_ratio)
    cfg["adaptive"]["max_output"] = int(ad_max_output)
    cfg["adaptive"]["gif_fps"] = int(ad_gif_fps)

    cfg.setdefault("preference_memory", {})
    cfg["preference_memory"]["enabled"] = bool(pm_enabled)

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
        return f"OK — provider={s.provider}, model={s.model}, response={out[:50]!r}"
    except Exception as e:
        return f"FAIL — {type(e).__name__}: {e}"


# ═══════════════════════════════════════════════════════════════════════
# UI
# ═══════════════════════════════════════════════════════════════════════

with gr.Blocks(title="GifAgent", theme=gr.themes.Soft()) as app:
    gr.Markdown("# GifAgent — Preference Memory")

    with gr.Tab("Review"):
        with gr.Row():
            with gr.Column(scale=3):
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
                    label="Candidate GIFs — ❤ liked | ✕ disliked | ⬚ unrated — click to select",
                    columns=4, height=600, object_fit="contain", allow_preview=True)
                with gr.Row():
                    filter_dropdown = gr.Dropdown(
                        choices=["candidate", "all", "liked", "disliked", "neutral", "rejected"],
                        value="candidate", label="Filter by status")
                    page_slider = gr.Slider(minimum=0, maximum=1, value=0, step=1, label="Page")

            with gr.Column(scale=1):
                gr.Markdown("## Rate")
                selected_label = gr.Textbox(label="Selected", interactive=False)
                candidate_id_input = gr.Textbox(label="Candidate ID", placeholder="Click GIF to select...")
                selected_preview = gr.Image(
                    label="Selected GIF",
                    interactive=False,
                    type="filepath",
                    height=300,
                )
                with gr.Row():
                    like_btn = gr.Button("❤ Like", variant="primary")
                    neutral_btn = gr.Button("○ Neutral")
                    dislike_btn = gr.Button("✕ Dislike", variant="stop")
                    skip_btn = gr.Button("Skip")
                note_input = gr.Textbox(label="Note (optional)")
                feedback_output = gr.Textbox(label="Result")

                gr.Markdown("---")
                gr.Markdown("## Profile")
                profile_status = gr.Textbox(label="Status", value="Loading...")
                build_btn = gr.Button("Build Profile")
                build_output = gr.Textbox(label="Build Result")

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
            return gal, info, p, page_items, "", "", None, ""

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
        folder_dropdown.change(fn=lambda folder, f: refresh_page(0, f, folder),
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

        def rate_and_refresh(cid, rating, note, expected_path, page, filtr, folder):
            """Submit feedback then refresh gallery so status updates immediately."""
            result = rate_candidate(cid, rating, note, expected_path)
            gal, info, p, page_items = load_candidate_page(int(page), filter_status=filtr, folder=folder)
            return result, gal, info, p, page_items, "", "", None, ""

        like_btn.click(fn=lambda c, n, ep, p, f, folder: rate_and_refresh(c, "like", n, ep, p, f, folder),
                       inputs=[
                           candidate_id_input, note_input, selected_artifact_path_state,
                           page_slider, filter_dropdown, folder_dropdown,
                       ],
                       outputs=[
                           feedback_output, gallery, info_text, page_slider,
                           page_items_state, candidate_id_input, selected_label,
                           selected_preview, selected_artifact_path_state,
                       ])
        neutral_btn.click(fn=lambda c, n, ep, p, f, folder: rate_and_refresh(c, "neutral", n, ep, p, f, folder),
                          inputs=[
                              candidate_id_input, note_input, selected_artifact_path_state,
                              page_slider, filter_dropdown, folder_dropdown,
                          ],
                          outputs=[
                              feedback_output, gallery, info_text, page_slider,
                              page_items_state, candidate_id_input, selected_label,
                              selected_preview, selected_artifact_path_state,
                          ])
        dislike_btn.click(fn=lambda c, n, ep, p, f, folder: rate_and_refresh(c, "dislike", n, ep, p, f, folder),
                          inputs=[
                              candidate_id_input, note_input, selected_artifact_path_state,
                              page_slider, filter_dropdown, folder_dropdown,
                          ],
                          outputs=[
                              feedback_output, gallery, info_text, page_slider,
                              page_items_state, candidate_id_input, selected_label,
                              selected_preview, selected_artifact_path_state,
                          ])
        skip_btn.click(fn=lambda c, n, ep, p, f, folder: rate_and_refresh(c, "skip", n, ep, p, f, folder),
                       inputs=[
                           candidate_id_input, note_input, selected_artifact_path_state,
                           page_slider, filter_dropdown, folder_dropdown,
                       ],
                       outputs=[
                           feedback_output, gallery, info_text, page_slider,
                           page_items_state, candidate_id_input, selected_label,
                           selected_preview, selected_artifact_path_state,
                       ])
        build_btn.click(fn=build_profile, outputs=[build_output])
        app.load(
            fn=lambda: ([], "Choose a data folder to review.", gr.update(value=0, maximum=1), []),
            outputs=[gallery, info_text, page_slider, page_items_state],
        )
        status_timer.tick(fn=get_profile_status, outputs=[profile_status])

    # ── Control Panel Tab ──────────────────────────────────────────────
    with gr.Tab("Control"):
        gr.Markdown("## Batch Processing Control")

        with gr.Row():
            with gr.Column(scale=2):
                with gr.Group():
                    gr.Markdown("### Start Batch")
                    dir_input = gr.Textbox(
                        label="Video Directory",
                        value="C:/Users/sunhao/Desktop/ToWatch/CumForKate",
                        placeholder="Path to video directory...")
                    limit_input = gr.Number(label="Limit (0=all)", value=0, precision=0)
                    with gr.Row():
                        start_btn = gr.Button("Start", variant="primary")
                        stop_btn = gr.Button("Stop", variant="stop")
                    control_output = gr.Textbox(label="Result", interactive=False)

            with gr.Column(scale=1):
                with gr.Group():
                    gr.Markdown("### Status")
                    status_text = gr.Textbox(label="Batch Status", interactive=False, lines=6,
                                             value="Loading...")
                    refresh_btn = gr.Button("Refresh")

        def refresh_status():
            s = get_batch_status()
            lines = [
                f"Running: {'YES' if s['running'] else 'NO'}",
                f"PID: {s['pid'] or 'N/A'}",
                f"Completed: {s['completed']}",
                f"GPU Model: {s['gpu_model']}",
            ]
            return "\n".join(lines)

        status_timer2 = gr.Timer(10)
        status_timer2.tick(fn=refresh_status, outputs=[status_text])

        start_btn.click(fn=start_batch, inputs=[dir_input, limit_input], outputs=[control_output])\
                .then(fn=refresh_status, outputs=[status_text])
        stop_btn.click(fn=stop_batch, outputs=[control_output])\
                .then(fn=refresh_status, outputs=[status_text])
        refresh_btn.click(fn=refresh_status, outputs=[status_text])

        app.load(fn=refresh_status, outputs=[status_text])

    # ── Config Tab ───────────────────────────────────────────────────────
    with gr.Tab("Config"):
        gr.Markdown("## Configuration Editor\nEdit values and click **Save**. Changes write to `configs/models.yaml`.")

        with gr.Row():
            with gr.Column():
                with gr.Group():
                    gr.Markdown("### LLM (text synthesis)")
                    llm_provider = gr.Textbox(label="provider", value="")
                    llm_model = gr.Textbox(label="model", value="")
                    llm_api_key_env = gr.Textbox(label="api_key_env", value="")
                    llm_base_url = gr.Textbox(label="base_url", value="")
                    with gr.Row():
                        llm_temperature = gr.Textbox(label="temperature", value="")
                        llm_max_tokens = gr.Textbox(label="max_tokens", value="")
                        llm_timeout = gr.Textbox(label="timeout_s", value="")
                    test_llm_btn = gr.Button("Test LLM Connection")
                    test_llm_output = gr.Textbox(label="LLM Test", interactive=False)

            with gr.Column():
                with gr.Group():
                    gr.Markdown("### VLM (vision analysis)")
                    vlm_model = gr.Textbox(label="model", value="")
                    vlm_base_url = gr.Textbox(label="base_url", value="")

                with gr.Group():
                    gr.Markdown("### Adaptive Sampling")
                    ad_sample_interval = gr.Textbox(label="sample_interval (s)", value="")
                    ad_merge_gap = gr.Textbox(label="merge_gap (s)", value="")
                    ad_merge_score_threshold = gr.Textbox(label="merge_score_threshold", value="")
                    ad_worthiness_threshold = gr.Textbox(label="worthiness_threshold", value="")
                    ad_refine_threshold = gr.Textbox(label="refine_threshold", value="")
                    ad_vlm_temperature = gr.Textbox(label="vlm_temperature", value="")
                    with gr.Row():
                        ad_output_ratio = gr.Textbox(label="output_ratio", value="")
                        ad_max_output = gr.Textbox(label="max_output (0=no cap)", value="")
                    ad_gif_fps = gr.Textbox(label="gif_fps (frames/s)", value="")

                with gr.Group():
                    gr.Markdown("### Preference Memory")
                    pm_enabled = gr.Checkbox(label="enabled", value=False)

        with gr.Row():
            save_btn = gr.Button("Save Config", variant="primary")
            reload_btn = gr.Button("Reload from File")
        config_status = gr.Textbox(label="Status", interactive=False)
        raw_yaml = gr.Textbox(label="Raw YAML (read-only preview)", lines=15, interactive=False)

        def _reload():
            llm_f, vlm_f, ad_f, pm_f, raw = load_config()
            return [*llm_f, *vlm_f, *ad_f, pm_f, "Loaded from " + CONFIG_FILE, raw]

        all_inputs = [llm_provider, llm_model, llm_api_key_env, llm_base_url,
                      llm_temperature, llm_max_tokens, llm_timeout,
                      vlm_model, vlm_base_url,
                      ad_sample_interval, ad_merge_gap, ad_merge_score_threshold,
                      ad_worthiness_threshold, ad_refine_threshold,
                      ad_vlm_temperature, ad_output_ratio, ad_max_output, ad_gif_fps,
                      pm_enabled, raw_yaml]
        save_btn.click(fn=save_config, inputs=all_inputs, outputs=[config_status, raw_yaml])
        reload_btn.click(fn=_reload, outputs=all_inputs + [config_status])
        test_llm_btn.click(fn=test_llm_connection, outputs=[test_llm_output])
        app.load(fn=_reload, outputs=all_inputs + [config_status])

if __name__ == "__main__":
    app.launch(
        server_name="127.0.0.1",
        server_port=7861,
        allowed_paths=GRADIO_ALLOWED_PATHS,
    )

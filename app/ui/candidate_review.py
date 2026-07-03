"""
Gradio UI — candidate GIF review + batch process control panel.
"""
import json, os, subprocess, signal, time

import gradio as gr
import httpx

API_BASE = "http://127.0.0.1:8000"
PID_FILE = "data/batch_pid.txt"
CHECKPOINT_FILE = "data/batch_checkpoint.json"


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

    cmd = [
        "uv", "run", "python", "-u", "scripts/test_video_batch.py",
        "--dir", video_dir,
    ]
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

def load_candidates():
    try:
        resp = httpx.get(f"{API_BASE}/api/candidates", timeout=10)
        if resp.status_code == 200:
            return resp.json().get("candidates", [])
        return []
    except Exception:
        return []


RATING_ICON = {"liked": "❤", "disliked": "✕", "neutral": "○", "candidate": "⬚"}


def load_candidate_page(page: int, page_size: int = 20, filter_status: str = "all"):
    candidates = load_candidates()
    if filter_status != "all":
        candidates = [c for c in candidates if c.get("status", "candidate") == filter_status]
    total = len(candidates)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages - 1)
    start = page * page_size
    end = min(start + page_size, total)
    page_items = candidates[start:end]

    status_counts = {}
    for c in candidates:
        s = c.get("status", "candidate")
        status_counts[s] = status_counts.get(s, 0) + 1
    counts_str = " | ".join(f"{RATING_ICON.get(k,k)} {v}" for k, v in sorted(status_counts.items()))

    gallery = []
    for c in page_items:
        path = c.get("artifact_path", "")
        cid = c.get("candidate_id", "")
        status = c.get("status", "candidate")
        icon = RATING_ICON.get(status, "?")
        start_s = c.get("start_sec", 0)
        end_s = c.get("end_sec", 0)
        label = f"{icon} [{status}] {start_s:.0f}s-{end_s:.0f}s | {cid[:16]}"
        if path:
            gallery.append((path, label))

    info = f"Page {page+1}/{total_pages} | {counts_str} | Total: {total}"
    return gallery, info, page


def select_candidate(evt: gr.SelectData, page: int, page_size: int = 20):
    candidates = load_candidates()
    start = page * page_size
    idx = start + evt.index
    if idx < len(candidates):
        cid = candidates[idx].get("candidate_id", "")
        src = candidates[idx].get("source_run_candidate_id", "?")
        return cid, f"Selected: {src[:40]}"
    return "", "Selection error"


def rate_candidate(candidate_id: str, rating: str, note: str = ""):
    if not candidate_id or not candidate_id.strip():
        return "Error: No candidate selected"
    try:
        cid = candidate_id.strip()
        resp = httpx.post(
            f"{API_BASE}/api/candidates/{cid}/feedback",
            json={"rating": rating, "note": note},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            return f"Rated: {data['status']}"
        return f"Error: {resp.status_code}"
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
# UI
# ═══════════════════════════════════════════════════════════════════════

with gr.Blocks(title="GifAgent", theme=gr.themes.Soft()) as app:
    gr.Markdown("# GifAgent — Preference Memory")

    with gr.Tab("Review"):
        with gr.Row():
            with gr.Column(scale=3):
                gallery = gr.Gallery(
                    label="Candidate GIFs — ❤ liked | ✕ disliked | ⬚ unrated — click to select",
                    columns=4, height=600, object_fit="contain", allow_preview=True)
                with gr.Row():
                    filter_dropdown = gr.Dropdown(
                        choices=["all", "candidate", "liked", "disliked", "neutral"],
                        value="all", label="Filter by status")
                    page_slider = gr.Slider(minimum=0, maximum=50, value=0, step=1, label="Page")

            with gr.Column(scale=1):
                gr.Markdown("## Rate")
                selected_label = gr.Textbox(label="Selected", interactive=False)
                candidate_id_input = gr.Textbox(label="Candidate ID", placeholder="Click GIF to select...")
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
        status_timer = gr.Timer(10)

        # Review events
        def refresh_page(page, filtr):
            return load_candidate_page(int(page), filter_status=filtr)
        page_slider.change(fn=refresh_page, inputs=[page_slider, filter_dropdown],
                           outputs=[gallery, info_text, page_slider])
        filter_dropdown.change(fn=lambda f: load_candidate_page(0, filter_status=f),
                               inputs=[filter_dropdown],
                               outputs=[gallery, info_text, page_slider])
        after_rate = lambda page, filtr: load_candidate_page(int(page), filter_status=filtr)
        gallery.select(fn=select_candidate, inputs=[page_slider],
                       outputs=[candidate_id_input, selected_label])

        def rate_and_refresh(cid, rating, note, page, filtr):
            """Submit feedback then refresh gallery so status updates immediately."""
            result = rate_candidate(cid, rating, note)
            gal, info, p = load_candidate_page(int(page), filter_status=filtr)
            return result, gal, info, p

        like_btn.click(fn=lambda c, n, p, f: rate_and_refresh(c, "like", n, p, f),
                       inputs=[candidate_id_input, note_input, page_slider, filter_dropdown],
                       outputs=[feedback_output, gallery, info_text, page_slider])
        neutral_btn.click(fn=lambda c, n, p, f: rate_and_refresh(c, "neutral", n, p, f),
                          inputs=[candidate_id_input, note_input, page_slider, filter_dropdown],
                          outputs=[feedback_output, gallery, info_text, page_slider])
        dislike_btn.click(fn=lambda c, n, p, f: rate_and_refresh(c, "dislike", n, p, f),
                          inputs=[candidate_id_input, note_input, page_slider, filter_dropdown],
                          outputs=[feedback_output, gallery, info_text, page_slider])
        skip_btn.click(fn=lambda c, n, p, f: rate_and_refresh(c, "skip", n, p, f),
                       inputs=[candidate_id_input, note_input, page_slider, filter_dropdown],
                       outputs=[feedback_output, gallery, info_text, page_slider])
        build_btn.click(fn=build_profile, outputs=[build_output])
        app.load(fn=lambda: load_candidate_page(0), outputs=[gallery, info_text, page_slider])
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

if __name__ == "__main__":
    app.launch(server_name="127.0.0.1", server_port=7861)

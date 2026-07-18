"""Review tab — candidate GIF review queue with gallery, ratings, and keyboard shortcuts.

``build_review_tab()`` should be called from inside a ``gr.Blocks`` context
(usually within ``with gr.Tab("审核"):``).  It creates all Gradio components
and wires their events internally, returning a dict of component references.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import gradio as gr
import httpx
from PIL import Image

from app.ui.components.common import _format_api_error

API_BASE = "http://127.0.0.1:8000"
PAGE_SIZE = 12
THUMB_DIR = "data/thumbs/candidates"
STATIC_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
DEFAULT_SAMPLE_ROOT = os.path.abspath(os.path.join("data", "exports", "adaptive_test"))

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


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
            return (
                gr.update(choices=[], value=None),
                f"Folder error: {_format_api_error(resp)}",
                [],
            )

        data = resp.json()
        folders = data.get("folders", [])
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
    counts_str = (
        " | ".join(
            f"{RATING_ICON.get(k, k)} {v}" for k, v in sorted(status_counts.items())
        )
        or "no candidates"
    )

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
    info = (
        f"Folder: {folder_name} | Page {page + 1}/{total_pages} | "
        f"{counts_str} | Showing: {total}"
    )
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


def refresh_page(page, filtr, folder):
    gal, info, p, page_items = load_candidate_page(
        int(page), filter_status=filtr, folder=folder
    )
    return gal, info, p, page_items, *select_first_candidate(page_items)


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
    *,
    _submit_action=None,
    _load_page=None,
    _load_folders=None,
):
    """Rate a GIF, select the next item, and advance folders when necessary."""
    submit_action = _submit_action or submit_review_action
    load_page = _load_page or load_candidate_page
    load_folders = _load_folders or load_folder_choices

    result = submit_action(candidate_id, rating, note, expected_artifact_path)
    if not result.startswith("Rated:"):
        return (
            result, gr.update(), gr.update(), gr.update(), gr.update(),
            candidate_id, "Rating failed; selection kept", expected_artifact_path or None,
            expected_artifact_path, gr.update(), previous_folders,
        )

    gallery, info, page_update, page_items = load_page(
        int(page), filter_status=filter_status, folder=folder
    )
    if page_items:
        cid, label, preview, artifact_path = select_first_candidate(page_items)
        return (
            result, gallery, info, page_update, page_items,
            cid, label, preview, artifact_path,
            gr.update(value=folder), previous_folders,
        )

    _folder_update, folder_info, refreshed_folders = load_folders(root_dir)
    next_folder = next_reviewable_folder(previous_folders, refreshed_folders, folder)
    folder_choices = [(_folder_label(item), item["folder"]) for item in refreshed_folders]
    if next_folder:
        gallery, next_info, page_update, page_items = load_page(
            0, filter_status=filter_status, folder=next_folder
        )
        cid, label, preview, artifact_path = select_first_candidate(page_items)
        return (
            result, gallery, f"Auto-advanced to next folder. {next_info}",
            page_update, page_items,
            cid, label, preview, artifact_path,
            gr.update(choices=folder_choices, value=next_folder), refreshed_folders,
        )

    return (
        result, [], folder_info, page_update, [],
        "", "All reviewable folders are complete.", None, "",
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


# ---------------------------------------------------------------------------
# CSS / JS constants (used by workbench.launch_kwargs)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Tab builder
# ---------------------------------------------------------------------------


def build_review_tab() -> dict:
    """Build the Gradio Review tab components inside the current Blocks context.

    Returns
    -------
    dict
        All Gradio components keyed by name so the caller can attach
        additional event handlers if needed.
    """
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

    # ---- Event wiring ----

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

    page_slider.change(
        fn=refresh_page,
        inputs=[page_slider, filter_dropdown, folder_dropdown],
        outputs=[
            gallery, info_text, page_slider, page_items_state,
            candidate_id_input, selected_label, selected_preview,
            selected_artifact_path_state,
        ],
    )

    filter_dropdown.change(
        fn=lambda f, folder: refresh_page(0, f, folder),
        inputs=[filter_dropdown, folder_dropdown],
        outputs=[
            gallery, info_text, page_slider, page_items_state,
            candidate_id_input, selected_label, selected_preview,
            selected_artifact_path_state,
        ],
    )

    folder_dropdown.change(
        fn=lambda folder, f: load_folder_page(folder, f),
        inputs=[folder_dropdown, filter_dropdown],
        outputs=[
            gallery, info_text, page_slider, page_items_state,
            candidate_id_input, selected_label, selected_preview,
            selected_artifact_path_state,
        ],
    )

    gallery.select(
        fn=select_candidate,
        inputs=[page_items_state],
        outputs=[
            candidate_id_input, selected_label, selected_preview,
            selected_artifact_path_state,
        ],
    )

    undo_btn.click(
        fn=undo_and_refresh,
        inputs=[page_slider, filter_dropdown, folder_dropdown],
        outputs=[
            feedback_output, gallery, info_text, page_slider,
            page_items_state, candidate_id_input, selected_label,
            selected_preview, selected_artifact_path_state,
        ],
    )

    _wire_rating_button(like_btn, "like", candidate_id_input, note_input,
                        selected_artifact_path_state, page_slider, filter_dropdown,
                        folder_dropdown, review_root_input, folder_choices_state,
                        feedback_output, gallery, info_text, page_slider,
                        page_items_state, selected_label, selected_preview,
                        selected_artifact_path_state, folder_dropdown, folder_choices_state)

    _wire_rating_button(neutral_btn, "neutral", candidate_id_input, note_input,
                        selected_artifact_path_state, page_slider, filter_dropdown,
                        folder_dropdown, review_root_input, folder_choices_state,
                        feedback_output, gallery, info_text, page_slider,
                        page_items_state, selected_label, selected_preview,
                        selected_artifact_path_state, folder_dropdown, folder_choices_state)

    _wire_rating_button(dislike_btn, "dislike", candidate_id_input, note_input,
                        selected_artifact_path_state, page_slider, filter_dropdown,
                        folder_dropdown, review_root_input, folder_choices_state,
                        feedback_output, gallery, info_text, page_slider,
                        page_items_state, selected_label, selected_preview,
                        selected_artifact_path_state, folder_dropdown, folder_choices_state)

    _wire_rating_button(skip_btn, "favorite", candidate_id_input, note_input,
                        selected_artifact_path_state, page_slider, filter_dropdown,
                        folder_dropdown, review_root_input, folder_choices_state,
                        feedback_output, gallery, info_text, page_slider,
                        page_items_state, selected_label, selected_preview,
                        selected_artifact_path_state, folder_dropdown, folder_choices_state)

    return {
        "gallery": gallery,
        "folder_dropdown": folder_dropdown,
        "filter_dropdown": filter_dropdown,
        "page_slider": page_slider,
        "candidate_id_input": candidate_id_input,
        "selected_label": selected_label,
        "selected_preview": selected_preview,
        "note_input": note_input,
        "feedback_output": feedback_output,
        "like_btn": like_btn,
        "neutral_btn": neutral_btn,
        "dislike_btn": dislike_btn,
        "skip_btn": skip_btn,
        "undo_btn": undo_btn,
        "info_text": info_text,
        "review_root_input": review_root_input,
        "load_folders_btn": load_folders_btn,
        "page_items_state": page_items_state,
        "folder_choices_state": folder_choices_state,
        "selected_artifact_path_state": selected_artifact_path_state,
        "status_timer": status_timer,
    }


def _wire_rating_button(
    btn, rating, candidate_id_input, note_input,
    artifact_state, page_slider, filter_dropdown,
    folder_dropdown, root_input, folder_choices_state,
    feedback_output, gallery, info_text, page_slider_out,
    page_items_state, selected_label, selected_preview,
    selected_artifact_path_state, folder_dropdown_out, folder_choices_out,
):
    """Wire a rating button's click event with ``rate_and_advance``."""
    btn.click(
        fn=lambda c, n, ep, p, f, folder, root, folders: rate_and_advance(
            c, rating, n, ep, p, f, folder, root, folders,
        ),
        inputs=[
            candidate_id_input, note_input, artifact_state,
            page_slider, filter_dropdown, folder_dropdown,
            root_input, folder_choices_state,
        ],
        outputs=[
            feedback_output, gallery, info_text, page_slider_out,
            page_items_state, candidate_id_input, selected_label,
            selected_preview, selected_artifact_path_state,
            folder_dropdown_out, folder_choices_out,
        ],
    )

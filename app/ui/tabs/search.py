"""Search tab -- semantic and filtered search across candidates.

``build_search_tab()`` should be called from inside a ``gr.Blocks`` context
(usually within ``with gr.Tab("搜索"):``).

When a result is selected from the gallery the source-video timeline is
fetched and displayed below with an interactive SVG timeline, GIF preview,
and PotPlayer double-click handler.
"""

from __future__ import annotations

import gradio as gr
import httpx

API_BASE = "http://127.0.0.1:8000"

_PAGE_SIZE = 24


def _timeline(video_id: str, start_sec: float, end_sec: float) -> str:
    """Call GET /api/workbench/videos/{video_id}/timeline and return HTML."""
    try:
        resp = httpx.get(
            f"{API_BASE}/api/workbench/videos/{video_id}/timeline",
            params={"start_sec": start_sec, "end_sec": end_sec, "max_thumbnails": 60},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return f"<div style='color:#888;'>Timeline unavailable: {exc}</div>"

    scenes = data.get("scenes", [])
    candidates = data.get("candidates", [])
    generated = data.get("generated_gifs", [])

    from app.ui.components.timeline import build_timeline_html

    return build_timeline_html(
        video_id=video_id,
        start_sec=data.get("start_sec", start_sec),
        end_sec=data.get("end_sec", end_sec),
        scenes=scenes,
        candidates=candidates,
        generated_gifs=generated,
    )


def _search(
    query_text: str,
    tags: str,
    folder: str,
    min_duration: float,
    max_duration: float,
    status_filter: str,
    created_after: str,
    created_before: str,
    page: int,
) -> tuple:
    """Call POST /api/workbench/search and return gallery items + metadata."""
    params = {
        "query_text": query_text,
        "tags": tags,
        "folder": folder,
        "statuses": status_filter,
        "limit": _PAGE_SIZE,
        "offset": (page - 1) * _PAGE_SIZE,
    }
    if min_duration > 0:
        params["min_duration"] = min_duration
    if max_duration > 0:
        params["max_duration"] = max_duration
    if created_after:
        params["created_after"] = created_after
    if created_before:
        params["created_before"] = created_before

    try:
        resp = httpx.post(
            f"{API_BASE}/api/workbench/search",
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return [], f"Search failed: {exc}", gr.update(value=1, visible=False), 0, []

    items = data.get("items", [])
    total = data.get("total", 0)
    degraded = data.get("degraded", False)
    diagnosis = data.get("diagnosis", "")

    # Build gallery items (path, label)
    gallery_items = []
    for item in items:
        preview = item.get("preview_path") or item.get("source_video_path") or ""
        label = _build_label(item)
        gallery_items.append((preview, label))

    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    parts = [f"{total} results"]
    if degraded and diagnosis:
        parts.append(f" (degraded: {diagnosis})")
    status_text = "".join(parts)

    page_slider_update = gr.update(
        value=min(page, total_pages),
        maximum=total_pages,
        visible=total_pages > 1,
    )

    return gallery_items, status_text, page_slider_update, total, items


def _build_label(item: dict) -> str:
    """Build a hover label for a gallery item."""
    parts = [
        item.get("candidate_id", ""),
        f"{item.get('duration', 0):.1f}s",
        item.get("status", ""),
    ]
    score = item.get("score")
    if score is not None:
        parts.append(f"score={score:.3f}")
    tags = item.get("tags", [])
    if tags:
        parts.append(" | ".join(tags[:3]))
    return " | ".join(p for p in parts if p)


def build_search_tab(context) -> None:
    """Build the Search tab with filters, text input, gallery, and timeline.

    All filter values are sent on every search invocation so pagination
    re-uses the same filter set without storing intermediate state.

    When a gallery item is selected the timeline for its source video is
    fetched and rendered below as an interactive SVG.
    """
    with gr.Row():
        with gr.Column(scale=3):
            query_text = gr.Textbox(
                label="Semantic Search",
                placeholder="Describe what you're looking for...",
            )
        with gr.Column(scale=1):
            search_btn = gr.Button("Search", variant="primary", size="lg")

    with gr.Accordion("Filters", open=False):
        with gr.Row():
            tags = gr.Textbox(
                label="Tags (comma-separated)",
                placeholder="e.g. joy, action, closeup",
            )
            folder = gr.Textbox(
                label="Folder",
                placeholder="e.g. JUR-639",
            )
            status_filter = gr.Textbox(
                label="Statuses (comma-separated)",
                placeholder="e.g. candidate, liked",
                value="candidate",
            )
        with gr.Row():
            min_duration = gr.Number(label="Min Duration (s)", value=0, minimum=0)
            max_duration = gr.Number(label="Max Duration (s)", value=0, minimum=0)
        with gr.Row():
            created_after = gr.Textbox(
                label="Created After (ISO)",
                placeholder="e.g. 2026-07-01T00:00:00+00:00",
            )
            created_before = gr.Textbox(
                label="Created Before (ISO)",
                placeholder="e.g. 2026-07-31T00:00:00+00:00",
            )

    status_text = gr.Markdown("Enter a query and press Search.")

    page_slider = gr.Slider(
        minimum=1, maximum=2, step=1, value=1,
        label="Page", visible=False,
    )

    total_state = gr.State(0)
    result_state = gr.State([])  # Full search result items

    gallery = gr.Gallery(
        label="Results",
        columns=4,
        object_fit="contain",
        height=600,
    )

    # Timeline section (hidden until a result is selected)
    timeline_html = gr.HTML(
        value="<div style='color:#888; padding:8px;'>Select a result to view its timeline.</div>",
        visible=True,
    )

    # Shared input list for both search and pagination
    _search_inputs = [
        query_text, tags, folder, min_duration, max_duration,
        status_filter, created_after, created_before,
    ]

    def _do_search(*args) -> tuple:
        """Call _search with all filter values and page=1."""
        return _search(*args, page=1)

    def _do_page(page_val: int, *args) -> tuple:
        """Call _search with the new page and existing filter values."""
        return _search(*args, page=int(page_val))

    def _on_select(evt: gr.SelectData, results: list) -> str:
        """When a gallery item is selected, fetch and render its timeline."""
        if not results:
            return "<div style='color:#888;'>No result data available.</div>"
        idx = evt.index
        if idx < 0 or idx >= len(results):
            return "<div style='color:#888;'>Invalid selection.</div>"
        item = results[idx]
        video_id = item.get("source_video_sha256", "")
        if not video_id:
            # Fall back: try to resolve via the media table; for now show a message
            return "<div style='color:#888;'>Source video not available for this candidate.</div>"
        start_sec = max(item.get("start_sec", 0) - 10, 0)
        end_sec = item.get("end_sec", 0) + 10
        return _timeline(video_id, start_sec, end_sec)

    search_btn.click(
        fn=_do_search,
        inputs=_search_inputs,
        outputs=[gallery, status_text, page_slider, total_state, result_state],
    )

    query_text.submit(
        fn=_do_search,
        inputs=_search_inputs,
        outputs=[gallery, status_text, page_slider, total_state, result_state],
    )

    page_slider.change(
        fn=_do_page,
        inputs=[page_slider] + _search_inputs,
        outputs=[gallery, status_text, page_slider, total_state, result_state],
    )

    gallery.select(
        fn=_on_select,
        inputs=[result_state],
        outputs=[timeline_html],
    )

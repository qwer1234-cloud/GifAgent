"""Collections tab — smart collections with create, refresh, freeze, export.

``build_collections_tab()`` should be called from inside a ``gr.Blocks`` context
(usually within ``with gr.Tab("合集"):``).

Provides buttons to create a smart collection from search criteria, refresh
its content with farthest-first diversity, freeze it, and export the latest
version to a JSON manifest + PBF binary file.
"""

from __future__ import annotations

from pathlib import Path

import gradio as gr
import httpx

API_BASE = "http://127.0.0.1:8000"


def _fetch_collections() -> list[dict]:
    """Call GET /api/workbench/collections."""
    try:
        resp = httpx.get(f"{API_BASE}/api/workbench/collections", timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return []


def _list_collections_ui() -> str:
    """Render the collection list as an HTML table."""
    collections = _fetch_collections()
    if not collections:
        return (
            "<div style='color:#888; padding:8px;'>"
            "No collections yet. Create one above.</div>"
        )

    rows_html = ""
    for c in collections:
        cid = c.get("collection_id", "")
        name = c.get("name", "?")
        ver = c.get("current_version", 0)
        frozen = c.get("frozen", False)
        target = c.get("target_count", 0)
        created = c.get("created_at", "")[:19].replace("T", " ")
        status_icon = "\U0001f512" if frozen else "\U0001f504"
        rows_html += (
            f"<tr>"
            f"  <td style='padding:4px;'>{name}</td>"
            f"  <td style='padding:4px;font-family:monospace;font-size:0.85em;'>{cid[:12]}...</td>"
            f"  <td style='padding:4px;'>{ver}</td>"
            f"  <td style='padding:4px;'>{status_icon}</td>"
            f"  <td style='padding:4px;'>{target}</td>"
            f"  <td style='padding:4px;'>{created}</td>"
            f"</tr>"
        )

    return (
        "<table style='width:100%; border-collapse:collapse;'>"
        "<thead><tr style='background:#f0f0f0;'>"
        "<th style='padding:6px;text-align:left;'>Name</th>"
        "<th style='padding:6px;text-align:left;'>ID</th>"
        "<th style='padding:6px;text-align:left;'>Version</th>"
        "<th style='padding:6px;text-align:left;'>Status</th>"
        "<th style='padding:6px;text-align:left;'>Target</th>"
        "<th style='padding:6px;text-align:left;'>Created</th>"
        "</tr></thead>"
        f"<tbody>{rows_html}</tbody>"
        "</table>"
    )


def _create_collection(
    name: str,
    query_text: str,
    tags: str,
    target_count: int,
    diversity_weight: float,
) -> tuple[str, str]:
    """Call POST /api/workbench/collections.

    Returns ``(status_message, updated_collection_list_html)``.
    """
    if not name.strip():
        return "Error: Name is required.", _list_collections_ui()

    payload = {
        "name": name,
        "query_text": query_text,
        "tags": tags,
        "target_count": target_count,
        "diversity_weight": diversity_weight,
    }
    try:
        resp = httpx.post(
            f"{API_BASE}/api/workbench/collections",
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        cid = data.get("collection_id", "?")
        return (
            f"Created collection '{name}' (ID: {cid[:12]}...).",
            _list_collections_ui(),
        )
    except Exception as exc:
        return f"Create failed: {exc}", _list_collections_ui()


def _do_refresh(cid: str) -> str:
    """Call POST /api/workbench/collections/{cid}/refresh."""
    if not cid.strip():
        return "Error: Collection ID is required."
    try:
        resp = httpx.post(
            f"{API_BASE}/api/workbench/collections/{cid}/refresh",
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        ver = data.get("version", "?")
        n = len(data.get("candidate_ids", []))
        return f"Refreshed collection {cid[:12]}... -> v{ver} ({n} candidates)."
    except Exception as exc:
        return f"Refresh failed: {exc}"


def _do_freeze(cid: str) -> str:
    """Call POST /api/workbench/collections/{cid}/freeze."""
    if not cid.strip():
        return "Error: Collection ID is required."
    try:
        resp = httpx.post(
            f"{API_BASE}/api/workbench/collections/{cid}/freeze",
            timeout=30,
        )
        resp.raise_for_status()
        return f"Frozen collection {cid[:12]}..."
    except Exception as exc:
        return f"Freeze failed: {exc}"


def _do_export(cid: str, output_dir: str) -> str:
    """Call POST /api/workbench/collections/{cid}/export."""
    if not cid.strip():
        return "Error: Collection ID is required."
    if not output_dir.strip():
        return "Error: Output directory is required."
    try:
        resp = httpx.post(
            f"{API_BASE}/api/workbench/collections/{cid}/export",
            json={"output_dir": output_dir},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        manifest = data.get("manifest_path", "?")
        exported = data.get("exported", 0)
        missing = data.get("missing_candidate_ids", [])
        msg = f"Exported {exported} candidates to {manifest}."
        if missing:
            msg += f" Missing: {len(missing)}."
        return msg
    except Exception as exc:
        return f"Export failed: {exc}"


def _do_taste_map(cid: str) -> dict:
    """Call POST /api/workbench/collections/{cid}/taste-map."""
    if not cid.strip():
        return {"error": "Collection ID is required."}
    try:
        resp = httpx.post(
            f"{API_BASE}/api/workbench/collections/{cid}/taste-map",
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return {"message": "No taste map data available (empty or no vectors)."}
        return {"points": data, "count": len(data)}
    except Exception as exc:
        return {"error": f"Taste map failed: {exc}"}


def _do_narrative(cid: str, beats: str) -> dict:
    """Call POST /api/workbench/collections/{cid}/narrative."""
    if not cid.strip():
        return {"error": "Collection ID is required."}
    if not beats.strip():
        beats = "opening,development,climax,ending"
    try:
        resp = httpx.post(
            f"{API_BASE}/api/workbench/collections/{cid}/narrative",
            params={"beats": beats},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return {"message": "No narrative data available."}
        return {"beats": data}
    except Exception as exc:
        return {"error": f"Narrative curation failed: {exc}"}


def build_collections_tab(context) -> None:
    """Build the Collections tab with creation, list, and action buttons."""
    gr.Markdown("## Smart Collections")
    gr.Markdown(
        "Create reproducible collections from search criteria. "
        "Refresh applies farthest-first diversity to select diverse candidates. "
        "Freeze locks the collection. Export writes JSON manifest + PBF."
    )

    # ── Create section ──────────────────────────────────────────────────────
    with gr.Accordion("Create Collection", open=True):
        with gr.Row():
            collection_name = gr.Textbox(
                label="Name",
                placeholder="e.g. Best Action Scenes",
                scale=2,
            )
            target_count = gr.Number(
                label="Target Count",
                value=24,
                minimum=1,
                maximum=500,
                precision=0,
                scale=1,
            )
            diversity_weight = gr.Slider(
                label="Diversity Weight",
                minimum=0.0,
                maximum=1.0,
                value=0.5,
                step=0.1,
                scale=1,
            )
        with gr.Row():
            query_text = gr.Textbox(
                label="Search Query (optional)",
                placeholder="Describe what you want...",
            )
            tags = gr.Textbox(
                label="Tags (comma-separated)",
                placeholder="e.g. action, funny, closeup",
            )
        create_btn = gr.Button("Create Collection", variant="primary")
        create_status = gr.Markdown("")

    # ── Collection list ─────────────────────────────────────────────────────
    gr.Markdown("---")
    gr.Markdown("### All Collections")
    collection_list_html = gr.HTML(value=_list_collections_ui())
    refresh_list_btn = gr.Button("Refresh List", size="sm")
    refresh_list_btn.click(
        fn=_list_collections_ui,
        outputs=[collection_list_html],
    )

    create_btn.click(
        fn=_create_collection,
        inputs=[collection_name, query_text, tags, target_count, diversity_weight],
        outputs=[create_status, collection_list_html],
    )

    # ── Actions section ─────────────────────────────────────────────────────
    gr.Markdown("---")
    gr.Markdown("### Manage Collections")
    with gr.Row():
        action_cid = gr.Textbox(
            label="Collection ID",
            placeholder="Paste a collection ID",
            scale=2,
        )
        refresh_btn = gr.Button("Refresh", variant="secondary", scale=1)
        freeze_btn = gr.Button("Freeze", variant="secondary", scale=1)
    with gr.Row():
        export_dir = gr.Textbox(
            label="Export Directory",
            placeholder="e.g. data/exports/",
            scale=2,
        )
        export_btn = gr.Button("Export", variant="secondary", scale=1)
    action_status = gr.Markdown("")

    refresh_btn.click(
        fn=_do_refresh,
        inputs=[action_cid],
        outputs=[action_status],
    )
    freeze_btn.click(
        fn=_do_freeze,
        inputs=[action_cid],
        outputs=[action_status],
    )
    export_btn.click(
        fn=_do_export,
        inputs=[action_cid, export_dir],
        outputs=[action_status],
    )

    # ── Taste Map & Narrative section ─────────────────────────────────────────
    gr.Markdown("---")
    gr.Markdown("### Taste Map & Narrative Curation")
    with gr.Row():
        tm_cid = gr.Textbox(
            label="Collection ID for Taste Map",
            placeholder="Paste a collection ID",
            scale=2,
        )
        taste_map_btn = gr.Button("Compute Taste Map", variant="secondary", scale=1)
    taste_map_output = gr.JSON(label="Taste Map (2D Projection)")

    with gr.Row():
        nar_cid = gr.Textbox(
            label="Collection ID for Narrative",
            placeholder="Paste a collection ID",
            scale=2,
        )
        nar_beats = gr.Textbox(
            label="Beats (comma-separated)",
            value="opening,development,climax,ending",
            scale=1,
        )
        narrative_btn = gr.Button("Curate Narrative", variant="secondary", scale=1)
    narrative_output = gr.JSON(label="Narrative Curation")

    taste_map_btn.click(
        fn=_do_taste_map,
        inputs=[tm_cid],
        outputs=[taste_map_output],
    )
    narrative_btn.click(
        fn=_do_narrative,
        inputs=[nar_cid, nar_beats],
        outputs=[narrative_output],
    )

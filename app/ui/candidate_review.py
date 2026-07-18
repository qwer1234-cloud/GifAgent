"""Gradio UI - candidate GIF review + batch process control panel.

Thin launcher: loads config, creates the workbench context, builds the Gradio
application, and launches it.  All tab logic lives in ``app/ui/tabs/`` and the
workbench shell in ``app/ui/workbench.py``.

Backward-compatible re-exports (deprecated, will be removed in a future release):
  - ``summarize_checkpoint_status``  -> ``app.ui.tabs.control``
  - ``CONFIG_FIELD_HELP`` / ``CONFIG_FIELD_KEYS`` -> ``app.ui.tabs.settings``
  - ``CONFIG_TOOLTIP_CSS`` / ``CONFIG_TOOLTIP_JS`` -> ``app.ui.tabs.settings``
  - ``REVIEW_LAYOUT_CSS`` / ``REVIEW_SHORTCUTS_JS`` -> ``app.ui.tabs.review``
"""

from __future__ import annotations

import json

import gradio as gr

# ---------------------------------------------------------------------------
# Backward-compatible re-exports (deprecated)
# ---------------------------------------------------------------------------
# These names were historically defined in this module.  They now live in
# their respective tab modules.  Import directly from the owning module;
# these re-exports will be removed in a future release.
from app.ui.tabs.control import (  # noqa: F401
    is_batch_command_line,
    summarize_checkpoint_status,
)
from app.ui.tabs.control import build_control_tab
from app.ui.tabs.settings import (  # noqa: F401
    CONFIG_FIELD_HELP,
    CONFIG_FIELD_KEYS,
    CONFIG_TOOLTIP_CSS,
    CONFIG_TOOLTIP_JS,
    config_checkbox_kwargs,
    config_field_kwargs,
    config_field_label,
    config_tooltip_icon,
)
import httpx  # noqa: F401 — re-export for legacy tests

from app.db import get_connection  # noqa: F401 — re-export for legacy tests
from app.services.candidate_vectors import backfill_candidate_vectors  # noqa: F401
from app.services.embedding import compute_text_embedding  # noqa: F401

from app.ui.tabs.review import build_review_tab
from app.ui.tabs.review import (  # noqa: F401
    PAGE_SIZE,
    REVIEW_LAYOUT_CSS,
    REVIEW_SHORTCUTS_JS,
    favorite_candidate,
    load_folder_choices,
    load_candidate_page,
    next_reviewable_folder,
    rate_candidate,
    select_first_candidate,
    undo_last_action,
)
from app.ui.tabs.profile import build_profile_tab
from app.ui.tabs.profile import (  # noqa: F401
    profile_publish_choices,
    publish_profile_version,
)
# ---------------------------------------------------------------------------

from app.ui.api_client import GifAgentApiClient, API_BASE
from app.ui.workbench import WorkbenchContext, build_workbench, launch_kwargs
from app.ui import legacy_candidate_review as _legacy_candidate_review

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

client = GifAgentApiClient(API_BASE)
LEGACY_QUEUE_ENV = "GIFAGENT_LEGACY_QUEUE_UI"


def load_folder_page(folder: str | None, filter_status: str = "candidate"):
    """Load page zero through compatibility-layer dependency hooks."""
    gallery, info, page_update, page_items = load_candidate_page(
        0, filter_status=filter_status, folder=folder
    )
    return gallery, info, page_update, page_items, *select_first_candidate(page_items)


def _legacy_submit_action(
    candidate_id: str,
    action: str,
    note: str = "",
    expected_artifact_path: str = "",
):
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
    """Rate and advance while preserving legacy monkeypatch behavior."""
    from app.ui.tabs.review import rate_and_advance as _rate_and_advance

    return _rate_and_advance(
        candidate_id,
        rating,
        note,
        expected_artifact_path,
        page,
        filter_status,
        folder,
        root_dir,
        previous_folders,
        _submit_action=_legacy_submit_action,
        _load_page=load_candidate_page,
        _load_folders=load_folder_choices,
    )


def backfill_profile_vectors():
    """Backfill vectors through the historical module dependency hooks."""
    conn = None
    try:
        conn = get_connection()
        result = backfill_candidate_vectors(
            conn,
            embed_fn=compute_text_embedding,
            only_feedback=True,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"}, indent=2)
    finally:
        if conn is not None:
            conn.close()


def build_legacy_candidate_review() -> gr.Blocks:
    """Build the historical Review, Control, and Profile tab layout."""
    legacy_app = gr.Blocks(title="GifAgent Legacy Candidate Review")
    with legacy_app:
        with gr.Tab("Review"):
            review_components = build_review_tab()
        with gr.Tab("Control"):
            control_components = build_control_tab(client)
        with gr.Tab("Profile"):
            profile_components = build_profile_tab()
    return legacy_app


context = WorkbenchContext(
    client=client,
    allowed_paths=(),  # override with launch_kwargs allowed_paths
)
app = build_workbench(context)


def __getattr__(name: str):
    """Resolve historical helpers from the dedicated compatibility module."""
    try:
        return getattr(_legacy_candidate_review, name)
    except AttributeError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

if __name__ == "__main__":
    app.launch(**launch_kwargs())

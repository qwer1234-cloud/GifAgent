"""Workbench shell — creates the top-level gr.Blocks and orchestrates all 7 tabs.

Usage::

    from app.ui.api_client import GifAgentApiClient
    from app.ui.workbench import WorkbenchContext, build_workbench, launch_kwargs

    client = GifAgentApiClient()
    context = WorkbenchContext(client=client, allowed_paths=("/tmp",))
    app = build_workbench(context)
    app.launch(**launch_kwargs())
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import gradio as gr

from app.ui.api_client import GifAgentApiClient


# ---------------------------------------------------------------------------
# Workbench context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkbenchContext:
    """Shared context for the Gradio workbench.

    Parameters
    ----------
    client : GifAgentApiClient
        HTTP client for the task-engine API.
    allowed_paths : tuple[str, ...]
        Paths that Gradio is allowed to serve.
    refresh_seconds : float
        Default refresh interval for timers.
    """

    client: GifAgentApiClient
    allowed_paths: tuple[str, ...]
    refresh_seconds: float = 2.0


# ---------------------------------------------------------------------------
# Gradio allowed paths
# ---------------------------------------------------------------------------

_GRADIO_ALLOWED_PATHS: tuple[str, ...] | None = None


def _build_gradio_allowed_paths() -> tuple[str, ...]:
    """Collect directories Gradio is allowed to serve."""
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
    return tuple(allowed)


def get_allowed_paths() -> tuple[str, ...]:
    """Return cached allowed paths, building on first call."""
    global _GRADIO_ALLOWED_PATHS
    if _GRADIO_ALLOWED_PATHS is None:
        _GRADIO_ALLOWED_PATHS = _build_gradio_allowed_paths()
    return _GRADIO_ALLOWED_PATHS


# ---------------------------------------------------------------------------
# Launch kwargs (used by candidate_review / launcher)
# ---------------------------------------------------------------------------


def launch_kwargs() -> dict:
    """Return keyword arguments for ``app.launch(**launch_kwargs())``."""
    # Deferred imports to avoid circular dependencies with settings / review.
    from app.ui.tabs.settings import CONFIG_TOOLTIP_CSS, CONFIG_TOOLTIP_JS
    from app.ui.tabs.review import REVIEW_LAYOUT_CSS, REVIEW_SHORTCUTS_JS

    return {
        "server_name": "127.0.0.1",
        "server_port": 7861,
        "allowed_paths": get_allowed_paths(),
        "theme": gr.themes.Soft(),
        "css": CONFIG_TOOLTIP_CSS + REVIEW_LAYOUT_CSS,
        "js": CONFIG_TOOLTIP_JS + REVIEW_SHORTCUTS_JS,
    }


# ---------------------------------------------------------------------------
# Tab builders (re-exported from tab modules so workbench exposes them)
# ---------------------------------------------------------------------------


def build_today_tab(context) -> None:
    """Build the Today summary tab (placeholder)."""
    from app.ui.tabs.today import build_today_tab as _build

    _build(context)


def build_search_tab(context) -> None:
    """Build the Search tab (placeholder)."""
    from app.ui.tabs.search import build_search_tab as _build

    _build(context)


def build_collections_tab(context) -> None:
    """Build the Collections tab (placeholder)."""
    from app.ui.tabs.collections import build_collections_tab as _build

    _build(context)


def build_settings_tab(context) -> None:
    """Build the Settings tab — config editor + profile management."""
    from app.ui.tabs.settings import build_settings_tab as _build

    _build(context)


# ---------------------------------------------------------------------------
# Workbench builder
# ---------------------------------------------------------------------------


def build_workbench(context: WorkbenchContext) -> gr.Blocks:
    """Create the top-level Gradio ``gr.Blocks`` with all 7 tabs.

    Parameters
    ----------
    context : WorkbenchContext
        Shared context including API client and allowed paths.

    Returns
    -------
    gr.Blocks
        The fully assembled Gradio application.
    """
    app = gr.Blocks(title="GifAgent")
    with app:
        gr.Markdown("# GifAgent - Preference Memory")

        # ---- Today ----------------------------------------------------------
        with gr.Tab("今日"):
            build_today_tab(context)

        # ---- Queue / Control -----------------------------------------------
        with gr.Tab("队列"):
            from app.ui.tabs.control import build_control_tab

            build_control_tab(context.client)

        # ---- Review --------------------------------------------------------
        with gr.Tab("审核"):
            from app.ui.tabs.review import build_review_tab

            review_components = build_review_tab()
            app.load(
                fn=lambda: (
                    [],
                    "Choose a data folder to review.",
                    gr.update(value=0, maximum=1),
                    [],
                ),
                outputs=[
                    review_components["gallery"],
                    review_components["info_text"],
                    review_components["page_slider"],
                    review_components["page_items_state"],
                ],
            )

        # ---- Search --------------------------------------------------------
        with gr.Tab("搜索"):
            build_search_tab(context)

        # ---- Collections ---------------------------------------------------
        with gr.Tab("合集"):
            build_collections_tab(context)

        # ---- Lab -----------------------------------------------------------
        with gr.Tab("实验室"):
            from app.ui.tabs.lab import build_lab_tab

            build_lab_tab()

        # ---- Settings + Profile --------------------------------------------
        with gr.Tab("设置"):
            build_settings_tab(context)

            from app.ui.tabs.profile import build_profile_tab, load_profile_publish_choices

            profile_components = build_profile_tab()
            app.load(
                fn=load_profile_publish_choices,
                outputs=[
                    profile_components["publish_profile_dropdown"],
                    profile_components["profile_status"],
                ],
            )

    return app

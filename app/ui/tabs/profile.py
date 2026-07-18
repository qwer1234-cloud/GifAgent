"""Profile tab — preference profile build, preview, publish, and vector backfill.

``build_profile_tab()`` should be called from inside a ``gr.Blocks`` context
(usually within ``with gr.Tab("设置"):`` as a sub-section).
"""

from __future__ import annotations

import json

import gradio as gr
import httpx

from app.ui.components.common import _format_api_error

API_BASE = "http://127.0.0.1:8000"

# ---------------------------------------------------------------------------
# Profile helpers
# ---------------------------------------------------------------------------


def get_profile_status():
    try:
        resp = httpx.get(f"{API_BASE}/api/preference/profiles", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            current = data.get("current")
            builds = data.get("profiles", [])
            if current:
                return (
                    f"Current: {current['profile_version'][:20]}... | "
                    f"Builds: {len(builds)}"
                )
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
            return (
                gr.update(choices=[], value=None),
                f"Error: {resp.status_code} - {_format_api_error(resp)}",
            )
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
        from app.db import get_connection
        from app.services.candidate_vectors import backfill_candidate_vectors
        from app.services.embedding import compute_text_embedding

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


# ---------------------------------------------------------------------------
# Tab builder
# ---------------------------------------------------------------------------


def build_profile_tab() -> dict:
    """Build the Gradio Profile tab components inside the current Blocks context.

    Returns
    -------
    dict
        All Gradio components keyed by name.
    """
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

    profile_status_timer = gr.Timer(10)
    profile_status_timer.tick(fn=get_profile_status, outputs=[profile_status])

    return {
        "profile_status": profile_status,
        "build_btn": build_btn,
        "backfill_vectors_btn": backfill_vectors_btn,
        "refresh_profiles_btn": refresh_profiles_btn,
        "publish_profile_dropdown": publish_profile_dropdown,
        "publish_btn": publish_btn,
        "build_output": build_output,
        "backfill_vectors_output": backfill_vectors_output,
        "publish_output": publish_output,
        "profile_status_timer": profile_status_timer,
    }

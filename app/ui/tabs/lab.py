"""Lab tab — quality-lab experiment runs, scorecards, and champion management.

``build_lab_tab()`` should be called from inside a ``gr.Blocks`` context
(usually within ``with gr.Tab("Lab"):``).  It creates all Gradio components
and wires their events internally, returning a dict of component references.
"""

from __future__ import annotations

import json
from typing import Any

import gradio as gr
import httpx

API_BASE = "http://127.0.0.1:8000"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _api_get(path: str) -> Any:
    """GET an API endpoint and return parsed JSON or an error dict."""
    try:
        resp = httpx.get(f"{API_BASE}{path}", timeout=10)
        if resp.status_code == 404:
            return {"error": "Not found"}
        resp.raise_for_status()
        return resp.json()
    except httpx.RequestError as e:
        return {"error": f"Connection failed: {e}"}
    except Exception as e:
        return {"error": str(e)}


def _api_post(path: str, json_data: dict | None = None) -> Any:
    """POST to an API endpoint and return parsed JSON or an error dict."""
    try:
        resp = httpx.post(
            f"{API_BASE}{path}",
            json=json_data or {},
            timeout=10,
        )
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            return {"error": str(detail)}
        return resp.json()
    except httpx.RequestError as e:
        return {"error": f"Connection failed: {e}"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tab builder
# ---------------------------------------------------------------------------


def build_lab_tab() -> dict[str, Any]:
    """Build the Gradio Lab tab components inside the current Blocks context.

    Returns
    -------
    dict
        All Gradio components keyed by name.
    """
    gr.Markdown("## Quality Lab")

    # ---- Experiment Runs --------------------------------------------------

    gr.Markdown("### Experiment Runs")
    runs_table = gr.Dataframe(
        headers=["Run ID", "Manifest", "Config", "Split", "Status", "Created"],
        label="Experiment Runs",
        interactive=False,
    )

    with gr.Row():
        with gr.Column(scale=1):
            run_selector = gr.Dropdown(
                choices=[],
                value=None,
                label="Select Run for Scorecard",
                interactive=True,
            )

        with gr.Column(scale=2):
            scorecard_output = gr.JSON(
                label="Scorecard",
                value={},
            )

    with gr.Row():
        refresh_runs_btn = gr.Button("Refresh Runs", variant="secondary")
        refresh_scorecard_btn = gr.Button("Refresh Scorecard", variant="secondary")

    # ---- AB Review Sessions -----------------------------------------------

    gr.Markdown("### Blind A/B Review")
    with gr.Row():
        with gr.Column():
            ab_run_a = gr.Textbox(label="Run A ID", placeholder="run_a_id")
            ab_run_b = gr.Textbox(label="Run B ID", placeholder="run_b_id")
            ab_seed = gr.Number(label="Seed", value=42, precision=0)
            create_ab_btn = gr.Button("Create Session", variant="primary")
        with gr.Column():
            ab_session_id = gr.Textbox(label="Session ID", interactive=False)
            ab_result = gr.Textbox(label="Result", interactive=False)

    gr.Markdown("#### Record Judgment")
    with gr.Row():
        ab_judge_session = gr.Textbox(label="Session ID", placeholder="ab_session_id")
        ab_pair_index = gr.Textbox(label="Pair Index", placeholder="0")
        ab_choice = gr.Dropdown(
            choices=["left", "right", "tie", "both_bad"],
            value="left",
            label="Choice",
        )
        record_judgment_btn = gr.Button("Record Judgment", variant="secondary")
    ab_judgment_result = gr.Textbox(label="Judgment Result", interactive=False)

    # ---- Champion Management ----------------------------------------------

    gr.Markdown("### Champion Management")
    with gr.Row():
        with gr.Column(scale=2):
            champion_info = gr.JSON(
                label="Current Champion",
                value={},
            )
        with gr.Column(scale=1):
            refresh_champion_btn = gr.Button("Refresh Champion", variant="secondary")

    gr.Markdown("#### Promote Config")
    with gr.Row():
        promote_config_id = gr.Textbox(
            label="Config ID",
            placeholder="config_id to promote",
        )
        promote_confirmation = gr.Textbox(
            label="Confirmation (must match Config ID)",
            placeholder="Type config ID to confirm",
        )
        promote_btn = gr.Button("Promote", variant="primary")
    promote_result = gr.Textbox(label="Promote Result", interactive=False)

    gr.Markdown("#### Rollback")
    with gr.Row():
        rollback_btn = gr.Button("Rollback to Previous Champion", variant="stop")
    rollback_result = gr.Textbox(label="Rollback Result", interactive=False)

    gr.Markdown("### Champion History")
    champion_history_table = gr.Dataframe(
        headers=["Event ID", "Config", "Action", "Previous Config", "Created"],
        label="Champion History",
        interactive=False,
    )
    refresh_history_btn = gr.Button("Refresh History", variant="secondary")

    # ---- Timer ------------------------------------------------------------

    timer = gr.Timer(30)

    # ---- Internal helpers -------------------------------------------------

    def _format_runs(runs: list | dict) -> list[list]:
        if isinstance(runs, dict) and "error" in runs:
            return []
        rows = []
        for r in runs if isinstance(runs, list) else []:
            rows.append(
                [
                    str(r.get("run_id", ""))[:12],
                    str(r.get("manifest_id", ""))[:12],
                    str(r.get("config_id", ""))[:12],
                    str(r.get("split", "")),
                    str(r.get("status", "")),
                    str(r.get("created_at", ""))[:19],
                ]
            )
        return rows

    def _runs_to_choices(runs: list | dict) -> list[str]:
        if isinstance(runs, dict) and "error" in runs:
            return []
        return [
            r["run_id"]
            for r in (runs if isinstance(runs, list) else [])
            if r.get("run_id")
        ]

    def _format_history(history: list | dict) -> list[list]:
        if isinstance(history, dict) and "error" in history:
            return []
        rows = []
        for h in history if isinstance(history, list) else []:
            rows.append(
                [
                    str(h.get("event_id", "")),
                    str(h.get("config_id", ""))[:16],
                    str(h.get("action", "")),
                    str((h.get("previous_config_id") or "")[:16]),
                    str(h.get("created_at", ""))[:19],
                ]
            )
        return rows

    # ---- Refresh all runs -------------------------------------------------

    def _refresh_runs():
        runs = _api_get("/api/quality/runs")
        rows = _format_runs(runs)
        choices = _runs_to_choices(runs)
        return rows, gr.update(choices=choices)

    # ---- Refresh scorecard ------------------------------------------------

    def _refresh_scorecard(run_id: str):
        if not run_id or not run_id.strip():
            return {}
        sc = _api_get(f"/api/quality/runs/{run_id.strip()}/scorecard")
        if isinstance(sc, dict) and "error" in sc:
            return {"error": sc["error"]}
        return sc.get("scorecard", {})

    # ---- Refresh champion -------------------------------------------------

    def _refresh_champion():
        champ = _api_get("/api/quality/champions/current")
        if isinstance(champ, dict) and "error" in champ:
            return {"message": "No current champion"}
        return champ

    # ---- Refresh history --------------------------------------------------

    def _refresh_history():
        history = _api_get("/api/quality/champions/history")
        return _format_history(history)

    # ---- Promote ----------------------------------------------------------

    def _do_promote(config_id: str, confirmation: str):
        if not config_id or not config_id.strip():
            return "Error: Config ID is required"
        if not confirmation or not confirmation.strip():
            return "Error: Confirmation is required"
        result = _api_post(
            f"/api/quality/champions/{config_id.strip()}/promote",
            json_data={"confirmation": confirmation.strip()},
        )
        if isinstance(result, dict) and "error" in result:
            return f"Error: {result['error']}"
        return json.dumps(result, indent=2)

    # ---- Rollback ---------------------------------------------------------

    def _do_rollback():
        result = _api_post("/api/quality/champions/rollback")
        if isinstance(result, dict) and "error" in result:
            return f"Error: {result['error']}"
        return json.dumps(result, indent=2)

    # ---- Create AB Session ------------------------------------------------

    def _do_create_ab(run_a: str, run_b: str, seed: int):
        if not run_a or not run_b:
            return "", "Error: Both Run A and Run B IDs are required"
        result = _api_post(
            "/api/quality/ab-sessions",
            json_data={"run_a": run_a.strip(), "run_b": run_b.strip(), "seed": int(seed)},
        )
        if isinstance(result, dict) and "error" in result:
            return "", f"Error: {result['error']}"
        return result.get("session_id", ""), f"Session created: {result.get('session_id', '')}"

    # ---- Record Judgment --------------------------------------------------

    def _do_record_judgment(session_id: str, pair_index: str, choice: str):
        if not session_id or not session_id.strip():
            return "Error: Session ID is required"
        if not pair_index or not pair_index.strip():
            return "Error: Pair index is required"
        result = _api_post(
            f"/api/quality/ab-sessions/{session_id.strip()}/judgments",
            json_data={"pair_index": pair_index.strip(), "choice": choice},
        )
        if isinstance(result, dict) and "error" in result:
            return f"Error: {result['error']}"
        return f"Judgment recorded for pair {pair_index}"

    # ---- Wire events ------------------------------------------------------

    runs_outputs = [runs_table, run_selector]

    timer.tick(fn=_refresh_runs, outputs=runs_outputs)
    refresh_runs_btn.click(fn=_refresh_runs, outputs=runs_outputs)

    refresh_scorecard_btn.click(
        fn=_refresh_scorecard,
        inputs=[run_selector],
        outputs=[scorecard_output],
    )

    run_selector.change(
        fn=_refresh_scorecard,
        inputs=[run_selector],
        outputs=[scorecard_output],
    )

    refresh_champion_btn.click(
        fn=_refresh_champion,
        outputs=[champion_info],
    )

    refresh_history_btn.click(
        fn=_refresh_history,
        outputs=[champion_history_table],
    )

    promote_btn.click(
        fn=_do_promote,
        inputs=[promote_config_id, promote_confirmation],
        outputs=[promote_result],
    )

    rollback_btn.click(
        fn=_do_rollback,
        outputs=[rollback_result],
    )

    create_ab_btn.click(
        fn=_do_create_ab,
        inputs=[ab_run_a, ab_run_b, ab_seed],
        outputs=[ab_session_id, ab_result],
    )

    record_judgment_btn.click(
        fn=_do_record_judgment,
        inputs=[ab_judge_session, ab_pair_index, ab_choice],
        outputs=[ab_judgment_result],
    )

    # ---- Initial load -----------------------------------------------------

    timer.tick(fn=_refresh_champion, outputs=[champion_info])
    timer.tick(fn=_refresh_history, outputs=[champion_history_table])

    return {
        "runs_table": runs_table,
        "run_selector": run_selector,
        "scorecard_output": scorecard_output,
        "champion_info": champion_info,
        "champion_history_table": champion_history_table,
        "promote_result": promote_result,
        "rollback_result": rollback_result,
        "timer": timer,
    }

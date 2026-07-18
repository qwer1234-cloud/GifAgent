"""Today tab — attention inbox with actionable cards and status summary.

``build_today_tab()`` should be called from inside a ``gr.Blocks`` context
(usually within ``with gr.Tab("今日"):``).
"""

from __future__ import annotations

import gradio as gr

# ── Severity colour palette ──────────────────────────────────────────────

_SEVERITY_COLORS = {
    "error": "#dc3545",
    "warning": "#e6a700",
    "info": "#0d6efd",
}

_SEVERITY_LABELS = {
    "error": "Error",
    "warning": "Warning",
    "info": "Info",
}


def _render_summary(items: list[dict]) -> str:
    """Return a short Markdown summary line for the attention inbox."""
    counts: dict[str, int] = {}
    for item in items:
        sev = item.get("severity", "info")
        counts[sev] = counts.get(sev, 0) + 1

    parts: list[str] = []
    for sev in ("error", "warning", "info"):
        n = counts.get(sev, 0)
        if n:
            colour = _SEVERITY_COLORS[sev]
            parts.append(
                f"<span style='color:{colour};font-weight:bold'>{n} "
                f"{_SEVERITY_LABELS[sev]}</span>"
            )

    if not parts:
        return '<span style="color:green">All clear</span>'

    return " | ".join(parts)


def _render_card(item: dict) -> str:
    """Return an HTML card for a single attention item."""
    sev = item.get("severity", "info")
    colour = _SEVERITY_COLORS.get(sev, "#6c757d")
    label = _SEVERITY_LABELS.get(sev, "Info")
    title = item.get("title", "")
    detail = item.get("detail", "")
    created_at = item.get("created_at", "")
    action_label = item.get("action_label", "")
    action_target = item.get("action_target", "")

    return f"""<div style="border:1px solid {colour};border-radius:8px;
padding:12px;margin:8px 0;background:#f8f9fa">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <strong>{title}</strong>
    <span style="color:{colour};font-weight:bold;font-size:0.85em">{label}</span>
  </div>
  <p style="margin:6px 0;color:#333">{detail}</p>
  <div style="display:flex;justify-content:space-between;align-items:center;font-size:0.85em">
    <span style="color:#666">{created_at}</span>
    <a href="{action_target}" style="color:{colour};text-decoration:none;font-weight:bold">{action_label}</a>
  </div>
</div>"""


def _render_warnings(warnings: list[str]) -> str:
    """Return HTML for source-warning banners."""
    if not warnings:
        return ""
    parts = [
        "<div style='margin:8px 0;padding:8px 12px;background:#fff3cd;"
        "border:1px solid #ffc107;border-radius:6px'>"
        "<strong>Source warnings:</strong><ul>"
    ]
    for w in warnings:
        parts.append(f"<li>{w}</li>")
    parts.append("</ul></div>")
    return "".join(parts)


# ── Tab builder ──────────────────────────────────────────────────────────


def build_today_tab(context) -> None:
    """Build the Today summary tab with attention inbox cards."""
    gr.Markdown("## Today")

    with gr.Column():
        refresh_btn = gr.Button("Refresh", variant="primary", scale=0)

        summary_md = gr.Markdown(
            value='<span style="color:green">All clear</span>'
        )

        cards_html = gr.HTML(value="")

    # ── Refresh handler ──────────────────────────────────────────────────

    def _do_refresh():
        try:
            client = context.client
            data = client.get_attention(limit=100)

            if isinstance(data, dict) and "error" in data:
                return f"Error: {data['error']}", ""

            items = data.get("items", [])
            warnings = data.get("source_warnings", [])

            summary = _render_summary(items)
            cards = "".join(_render_card(item) for item in items)
            warning_html = _render_warnings(warnings)

            # If there are no items and no warnings, show a clear message
            if not items and not warnings:
                cards = (
                    '<div style="padding:24px;text-align:center;color:#666">'
                    "No items requiring attention.</div>"
                )

            return summary, warning_html + cards

        except Exception as exc:
            return (
                "Error loading attention data",
                f"<div style='color:red;padding:8px'>"
                f"Failed to load: {exc}</div>",
            )

    refresh_btn.click(
        fn=_do_refresh,
        inputs=[],
        outputs=[summary_md, cards_html],
    )

    # Auto-load on page load
    refresh_btn.click(
        fn=_do_refresh,
        inputs=[],
        outputs=[summary_md, cards_html],
    )

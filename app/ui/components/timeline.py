"""Timeline UI component for Gradio workbench.

Renders a horizontal SVG timeline bar showing scenes, candidates, and
generated GIFs as coloured segments. Supports selection for GIF preview
and PotPlayer jump targets.
"""

from __future__ import annotations

import html
from typing import Any


def build_timeline_html(
    *,
    video_id: str,
    start_sec: float,
    end_sec: float,
    scenes: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    generated_gifs: list[dict[str, Any]],
) -> str:
    """Return an HTML+SVG string that draws an interactive timeline.

    Parameters
    ----------
    video_id:
        The source video identifier (embedded in data attributes).
    start_sec, end_sec:
        The visible time range in seconds.
    scenes:
        List of span dicts with keys ``span_id``, ``start_sec``, ``end_sec``,
        ``label``, ``base_score``, ``preference_score``, ``thumbnail_path``,
        ``potplayer_target``.
    candidates:
        Same structure -- shown in a different colour.
    generated_gifs:
        Same structure -- shown in a third colour.

    Returns
    -------
    str
        Safe HTML with embedded SVG that Gradio can render via ``gr.HTML``.
    """
    duration = max(end_sec - start_sec, 1.0)
    timeline_width = 800
    timeline_height = 64
    lane_y = {"scenes": 4, "candidates": 24, "generated": 44}
    lane_h = 18

    def _x(sec: float) -> float:
        return (sec - start_sec) / duration * timeline_width

    def _span_svg(spans: list[dict], lane: str, colour: str) -> str:
        """Build SVG ``<rect>`` elements for a list of spans."""
        if not spans:
            return ""
        y = lane_y[lane]
        rects = []
        for sp in spans:
            x1 = max(_x(sp["start_sec"]), 0)
            x2 = min(_x(sp["end_sec"]), timeline_width)
            w = max(x2 - x1, 2)
            label = html.escape(sp.get("label") or "")
            tip_parts = [f"{sp['start_sec']:.1f}s - {sp['end_sec']:.1f}s"]
            score = sp.get("preference_score")
            if score is not None:
                tip_parts.append(f"score={score:.3f}")
            base = sp.get("base_score")
            if base is not None:
                tip_parts.append(f"base={base:.3f}")
            tooltip = html.escape(" | ".join(tip_parts))
            rects.append(
                f'<rect x="{x1:.1f}" y="{y}" width="{w:.1f}" height="{lane_h}" '
                f'fill="{colour}" rx="3" ry="3" '
                f'data-span-id="{html.escape(sp["span_id"])}" '
                f'data-label="{label}" '
                f'data-tooltip="{tooltip}" '
                f'data-potplayer="{html.escape(sp.get("potplayer_target") or "")}" '
                f'data-thumbnail="{html.escape(sp.get("thumbnail_path") or "")}" '
                f'class="tl-span" '
                f'onclick="selectTimelineSpan(this)" '
                f'ondblclick="openPotPlayer(this)" '
                f'/>'
            )
        return "\n".join(rects)

    # Tick marks every N seconds
    tick_interval = _auto_tick_interval(duration)
    ticks_html = ""
    t = start_sec + (tick_interval - (start_sec % tick_interval)) % tick_interval
    while t < end_sec:
        x = _x(t)
        label = _format_tick_label(t)
        ticks_html += (
            f'<text x="{x:.1f}" y="{timeline_height - 2}" '
            f'font-size="10" fill="#999" text-anchor="middle">{label}</text>'
        )
        t += tick_interval

    # Legend
    legend_html = """
    <div style="display:flex; gap:16px; font-size:12px; margin-top:4px;">
      <span><span style="display:inline-block; width:12px; height:12px;
            background:#4a90d9; border-radius:2px; vertical-align:middle;
            margin-right:4px;"></span>Scenes</span>
      <span><span style="display:inline-block; width:12px; height:12px;
            background:#50b86c; border-radius:2px; vertical-align:middle;
            margin-right:4px;"></span>Candidates</span>
      <span><span style="display:inline-block; width:12px; height:12px;
            background:#e8b840; border-radius:2px; vertical-align:middle;
            margin-right:4px;"></span>Generated GIFs</span>
    </div>
    """

    svg = f"""<svg width="{timeline_width}" height="{timeline_height + 4}"
         xmlns="http://www.w3.org/2000/svg"
         style="border:1px solid #444; border-radius:4px; background:#1e1e1e; display:block;">
      {_span_svg(scenes, "scenes", "#4a90d9")}
      {_span_svg(candidates, "candidates", "#50b86c")}
      {_span_svg(generated_gifs, "generated", "#e8b840")}
      {ticks_html}
    </svg>
    """

    preview_html = """
    <div id="tl-preview"
         style="margin-top:8px; min-height:60px; display:flex; align-items:center;
                justify-content:center; border:1px dashed #555; border-radius:4px;
                color:#888; font-size:13px;">
      Click a timeline span to preview
    </div>
    """

    potplayer_hint = """
    <div style="font-size:11px; color:#666; margin-top:2px;">
      Double-click a span to open in PotPlayer
    </div>
    """

    script = """
    <script>
    function selectTimelineSpan(el) {
      var preview = document.getElementById('tl-preview');
      var thumb = el.getAttribute('data-thumbnail');
      var label = el.getAttribute('data-label');
      if (thumb) {
        preview.innerHTML = '<div style="text-align:center">'
          + '<img src="' + thumb + '" style="max-height:120px; border-radius:4px;'
          + ' display:block; margin:0 auto;" />'
          + '<span style="font-size:12px; color:#ccc; margin-top:4px;">'
          + label + '</span></div>';
      } else {
        preview.innerHTML = '<span style="color:#aaa;">'
          + (label || 'No preview available') + '</span>';
      }
    }
    function openPotPlayer(el) {
      var target = el.getAttribute('data-potplayer');
      if (target) {
        // Dispatch to desktop launcher via POST
        fetch('/api/workbench/launch-potplayer', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({target: target})
        }).catch(function(e) {
          console.warn('PotPlayer launch failed:', e);
        });
      }
    }
    </script>
    """

    return (
        f'<div id="timeline-{html.escape(video_id)}" '
        f'data-video-id="{html.escape(video_id)}">'
        f'{svg}'
        f'{legend_html}'
        f'{preview_html}'
        f'{potplayer_hint}'
        f'{script}'
        f'</div>'
    )


def _auto_tick_interval(duration_sec: float) -> float:
    """Choose a reasonable tick interval for the visible duration."""
    if duration_sec <= 30:
        return 5.0
    if duration_sec <= 120:
        return 15.0
    if duration_sec <= 600:
        return 60.0
    return 300.0


def _format_tick_label(seconds: float) -> str:
    """Format a tick label as ``MM:SS`` or ``H:MM:SS``."""
    total = int(seconds)
    h, r = divmod(total, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

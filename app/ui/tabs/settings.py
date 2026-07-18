"""Settings tab — configuration editor for models.yaml and preference memory.

``build_settings_tab()`` should be called from inside a ``gr.Blocks`` context
(usually within ``with gr.Tab("设置"):``).
"""

from __future__ import annotations

import html
import json
import os

import gradio as gr
import httpx
import yaml

API_BASE = "http://127.0.0.1:8000"
CONFIG_FILE = "configs/models.yaml"

# ---------------------------------------------------------------------------
# Config field metadata
# ---------------------------------------------------------------------------

CONFIG_FIELD_KEYS = (
    "llm.provider",
    "llm.model",
    "llm.api_key_env",
    "llm.base_url",
    "llm.temperature",
    "llm.max_tokens",
    "llm.timeout_s",
    "vlm.model",
    "vlm.base_url",
    "adaptive.sample_interval",
    "adaptive.merge_gap",
    "adaptive.merge_score_threshold",
    "adaptive.worthiness_threshold",
    "adaptive.refine_threshold",
    "adaptive.max_duration",
    "adaptive.vlm_temperature",
    "adaptive.output_ratio",
    "adaptive.max_output",
    "adaptive.gif_fps",
    "preference_memory.enabled",
    "preference_memory.base_score_weight",
    "preference_memory.preference_score_weight",
)

CONFIG_FIELD_HELP = {
    "llm.provider": "文本合成使用的模型服务类型，例如 openai_compatible。",
    "llm.model": "用于生成摘要、标签和描述的语言模型名称。",
    "llm.api_key_env": "从环境变量读取云端模型 API Key 的变量名。",
    "llm.base_url": "语言模型兼容 API 的服务地址。",
    "llm.temperature": "文本生成随机性；数值越高越有变化，越低越稳定。",
    "llm.max_tokens": "单次文本生成允许输出的最大 token 数。",
    "llm.timeout_s": "等待语言模型响应的最长时间，单位为秒。",
    "vlm.model": "用于分析视频帧和评分的视觉语言模型名称。",
    "vlm.base_url": "视觉语言模型服务的访问地址。",
    "adaptive.sample_interval": "粗采样相邻帧的时间间隔，单位为秒；越小越密集。",
    "adaptive.merge_gap": "相邻高分帧允许合并的最大时间间隔，单位为秒。",
    "adaptive.merge_score_threshold": "只有两帧评分都达到此值时才允许合并。",
    "adaptive.worthiness_threshold": "帧被认为值得导出为 GIF 的最低评分。",
    "adaptive.refine_threshold": "达到此评分的帧会触发周边时间段的细采样。",
    "adaptive.max_duration": "单个导出 GIF 的最长时长，单位为秒。",
    "adaptive.vlm_temperature": "视觉模型评分时的随机性；较低值通常更稳定。",
    "adaptive.output_ratio": "从去重后的候选片段中导出的比例，范围通常为 0 到 1。",
    "adaptive.max_output": "每个视频最多导出的 GIF 数量；填写 0 表示不设上限。",
    "adaptive.gif_fps": "导出 GIF 的播放帧率，单位为每秒帧数。",
    "preference_memory.enabled": "是否启用基于用户反馈构建偏好画像并参与后续排序。",
    "preference_memory.base_score_weight": "导出排序中原始 VLM gif_worthiness 评分的权重；与偏好权重按比例归一化。",
    "preference_memory.preference_score_weight": "导出排序中已发布偏好画像评分的权重；与原始评分权重按比例归一化。",
}

CONFIG_FIELD_LABELS = {
    "adaptive.sample_interval": "sample_interval (s)",
    "adaptive.merge_gap": "merge_gap (s)",
    "adaptive.max_duration": "max_duration (s)",
    "adaptive.max_output": "max_output (0=no cap)",
    "adaptive.gif_fps": "gif_fps (frames/s)",
}

CONFIG_TOOLTIP_CSS = """
.config-field-label {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    min-height: 1.35rem;
    margin: 0 0 0.2rem 0;
    color: var(--body-text-color);
    font-size: var(--text-sm);
    font-weight: 500;
}
.config-tooltip-icon {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1rem;
    height: 1rem;
    border: 1px solid var(--border-color-primary);
    border-radius: 50%;
    color: var(--body-text-color-subdued);
    cursor: help;
    font-size: 0.72rem;
    font-weight: 700;
    line-height: 1;
}
.preference-tooltip-icon {
    margin-left: 0.35rem;
}
"""


def config_field_name(key: str) -> str:
    return CONFIG_FIELD_LABELS.get(key, key.rsplit(".", 1)[-1])


def config_tooltip_icon(key: str) -> str:
    """Render the shared accessible hover tooltip icon."""
    help_text = html.escape(CONFIG_FIELD_HELP[key], quote=True)
    return (
        f'<span class="config-tooltip-icon" tabindex="0" '
        f'title="{help_text}" aria-label="{help_text}">?</span>'
    )


def config_field_label(key: str) -> str:
    """Render a non-persistent label with an accessible hover tooltip icon."""
    name = html.escape(config_field_name(key))
    return (
        f'<div class="config-field-label">'
        f"<span>{name}</span>{config_tooltip_icon(key)}</div>"
    )


def config_field_kwargs(key: str) -> dict[str, str | bool]:
    """Hide Gradio's persistent help text in favor of the HTML tooltip icon."""
    return {"label": config_field_name(key), "show_label": False}


def config_checkbox_kwargs(key: str) -> dict[str, str | bool]:
    """Keep a Checkbox's native, clickable label visible beside the tooltip."""
    return {
        "label": config_field_name(key),
        "container": False,
        "elem_id": "preference-memory-enabled",
    }


def config_textbox(key: str, **kwargs):
    gr.HTML(config_field_label(key), sanitize_html=False)
    return gr.Textbox(**config_field_kwargs(key), **kwargs)


def config_checkbox(key: str, **kwargs):
    return gr.Checkbox(**config_checkbox_kwargs(key), **kwargs)


CONFIG_TOOLTIP_JS = f"""
(() => {{
    const attach = () => {{
        const label = document.querySelector('#preference-memory-enabled label');
        if (!label || label.querySelector('.preference-tooltip-icon')) return;
        const icon = document.createElement('span');
        icon.className = 'config-tooltip-icon preference-tooltip-icon';
        icon.tabIndex = 0;
        icon.textContent = '?';
        icon.title = {json.dumps(CONFIG_FIELD_HELP['preference_memory.enabled'], ensure_ascii=False)};
        icon.setAttribute('aria-label', icon.title);
        label.append(icon);
    }};
    requestAnimationFrame(attach);
    setTimeout(attach, 250);
    setTimeout(attach, 1000);
}})();
"""

# ---------------------------------------------------------------------------
# Config load / save helpers
# ---------------------------------------------------------------------------


def load_config():
    """Load configs/models.yaml, return field tuples + raw YAML."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        return (
            [str(e)] * 7,
            [str(e)] * 2,
            [str(e)] * 10,
            [False, "0.50", "0.50"],
            "",
        )

    llm = cfg.get("llm", {}) or {}
    vlm = cfg.get("vlm", {}) or {}
    adaptive = cfg.get("adaptive", {}) or {}
    pm = cfg.get("preference_memory", {}) or {}

    llm_fields = [
        llm.get("provider", ""),
        llm.get("model", ""),
        llm.get("api_key_env", ""),
        llm.get("base_url", ""),
        str(llm.get("temperature", 0.3)),
        str(llm.get("max_tokens", 2048)),
        str(llm.get("timeout_s", 120)),
    ]
    vlm_fields = [
        vlm.get("model", ""),
        vlm.get("base_url", ""),
    ]
    adaptive_fields = [
        str(adaptive.get("sample_interval", 10)),
        str(adaptive.get("merge_gap", 12)),
        str(adaptive.get("merge_score_threshold", 0.55)),
        str(adaptive.get("worthiness_threshold", 0.2)),
        str(adaptive.get("refine_threshold", 0.5)),
        str(adaptive.get("max_duration", 10)),
        str(adaptive.get("vlm_temperature", 0.65)),
        str(adaptive.get("output_ratio", 1.0)),
        str(adaptive.get("max_output", 0)),
        str(adaptive.get("gif_fps", 24)),
    ]
    pm_fields = [
        bool(pm.get("enabled", False)),
        str(pm.get("base_score_weight", 0.50)),
        str(pm.get("preference_score_weight", 0.50)),
    ]
    raw_text = yaml.dump(cfg, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return llm_fields, vlm_fields, adaptive_fields, pm_fields, raw_text


def save_config(
    llm_provider, llm_model, llm_api_key_env, llm_base_url,
    llm_temperature, llm_max_tokens, llm_timeout,
    vlm_model, vlm_base_url,
    ad_sample_interval, ad_merge_gap, ad_merge_score_threshold,
    ad_worthiness_threshold, ad_refine_threshold,
    ad_max_duration,
    ad_vlm_temperature, ad_output_ratio, ad_max_output, ad_gif_fps,
    pm_enabled, pm_base_score_weight, pm_preference_score_weight, raw_text,
):
    """Save edited fields back to configs/models.yaml, preserving other sections."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}

    cfg.setdefault("llm", {})
    cfg["llm"]["provider"] = llm_provider
    cfg["llm"]["model"] = llm_model
    cfg["llm"]["api_key_env"] = llm_api_key_env
    cfg["llm"]["base_url"] = llm_base_url
    cfg["llm"]["temperature"] = float(llm_temperature)
    cfg["llm"]["max_tokens"] = int(llm_max_tokens)
    cfg["llm"]["timeout_s"] = int(llm_timeout)

    cfg.setdefault("vlm", {})
    cfg["vlm"]["model"] = vlm_model
    cfg["vlm"]["base_url"] = vlm_base_url

    cfg.setdefault("adaptive", {})
    cfg["adaptive"]["sample_interval"] = int(ad_sample_interval)
    cfg["adaptive"]["merge_gap"] = int(ad_merge_gap)
    cfg["adaptive"]["merge_score_threshold"] = float(ad_merge_score_threshold)
    cfg["adaptive"]["worthiness_threshold"] = float(ad_worthiness_threshold)
    cfg["adaptive"]["refine_threshold"] = float(ad_refine_threshold)
    cfg["adaptive"]["max_duration"] = float(ad_max_duration)
    cfg["adaptive"]["vlm_temperature"] = float(ad_vlm_temperature)
    cfg["adaptive"]["output_ratio"] = float(ad_output_ratio)
    cfg["adaptive"]["max_output"] = int(ad_max_output)
    cfg["adaptive"]["gif_fps"] = int(ad_gif_fps)

    cfg.setdefault("preference_memory", {})
    cfg["preference_memory"]["enabled"] = bool(pm_enabled)
    cfg["preference_memory"]["base_score_weight"] = float(pm_base_score_weight)
    cfg["preference_memory"]["preference_score_weight"] = float(pm_preference_score_weight)

    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    new_raw = yaml.dump(cfg, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return "Saved to " + CONFIG_FILE, new_raw


def test_llm_connection():
    """Quick ping to the configured LLM to verify connectivity."""
    try:
        resp = httpx.post(f"{API_BASE}/api/status", timeout=5)
        if resp.status_code != 200:
            return f"API server not running (status {resp.status_code})"
    except Exception:
        return "API server not running at " + API_BASE

    try:
        from app.services.llm_client import generate_llm_text, get_llm_settings
        s = get_llm_settings()
        out = generate_llm_text("Reply OK", max_tokens=16, timeout=30)
        return f"OK - provider={s.provider}, model={s.model}, response={out[:50]!r}"
    except Exception as e:
        return f"FAIL - {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Tab builder
# ---------------------------------------------------------------------------


def build_settings_tab(context) -> None:
    """Build the Settings tab — configuration editor for models.yaml.

    Parameters
    ----------
    context : WorkbenchContext
        The workbench context (used for shared state).
    """
    gr.Markdown(
        "## Configuration Editor\n"
        "Edit values and click **Save**. Changes write to ``configs/models.yaml``."
    )

    with gr.Row():
        with gr.Column():
            with gr.Group():
                gr.Markdown("### LLM (text synthesis)")
                llm_provider = config_textbox("llm.provider", value="")
                llm_model = config_textbox("llm.model", value="")
                llm_api_key_env = config_textbox("llm.api_key_env", value="")
                llm_base_url = config_textbox("llm.base_url", value="")
                with gr.Row():
                    with gr.Column(min_width=160):
                        llm_temperature = config_textbox("llm.temperature", value="")
                    with gr.Column(min_width=160):
                        llm_max_tokens = config_textbox("llm.max_tokens", value="")
                    with gr.Column(min_width=160):
                        llm_timeout = config_textbox("llm.timeout_s", value="")
                test_llm_btn = gr.Button("Test LLM Connection")
                test_llm_output = gr.Textbox(label="LLM Test", interactive=False)

        with gr.Column():
            with gr.Group():
                gr.Markdown("### VLM (vision analysis)")
                vlm_model = config_textbox("vlm.model", value="")
                vlm_base_url = config_textbox("vlm.base_url", value="")

            with gr.Group():
                gr.Markdown("### Adaptive Sampling")
                ad_sample_interval = config_textbox("adaptive.sample_interval", value="")
                ad_merge_gap = config_textbox("adaptive.merge_gap", value="")
                ad_merge_score_threshold = config_textbox("adaptive.merge_score_threshold", value="")
                ad_worthiness_threshold = config_textbox("adaptive.worthiness_threshold", value="")
                ad_refine_threshold = config_textbox("adaptive.refine_threshold", value="")
                ad_max_duration = config_textbox("adaptive.max_duration", value="")
                ad_vlm_temperature = config_textbox("adaptive.vlm_temperature", value="")
                with gr.Row():
                    with gr.Column(min_width=160):
                        ad_output_ratio = config_textbox("adaptive.output_ratio", value="")
                    with gr.Column(min_width=160):
                        ad_max_output = config_textbox("adaptive.max_output", value="")
                ad_gif_fps = config_textbox("adaptive.gif_fps", value="")

            with gr.Group():
                gr.Markdown("### Preference Memory")
                pm_enabled = config_checkbox("preference_memory.enabled", value=False)
                with gr.Row():
                    with gr.Column(min_width=180):
                        pm_base_score_weight = config_textbox(
                            "preference_memory.base_score_weight", value="0.50"
                        )
                    with gr.Column(min_width=180):
                        pm_preference_score_weight = config_textbox(
                            "preference_memory.preference_score_weight", value="0.50"
                        )

    with gr.Row():
        save_btn = gr.Button("Save Config", variant="primary")
        reload_btn = gr.Button("Reload from File")
    config_status = gr.Textbox(label="Status", interactive=False)
    raw_yaml = gr.Textbox(label="Raw YAML (read-only preview)", lines=15, interactive=False)

    def _reload():
        llm_f, vlm_f, ad_f, pm_f, raw = load_config()
        return [*llm_f, *vlm_f, *ad_f, *pm_f, raw, "Loaded from " + CONFIG_FILE]

    all_inputs = [
        llm_provider, llm_model, llm_api_key_env, llm_base_url,
        llm_temperature, llm_max_tokens, llm_timeout,
        vlm_model, vlm_base_url,
        ad_sample_interval, ad_merge_gap, ad_merge_score_threshold,
        ad_worthiness_threshold, ad_refine_threshold,
        ad_max_duration, ad_vlm_temperature, ad_output_ratio, ad_max_output, ad_gif_fps,
        pm_enabled, pm_base_score_weight, pm_preference_score_weight, raw_yaml,
    ]
    save_btn.click(fn=save_config, inputs=all_inputs, outputs=[config_status, raw_yaml])
    reload_btn.click(fn=_reload, outputs=all_inputs + [config_status])
    test_llm_btn.click(fn=test_llm_connection, outputs=[test_llm_output])

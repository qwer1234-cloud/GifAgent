# Config Tooltip Icon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Config page's always-visible help text with a circular `?` icon whose Chinese explanation appears only on hover.

**Architecture:** `CONFIG_FIELD_HELP` remains the source for 20 explanations. A helper renders an escaped HTML label row and CSS tooltip, while Gradio inputs hide their native labels and omit `info`.

**Tech Stack:** Python 3.14, Gradio 6.18, pytest, CSS, PyInstaller.

## Global Constraints

- Cover the 20 editable Config-tab parameters.
- Do not render explanation text persistently beneath inputs.
- Do not change YAML loading/saving or runtime-history preservation.

---

### Task 1: Implement tooltip-only labels

**Files:**

- Modify: `app/ui/candidate_review.py`
- Modify: `tests/test_config_help_annotations.py`

- [ ] Add this failing test:

```python
def test_config_fields_use_html_tooltip_labels_without_gradio_info():
    for key in CONFIG_FIELD_KEYS:
        kwargs = config_field_kwargs(key)
        assert kwargs["show_label"] is False
        assert "info" not in kwargs
        assert 'class="config-tooltip-icon"' in config_field_label(key)
```

- [ ] Run `uv run pytest tests/test_config_help_annotations.py -q`; it must fail because fields currently use Gradio `info`.

- [ ] Add `config_field_label(key)` with escaped HTML and CSS for a hover-visible tooltip. Change `config_field_kwargs(key)` to return `show_label=False` without `info`.

- [ ] Render `gr.HTML(config_field_label(key))` directly before each of the 20 existing Config inputs, preserving their values and event wiring.

- [ ] Run `uv run pytest tests/test_config_help_annotations.py -q` and the full `uv run pytest tests/ -q` suite.

### Task 2: Rebuild without losing runtime history

**Files:**

- Modify: generated `dist/GifAgentUI/`
- Preserve: `dist/GifAgentUI/data/`

- [ ] Move the data directory to a timestamped backup, run `.\.venv\Scripts\python.exe -m PyInstaller --noconfirm build_exe.spec`, then restore the data directory.

- [ ] Verify `dist/GifAgentUI/GifAgentUI.exe` has a fresh timestamp and `dist/GifAgentUI/data/library.db` has the same byte size as before the build.

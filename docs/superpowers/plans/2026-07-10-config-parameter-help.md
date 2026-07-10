# Config Parameter Help Annotations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Chinese `?` help annotations to every editable parameter on the GUI Config tab while preserving existing config behavior and packaged runtime history.

**Architecture:** Keep all annotation copy in a single mapping in `app/ui/candidate_review.py`. A small helper will turn each field key into a label with a `?` marker and Gradio `info` text. Existing load/save functions and YAML preservation remain unchanged; tests validate annotation coverage independently from Gradio rendering.

**Tech Stack:** Python 3.11+, Gradio 6.18.0, pytest, PyInstaller, existing `scripts/rebuild_exe.sh`.

## Global Constraints

- Use Chinese annotation text.
- Cover exactly the 20 editable Config-tab parameters currently shown by the GUI.
- Do not change config parsing, type conversion, or preservation of YAML sections not shown in the GUI.
- Build with `scripts/rebuild_exe.sh` so `dist/GifAgentUI/data/` is backed up and restored.
- Preserve unrelated existing working-tree changes.

---

### Task 1: Add annotation metadata coverage test

**Files:**
- Create: `tests/test_config_help_annotations.py`
- Modify: `app/ui/candidate_review.py`

**Interfaces:**
- Test consumes `CONFIG_FIELD_HELP` and `config_field_kwargs` from `app.ui.candidate_review`.
- Production code will expose a mapping keyed by the 20 Config-tab field names and a helper returning `label` and `info` values.

- [ ] **Step 1: Write the failing test**

```python
from app.ui.candidate_review import CONFIG_FIELD_HELP, CONFIG_FIELD_KEYS, config_field_kwargs


def test_every_config_field_has_non_empty_chinese_help_and_question_marker():
    assert set(CONFIG_FIELD_HELP) == set(CONFIG_FIELD_KEYS)
    assert len(CONFIG_FIELD_KEYS) == 20
    for key in CONFIG_FIELD_KEYS:
        kwargs = config_field_kwargs(key)
        assert "?" in kwargs["label"]
        assert kwargs["info"]
        assert any("\u4e00" <= char <= "\u9fff" for char in kwargs["info"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_help_annotations.py -q`

Expected: FAIL because the annotation mapping and helper do not yet exist.

- [ ] **Step 3: Implement the minimal metadata API**

Add `CONFIG_FIELD_KEYS`, `CONFIG_FIELD_HELP`, and:

```python
def config_field_kwargs(key: str) -> dict[str, str]:
    label = key.rsplit(".", 1)[-1]
    return {"label": f"{label} ?", "info": CONFIG_FIELD_HELP[key]}
```

Populate one concise Chinese explanation for each of the 20 existing field keys.

- [ ] **Step 4: Run the focused test**

Run: `uv run pytest tests/test_config_help_annotations.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_config_help_annotations.py app/ui/candidate_review.py
git commit -m "ui: define config parameter help annotations"
```

### Task 2: Apply annotations to every Config component

**Files:**
- Modify: `app/ui/candidate_review.py` in the Config tab component declarations

**Interfaces:**
- Consumes `config_field_kwargs(key)` from Task 1.
- Produces Config components with the same values, input order, and event wiring as before, plus `?` labels and Gradio info text.

- [ ] **Step 1: Add kwargs to all LLM fields**

For each LLM component, pass the corresponding helper expansion, preserving its existing value:

```python
llm_provider = gr.Textbox(value="", **config_field_kwargs("llm.provider"))
llm_model = gr.Textbox(value="", **config_field_kwargs("llm.model"))
llm_api_key_env = gr.Textbox(value="", **config_field_kwargs("llm.api_key_env"))
llm_base_url = gr.Textbox(value="", **config_field_kwargs("llm.base_url"))
llm_temperature = gr.Textbox(value="", **config_field_kwargs("llm.temperature"))
llm_max_tokens = gr.Textbox(value="", **config_field_kwargs("llm.max_tokens"))
llm_timeout = gr.Textbox(value="", **config_field_kwargs("llm.timeout_s"))
```

Use unique field keys when the same YAML key appears in separate sections, such as `llm.model` and `vlm.model`.

- [ ] **Step 2: Add kwargs to VLM, adaptive, and Preference Memory fields**

Apply the helper to all remaining 13 fields, using keys `vlm.model`, `vlm.base_url`, `adaptive.sample_interval`, `adaptive.merge_gap`, `adaptive.merge_score_threshold`, `adaptive.worthiness_threshold`, `adaptive.refine_threshold`, `adaptive.max_duration`, `adaptive.vlm_temperature`, `adaptive.output_ratio`, `adaptive.max_output`, `adaptive.gif_fps`, and `preference_memory.enabled`.

- [ ] **Step 3: Run focused and import smoke tests**

Run: `uv run pytest tests/test_config_help_annotations.py -q`

Run: `uv run python -c "import app.ui.candidate_review as m; assert len(m.CONFIG_FIELD_KEYS) == 20"`

Expected: both commands exit 0; Config page import must not start the server.

- [ ] **Step 4: Commit**

```bash
git add app/ui/candidate_review.py
git commit -m "ui: annotate every config field"
```

### Task 3: Full verification and history-preserving GUI build

**Files:**
- Modify: generated files under `dist/` only as produced by the build
- Preserve: existing `dist/GifAgentUI/data/` contents through the rebuild script

**Interfaces:**
- Consumes the updated Config UI and existing PyInstaller spec.
- Produces a verified `dist/GifAgentUI/GifAgentUI.exe` and preserved runtime data directory.

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/ -v`

Expected: all tests pass, with only the repository’s known skipped tests if applicable.

- [ ] **Step 2: Build with history preservation**

Run: `bash scripts/rebuild_exe.sh`

Expected: the script backs up `dist/GifAgentUI/data/`, completes PyInstaller, restores/merges the data directory, and reports the executable exists.

- [ ] **Step 3: Independently verify build artifacts and preserved data**

Run: `Test-Path 'dist/GifAgentUI/GifAgentUI.exe'; Test-Path 'dist/GifAgentUI/data'; Get-ChildItem 'dist/GifAgentUI/data' -Force | Select-Object -First 10`

Expected: both `Test-Path` calls return `True`, and the data directory contains the pre-build runtime history rather than being discarded.

- [ ] **Step 4: Review final diff and status**

Run: `git diff HEAD~2 --stat; git status --short`

Confirm only intended source/test/plan changes are attributed to this task; existing user data changes remain untouched.

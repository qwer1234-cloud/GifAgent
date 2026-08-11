# Quality-First GUI EXE Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a safely rebuilt GUI EXE whose default and writable runtime configuration prioritizes GIF quality without imposing artificially low output.

**Architecture:** Keep the existing Task Engine and MoE integration unchanged. Express the product decision as a tested adaptive configuration profile, update the preserved packaged writable config without touching unrelated settings, then rebuild through the data-preserving release script and verify the real executable.

**Tech Stack:** Python 3.11, pytest, YAML, Gradio/pywebview, PyInstaller, PowerShell, Git Bash

## Global Constraints

- Do not modify `data/*.db`, logs, FAISS indexes, source media, or export directories.
- Do not enable active MoE rejection before human calibration; keep `report_only: true`.
- Preserve the packaged `dist/GifAgentUI/data` and writable `configs` directories.
- Quality is enforced by candidate gates and deduplication, not by an arbitrarily tiny output cap.

---

### Task 1: Lock the quality-first profile

**Files:**
- Modify: `tests/test_adaptive_config.py`
- Modify: `configs/models.yaml`
- Modify: `Agent.md`

**Interfaces:**
- Consumes: `yaml.safe_load(configs/models.yaml)` and `scripts.test_video_adaptive.extract_config`
- Produces: the frozen adaptive profile used by Direct and Staged execution

- [ ] Add `test_models_yaml_uses_balanced_quality_first_profile` asserting the exact thresholds, sampling coverage, output allowance, render settings, dedup settings, and `quality_moe.report_only`.
- [ ] Run the test and verify it fails against the previous profile.
- [ ] Change only the selected adaptive values in `configs/models.yaml` and document the profile in `Agent.md`.
- [ ] Run `uv run pytest -q tests/test_adaptive_config.py tests/quality_moe` and verify all tests pass.

### Task 2: Prepare and verify the writable packaged configuration

**Files:**
- Runtime artifact: `dist/GifAgentUI/configs/models.yaml`
- Backup artifact: `dist/GifAgentUI/configs/models.before-quality-first-20260811.yaml`

**Interfaces:**
- Consumes: the existing user-owned writable YAML
- Produces: the same YAML with only quality-profile keys overlaid

- [ ] Hash packaged runtime databases and copy the existing writable config to the named backup.
- [ ] Update the selected adaptive fields and add the strict `quality_moe` section while preserving endpoints, model choices, preference settings, and paths.
- [ ] Parse both source and writable configs and assert that the quality-profile values match.

### Task 3: Run release gates

**Files:** none

**Interfaces:**
- Consumes: repository source and tests
- Produces: fresh verification evidence

- [ ] Run `uv run python -m compileall -q app scripts tests`.
- [ ] Run `uv run pytest -q` and require zero failures.
- [ ] Run `git diff --check`.

### Task 4: Rebuild and smoke-test the real EXE

**Files:**
- Build artifact: `dist/GifAgentUI/GifAgentUI.exe`

**Interfaces:**
- Consumes: `build_exe.spec`, source tree, preserved packaged data/config
- Produces: a self-contained Windows GUI distribution

- [ ] Run `bash scripts/rebuild_exe.sh` without `--no-backup`.
- [ ] Verify the packaged internal config and `app/quality_moe` modules are present.
- [ ] Start the EXE hidden and wait for HTTP 200 from ports 8000 and 7861.
- [ ] Close it through `WM_CLOSE`, verify the process exits, and verify both ports are released.
- [ ] Re-hash packaged and production databases and require exact equality with pre-build values.

### Task 5: Commit the release source changes

**Files:**
- Modify: the tracked files from Task 1 plus this specification and plan

**Interfaces:**
- Consumes: verified source diff and build evidence
- Produces: an auditable local commit; no remote push

- [ ] Review `git diff`, `git status`, and the requirement checklist.
- [ ] Commit only the intended tracked files with `feat: ship quality-first GUI profile`.
- [ ] Report the EXE path, hashes, tests, runtime safety result, and the remaining `report_only` limitation.


# Review Auto Advance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep a GIF preview selected after feedback and automatically advance to the next reviewable folder.

**Architecture:** Reusable selection helpers return the same state for gallery clicks and post-feedback refreshes. After a successful rating, the UI reloads the current page first; only an empty page triggers a refresh of the ordered folder queue and a switch to the next remaining folder.

**Tech Stack:** Python, Gradio, HTTPX, pytest.

## Global Constraints

- Only a successful rating changes the review position.
- Use the first currently unreviewed GIF as the next selection.
- Folder order remains API order: shallow path, then case-insensitive name.
- If no reviewable folders remain, clear selection and show completion.
- Packaging replaces only `dist/GifAgentUI/GifAgentUI.exe` and retains `dist/GifAgentUI/data/`.

---

### Task 1: Test candidate and folder progression helpers

**Files:**
- Modify: `app/ui/candidate_review.py`
- Create: `tests/test_candidate_review_auto_advance.py`

**Interfaces:**
- Produces `select_first_candidate(page_items) -> tuple[str, str, str | None, str]`.
- Produces `next_reviewable_folder(previous_folders, refreshed_folders, current_folder) -> str | None`.

- [ ] **Step 1: Write a failing test**

```python
selected = select_first_candidate([{"candidate_id": "cand-next", "artifact_path": "D:/out/next.gif"}])
assert selected[0] == "cand-next"
assert selected[2] == "D:/out/next.gif"
```

- [ ] **Step 2: Verify the test fails**

Run: `uv run pytest tests/test_candidate_review_auto_advance.py -q`

Expected: FAIL because the helper does not exist.

- [ ] **Step 3: Implement the minimal helpers**

```python
def next_reviewable_folder(previous_folders, refreshed_folders, current_folder):
    # choose the next still-reviewable folder in the previous ordering.
```

- [ ] **Step 4: Verify the focused tests pass**

Run: `uv run pytest tests/test_candidate_review_auto_advance.py -q`

Expected: PASS.

### Task 2: Advance state after feedback

**Files:**
- Modify: `app/ui/candidate_review.py`
- Test: `tests/test_candidate_review_auto_advance.py`

**Interfaces:**
- Consumes the current folder, root, page, filter, and prior ordered folder list.
- Produces refreshed gallery/page data, an automatic preview selection, an updated folder dropdown, and refreshed folder state.

- [ ] **Step 1: Write failing transition tests**

```python
result = rate_and_advance(...)
assert result.selected_candidate_id == "next-in-folder"
assert result.folder_value == "D:/exports/next-folder"
```

- [ ] **Step 2: Verify the tests fail**

Run: `uv run pytest tests/test_candidate_review_auto_advance.py -q`

Expected: FAIL because refresh clears selection and never updates the folder.

- [ ] **Step 3: Implement state transition**

```python
if refreshed_page_items:
    return select_first_candidate(refreshed_page_items)
refresh_folder_choices_and_load_next_folder()
```

- [ ] **Step 4: Verify focused tests pass**

Run: `uv run pytest tests/test_candidate_review_auto_advance.py -q`

Expected: PASS.

### Task 3: Verify and package

**Files:**
- Modify: `dist/GifAgentUI/GifAgentUI.exe` after source tests pass.

- [ ] **Step 1: Run complete tests**

Run: `uv run pytest tests/ -q`

Expected: PASS.

- [ ] **Step 2: Build and copy only the EXE**

Run PyInstaller to `dist/_build_<timestamp>` and replace only `dist/GifAgentUI/GifAgentUI.exe` after checking the runtime database hash.

- [ ] **Step 3: Commit source and tests**

Run: `git commit -m "fix: auto advance candidate review"`

# Review Profile Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans (recommended) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make folder changes display the first GIF automatically, move Profile controls to their own page, and keep the selected GIF preview centered.

**Architecture:** Add a folder-change loader that reuses the existing page-loading and selection helpers. Move Profile components and callbacks into a sibling Gradio tab without changing their service functions. Add a dedicated preview element ID and CSS layout rules, then verify the rendered packaged UI.

**Tech Stack:** Python, Gradio, CSS, pytest, PyInstaller.

## Global Constraints

- Folder selection must load page 0 and select its first candidate in one event chain.
- Profile actions remain available without changing their existing API behavior.
- The preview image uses `contain` semantics and stays centered in its fixed preview area.
- Existing data and export history are preserved while replacing the packaged executable.

---

### Task 1: Fix automatic first-GIF selection after folder changes

**Files:**
- Modify: `app/ui/candidate_review.py`
- Test: `tests/test_candidate_review_auto_advance.py`

- [ ] **Step 1: Write the failing test**

```python
result = load_folder_page("D:/exports/A", "candidate")
assert result[5] == "cand-first"
assert result[7] == "D:/exports/A/first.gif"
```

- [ ] **Step 2: Verify the test fails**

Run: `uv run pytest tests/test_candidate_review_auto_advance.py -q`

Expected: FAIL because folder-change refresh currently clears the selection.

- [ ] **Step 3: Implement the loader**

```python
def load_folder_page(folder, filter_status):
    gallery, info, page, items = load_candidate_page(0, filter_status=filter_status, folder=folder)
    return gallery, info, page, items, *select_first_candidate(items)
```

- [ ] **Step 4: Verify the focused test passes**

Run: `uv run pytest tests/test_candidate_review_auto_advance.py -q`

Expected: PASS.

### Task 2: Move Profile controls into a dedicated tab

**Files:**
- Modify: `app/ui/candidate_review.py`
- Test: `tests/test_candidate_review_layout.py`

- [ ] **Step 1: Write the failing layout test**

```python
assert "Profile" in REVIEW_TAB_NAMES
assert PROFILE_COMPONENT_GROUP == "Profile"
```

- [ ] **Step 2: Verify the test fails**

Run: `uv run pytest tests/test_candidate_review_layout.py -q`

Expected: FAIL because Profile controls are nested inside Review.

- [ ] **Step 3: Move the controls**

Create a sibling `gr.Tab("Profile")` containing status, Build Profile, Backfill Missing Vectors, Refresh Profiles, publish selection, and result outputs. Keep Review limited to folder/GIF review and rating controls.

- [ ] **Step 4: Verify layout tests pass**

Run: `uv run pytest tests/test_candidate_review_layout.py -q`

Expected: PASS.

### Task 3: Center the GIF preview

**Files:**
- Modify: `app/ui/candidate_review.py`
- Test: `tests/test_candidate_review_layout.py`

- [ ] **Step 1: Write the failing CSS test**

```python
assert "selected-gif-preview" in REVIEW_LAYOUT_CSS
assert "object-position: center" in REVIEW_LAYOUT_CSS
```

- [ ] **Step 2: Verify the test fails**

Run: `uv run pytest tests/test_candidate_review_layout.py -q`

Expected: FAIL because the preview has no dedicated centered layout.

- [ ] **Step 3: Implement and wire preview CSS**

Give `gr.Image` the element ID `selected-gif-preview` and add flex centering plus `object-fit: contain` and `object-position: center` rules to the launch CSS.

- [ ] **Step 4: Verify all tests and packaged startup**

Run: `uv run pytest tests/ -q`, then build to `dist/_build_<timestamp>` and replace only `dist/GifAgentUI/GifAgentUI.exe`. Start the packaged GUI and confirm port 7861 listens.

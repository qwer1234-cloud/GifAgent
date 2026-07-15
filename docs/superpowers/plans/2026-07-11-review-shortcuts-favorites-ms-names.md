# Review Shortcuts, Favorites, and Millisecond Names Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans (recommended) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Review shortcuts, millisecond-based GIF names, and a persistent Favorite action.

**Architecture:** Review buttons receive stable DOM IDs and a guarded keyboard listener. Adaptive export names use absolute video offsets in milliseconds while candidate import parses both new millisecond and legacy second names. Favorite is a separate database/API operation that records the full GIF path, marks the candidate as `favorited`, and reuses the existing auto-advance flow.

**Tech Stack:** Python, SQLite, FastAPI, Gradio JavaScript, pytest, PyInstaller.

## Global Constraints

- Review shortcuts are `1=Like`, `2=Neutral`, `3=Dislike`, `4=Favorite`.
- Shortcuts are ignored while an input, textarea, select, or contenteditable element is focused.
- New names use `@@@<rank>_<start_ms>ms-<end_ms>ms.gif`; old `s` names remain readable.
- Favorite writes the complete GIF path to `favorite_gifs` and is not a Like feedback event.
- Existing user data and exports are preserved when packaging.

---

### Task 1: Add failing tests for shortcuts and millisecond names

**Files:**
- Modify: `tests/test_candidate_review_layout.py`
- Create: `tests/test_adaptive_filename.py`

- [ ] **Step 1: Write failing tests**

```python
assert "'4': 'favorite-btn'" in REVIEW_SHORTCUTS_JS
assert build_gif_filename("movie", 1, 12.345, 17.89) == "movie@@@001_12345ms-17890ms.gif"
```

- [ ] **Step 2: Verify the tests fail**

Run: `uv run pytest tests/test_candidate_review_layout.py tests/test_adaptive_filename.py -q`

Expected: FAIL because the shortcut script and filename helper do not exist.

- [ ] **Step 3: Implement the shortcut script and filename helper**

```python
REVIEW_SHORTCUTS_JS = """(() => {
  const keys = {'1': 'like-btn', '2': 'neutral-btn', '3': 'dislike-btn', '4': 'favorite-btn'};
  document.addEventListener('keydown', event => {
    const active = document.activeElement;
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(active?.tagName) || active?.isContentEditable) return;
    const button = document.querySelector(`#${keys[event.key]} button`);
    if (button) { event.preventDefault(); button.click(); }
  });
})();"""
```

- [ ] **Step 4: Verify focused tests pass**

Run: `uv run pytest tests/test_candidate_review_layout.py tests/test_adaptive_filename.py -q`

Expected: PASS.

### Task 2: Add Favorite persistence and API

**Files:**
- Modify: `app/services/preference_schema.py`
- Modify: `app/services/preference_types.py`
- Modify: `app/routers/candidates.py`
- Test: `tests/test_candidates_api.py`

- [ ] **Step 1: Write a failing database/API test**

```python
response = candidates_router.favorite_candidate("cand-1")
row = conn.execute("SELECT full_path FROM favorite_gifs WHERE candidate_id='cand-1'").fetchone()
assert response["status"] == "favorited"
assert row["full_path"] == expected_path
```

- [ ] **Step 2: Verify it fails**

Run: `uv run pytest tests/test_candidates_api.py -q`

Expected: FAIL because `favorite_gifs` and the endpoint do not exist.

- [ ] **Step 3: Implement schema and endpoint**

Create `favorite_gifs(favorite_id, candidate_id UNIQUE, full_path UNIQUE, created_at)`; add `favorited` candidate status; insert idempotently, update candidate status, and return the full path.

- [ ] **Step 4: Verify focused API tests pass**

Run: `uv run pytest tests/test_candidates_api.py -q`

Expected: PASS.

### Task 3: Wire Favorite button and millisecond export names

**Files:**
- Modify: `app/ui/candidate_review.py`
- Modify: `scripts/test_video_adaptive.py`
- Modify: `app/routers/candidates.py`
- Test: `tests/test_candidate_review_auto_advance.py`

- [ ] **Step 1: Write failing UI/action tests**

```python
assert candidate_review.favorite_candidate("cand-1", "D:/gif.gif").startswith("Rated: favorited")
assert "Favorite" in candidate_review.SKIP_BUTTON_LABEL
```

- [ ] **Step 2: Verify it fails**

Run: `uv run pytest tests/test_candidate_review_auto_advance.py -q`

Expected: FAIL because the UI still submits Skip.

- [ ] **Step 3: Implement Favorite flow**

Rename the button to `Favorite`, post to `/api/candidates/{id}/favorite`, and route its successful response through `rate_and_advance` so the next candidate loads.

- [ ] **Step 4: Implement millisecond export naming and backwards-compatible parsing**

Use `round(seconds * 1000)` for filenames and convert parsed `ms` values back to seconds for candidate metadata; retain the legacy `s` parser.

- [ ] **Step 5: Verify focused tests pass**

Run: `uv run pytest tests/test_candidate_review_auto_advance.py tests/test_adaptive_filename.py -q`

Expected: PASS.

### Task 4: Verify and package safely

**Files:**
- Modify: `dist/GifAgentUI/GifAgentUI.exe` after tests pass.

- [ ] **Step 1: Run complete tests**

Run: `uv run pytest tests/ -q`

Expected: PASS.

- [ ] **Step 2: Build a temporary EXE**

Run PyInstaller to `dist/_build_<timestamp>` and verify the output hash.

- [ ] **Step 3: Replace only the EXE**

Stop the GUI if needed, copy the new executable, and verify the runtime database remains intact.

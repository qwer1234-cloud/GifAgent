# Review Undo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans (recommended) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Ctrl+Z to undo the most recent GIF review action while preserving an auditable event history.

**Architecture:** Preference events gain nullable undo metadata and the candidate status before the action. Undo marks the latest active candidate review event as undone, restores the prior status, and removes its Favorite record when applicable. The Review UI exposes a guarded Ctrl+Z listener and refreshes the current gallery after the API response.

**Tech Stack:** Python, SQLite, FastAPI, Gradio JavaScript, pytest, PyInstaller.

## Global Constraints

- Ctrl+Z is ignored in input, textarea, select, and contenteditable elements.
- Undo changes state by marking the event undone; it never deletes the event row.
- Favorite undo removes only the associated `favorite_gifs` row and restores the prior candidate status.
- A second undo with no active prior action returns a harmless “nothing to undo” response.
- Existing user data and exports are preserved during packaging.

---

### Task 1: Add failing undo service tests

**Files:**
- Modify: `tests/test_preference_events.py`
- Create: `tests/test_candidate_review_undo.py`

- [ ] **Step 1: Write failing tests**

```python
event = service.record_feedback(..., rating="like")
result = service.undo_last_candidate_action()
assert result["event_id"] == event.event_id
assert conn.execute("SELECT undone_at FROM preference_events WHERE event_id=?", (event.event_id,)).fetchone()[0]
```

- [ ] **Step 2: Verify the tests fail**

Run: `uv run pytest tests/test_preference_events.py tests/test_candidate_review_undo.py -q`

Expected: FAIL because undo metadata and the undo method do not exist.

- [ ] **Step 3: Implement migration and undo method**

Add `previous_status`, `undone_at`, and `undone_reason` columns through the existing migration path. Record the previous status on every candidate event, and implement `undo_last_candidate_action()` to mark the latest active event undone, restore status, and delete a matching Favorite row.

- [ ] **Step 4: Verify focused tests pass**

Run: `uv run pytest tests/test_preference_events.py tests/test_candidate_review_undo.py -q`

Expected: PASS.

### Task 2: Add undo API and guarded Ctrl+Z

**Files:**
- Modify: `app/routers/candidates.py`
- Modify: `app/ui/candidate_review.py`
- Test: `tests/test_candidates_api.py`, `tests/test_candidate_review_layout.py`

- [ ] **Step 1: Write failing endpoint/UI tests**

```python
response = candidates_router.undo_last_action()
assert response["status"] == "undone"
assert "ctrlKey" in REVIEW_SHORTCUTS_JS
```

- [ ] **Step 2: Verify tests fail**

Run: `uv run pytest tests/test_candidates_api.py tests/test_candidate_review_layout.py -q`

Expected: FAIL because the endpoint and shortcut are absent.

- [ ] **Step 3: Implement endpoint and UI refresh**

Add `/api/candidates/undo-last`, add `Undo Last (Ctrl+Z)` to Review, and refresh the current folder/page after a successful undo. The shortcut must call the button only when Ctrl+Z is pressed outside editable fields.

- [ ] **Step 4: Verify focused tests pass**

Run: `uv run pytest tests/test_candidates_api.py tests/test_candidate_review_layout.py -q`

Expected: PASS.

### Task 3: Verify and package

**Files:**
- Modify: `dist/GifAgentUI/GifAgentUI.exe` after all tests pass.

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/ -q`

Expected: PASS.

- [ ] **Step 2: Build and replace only the executable**

Build to `dist/_build_<timestamp>`, copy only the EXE, verify the runtime database hash and startup health.

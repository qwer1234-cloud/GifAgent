# Preference Export Weighting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Export GIF candidates using an equal 50/50 blend of VLM worthiness and the published preference-profile score.

**Architecture:** `PreferenceReranker` will expose a normalized preference score and blend it with the base score using explicit weights. The adaptive exporter will score every deduplicated candidate before sorting and applying the output cap. The configuration screen will persist the two weights under `preference_memory`.

**Tech Stack:** Python, NumPy, SQLite, Gradio, YAML, pytest.

## Global Constraints

- Default `base_score_weight` and `preference_score_weight` are both `0.50`.
- A missing profile, embedding, or reranking failure must retain the original VLM score.
- The published preference profile is the only preference source used for export ranking.
- Packaging replaces only `dist/GifAgentUI/GifAgentUI.exe`; runtime data and exports remain untouched.

---

### Task 1: Add explicit weighted blend to the reranker

**Files:**
- Modify: `app/services/reranker.py`
- Test: `tests/test_preference_reranker.py`

**Interfaces:**
- Consumes: base score in `[0, 1]`, published preference centroids, and non-negative blend weights.
- Produces: `ScoreBreakdown` with `profile_score` normalized to `[0, 1]` and `final_score = base_weight * base + preference_weight * profile`.

- [ ] **Step 1: Write the failing test**

```python
score = reranker.score(
    candidate_vector=liked_centroid,
    base_rag_similarity=0.20,
    scenario_keys=[],
    profile_version=None,
    enabled=True,
    base_score_weight=0.50,
    preference_score_weight=0.50,
)
assert score["final_score"] == pytest.approx(
    0.50 * score["base_rag_similarity"] + 0.50 * score["profile_score"]
)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_preference_reranker.py::test_reranker_blends_base_and_preference_scores_equally -q`

Expected: FAIL because `score` does not accept the two explicit weights.

- [ ] **Step 3: Write minimal implementation**

```python
def score(..., base_score_weight: float = 0.50,
          preference_score_weight: float = 0.50) -> ScoreBreakdown:
    # return baseline unchanged when no published preference signal exists
    # otherwise normalize the profile signal to [0, 1] and blend it.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_preference_reranker.py -q`

Expected: PASS.

### Task 2: Rank the full export candidate pool with the blend

**Files:**
- Modify: `scripts/test_video_adaptive.py`
- Test: `tests/test_adaptive_preference_ranking.py`

**Interfaces:**
- Consumes: deduplicated clips containing `gif_worthiness`, captions, and optional emotion keys.
- Produces: a ranked list sorted by `final_score` before the configured Top-N selection.

- [ ] **Step 1: Write the failing test**

```python
ranked = rank_clips_for_export(
    clips=[{"gif_worthiness": 0.90}, {"gif_worthiness": 0.70}],
    score_clip=lambda clip: 0.50 if clip["gif_worthiness"] == 0.90 else 0.95,
)
assert [clip["gif_worthiness"] for clip in ranked] == [0.70, 0.90]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_adaptive_preference_ranking.py -q`

Expected: FAIL because the exporter has no testable pre-selection ranking helper.

- [ ] **Step 3: Write minimal implementation**

```python
def rank_clips_for_export(clips, score_clip):
    for clip in clips:
        clip["final_score"] = score_clip(clip)
    return sorted(clips, key=lambda clip: clip["final_score"], reverse=True)
```

- [ ] **Step 4: Run tests to verify selection semantics**

Run: `uv run pytest tests/test_adaptive_preference_ranking.py -q`

Expected: PASS; the preference-favored clip precedes a higher raw-score clip.

### Task 3: Expose and persist 50/50 configuration

**Files:**
- Modify: `configs/models.yaml`
- Modify: `dist/GifAgentUI/configs/models.yaml`
- Modify: `app/ui/candidate_review.py`
- Test: `tests/test_config_help_annotations.py`

**Interfaces:**
- Consumes: `preference_memory.base_score_weight` and `preference_memory.preference_score_weight` from YAML.
- Produces: two GUI config fields with tooltip icons and round-tripped YAML values.

- [ ] **Step 1: Write failing config-field coverage**

```python
assert "preference_memory.base_score_weight" in CONFIG_FIELD_KEYS
assert "preference_memory.preference_score_weight" in CONFIG_FIELD_KEYS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config_help_annotations.py -q`

Expected: FAIL because the new fields and tooltip help are absent.

- [ ] **Step 3: Implement YAML and GUI round trip**

```yaml
preference_memory:
  enabled: true
  base_score_weight: 0.50
  preference_score_weight: 0.50
```

- [ ] **Step 4: Run config tests**

Run: `uv run pytest tests/test_config_help_annotations.py -q`

Expected: PASS.

### Task 4: Verify and package safely

**Files:**
- Modify: packaged `dist/GifAgentUI/GifAgentUI.exe` only after tests pass.

- [ ] **Step 1: Run the complete test suite**

Run: `uv run pytest tests/ -q`

Expected: PASS.

- [ ] **Step 2: Build into a temporary distribution directory**

Run: `.\\.venv\\Scripts\\python.exe -m PyInstaller --noconfirm --distpath dist/_build_<timestamp> build_exe.spec`

Expected: a new temporary `GifAgentUI.exe`.

- [ ] **Step 3: Preserve history while updating the executable**

Stop the GUI process if it holds the executable, copy only the EXE to `dist/GifAgentUI/GifAgentUI.exe`, and verify that `dist/GifAgentUI/data/library.db` has not changed.

- [ ] **Step 4: Commit implementation files**

```bash
git add app/services/reranker.py app/ui/candidate_review.py scripts/test_video_adaptive.py configs/models.yaml dist/GifAgentUI/configs/models.yaml tests/
git commit -m "feat: blend export scores with preference memory"
```

# Agent.md — GifAgent Project Reference

> Quick-reference for AI agents working on this codebase. Read this first.

## What This Project Does

Local movie-scene GIF auto-tagging and preference-mining agent. Scans GIFs/videos → VLM frame analysis → LLM synthesis → FAISS vector index → RAG-enhanced clip discovery → candidate GIF export → human feedback → preference profile building.

## Current Model Stack

| Role | Model | Provider | Endpoint |
|------|-------|----------|----------|
| VLM | `llava:13b` | Ollama (local) | `http://localhost:11434` |
| LLM | `deepseek-v4-flash` | DeepSeek (cloud) | `https://api.deepseek.com/v1` |
| Embedding | `nomic-embed-text:latest` | Ollama (local) | `http://localhost:11434` |

- **LLM is cloud-based, handles NSFW content without rejection** (tested 3/3 success on adult content)
- API key env var: `DEEPSEEK_API_KEY` (sk- prefix, DeepSeek native key)
- LLM provider is `openai_compatible` (NOT anthropic_compatible — that was the old Ark setup)

## Key Parameters (scripts/test_video_adaptive.py)

```python
SAMPLE_INTERVAL = 10          # coarse sampling every N seconds
MERGE_GAP = 12                # max gap to merge adjacent frames
MERGE_SCORE_THRESHOLD = 0.55  # only merge when BOTH frames >= this
WORTHINESS_THRESHOLD = 0.2    # min score to keep a frame
REFINE_THRESHOLD = 0.5        # min score to trigger refinement sampling
REFINE_RADIUS = 20            # ±seconds around high-score frame
REFINE_INTERVAL = 10          # fine sampling interval
OUTPUT_RATIO = 1.0            # fraction of clips to export (1.0 = all)
MAX_OUTPUT = 0                # 0 = no cap
VLM_OPTIONS = {"temperature": 0.65, "top_p": 0.95, "top_k": 60}
```

**Score-gated merge logic**: adjacent frames merge only when `gap <= MERGE_GAP` AND both frames' `gif_worthiness >= MERGE_SCORE_THRESHOLD`. This produces a mix of long multi-frame GIFs (sustained good moments) + short single-frame GIFs (isolated moments).

## How to Run

### Prerequisites
```bash
ollama pull llava:13b
ollama pull nomic-embed-text:latest
export DEEPSEEK_API_KEY=sk-...     # DeepSeek native API key
```

### Batch video processing (main workflow)
```bash
# Single video
uv run python scripts/test_video_adaptive.py --video <path>

# Batch with checkpoint resume
uv run python scripts/test_video_batch.py --dir "<video_dir>" --extensions ".ts,.mp4,.mkv"
```

Output structure: `data/exports/adaptive_test/{input_folder_name}/{video_name}@@@{seq}_{start}s-{end}s.gif`

### Packaged GUI

```bash
uv run pyinstaller --noconfirm build_exe.spec
```

Output: `dist/GifAgentUI/GifAgentUI.exe`. If an older packaged GUI is running,
stop it before rebuilding because it holds `dist/GifAgentUI/data/library.db`.
Preserve `dist/GifAgentUI/data/` when replacing the package; it contains the
local runtime DB and the `exports` junction.

### Services
```bash
# FastAPI (port 8000)
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# Gradio candidate review UI (port 7861)
uv run python app/ui/candidate_review.py

# Gradio original review UI (port 7860)
uv run python app/ui/review.py
```

## Architecture Overview

```
app/
├── main.py                    # FastAPI app (19 endpoints)
├── routers/
│   ├── candidates.py          # GET /api/candidates/folders, GET /api/candidates, POST /api/candidates/{id}/feedback
│   └── preference.py          # profile build/publish/evaluate endpoints
├── services/
│   ├── llm_client.py          # Shared LLM client (Ollama + OpenAI + Anthropic compatible)
│   ├── json_guard.py          # Unified JSON parser (strips <think>, markdown fence)
│   ├── quality.py             # Placeholder detection + Pydantic validation
│   ├── video_fingerprint.py   # Duration + keyframe pHash dedup (pre-processing)
│   ├── preference_*.py        # Preference Memory subsystem (6 tables)
│   ├── reranker.py            # Score-gated preference reranker
│   └── candidates.py          # Candidate GIF materialization
├── ui/
│   ├── candidate_review.py    # Gradio UI: review + batch control (port 7861)
│   └── review.py              # Gradio UI: original GIF review (port 7860)
scripts/
├── test_video_adaptive.py     # Core: adaptive GIF extraction (4 phases)
├── test_video_batch.py        # Batch wrapper with checkpoint
├── pipeline_stage2.py         # Post-VLM LLM synthesis + FAISS rebuild
└── vlm_loop.py                # Production VLM processing loop
configs/
└── models.yaml                # Main config (models, paths, preference_memory flag)
```

## test_video_adaptive.py — 4 Phases

1. **Probe + sample**: ffprobe duration → sample at SAMPLE_INTERVAL → dark filter
2. **VLM scoring**: llava:13b scores each frame (0.0-1.0) → refinement around high-score regions
3. **RAG + LLM synthesis**: FAISS search per clip → DeepSeek synthesizes summary/tags (non-fatal)
3.5. **9-grid thumbnail**: select top-9 scored frames with pHash dedup (Hamming > 10) → export 9 individual JPEGs + 3x3 grid to `Sample/` subfolder
4. **GIF export**: ffmpeg palette two-pass, ranked by gif_worthiness

**Non-fatal LLM**: if LLM fails, GIFs still export. Synthesis metadata is skipped.

## Preference Memory Subsystem

6 tables: `candidate_gifs`, `candidate_vectors`, `preference_events`, `preference_profile_builds`, `preference_profiles`, `preference_profile_current`

Flow: candidate materialize → human feedback (like/dislike/neutral/skip) → profile build (7 gates) → holdout evaluation → explicit publish → reranker (behind `preference_memory.enabled` flag, default false)

### Candidate Review UI (2026-07-04)

- `GET /api/candidates` is paginated and filtered server-side. Defaults:
  `status=candidate`, `limit=24`, `offset=0`; callers can use `status=all`
  and pass `folder` for exact-folder review.
- `GET /api/candidates/folders` discovers recursive candidate folders under a
  selected root directory and returns per-folder totals, missing counts, and
  status counts.
- The Review tab does not auto-load every candidate on open. Users first choose
  a data root, click `Load Folders`, then choose the exact folder to review from
  the recursive folder list.
- `candidate_review.py` uses `PAGE_SIZE=12` for the Gradio gallery.
- Gallery items use cached static thumbnails under `data/thumbs/candidates/`;
  full animated GIFs load only in the selected preview pane.
- Selection uses the current page's `gr.State` item list. Do not reintroduce
  index math against a freshly fetched unfiltered list, or filtered-page clicks
  can rate the wrong candidate.
- Candidate display and feedback require the GIF file to still exist at its
  original `artifact_path`. If the path changed or the file is missing, the API
  returns 409 instead of showing or rating stale data.

## Known Gotchas

1. **`safe_worth()` vs `validate_frame_analysis`**: VLM sometimes returns string labels ("AVERAGE - ...") instead of numbers for `gif_worthiness`. `safe_worth()` handles this, but `validate_frame_analysis()` in quality.py tries `float()` first and can raise. The exception is caught (frame skipped after 3 retries), but it's not clean.

2. **LLM synthesis was silently failing**: `parse_json(raw)` was a NameError — the function is `parse_json_response(raw)` which returns a `JsonParseResult` object (use `.ok` and `.data`). Fixed 2026-07-04.

3. **DeepSeek model name is lowercase**: `deepseek-v4-flash` works, `DeepSeek-V4-Flash` returns 400. The API is OpenAI-compatible, not Anthropic-compatible.

4. **Export dir nesting**: `test_video_batch.py` now nests output as `adaptive_test/{input_folder}/{video}/`. Old runs have flat `adaptive_test/{video}/` structure.

5. **VLM scoring distribution**: llava:13b tends to score everything 0.5-0.7 regardless of temperature. The `temperature=0.65` setting gives the best balance — lower (0.5) makes it too conservative (0 frames above 0.7), higher (1.0) spreads but doesn't help merge count.

6. **Merge count vs threshold**: Lowering `MERGE_SCORE_THRESHOLD` from 0.6 to 0.55 barely increased merges (92→90 clips) because low-score frames (0.3-0.5) interspersed in the timeline break merge chains. The bottleneck is score distribution, not threshold.

7. **Video fingerprint dedup**: `test_video_batch.py` computes a content fingerprint (duration + 5 keyframe pHashes at 10%/30%/50%/70%/90%) for each video before processing. If Hamming distance ≤ 5 vs any already-processed video, it's marked `dedup_skipped` with `duplicate_of` field. Robust to re-encode/container/filename changes; NOT robust to crop/watermark. Stored in `data/batch_checkpoint.json` under each video's `fingerprint` key. Use `--force` to bypass dedup.

## API Endpoints (19 total)

Key ones:
- `GET /api/candidates/folders` - recursive candidate-folder discovery below a
  selected root directory
- `GET /api/candidates` - paginated candidate list (`status`, `limit`, `offset`,
  optional exact `folder`); defaults to `status=candidate`
- `POST /api/candidates/{id}/feedback` — rate (like/neutral/dislike/skip)
- `GET /api/preference/profiles` — list profile builds
- `POST /api/preference/profiles/build` — build new profile
- `POST /api/preference/profiles/{version}/publish` — publish
- `POST /api/preference/evaluate` — holdout eval

## Test Suite

```bash
uv run pytest tests/ -v   # 95 tests, 1 skipped
```

Covers: JSON parsing, placeholder detection, FAISS manifest, reset safety, candidate materialization, feedback events, preference profiles, holdout evaluation, reranker.

## Recent Tuning History (2026-07-04)

| Change | Before | After | Effect |
|--------|--------|-------|--------|
| SAMPLE_INTERVAL | 15 | 10 | +50% sample density |
| MERGE_GAP | 6 | 12 | enables adjacent-frame merge |
| MERGE_SCORE_THRESHOLD | (none) | 0.55 | score-gated merge |
| VLM temperature | default (~0.8) | 0.65 | consistent scoring |
| LLM provider | anthropic_compatible (Ark) | openai_compatible (DeepSeek) | native DeepSeek API |
| parse_json bug | broken | fixed | LLM synthesis works |
| Candidate review loading | full candidate list + full GIF gallery | paged API + cached static thumbnails | much faster Web/GUI review |
| Candidate click mapping | page index remapped into unfiltered list | current-page `gr.State` item list | Like/Dislike updates the clicked candidate |
| **GIF output per 3 videos** | **5** | **306** | **61x increase** |

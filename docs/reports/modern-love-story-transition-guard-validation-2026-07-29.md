# Modern Love Story transition-guard validation — 2026-07-29

## Result

**Partial, time-bounded validation; visual acceptance is not claimed.** The
target film is 7,068.032 seconds (117.8 minutes). A complete direct run would
score 999 VLM frames and projected to take roughly 90–100 minutes for its first
scoring pass alone. It was deliberately stopped after a bounded, successful
sample of 30 scored frames within the agreed time budget. The user's prior
`data/adaptive_test_result.json` was captured and restored after every run.

## Target and command

| Item | Value |
| --- | --- |
| Requested source path | `C:\Users\sunhao\Desktop\ToWatch\现代爱情故事.1991.BD1080p.国英双语中字.mp4` |
| Resolved media file | `C:\Users\sunhao\Desktop\ToWatch\现代爱情故事.1991.BD1080p.国英双语中字.mp4\现代爱情故事.1991.BD1080p.国英双语中字.mp4` |
| Source duration | `7068.032000` seconds |
| Export root | `data\exports\transition_guard_validation` |
| Direct command | `uv run python -u scripts/test_video_adaptive.py --video C:\Users\sunhao\AppData\Local\Temp\transition_guard_modern_love_73d767815d1e4df7b63709557e55482e.mp4 --export-dir data\exports\transition_guard_validation` |
| Reason for hardlink | The requested path is a directory containing the MP4, and this Windows FFmpeg build cannot open the Unicode media path directly. The hardlink references the same source bytes; no source video was copied or modified. |
| Config SHA-256 | `EB5BBE2BCBBF34F010E19F4EF1EB2B7AE4F30D7884F4841192BA2C7633AC7372` (`configs/models.yaml`) |
| Guard algorithm version | Not explicitly versioned in source; validated implementation is `app.services.transition_guard.guard_candidate_window` exercised by `tests/test_transition_guard.py`. |
| VLM endpoint/model | `http://172.27.227.98:11434`, `llava:13b` |

Endpoint preflight passed: `POST /api/generate` for `llava:13b` returned HTTP
200 (about 14.8 seconds including model loading). During the bounded run,
`/api/ps` confirmed that `llava:13b` was resident (10,776,288,950 bytes VRAM).

## Run evidence and data safety

The initial direct run sampled all 1,008 timestamps and retained 999 frames,
then failed with `ERROR: VLM not responding` after 447.529 seconds. Root cause:
direct mode used its module fallback `127.0.0.1` rather than the configured VLM
endpoint. This was fixed before the second run.

The second run used the configured endpoint and reached:

| Metric | Observed value |
| --- | ---: |
| Sample timestamps | 1,008 |
| Frames after brightness filter | 999 |
| Brightness-filter drops | 9 |
| Bounded VLM frames scored | 30 |
| Bounded VLM candidates kept | 22 |
| Average worthiness at frame 30 | 0.51 |
| Target run completion | Intentionally stopped for time budget |

A verified hidden watcher recorded the backup hash
`08289C7CAFC9F971C11FACCB912153842D3DA273D534BDC91047FF3D1A6D1656`, then
captured the current result and restored `data/adaptive_test_result.json` with
the same hash. The stopped run never wrote a target result JSON; its captured
file was the existing 30,918-byte user result.

## Transition and artifact audit

The direct run did not reach candidate merging, transition guarding, or GIF
export. Therefore these target-film values are **not available** and are not
inferred from partial scores:

| Required audit | Status |
| --- | --- |
| Before/after guarded candidate counts | Not reached |
| Hard-cut / soft-transition counts | Not reached |
| Trim / split / drop / unverified counters | Not reached |
| Every final GIF duration <= configured max (10s) | No target GIFs produced |
| Every exported segment duration >= 2s | No target GIFs produced |
| Confirmed boundary outside every final GIF interval | No target GIFs produced |
| Final GIF paths / manifests | None for this partial run |
| Hard-cut thumbnail/contact-sheet review | Not possible: no exported samples |
| Slow-motion retention review | Not possible: no exported samples |

Manual review table:

| Review item | Result | Evidence |
| --- | --- | --- |
| Hard-cut mitigation | Not accepted | Target pipeline did not reach guard/export; synthetic guard regression tests passed. |
| Slow-motion retention | Not accepted | Target pipeline did not reach guard/export; no target candidate/GIF visual sample exists. |

## Source fixes validated by regression

1. Direct mode now resolves and passes the configured `VlmRuntimeConfig` into
   the shared pipeline. VLM lifecycle, initial scoring, refinement, and
   guard-rescoring all use its configured model and base URL rather than an
   ambient localhost fallback.
2. The staged `gif_clip` export applies the shared `build_export_window()` at
   the FFmpeg boundary. This preserves transition decisions while safely
   capping legacy uncapped merged spans before export.

Focused and production-path regressions after these fixes passed: **34 passed**
(`test_adaptive_direct_transition`, transition guard/window/candidate/merge,
adaptive config/help, and the two task-engine production suites). A complete
`uv run pytest -q` completed with **1,029 passed, 11 failed, 2 skipped** in
131.76 seconds. All 11 failures are pre-existing/unrelated
`tests/test_version_manifest.py` failures: the child manifest process decodes
`git` output with the Windows GBK codec, raises `UnicodeDecodeError`, then
dereferences `result.stdout` when it is `None`. No transition-guard test failed.

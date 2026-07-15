# Control Batch Folder Queue and Detailed Status Design

**Date:** 2026-07-13  
**Status:** Approved by user

## Goal

Allow the Control tab to append video folders to a persistent serial queue while a batch is running, and expose a stable batch summary alongside detailed per-video and per-GIF output logs.

## Scope and constraints

- Processing remains strictly serial. Only one batch worker and one adaptive video export run may be active at a time.
- Existing single-folder CLI behavior remains supported.
- The existing adaptive extraction and export algorithms are unchanged; the change adds queue orchestration and observable logging.
- Existing database, exports, labels, checkpoints, and packaged runtime data are preserved.
- The fixed `Batch Status` area remains a concise summary. Detailed output is displayed in a separate scrollable log area.

## Current context

- `app/ui/candidate_review.py` owns the Gradio Control tab and currently starts `scripts/test_video_batch.py` with one `--dir` argument.
- The Control tab redirects the worker's stdout/stderr to `data/batch_subprocess.log`.
- `scripts/test_video_batch.py` processes one directory and invokes `scripts/test_video_adaptive.py` once per video.
- `scripts/test_video_adaptive.py` already prints one export line per ranked GIF, but the UI currently shows only checkpoint totals and does not surface the log file.

## Chosen architecture

### Persistent queue

Create `app/services/batch_queue.py` as the small, testable queue persistence boundary. The queue file is `data/batch_queue.json` and contains ordered jobs:

```json
{
  "jobs": [
    {
      "job_id": "stable-id",
      "directory": "C:/videos/first",
      "limit": 0,
      "extensions": ".mp4,.mkv",
      "added_at": "2026-07-13T00:00:00"
    }
  ],
  "updated_at": "2026-07-13T00:00:00"
}
```

The worker-owned `data/batch_queue_state.json` records the current job and terminal job results. Queue jobs are append-only from the UI perspective; state is written separately by the worker so a folder append cannot overwrite a worker progress update. Writes use a temporary file followed by `os.replace`.

The queue boundary exposes helpers for loading/normalizing jobs, appending a validated directory, identifying pending jobs, recording current/completed/failed jobs, and producing a display-ready ordered summary. These helpers do not touch the database or export files.

### Serial worker

Extend `scripts/test_video_batch.py` with an optional queue-file mode. In queue mode it repeatedly:

1. Reads the first pending job from the queue.
2. Marks that job current in `batch_queue_state.json`.
3. Runs the existing single-directory batch logic with that job's directory, limit, and extensions.
4. Records success or failure, then reloads the queue so folders appended during processing are picked up.
5. Exits only when no pending job remains.

The existing `--dir` mode continues to run one directory without consulting a queue. Queue mode writes clear folder boundaries to the same subprocess log, so the UI can distinguish folder, video, and GIF entries.

If a folder fails, the worker records the failure and continues with the next queued folder; the final process status remains a completed-with-failures state.

### Detailed GIF output

Keep the adaptive exporter behavior unchanged and make each attempted GIF export emit a structured human-readable log line containing:

- source video name and GIF ordinal/total;
- output path;
- success or failure;
- worthiness/final score, duration, timestamp, merge type/frame count, and output size when successful.

The existing `data/batch_subprocess.log` remains the durable full log. The Control tab reads it on refresh and displays the complete available text in a scrollable read-only component.

### Control tab layout

The Control tab will contain:

- current folder input and processing options;
- `Start Queue` to add the initial folder and launch the worker when idle;
- `Append Folder` to add another folder without interrupting the current job;
- an ordered queue display showing queued/current/completed/failed folders;
- a fixed `Batch Status` summary showing worker state, PID, current folder, current video, video progress, success/failure totals, and queue progress;
- a separate scrollable `Detailed Output Log` component showing the full subprocess output;
- refresh and stop controls.

When an append arrives while the worker is active, it only updates the queue file. When the worker is idle, adding a folder starts queue mode automatically. A post-write running-state check closes the small race where the previous worker exits while a folder is being appended.

The summary and detailed log are refreshed together by the existing timer and explicit Refresh button. The summary does not get replaced by log text.

## Error handling

- Nonexistent or non-directory paths are rejected before entering the queue.
- Malformed queue/state/log files produce a visible Control error/status message while leaving existing data intact.
- Queue writes are atomic; failed writes do not replace the last valid queue.
- A failed video remains represented by the existing batch checkpoint and its folder job continues according to the queue policy above.
- Stopping the worker leaves queue and checkpoint state available for a later resume.

## Testing strategy

- Unit-test queue normalization, ordered append, pending selection, state transitions, and malformed-file recovery.
- Unit-test Control helpers for append-while-running, idle auto-start, queue display, and stable summary/log composition.
- Unit-test GIF log formatting to verify every export line includes the output path and status fields.
- Add source/layout assertions for the separate fixed `Batch Status` and scrollable detailed log controls.
- Run the focused tests first, then `uv run pytest tests/ -v`.
- Verify packaging inputs and, if a packaged build is produced, preserve `dist/GifAgentUI/data/` and inspect both `GifAgentUI.exe` and `_internal/`.

## Self-review

- Requirement 1 is covered by the persistent ordered queue, append action, and serial worker loop.
- Requirement 2 is covered by the separate fixed summary, full subprocess log display, and per-GIF structured exporter lines.
- No implementation step deletes or resets existing runtime data.
- Single-directory CLI behavior remains an explicit compatibility constraint.

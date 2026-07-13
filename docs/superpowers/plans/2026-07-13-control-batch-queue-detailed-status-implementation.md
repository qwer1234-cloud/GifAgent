# Control Batch Queue and Detailed Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Each task follows a red-green-refactor cycle and ends with an independently testable result.

**Goal:** Add a persistent serial video-folder queue to the Control tab and show a fixed batch summary beside complete per-video/per-GIF output logs.

**Architecture:** Keep the existing single-folder batch logic intact behind a callable entry point, and add an optional queue mode to scripts/test_video_batch.py. The UI appends immutable jobs to data/batch_queue.json; the worker writes progress to the separate data/batch_queue_state.json, avoiding UI/worker write conflicts. scripts/test_video_adaptive.py emits structured GIF export lines, while the Control tab reads the durable subprocess log into a separate scrollable component.

**Tech Stack:** Python 3.11, Gradio, JSON checkpoint files, PyInstaller script data, pytest, existing uv workflow.

## Global Constraints

- Process folders strictly serially; never start concurrent batch/adaptive exports.
- Preserve existing data/ database, exports, labels, checkpoints, and user changes.
- Keep direct scripts/test_video_batch.py --dir <folder> behavior working.
- Use apply_patch for source edits and add tests before production code.
- Use Chinese help/status copy only where user-facing text is introduced; keep existing English Control labels unless the requested feature needs a new label.
- Do not change adaptive clip selection, ranking, deduplication, or export algorithms.

---

### Task 1: Add the persistent ordered queue

**Files:**
- Create: app/services/batch_queue.py
- Test: tests/test_batch_queue.py

**Interfaces:**

~~~python
DEFAULT_QUEUE_FILE = Path("data/batch_queue.json")
DEFAULT_STATE_FILE = Path("data/batch_queue_state.json")

def load_queue(path: str | Path = DEFAULT_QUEUE_FILE) -> dict: ...
def save_queue(queue: dict, path: str | Path = DEFAULT_QUEUE_FILE) -> None: ...
def append_queue_job(
    directory: str,
    limit: int = 0,
    extensions: str = "",
    path: str | Path = DEFAULT_QUEUE_FILE,
) -> dict: ...
def load_queue_state(path: str | Path = DEFAULT_STATE_FILE) -> dict: ...
def save_queue_state(state: dict, path: str | Path = DEFAULT_STATE_FILE) -> None: ...
def pending_jobs(queue: dict, state: dict) -> list[dict]: ...
def update_job_state(state: dict, job_id: str, status: str, **updates) -> dict: ...
def format_queue_status(queue: dict, state: dict) -> str: ...
~~~

- [ ] **Step 1: Write the failing tests**

~~~python
def test_append_queue_job_preserves_order_and_options(tmp_path):
    from app.services.batch_queue import append_queue_job, load_queue

    queue_path = tmp_path / "batch_queue.json"
    first = append_queue_job("C:/videos/one", 0, ".mp4", queue_path)
    second = append_queue_job("C:/videos/two", 3, ".mkv", queue_path)

    queue = load_queue(queue_path)
    assert [job["job_id"] for job in queue["jobs"]] == [first["job_id"], second["job_id"]]
    assert queue["jobs"][1]["directory"] == "C:/videos/two"
    assert queue["jobs"][1]["limit"] == 3
    assert queue["jobs"][1]["extensions"] == ".mkv"


def test_pending_jobs_excludes_completed_and_failed_jobs(tmp_path):
    from app.services.batch_queue import append_queue_job, load_queue, pending_jobs, update_job_state

    queue_path = tmp_path / "batch_queue.json"
    first = append_queue_job("C:/videos/one", path=queue_path)
    second = append_queue_job("C:/videos/two", path=queue_path)
    state = {"status": "running", "current_job_id": None, "jobs": {}}
    update_job_state(state, first["job_id"], "completed")
    update_job_state(state, second["job_id"], "failed", error="exit 1")

    assert pending_jobs(load_queue(queue_path), state) == []


def test_malformed_queue_raises_without_replacing_existing_file(tmp_path):
    from app.services.batch_queue import BatchQueueFormatError, load_queue

    queue_path = tmp_path / "batch_queue.json"
    queue_path.write_text("not-json", encoding="utf-8")

    with pytest.raises(BatchQueueFormatError):
        load_queue(queue_path)
    assert queue_path.read_text(encoding="utf-8") == "not-json"
~~~

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run: uv run pytest tests/test_batch_queue.py -v  
Expected: FAIL because app.services.batch_queue and its queue interfaces do not exist yet.

- [ ] **Step 3: Implement the smallest queue boundary**

Use a normalized payload with jobs: [] and updated_at, a state payload with status, current_job_id, and jobs, and write JSON through a sibling .tmp file followed by os.replace. load_queue must raise BatchQueueFormatError for invalid JSON or non-list jobs; it must not overwrite the malformed file. append_queue_job creates a UUID job id, stores the directory/options/UTC timestamp, appends in order, and returns the new job. pending_jobs returns queue order for state entries whose status is not completed or failed, including an interrupted running job so it can resume.

~~~python
def pending_jobs(queue: dict, state: dict) -> list[dict]:
    terminal = {"completed", "failed"}
    state_jobs = state.get("jobs", {})
    return [
        job for job in queue.get("jobs", [])
        if state_jobs.get(job["job_id"], {}).get("status") not in terminal
    ]
~~~

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: uv run pytest tests/test_batch_queue.py -v  
Expected: PASS for ordering, terminal filtering, atomic persistence, and malformed-file protection.

- [ ] **Step 5: Commit the queue boundary**

~~~powershell
git add app/services/batch_queue.py tests/test_batch_queue.py
git commit -m "feat: add persistent batch folder queue"
~~~

### Task 2: Add serial queue mode to the batch worker

**Files:**
- Modify: scripts/test_video_batch.py
- Create: tests/test_video_batch_queue.py

**Interfaces:**

~~~python
def build_single_batch_command(
    video_dir: str,
    limit: int,
    extensions: str,
) -> list[str]: ...

def run_queue(
    queue_file: str,
    process_job: Callable[[dict], int] | None = None,
) -> int: ...
~~~

- [ ] **Step 1: Write the failing queue-worker tests**

~~~python
def test_run_queue_processes_jobs_in_order_and_continues_after_failure(tmp_path):
    from app.services.batch_queue import append_queue_job, load_queue_state
    from scripts.test_video_batch import run_queue

    queue_path = tmp_path / "batch_queue.json"
    first = append_queue_job("C:/videos/one", path=queue_path)
    second = append_queue_job("C:/videos/two", path=queue_path)
    calls = []

    def fake_process(job):
        calls.append(job["directory"])
        return 1 if job["job_id"] == first["job_id"] else 0

    result = run_queue(str(queue_path), process_job=fake_process)

    assert result == 1
    assert calls == ["C:/videos/one", "C:/videos/two"]
    state = load_queue_state(tmp_path / "batch_queue_state.json")
    assert state["jobs"][first["job_id"]]["status"] == "failed"
    assert state["jobs"][second["job_id"]]["status"] == "completed"


def test_build_single_batch_command_keeps_frozen_and_source_modes_distinct(monkeypatch):
    from scripts import test_video_batch

    monkeypatch.setattr(test_video_batch.sys, "frozen", False, raising=False)
    source_cmd = test_video_batch.build_single_batch_command("C:/videos", 2, ".mp4")
    assert source_cmd[-6:] == ["--dir", "C:/videos", "--limit", "2", "--extensions", ".mp4"]
~~~

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run: uv run pytest tests/test_video_batch_queue.py -v  
Expected: FAIL because queue mode and the command builder are not defined.

- [ ] **Step 3: Refactor the existing single-directory body behind a callable and add queue mode**

Make --dir optional at argparse level, add --queue-file, and reject the invocation only when neither is supplied. Move the current single-directory processing body into run_single_directory(video_dir, limit, extensions, force) -> int without changing its checkpoint or export behavior. main() calls that function for direct mode and calls run_queue() for queue mode.

run_queue() loads data/batch_queue_state.json beside the queue file, marks each selected job running, prints a [QUEUE] folder boundary with flush=True, runs the existing single-directory command, records completed or failed, and then reloads the queue before selecting the next pending job. It uses an injectable process_job in tests; production uses subprocess.run(build_single_batch_command(...), cwd="."). It continues after a failed folder and returns 1 if any folder failed.

- [ ] **Step 4: Run the focused tests and the existing batch checkpoint tests**

Run: uv run pytest tests/test_video_batch_queue.py tests/test_video_batch_checkpoint.py -v  
Expected: PASS; existing checkpoint normalization and discovery behavior remains unchanged.

- [ ] **Step 5: Commit the serial worker**

~~~powershell
git add scripts/test_video_batch.py tests/test_video_batch_queue.py
git commit -m "feat: process appended batch folders serially"
~~~

### Task 3: Emit structured per-GIF logs and expose the log reader

**Files:**
- Create: app/services/batch_logging.py
- Modify: scripts/test_video_adaptive.py
- Test: tests/test_batch_logging.py

**Interfaces:**

~~~python
def format_gif_export_line(
    *,
    video_name: str,
    index: int,
    total: int,
    output_path: str,
    status: str,
    worthiness: float,
    duration_s: float,
    timestamp_s: int,
    merged: bool,
    frame_count: int,
    size_bytes: int = 0,
    emotional_core: str = "?",
) -> str: ...

def read_batch_log(path: str | Path) -> str: ...
~~~

- [ ] **Step 1: Write the failing logging tests**

~~~python
def test_format_gif_export_line_contains_video_gif_path_and_result():
    from app.services.batch_logging import format_gif_export_line

    line = format_gif_export_line(
        video_name="VideoA", index=1, total=3,
        output_path="data/exports/VideoA@@@001.gif", status="OK",
        worthiness=0.91, duration_s=4.5, timestamp_s=27,
        merged=True, frame_count=2, size_bytes=2048,
        emotional_core="laugh",
    )

    assert "[GIF 1/3]" in line
    assert "VideoA" in line
    assert "OK" in line
    assert "data/exports/VideoA@@@001.gif" in line
    assert "score=0.91" in line
    assert "size=2KB" in line


def test_read_batch_log_returns_full_utf8_content(tmp_path):
    from app.services.batch_logging import read_batch_log

    path = tmp_path / "batch_subprocess.log"
    path.write_text("[VIDEO] A\n[GIF 1/1] OK: 你好.gif\n", encoding="utf-8")

    assert read_batch_log(path) == "[VIDEO] A\n[GIF 1/1] OK: 你好.gif\n"
~~~

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run: uv run pytest tests/test_batch_logging.py -v  
Expected: FAIL because the structured formatter and reader do not exist.

- [ ] **Step 3: Implement the formatter/reader and use them in the adaptive exporter**

Format a stable line such as:

~~~text
[GIF 1/3] VideoA OK path=data/exports/VideoA@@@001.gif score=0.91 dur=4.5s t=27s [merged:2fr] size=2KB emotion=laugh
~~~

In the export loop, retain both ffmpeg calls and palette cleanup, then print one OK line when the GIF exists and one FAILED line when it does not. Include the output path in both cases and flush the line immediately. Do not change clip ranking or bookmark behavior.

- [ ] **Step 4: Run the logging tests and syntax-check the exporter**

Run: uv run pytest tests/test_batch_logging.py -v and uv run python -m py_compile app/services/batch_logging.py scripts/test_video_adaptive.py  
Expected: PASS with no syntax errors.

- [ ] **Step 5: Commit structured GIF logging**

~~~powershell
git add app/services/batch_logging.py scripts/test_video_adaptive.py tests/test_batch_logging.py
git commit -m "feat: log every adaptive GIF export"
~~~

### Task 4: Add Control queue actions, fixed summary, and detailed log display

**Files:**
- Modify: app/ui/candidate_review.py
- Create: tests/test_candidate_review_batch_queue.py
- Modify: tests/test_batch_process_status.py
- Modify: tests/test_candidate_review_layout.py

**Interfaces:**

~~~python
def append_batch_directory(
    video_dir: str,
    limit: int = 0,
    extensions: str = "",
) -> tuple[str, str]: ...

def format_batch_status(status: dict) -> str: ...

def refresh_batch_status() -> tuple[str, str, str]: ...
~~~

- [ ] **Step 1: Write the failing UI-helper and layout tests**

~~~python
def test_append_batch_directory_adds_to_running_queue(monkeypatch, tmp_path):
    from app.ui import candidate_review

    monkeypatch.setattr(candidate_review, "get_batch_status", lambda: {"running": True})
    monkeypatch.setattr(candidate_review, "append_queue_job", lambda directory, limit, extensions: {
        "job_id": "job-2", "directory": directory, "limit": limit, "extensions": extensions,
    })
    monkeypatch.setattr(candidate_review, "load_queue", lambda: {
        "jobs": [{"job_id": "job-2", "directory": str(tmp_path)}]
    })
    monkeypatch.setattr(candidate_review, "load_queue_state", lambda: {
        "status": "running", "current_job_id": "job-1", "jobs": {}
    })

    message, queue_text = candidate_review.append_batch_directory(str(tmp_path), 0, ".mp4")

    assert "Queued" in message
    assert str(tmp_path) in queue_text


def test_format_batch_status_keeps_summary_fields_separate_from_log():
    from app.ui.candidate_review import format_batch_status

    text = format_batch_status({
        "running": True, "pid": 123, "current_folder": "C:/videos/A",
        "current_video": "clip-01", "completed": 2, "failed": 1,
        "total": 4, "queue_completed": 1, "queue_total": 3,
        "gpu_model": "llava:13b",
    })

    assert "Running: YES" in text
    assert "Current Folder: C:/videos/A" in text
    assert "Current Video: clip-01" in text
    assert "Queue: 1/3" in text
    assert "GIF" not in text


def test_control_layout_declares_fixed_summary_and_detailed_log():
    from pathlib import Path
    from app.ui import candidate_review

    source = Path(candidate_review.__file__).read_text(encoding="utf-8")
    assert 'label="Batch Status"' in source
    assert 'label="Detailed Output Log"' in source
    assert 'elem_id="batch-status"' in source
    assert 'elem_id="batch-log"' in source
~~~

- [ ] **Step 2: Run the focused UI tests and verify the expected failure**

Run: uv run pytest tests/test_candidate_review_batch_queue.py tests/test_batch_process_status.py tests/test_candidate_review_layout.py -v  
Expected: FAIL because the new queue helpers and Control components do not exist.

- [ ] **Step 3: Implement the Control integration**

Add queue/state/log path constants and import the queue/log helpers. append_batch_directory rejects blank/non-directory paths, appends a job with the current limit/extensions, starts queue mode when no valid worker is running, and returns a message plus display text. After writing a job, re-check the worker state; if the old worker exited during the append, start the queue worker so the new job cannot remain stranded.

Extend get_batch_status() with current folder and queue totals from the queue state, while retaining the existing checkpoint-derived video totals and GPU check. Implement format_batch_status() as the fixed summary formatter and refresh_batch_status() as (summary, queue_display, read_batch_log(LOG_FILE)).

Replace the single Control status_text output with:

~~~python
status_text = gr.Textbox(
    label="Batch Status", interactive=False, lines=9, elem_id="batch-status",
    value="Loading...",
)
queue_text = gr.Textbox(
    label="Folder Queue", interactive=False, lines=8, elem_id="batch-queue",
)
log_text = gr.Textbox(
    label="Detailed Output Log", interactive=False, lines=24, elem_id="batch-log",
)
~~~

Wire the timer, Refresh, Start Queue, Append Folder, and Stop events so summary, queue, and log are updated independently. Start Queue adds the initial folder and launches the queue worker; Append Folder adds without interrupting a running worker. Keep Batch Status as its own summary component and keep the detailed log in the separate scrollable textbox.

- [ ] **Step 4: Run focused UI/status tests and the existing candidate-review tests**

Run: uv run pytest tests/test_candidate_review_batch_queue.py tests/test_batch_process_status.py tests/test_candidate_review_layout.py tests/test_candidate_review_auto_advance.py -v  
Expected: PASS, including the existing review/profile/layout behavior.

- [ ] **Step 5: Commit the Control integration**

~~~powershell
git add app/ui/candidate_review.py tests/test_candidate_review_batch_queue.py tests/test_batch_process_status.py tests/test_candidate_review_layout.py
git commit -m "feat: add Control batch queue and detailed status"
~~~

### Task 5: Verify the complete change and packaged inputs

**Files:**
- Inspect: build_exe.spec
- Inspect: dist/GifAgentUI/GifAgentUI.exe and dist/GifAgentUI/_internal/ only if a packaged build is available or rebuilt

- [ ] **Step 1: Run all focused feature tests together**

Run:

~~~powershell
uv run pytest tests/test_batch_queue.py tests/test_video_batch_queue.py tests/test_batch_logging.py tests/test_candidate_review_batch_queue.py -v
~~~

Expected: PASS for queue persistence, serial worker behavior, per-GIF logging, and Control integration.

- [ ] **Step 2: Run the full project test suite**

Run: uv run pytest tests/ -v  
Expected: all existing and new tests pass; any pre-existing unrelated failure is recorded with its exact output.

- [ ] **Step 3: Run static checks without mutating runtime data**

Run:

~~~powershell
uv run python -m py_compile app/services/batch_queue.py app/services/batch_logging.py app/ui/candidate_review.py scripts/test_video_batch.py scripts/test_video_adaptive.py
git diff --check
~~~

Expected: no syntax errors and no whitespace errors.

- [ ] **Step 4: Verify packaging coverage**

Confirm build_exe.spec continues to include datas += [("scripts", "scripts")] and collect_submodules("app"), so the new script/module are included without deleting dist/GifAgentUI/data/. If rebuilding is explicitly needed, stop any running packaged GUI first, run uv run pyinstaller --noconfirm build_exe.spec, and inspect both the EXE and _internal/ for the new queue/log code while preserving the existing data directory.

- [ ] **Step 5: Report the result with exact files and test evidence**

Include the new queue/status behavior, the focused and full-suite test commands/results, and any packaging verification performed. Do not report success until the tests and file checks have completed.


import subprocess
import sys

import pytest


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


def test_pending_jobs_includes_running_job(tmp_path):
    from app.services.batch_queue import append_queue_job, load_queue, pending_jobs, update_job_state

    queue_path = tmp_path / "batch_queue.json"
    job = append_queue_job("C:/videos/running", path=queue_path)
    state = {"status": "running", "current_job_id": job["job_id"], "jobs": {}}
    update_job_state(state, job["job_id"], "running")

    assert pending_jobs(load_queue(queue_path), state) == [job]


def test_save_queue_state_persists_required_fields(tmp_path):
    from app.services.batch_queue import load_queue_state, save_queue_state

    state_path = tmp_path / "batch_queue_state.json"
    save_queue_state({"jobs": {"job-1": {"status": "running"}}}, state_path)

    assert load_queue_state(state_path) == {
        "status": "idle",
        "current_job_id": None,
        "jobs": {"job-1": {"status": "running"}},
    }


def test_malformed_queue_raises_without_replacing_existing_file(tmp_path):
    from app.services.batch_queue import BatchQueueFormatError, load_queue

    queue_path = tmp_path / "batch_queue.json"
    queue_path.write_text("not-json", encoding="utf-8")

    with pytest.raises(BatchQueueFormatError):
        load_queue(queue_path)
    assert queue_path.read_text(encoding="utf-8") == "not-json"


def test_worker_lease_blocks_another_process_until_released(tmp_path):
    from app.services.batch_queue import WorkerLease

    lease_path = tmp_path / "batch_worker.lock"
    owner = WorkerLease(lease_path, mode="queue")
    owner.acquire()
    script = """
import sys
from app.services.batch_queue import WorkerLease, WorkerLeaseBusyError
lease = WorkerLease(sys.argv[1], mode='direct')
try:
    lease.acquire()
except WorkerLeaseBusyError:
    print('busy')
else:
    print('acquired')
    lease.release()
"""
    try:
        blocked = subprocess.run(
            [sys.executable, "-c", script, str(lease_path)],
            capture_output=True,
            text=True,
            check=True,
        )
    finally:
        owner.release()

    acquired = subprocess.run(
        [sys.executable, "-c", script, str(lease_path)],
        capture_output=True,
        text=True,
        check=True,
    )

    assert blocked.stdout.strip() == "busy"
    assert acquired.stdout.strip() == "acquired"


def test_queue_state_preserves_launch_and_error_metadata(tmp_path):
    from app.services.batch_queue import load_queue_state, save_queue_state

    state_path = tmp_path / "batch_queue_state.json"
    expected = {
        "status": "running",
        "current_job_id": "job-1",
        "jobs": {"job-1": {"status": "running"}},
        "worker_pid": 321,
        "launch_token": "launch-1",
        "launcher_pid": 123,
        "cleanup_pending": True,
        "last_error": "cleanup failed",
        "custom_future_field": {"keep": True},
    }

    save_queue_state(expected, state_path)

    assert load_queue_state(state_path) == expected


def test_format_queue_status_includes_worker_state_and_persisted_error():
    from app.services.batch_queue import format_queue_status

    text = format_queue_status(
        {"jobs": []},
        {
            "status": "starting",
            "worker_pid": 444,
            "cleanup_pending": True,
            "last_error": "PID write failed",
            "jobs": {},
        },
    )

    assert "Worker: starting" in text
    assert "PID: 444" in text
    assert "Cleanup pending: YES" in text
    assert "Last error: PID write failed" in text


def test_concurrent_process_appends_preserve_every_job(tmp_path):
    from app.services.batch_queue import load_queue

    queue_path = tmp_path / "batch_queue.json"
    script = """
import sys
from app.services.batch_queue import append_queue_job
append_queue_job(sys.argv[2], path=sys.argv[1])
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(queue_path), f"C:/videos/{index}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(12)
    ]
    results = [process.communicate(timeout=10) + (process.returncode,) for process in processes]

    assert all(returncode == 0 for _stdout, _stderr, returncode in results), results
    assert {job["directory"] for job in load_queue(queue_path)["jobs"]} == {
        f"C:/videos/{index}" for index in range(12)
    }


def test_append_queue_job_rejects_duplicate_active_directory(tmp_path):
    from app.services.batch_queue import (
        DuplicateQueueJobError,
        append_queue_job,
        load_queue,
    )

    queue_path = tmp_path / "batch_queue.json"
    directory = tmp_path / "videos"
    directory.mkdir()
    first = append_queue_job(str(directory), path=queue_path)

    with pytest.raises(DuplicateQueueJobError) as exc_info:
        append_queue_job(str(directory) + "\\", path=queue_path)

    assert exc_info.value.existing_job["job_id"] == first["job_id"]
    assert len(load_queue(queue_path)["jobs"]) == 1


def test_append_queue_job_allows_requeue_after_terminal_job(tmp_path):
    from app.services.batch_queue import (
        append_queue_job,
        load_queue,
        load_queue_state,
        save_queue_state,
        update_job_state,
    )

    queue_path = tmp_path / "batch_queue.json"
    state_path = tmp_path / "batch_queue_state.json"
    directory = tmp_path / "videos"
    directory.mkdir()
    first = append_queue_job(str(directory), path=queue_path, state_path=state_path)
    state = load_queue_state(state_path)
    update_job_state(state, first["job_id"], "completed")
    save_queue_state(state, state_path)

    second = append_queue_job(str(directory), path=queue_path, state_path=state_path)

    assert second["job_id"] != first["job_id"]
    assert len(load_queue(queue_path)["jobs"]) == 2

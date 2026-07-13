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

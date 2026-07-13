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

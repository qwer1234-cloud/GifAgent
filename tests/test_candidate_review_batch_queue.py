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


def test_append_batch_directory_restarts_queue_when_worker_exits_during_append(monkeypatch, tmp_path):
    from app.ui import candidate_review

    statuses = iter(({"running": True}, {"running": False}))
    monkeypatch.setattr(candidate_review, "get_batch_status", lambda: next(statuses))
    monkeypatch.setattr(candidate_review, "append_queue_job", lambda *_args: {"job_id": "job-2"})
    monkeypatch.setattr(candidate_review, "load_queue", lambda: {"jobs": []})
    monkeypatch.setattr(candidate_review, "load_queue_state", lambda: {"jobs": {}})
    started = []
    monkeypatch.setattr(candidate_review, "start_batch_queue", lambda **_kwargs: started.append(True) or "started")

    candidate_review.append_batch_directory(str(tmp_path))

    assert started == [True]


def test_append_batch_directory_waits_for_draining_worker_adoption(monkeypatch, tmp_path):
    from app.ui import candidate_review

    monkeypatch.setattr(candidate_review, "get_batch_status", lambda: {"running": True})
    monkeypatch.setattr(candidate_review, "append_queue_job", lambda *_args: {"job_id": "job-2"})
    monkeypatch.setattr(candidate_review, "load_queue", lambda: {"jobs": []})
    states = iter((
        {"status": "draining", "jobs": {}},
        {"status": "running", "jobs": {}},
    ))
    monkeypatch.setattr(candidate_review, "load_queue_state", lambda: next(states))
    sleeps = []
    monkeypatch.setattr(candidate_review.time, "sleep", sleeps.append)
    started = []
    monkeypatch.setattr(candidate_review, "start_batch_queue", lambda **_kwargs: started.append(True) or "started")

    candidate_review.append_batch_directory(str(tmp_path))

    assert sleeps == [candidate_review.DRAINING_POLL_INTERVAL_SECONDS]
    assert started == []


def test_append_batch_directory_starts_one_successor_after_draining_worker_goes_idle(monkeypatch, tmp_path):
    from app.ui import candidate_review

    monkeypatch.setattr(candidate_review, "get_batch_status", lambda: {"running": True})
    monkeypatch.setattr(candidate_review, "append_queue_job", lambda *_args: {"job_id": "job-2"})
    monkeypatch.setattr(candidate_review, "load_queue", lambda: {"jobs": []})
    states = iter((
        {"status": "draining", "jobs": {}},
        {"status": "idle", "jobs": {}},
    ))
    monkeypatch.setattr(candidate_review, "load_queue_state", lambda: next(states))
    monkeypatch.setattr(candidate_review.time, "sleep", lambda _seconds: None)
    started = []
    monkeypatch.setattr(candidate_review, "start_batch_queue", lambda **_kwargs: started.append(True) or "started")

    candidate_review.append_batch_directory(str(tmp_path))

    assert started == [True]


def test_refresh_batch_status_keeps_summary_and_queue_when_log_cannot_be_read(monkeypatch):
    from app.ui import candidate_review

    monkeypatch.setattr(candidate_review, "get_batch_status", lambda: {"running": False})
    monkeypatch.setattr(candidate_review, "load_queue", lambda: {"jobs": []})
    monkeypatch.setattr(candidate_review, "load_queue_state", lambda: {"jobs": {}})

    def locked_log(_path):
        raise PermissionError("log is locked")

    monkeypatch.setattr(candidate_review, "read_batch_log", locked_log)

    summary, queue_text, log_text = candidate_review.refresh_batch_status()

    assert "Running: NO" in summary
    assert "Batch queue (0 jobs)" in queue_text
    assert "Detailed output log unavailable" in log_text
    assert "log is locked" in log_text


def test_append_batch_directory_rejects_blank_or_missing_paths(monkeypatch, tmp_path):
    from app.ui import candidate_review

    appended = []
    monkeypatch.setattr(candidate_review, "append_queue_job", lambda *_args: appended.append(True))

    blank_message, _ = candidate_review.append_batch_directory("   ")
    missing_message, _ = candidate_review.append_batch_directory(str(tmp_path / "missing"))

    assert "Invalid directory" in blank_message
    assert "Invalid directory" in missing_message
    assert appended == []

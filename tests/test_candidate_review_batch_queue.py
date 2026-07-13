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
    monkeypatch.setattr(candidate_review, "_start_batch_queue_locked", lambda **_kwargs: started.append(True) or "started")

    candidate_review.append_batch_directory(str(tmp_path))

    assert started == [True]


def test_append_batch_directory_leaves_draining_worker_to_adopt_job(monkeypatch, tmp_path):
    from app.ui import candidate_review

    monkeypatch.setattr(candidate_review, "get_batch_status", lambda: {"running": True})
    monkeypatch.setattr(candidate_review, "append_queue_job", lambda *_args: {"job_id": "job-2"})
    monkeypatch.setattr(candidate_review, "load_queue", lambda: {"jobs": []})
    monkeypatch.setattr(
        candidate_review,
        "load_queue_state",
        lambda: {"status": "draining", "jobs": {}},
    )
    sleeps = []
    monkeypatch.setattr(candidate_review.time, "sleep", sleeps.append)
    started = []
    monkeypatch.setattr(
        candidate_review,
        "_start_batch_queue_locked",
        lambda: started.append(True) or "Batch queue already draining.",
    )

    candidate_review.append_batch_directory(str(tmp_path))

    assert sleeps == []
    assert started == [True]


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
    monkeypatch.setattr(candidate_review, "_start_batch_queue_locked", lambda **_kwargs: started.append(True) or "started")

    candidate_review.append_batch_directory(str(tmp_path))

    assert started == [True]


def test_append_batch_directory_reclaims_dead_cleanup_claim_and_starts_successor(monkeypatch, tmp_path):
    from app.ui import candidate_review

    state_store = {
        "status": "starting",
        "current_job_id": None,
        "worker_pid": 999,
        "cleanup_pending": True,
        "last_error": "PID persistence failed",
        "jobs": {},
    }
    saved_states = []
    launches = []
    monkeypatch.setattr(candidate_review, "append_queue_job", lambda *_args: {"job_id": "job-2"})
    monkeypatch.setattr(candidate_review, "load_queue", lambda: {"jobs": [{"job_id": "job-2"}]})
    monkeypatch.setattr(candidate_review, "load_queue_state", lambda: dict(state_store))
    monkeypatch.setattr(candidate_review, "pending_jobs", lambda queue, state: queue["jobs"])
    monkeypatch.setattr(candidate_review, "get_batch_status", lambda: {"running": False, "pid": None})
    monkeypatch.setattr(candidate_review, "is_batch_process", lambda _pid: False)
    monkeypatch.setattr(candidate_review, "_is_process_definitely_gone", lambda pid: pid == 999)
    monkeypatch.setattr(candidate_review, "PID_FILE", str(tmp_path / "batch.pid"))
    monkeypatch.setattr(candidate_review, "BATCH_LOG_FILE", str(tmp_path / "batch.log"))

    def save_state(state):
        state_store.clear()
        state_store.update(state)
        saved_states.append(dict(state))

    class FakeProcess:
        pid = 333

    monkeypatch.setattr(candidate_review, "save_queue_state", save_state)
    monkeypatch.setattr(
        candidate_review.subprocess,
        "Popen",
        lambda *_args, **_kwargs: launches.append(True) or FakeProcess(),
    )

    message, _ = candidate_review.append_batch_directory(str(tmp_path))

    assert "Batch queue launch requested (PID 333)" in message
    assert launches == [True]
    assert saved_states[0]["status"] == "idle"
    assert state_store["status"] == "starting"
    assert "worker_pid" not in state_store
    assert state_store["launch_token"]
    assert "cleanup_pending" not in state_store
    assert state_store["last_error"] == "PID persistence failed"


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


def test_rapid_append_requests_launch_one_successor_worker(monkeypatch, tmp_path):
    import threading

    from app.ui import candidate_review

    state_store = {"status": "idle", "current_job_id": None, "jobs": {}}
    saved_statuses = []
    launches = []
    launch_started = threading.Event()
    release_launch = threading.Event()
    second_attempted = threading.Event()

    monkeypatch.setattr(candidate_review, "append_queue_job", lambda *_args: {"job_id": "job-2"})
    monkeypatch.setattr(candidate_review, "load_queue", lambda: {"jobs": [{"job_id": "job-2"}]})
    monkeypatch.setattr(candidate_review, "load_queue_state", lambda: dict(state_store))

    def save_state(state):
        state_store.update(state)
        saved_statuses.append(state["status"])

    monkeypatch.setattr(candidate_review, "save_queue_state", save_state, raising=False)
    monkeypatch.setattr(candidate_review, "pending_jobs", lambda queue, state: queue["jobs"])
    monkeypatch.setattr(candidate_review, "get_batch_status", lambda: {"running": False, "pid": None})
    monkeypatch.setattr(candidate_review, "is_batch_process", lambda pid: pid == 123)
    monkeypatch.setattr(candidate_review, "_is_process_definitely_gone", lambda _pid: False)
    monkeypatch.setattr(candidate_review, "PID_FILE", str(tmp_path / "batch.pid"))
    monkeypatch.setattr(candidate_review, "BATCH_LOG_FILE", str(tmp_path / "batch.log"))

    class FakeProcess:
        pid = 123

    def fake_popen(*_args, **_kwargs):
        launches.append(True)
        if len(launches) == 1:
            launch_started.set()
            assert release_launch.wait(timeout=2)
        return FakeProcess()

    monkeypatch.setattr(candidate_review.subprocess, "Popen", fake_popen)

    first = threading.Thread(target=lambda: candidate_review.append_batch_directory(str(tmp_path)))

    def append_second():
        second_attempted.set()
        candidate_review.append_batch_directory(str(tmp_path))

    second = threading.Thread(target=append_second)
    first.start()
    assert launch_started.wait(timeout=2)
    second.start()
    assert second_attempted.wait(timeout=2)
    release_launch.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert launches == [True]
    assert saved_statuses == ["starting", "starting"]


def test_start_batch_queue_restores_idle_state_after_launch_failure(monkeypatch, tmp_path):
    from app.ui import candidate_review

    state_store = {"status": "idle", "current_job_id": None, "jobs": {}}
    saved_statuses = []
    monkeypatch.setattr(candidate_review, "load_queue", lambda: {"jobs": [{"job_id": "job-2"}]})
    monkeypatch.setattr(candidate_review, "load_queue_state", lambda: dict(state_store))
    monkeypatch.setattr(candidate_review, "pending_jobs", lambda queue, state: queue["jobs"])
    monkeypatch.setattr(candidate_review, "get_batch_status", lambda: {"running": False, "pid": None})
    monkeypatch.setattr(candidate_review, "PID_FILE", str(tmp_path / "batch.pid"))
    monkeypatch.setattr(candidate_review, "BATCH_LOG_FILE", str(tmp_path / "batch.log"))

    def save_state(state):
        state_store.update(state)
        saved_statuses.append(state["status"])

    monkeypatch.setattr(candidate_review, "save_queue_state", save_state, raising=False)
    monkeypatch.setattr(candidate_review.subprocess, "Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("launch failed")))

    message = candidate_review.start_batch_queue()

    assert "Failed to start queue" in message
    assert state_store["status"] == "idle"
    assert saved_statuses == ["starting", "idle"]


def test_queue_parent_leaves_pid_persistence_to_child(monkeypatch, tmp_path):
    from app.ui import candidate_review

    state_store = {"status": "idle", "current_job_id": None, "jobs": {}}
    saved_statuses = []
    taskkill_calls = []
    checked_pids = []
    monkeypatch.setattr(candidate_review, "load_queue", lambda: {"jobs": [{"job_id": "job-2"}]})
    monkeypatch.setattr(candidate_review, "load_queue_state", lambda: dict(state_store))
    monkeypatch.setattr(candidate_review, "pending_jobs", lambda queue, state: queue["jobs"])
    monkeypatch.setattr(candidate_review, "get_batch_status", lambda: {"running": False, "pid": None})
    monkeypatch.setattr(candidate_review, "BATCH_LOG_FILE", str(tmp_path / "batch.log"))
    monkeypatch.setattr(candidate_review, "PID_FILE", str(tmp_path))

    def save_state(state):
        state_store.update(state)
        saved_statuses.append(state["status"])

    class FakeProcess:
        pid = 444

    monkeypatch.setattr(candidate_review, "save_queue_state", save_state)
    monkeypatch.setattr(candidate_review.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(
        candidate_review.subprocess,
        "run",
        lambda command, **_kwargs: (
            taskkill_calls.append(command)
            if command[0] == "taskkill"
            else None
        ) or type("Result", (), {"returncode": 0})(),
    )
    monkeypatch.setattr(candidate_review, "is_batch_process", lambda pid: checked_pids.append(pid) or False)

    message = candidate_review.start_batch_queue()

    assert "launch requested (PID 444)" in message
    assert taskkill_calls == []
    assert checked_pids == []
    assert state_store["status"] == "starting"
    assert "worker_pid" not in state_store
    assert saved_statuses == ["starting", "starting"]


def test_start_batch_queue_keeps_claim_when_worker_cleanup_cannot_be_confirmed(monkeypatch, tmp_path):
    from app.ui import candidate_review

    state_store = {"status": "idle", "current_job_id": None, "jobs": {}}
    saved_states = []
    launches = []
    monkeypatch.setattr(candidate_review, "load_queue", lambda: {"jobs": [{"job_id": "job-2"}]})
    monkeypatch.setattr(candidate_review, "load_queue_state", lambda: dict(state_store))
    monkeypatch.setattr(candidate_review, "pending_jobs", lambda queue, state: queue["jobs"])
    monkeypatch.setattr(candidate_review, "get_batch_status", lambda: {"running": False, "pid": None})
    monkeypatch.setattr(candidate_review, "BATCH_LOG_FILE", str(tmp_path / "batch.log"))
    monkeypatch.setattr(candidate_review, "PID_FILE", str(tmp_path))

    def save_state(state):
        state_store.clear()
        state_store.update(state)
        saved_states.append(dict(state))

    class FakeProcess:
        pid = 445

    monkeypatch.setattr(candidate_review, "save_queue_state", save_state)
    monkeypatch.setattr(
        candidate_review.subprocess,
        "Popen",
        lambda *_args, **_kwargs: launches.append(True) or FakeProcess(),
    )
    monkeypatch.setattr(
        candidate_review.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 1})(),
    )
    monkeypatch.setattr(candidate_review, "is_batch_process", lambda pid: pid == 445)

    message = candidate_review.start_batch_queue()
    second_message = candidate_review.start_batch_queue()

    assert "launch requested (PID 445)" in message
    assert second_message == "Batch queue already starting."
    assert launches == [True]
    assert state_store["status"] == "starting"
    assert "worker_pid" not in state_store
    assert state_store["launch_token"]
    assert saved_states[-1]["status"] == "starting"


def test_start_batch_queue_keeps_claim_when_cleanup_verification_errors(monkeypatch, tmp_path):
    import subprocess

    from app.ui import candidate_review

    state_store = {"status": "idle", "current_job_id": None, "jobs": {}}
    launches = []
    monkeypatch.setattr(candidate_review, "load_queue", lambda: {"jobs": [{"job_id": "job-2"}]})
    monkeypatch.setattr(candidate_review, "load_queue_state", lambda: dict(state_store))
    monkeypatch.setattr(candidate_review, "pending_jobs", lambda queue, state: queue["jobs"])
    monkeypatch.setattr(candidate_review, "get_batch_status", lambda: {"running": False, "pid": None})
    monkeypatch.setattr(candidate_review, "BATCH_LOG_FILE", str(tmp_path / "batch.log"))
    monkeypatch.setattr(candidate_review, "PID_FILE", str(tmp_path))

    def save_state(state):
        state_store.clear()
        state_store.update(state)

    class FakeProcess:
        pid = 446

    def fake_run(command, **_kwargs):
        if command[0] == "taskkill":
            return type("Result", (), {"returncode": 0})()
        raise subprocess.TimeoutExpired(command, 3)

    monkeypatch.setattr(candidate_review, "save_queue_state", save_state)
    monkeypatch.setattr(
        candidate_review.subprocess,
        "Popen",
        lambda *_args, **_kwargs: launches.append(True) or FakeProcess(),
    )
    monkeypatch.setattr(candidate_review.subprocess, "run", fake_run)
    monkeypatch.setattr(candidate_review, "is_batch_process", lambda _pid: False)

    message = candidate_review.start_batch_queue()
    second_message = candidate_review.start_batch_queue()

    assert "launch requested (PID 446)" in message
    assert second_message == "Batch queue already starting."
    assert launches == [True]
    assert state_store["status"] == "starting"
    assert "worker_pid" not in state_store


def test_start_batch_queue_reclaims_stale_starting_claim_once(monkeypatch, tmp_path):
    from app.ui import candidate_review

    state_store = {
        "status": "starting",
        "current_job_id": None,
        "worker_pid": 999,
        "cleanup_pending": True,
        "last_error": "PID persistence failed",
        "jobs": {},
    }
    saved_statuses = []
    launches = []
    monkeypatch.setattr(candidate_review, "load_queue", lambda: {"jobs": [{"job_id": "job-2"}]})
    monkeypatch.setattr(candidate_review, "load_queue_state", lambda: dict(state_store))
    monkeypatch.setattr(candidate_review, "pending_jobs", lambda queue, state: queue["jobs"])
    monkeypatch.setattr(candidate_review, "get_batch_status", lambda: {"running": False, "pid": None})
    monkeypatch.setattr(candidate_review, "is_batch_process", lambda pid: pid == 333)
    monkeypatch.setattr(candidate_review, "_is_process_definitely_gone", lambda pid: pid == 999)
    monkeypatch.setattr(candidate_review, "PID_FILE", str(tmp_path / "batch.pid"))
    monkeypatch.setattr(candidate_review, "BATCH_LOG_FILE", str(tmp_path / "batch.log"))

    def save_state(state):
        state_store.clear()
        state_store.update(state)
        saved_statuses.append(state["status"])

    class FakeProcess:
        pid = 333

    monkeypatch.setattr(candidate_review, "save_queue_state", save_state)
    monkeypatch.setattr(candidate_review.subprocess, "Popen", lambda *_args, **_kwargs: launches.append(True) or FakeProcess())

    candidate_review.start_batch_queue()
    candidate_review.start_batch_queue()

    assert launches == [True]
    assert saved_statuses == ["idle", "starting", "starting"]
    assert "worker_pid" not in state_store
    assert state_store["launch_token"]
    assert "cleanup_pending" not in state_store
    assert state_store["last_error"] == "PID persistence failed"


import pytest


@pytest.mark.parametrize("stale_status", ["starting", "running", "draining"])
def test_stale_active_state_is_recovered_without_losing_pending_job(monkeypatch, stale_status):
    from app.ui import candidate_review

    state = {
        "status": stale_status,
        "current_job_id": "job-1",
        "worker_pid": 999,
        "launch_token": "old-launch",
        "jobs": {"job-1": {"status": "running"}},
    }
    saved = []
    monkeypatch.setattr(candidate_review, "_is_process_definitely_gone", lambda pid: pid == 999)
    monkeypatch.setattr(candidate_review, "save_queue_state", lambda value: saved.append(dict(value)))

    recovered = candidate_review._recover_stale_queue_state_locked(state)

    assert recovered == "idle"
    assert state["status"] == "idle"
    assert state["current_job_id"] is None
    assert state["jobs"]["job-1"]["status"] == "running"
    assert "worker_pid" not in state
    assert saved[-1]["jobs"]["job-1"]["status"] == "running"


def test_starting_claim_recovers_when_spawned_child_dies_before_handshake(monkeypatch):
    from app.ui import candidate_review

    state = {
        "status": "starting",
        "current_job_id": None,
        "launcher_pid": 123,
        "spawned_pid": 999,
        "launch_token": "launch-1",
        "jobs": {"job-1": {"status": "running"}},
    }
    saved = []
    monkeypatch.setattr(candidate_review, "_is_process_definitely_gone", lambda pid: pid == 999)
    monkeypatch.setattr(candidate_review, "save_queue_state", lambda value: saved.append(dict(value)))

    recovered = candidate_review._recover_stale_queue_state_locked(state)

    assert recovered == "idle"
    assert state["status"] == "idle"
    assert "spawned_pid" not in state
    assert "launcher_pid" not in state
    assert state["jobs"]["job-1"]["status"] == "running"


def test_stop_recovers_exact_worker_state_and_preserves_pending_job(monkeypatch, tmp_path):
    from app.ui import candidate_review

    pid_file = tmp_path / "batch_pid.txt"
    pid_file.write_text("777", encoding="ascii")
    state_store = {
        "status": "running",
        "current_job_id": "job-1",
        "worker_pid": 777,
        "launch_token": "launch-1",
        "jobs": {"job-1": {"status": "running"}},
    }
    monkeypatch.setattr(candidate_review, "PID_FILE", str(pid_file))
    monkeypatch.setattr(
        candidate_review,
        "get_batch_status",
        lambda: {"running": True, "pid": 777},
    )
    monkeypatch.setattr(candidate_review.subprocess, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(candidate_review, "is_batch_process", lambda _pid: False)
    monkeypatch.setattr(candidate_review, "_is_process_definitely_gone", lambda pid: pid == 777)
    monkeypatch.setattr(candidate_review.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(candidate_review, "load_queue_state", lambda: dict(state_store))

    def save_state(value):
        state_store.clear()
        state_store.update(value)

    monkeypatch.setattr(candidate_review, "save_queue_state", save_state)

    message = candidate_review.stop_batch()

    assert "Batch stopped" in message
    assert state_store["status"] == "idle"
    assert state_store["current_job_id"] is None
    assert state_store["jobs"]["job-1"]["status"] == "running"
    assert "worker_pid" not in state_store
    assert not pid_file.exists()


def test_queue_parent_does_not_overwrite_child_first_state(monkeypatch, tmp_path):
    from app.ui import candidate_review

    state_store = {"status": "idle", "current_job_id": None, "jobs": {}}
    monkeypatch.setattr(candidate_review, "load_queue", lambda: {"jobs": [{"job_id": "job-1"}]})
    monkeypatch.setattr(candidate_review, "load_queue_state", lambda: dict(state_store))
    monkeypatch.setattr(candidate_review, "pending_jobs", lambda queue, state: queue["jobs"])
    monkeypatch.setattr(candidate_review, "get_batch_status", lambda: {"running": False, "pid": None})
    monkeypatch.setattr(candidate_review, "PID_FILE", str(tmp_path / "batch.pid"))
    monkeypatch.setattr(candidate_review, "BATCH_LOG_FILE", str(tmp_path / "batch.log"))

    def save_state(value):
        state_store.clear()
        state_store.update(value)

    class FastChild:
        pid = 333

    def popen(*_args, **_kwargs):
        state_store.update(
            status="running",
            current_job_id="job-1",
            worker_pid=333,
            launch_token=state_store.get("launch_token"),
        )
        return FastChild()

    monkeypatch.setattr(candidate_review, "save_queue_state", save_state)
    monkeypatch.setattr(candidate_review.subprocess, "Popen", popen)

    candidate_review.start_batch_queue()

    assert state_store["status"] == "running"
    assert state_store["current_job_id"] == "job-1"
    assert state_store["worker_pid"] == 333


def test_append_never_bypasses_an_external_live_batch_pid(monkeypatch, tmp_path):
    from app.ui import candidate_review

    launches = []
    monkeypatch.setattr(candidate_review, "append_queue_job", lambda *_args: {"job_id": "job-1"})
    monkeypatch.setattr(candidate_review, "load_queue", lambda: {"jobs": [{"job_id": "job-1"}]})
    monkeypatch.setattr(
        candidate_review,
        "load_queue_state",
        lambda: {"status": "idle", "current_job_id": None, "jobs": {}},
    )
    monkeypatch.setattr(
        candidate_review,
        "get_batch_status",
        lambda: {"running": True, "pid": 888},
    )
    monkeypatch.setattr(
        candidate_review.subprocess,
        "Popen",
        lambda *_args, **_kwargs: launches.append(True),
    )

    message, _ = candidate_review.append_batch_directory(str(tmp_path))

    assert "already running" in message
    assert launches == []


def test_start_recovers_crashed_running_worker_and_resumes_pending_job(monkeypatch, tmp_path):
    from app.ui import candidate_review

    state_store = {
        "status": "running",
        "current_job_id": "job-1",
        "worker_pid": 999,
        "launch_token": "crashed",
        "jobs": {"job-1": {"status": "running"}},
    }
    launches = []
    monkeypatch.setattr(candidate_review, "load_queue", lambda: {"jobs": [{"job_id": "job-1"}]})
    monkeypatch.setattr(candidate_review, "load_queue_state", lambda: dict(state_store))
    monkeypatch.setattr(candidate_review, "pending_jobs", lambda queue, state: queue["jobs"])
    monkeypatch.setattr(candidate_review, "get_batch_status", lambda: {"running": False, "pid": None})
    monkeypatch.setattr(candidate_review, "_is_process_definitely_gone", lambda pid: pid == 999)
    monkeypatch.setattr(candidate_review, "PID_FILE", str(tmp_path / "batch.pid"))
    monkeypatch.setattr(candidate_review, "BATCH_LOG_FILE", str(tmp_path / "batch.log"))

    def save_state(value):
        state_store.clear()
        state_store.update(value)

    class Child:
        pid = 1001

    monkeypatch.setattr(candidate_review, "save_queue_state", save_state)
    monkeypatch.setattr(
        candidate_review.subprocess,
        "Popen",
        lambda *_args, **_kwargs: launches.append(True) or Child(),
    )

    message = candidate_review.start_batch_queue()

    assert "launch requested" in message
    assert launches == [True]
    assert state_store["jobs"]["job-1"]["status"] == "running"
    assert state_store["status"] == "starting"


def test_stop_does_not_clear_new_worker_that_replaced_old_pid(monkeypatch, tmp_path):
    from app.ui import candidate_review

    pid_file = tmp_path / "batch.pid"
    pid_file.write_text("777", encoding="ascii")
    state_store = {
        "status": "running",
        "current_job_id": "old-job",
        "worker_pid": 777,
        "launch_token": "old",
        "jobs": {"old-job": {"status": "running"}},
    }
    replaced = False
    monkeypatch.setattr(candidate_review, "PID_FILE", str(pid_file))
    monkeypatch.setattr(candidate_review, "get_batch_status", lambda: {"running": True, "pid": 777})
    monkeypatch.setattr(candidate_review.subprocess, "run", lambda *_args, **_kwargs: None)

    def gone(pid):
        nonlocal replaced
        if pid == 777 and not replaced:
            replaced = True
            state_store.update(
                status="running",
                current_job_id="new-job",
                worker_pid=888,
                launch_token="new",
            )
            state_store["jobs"]["new-job"] = {"status": "running"}
            pid_file.write_text("888", encoding="ascii")
            return True
        return False

    monkeypatch.setattr(candidate_review, "_is_process_definitely_gone", gone)
    monkeypatch.setattr(candidate_review, "load_queue_state", lambda: dict(state_store))
    monkeypatch.setattr(
        candidate_review,
        "save_queue_state",
        lambda value: state_store.update(value),
    )

    candidate_review.stop_batch()

    assert state_store["status"] == "running"
    assert state_store["worker_pid"] == 888
    assert state_store["current_job_id"] == "new-job"
    assert pid_file.read_text(encoding="ascii") == "888"


def test_start_queue_accepts_initial_directory_and_options(monkeypatch, tmp_path):
    from app.ui import candidate_review

    appended = []
    monkeypatch.setattr(
        candidate_review,
        "append_batch_directory",
        lambda directory, limit, extensions: (
            appended.append((directory, limit, extensions)) or "Queued and started",
            "queue",
        ),
    )

    message = candidate_review.start_batch_queue(str(tmp_path), 4, ".mp4,.mkv")

    assert message == "Queued and started"
    assert appended == [(str(tmp_path), 4, ".mp4,.mkv")]


def test_stop_then_start_resumes_preserved_pending_job(monkeypatch, tmp_path):
    from app.ui import candidate_review

    pid_file = tmp_path / "batch.pid"
    pid_file.write_text("777", encoding="ascii")
    state_store = {
        "status": "running",
        "current_job_id": "job-1",
        "worker_pid": 777,
        "launch_token": "old",
        "jobs": {"job-1": {"status": "running"}},
    }
    status_calls = iter((
        {"running": True, "pid": 777},
        {"running": False, "pid": None},
    ))
    launches = []
    monkeypatch.setattr(candidate_review, "PID_FILE", str(pid_file))
    monkeypatch.setattr(candidate_review, "BATCH_LOG_FILE", str(tmp_path / "batch.log"))
    monkeypatch.setattr(candidate_review, "get_batch_status", lambda: next(status_calls))
    monkeypatch.setattr(candidate_review.subprocess, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(candidate_review, "_is_process_definitely_gone", lambda pid: pid == 777)
    monkeypatch.setattr(candidate_review, "load_queue_state", lambda: dict(state_store))
    monkeypatch.setattr(candidate_review, "load_queue", lambda: {"jobs": [{"job_id": "job-1"}]})
    monkeypatch.setattr(candidate_review, "pending_jobs", lambda queue, state: queue["jobs"])

    def save_state(value):
        state_store.clear()
        state_store.update(value)

    class Child:
        pid = 1002

    monkeypatch.setattr(candidate_review, "save_queue_state", save_state)
    monkeypatch.setattr(
        candidate_review.subprocess,
        "Popen",
        lambda *_args, **_kwargs: launches.append(True) or Child(),
    )

    assert "Batch stopped" in candidate_review.stop_batch()
    message = candidate_review.start_batch_queue()

    assert "launch requested" in message
    assert launches == [True]
    assert state_store["jobs"]["job-1"]["status"] == "running"
    assert state_store["status"] == "starting"


def test_successor_launch_waits_for_exact_terminal_pid_handshake(monkeypatch, tmp_path):
    from app.ui import candidate_review

    state_store = {
        "status": "idle",
        "current_job_id": None,
        "previous_worker_pid": 999,
        "completed_launch_token": "done-1",
        "jobs": {"job-1": {"status": "running"}},
    }
    events = []
    gone_results = iter((False, True))
    monkeypatch.setattr(candidate_review, "load_queue_state", lambda: dict(state_store))
    monkeypatch.setattr(candidate_review, "load_queue", lambda: {"jobs": [{"job_id": "job-1"}]})
    monkeypatch.setattr(candidate_review, "pending_jobs", lambda queue, state: queue["jobs"])
    monkeypatch.setattr(candidate_review, "get_batch_status", lambda: {"running": False, "pid": None})
    monkeypatch.setattr(
        candidate_review,
        "_is_process_definitely_gone",
        lambda pid: events.append(("gone", pid)) or next(gone_results),
    )
    monkeypatch.setattr(candidate_review.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(candidate_review, "PID_FILE", str(tmp_path / "batch.pid"))
    monkeypatch.setattr(candidate_review, "BATCH_LOG_FILE", str(tmp_path / "batch.log"))

    def save_state(value):
        state_store.clear()
        state_store.update(value)

    class Child:
        pid = 1003

    monkeypatch.setattr(candidate_review, "save_queue_state", save_state)
    monkeypatch.setattr(
        candidate_review.subprocess,
        "Popen",
        lambda *_args, **_kwargs: events.append(("launch", 1003)) or Child(),
    )

    message = candidate_review.start_batch_queue()

    assert "launch requested" in message
    assert events[:2] == [("gone", 999), ("gone", 999)]
    assert events[-1] == ("launch", 1003)

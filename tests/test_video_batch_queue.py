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


def test_run_queue_adopts_job_appended_while_draining(monkeypatch, tmp_path):
    from app.services.batch_queue import load_queue, save_queue
    from scripts import test_video_batch

    queue_path = tmp_path / "batch_queue.json"
    calls = []
    appended = False
    saved_statuses = []
    original_save_state = test_video_batch.save_queue_state

    def append_when_draining(state, path):
        nonlocal appended
        saved_statuses.append(state["status"])
        original_save_state(state, path)
        if state["status"] == "draining" and not appended:
            appended = True
            queue = load_queue(queue_path)
            queue["jobs"].append(
                {
                    "job_id": "late-job",
                    "directory": "C:/videos/late",
                    "limit": 0,
                    "extensions": "",
                }
            )
            save_queue(queue, queue_path)

    monkeypatch.setattr(test_video_batch, "save_queue_state", append_when_draining)

    result = test_video_batch.run_queue(
        str(queue_path),
        process_job=lambda job: calls.append(job["directory"]) or 0,
    )

    assert result == 0
    assert calls == ["C:/videos/late"]
    draining_index = saved_statuses.index("draining")
    assert saved_statuses[draining_index:draining_index + 2] == ["draining", "running"]


def test_run_queue_final_drain_transaction_adopts_append_before_idle(monkeypatch, tmp_path):
    from app.services.batch_queue import append_queue_job, load_queue_state
    from scripts import test_video_batch

    queue_path = tmp_path / "batch_queue.json"
    late_directory = str(tmp_path / "late")
    calls = []
    appended = False
    original_finish = test_video_batch._finish_queue_worker

    def append_in_final_drain_gap(*args, **kwargs):
        nonlocal appended
        if not appended:
            appended = True
            append_queue_job(late_directory, path=queue_path)
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(
        test_video_batch,
        "_finish_queue_worker",
        append_in_final_drain_gap,
    )

    result = test_video_batch.run_queue(
        str(queue_path),
        process_job=lambda job: calls.append(job["directory"]) or 0,
    )

    assert result == 0
    assert calls == [late_directory]
    state = load_queue_state(tmp_path / "batch_queue_state.json")
    assert all(item.get("status") == "completed" for item in state["jobs"].values())


def test_queue_worker_retries_lease_until_direct_owner_releases(
    monkeypatch, tmp_path
):
    import subprocess
    import sys
    import threading
    import time

    from app.services.batch_queue import (
        WorkerLease,
        WorkerLeaseBusyError,
        append_queue_job,
        load_queue_state,
        save_queue_state,
    )
    from scripts import test_video_batch

    queue_path = tmp_path / "batch_queue.json"
    lease_path = tmp_path / "batch_worker.lock"
    pid_path = tmp_path / "batch.pid"
    ready_path = tmp_path / "direct-ready"
    release_path = tmp_path / "direct-release"
    busy_path = tmp_path / "queue-saw-direct-owner"
    append_queue_job("C:/videos/after-direct", path=queue_path)
    save_queue_state(
        {
            "status": "starting",
            "current_job_id": None,
            "launch_token": "launch-after-direct",
            "launcher_pid": 123,
            "jobs": {},
        },
        tmp_path / "batch_queue_state.json",
    )

    owner_script = """
import sys
import time
from pathlib import Path
from app.services.batch_queue import WorkerLease

lease = WorkerLease(sys.argv[1], mode="direct").acquire()
Path(sys.argv[2]).write_text("ready", encoding="ascii")
try:
    while not Path(sys.argv[3]).exists():
        time.sleep(0.01)
finally:
    lease.release()
"""
    owner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            owner_script,
            str(lease_path),
            str(ready_path),
            str(release_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    results = {}
    calls = []
    original_lease = test_video_batch.WorkerLease

    class ObservedLease(original_lease):
        def acquire(self):
            try:
                return super().acquire()
            except WorkerLeaseBusyError:
                busy_path.write_text("busy", encoding="ascii")
                raise

    monkeypatch.setattr(test_video_batch, "WorkerLease", ObservedLease)

    def run_worker():
        results["result"] = test_video_batch.run_queue(
            str(queue_path),
            process_job=lambda job: calls.append(job["directory"]) or 0,
            worker_lease_file=lease_path,
            pid_file=pid_path,
            launch_token="launch-after-direct",
        )

    worker = threading.Thread(target=run_worker)
    try:
        deadline = time.monotonic() + 5
        while not ready_path.exists() and owner.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready_path.exists(), owner.communicate(timeout=1)

        worker.start()
        deadline = time.monotonic() + 5
        while not busy_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert busy_path.exists()

        release_path.write_text("release", encoding="ascii")
        worker.join(timeout=5)
        assert not worker.is_alive()
    finally:
        release_path.write_text("release", encoding="ascii")
        if worker.is_alive():
            worker.join(timeout=1)
        if owner.poll() is None:
            owner.terminate()
            owner.wait(timeout=3)

    assert results["result"] == 0
    assert calls == ["C:/videos/after-direct"]
    state = load_queue_state(tmp_path / "batch_queue_state.json")
    job_state = next(iter(state["jobs"].values()))
    assert job_state["status"] == "completed"
    assert state["status"] == "idle"


def test_build_single_batch_command_keeps_frozen_and_source_modes_distinct(monkeypatch):
    from scripts import test_video_batch

    monkeypatch.setattr(test_video_batch.sys, "frozen", False, raising=False)
    source_cmd = test_video_batch.build_single_batch_command("C:/videos", 2, ".mp4")

    assert source_cmd[-6:] == ["--dir", "C:/videos", "--limit", "2", "--extensions", ".mp4"]


def test_run_queue_marks_unexpected_job_error_failed_and_continues(tmp_path, capsys):
    from app.services.batch_queue import append_queue_job, load_queue_state
    from scripts.test_video_batch import run_queue

    queue_path = tmp_path / "batch_queue.json"
    first_dir = str(tmp_path / "first" / "same")
    second_dir = str(tmp_path / "second" / "same")
    first = append_queue_job(first_dir, path=queue_path)
    second = append_queue_job(second_dir, path=queue_path)
    calls = []

    def process(job):
        calls.append(job["directory"])
        if job["job_id"] == first["job_id"]:
            raise OSError("cannot spawn adaptive worker")
        return 0

    result = run_queue(str(queue_path), process_job=process)

    state = load_queue_state(tmp_path / "batch_queue_state.json")
    output = capsys.readouterr().out
    assert result == 1
    assert calls == [first_dir, second_dir]
    assert state["jobs"][first["job_id"]]["status"] == "failed"
    assert "cannot spawn adaptive worker" in state["jobs"][first["job_id"]]["error"]
    assert state["jobs"][second["job_id"]]["status"] == "completed"
    assert first_dir in output
    assert "status=FAILED" in output


def test_run_single_directory_persists_current_video_before_spawn_and_logs_terminal(
    monkeypatch, tmp_path, capsys
):
    import json

    from scripts import test_video_batch

    video_dir = tmp_path / "videos"
    video_dir.mkdir()
    video = video_dir / "clip.mp4"
    video.write_bytes(b"video")
    checkpoint = tmp_path / "checkpoint.json"
    monkeypatch.setattr(test_video_batch, "CHECKPOINT_FILE", str(checkpoint))
    monkeypatch.setattr(test_video_batch, "compute_fingerprint", lambda _path: None)

    class Result:
        returncode = 0

    def run_adaptive(*_args, **_kwargs):
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
        assert state["last_run"]["current_video"] == str(video)
        return Result()

    monkeypatch.setattr(test_video_batch.subprocess, "run", run_adaptive)

    assert test_video_batch.run_single_directory(str(video_dir), 0, ".mp4", False) == 0

    output = capsys.readouterr().out
    assert f"[VIDEO] status=START path={video}" in output
    assert f"[VIDEO] status=OK path={video}" in output


def test_same_basename_in_different_directories_is_processed_independently(
    monkeypatch, tmp_path
):
    from scripts import test_video_batch

    checkpoint = tmp_path / "checkpoint.json"
    monkeypatch.setattr(test_video_batch, "CHECKPOINT_FILE", str(checkpoint))
    monkeypatch.setattr(test_video_batch, "compute_fingerprint", lambda _path: None)
    calls = []

    class Result:
        returncode = 0

    monkeypatch.setattr(
        test_video_batch.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command) or Result(),
    )

    for folder in (tmp_path / "one", tmp_path / "two"):
        folder.mkdir()
        (folder / "clip.mp4").write_bytes(b"video")
        assert test_video_batch.run_single_directory(str(folder), 0, ".mp4", False) == 0

    checkpoint_data = test_video_batch.load_checkpoint()
    assert len(calls) == 2
    assert len(checkpoint_data["completed"]) == 2
    assert all(key.startswith("path:") for key in checkpoint_data["completed"])


def test_queue_worker_refuses_lease_owned_by_direct_worker(tmp_path):
    from app.services.batch_queue import WorkerLease, append_queue_job
    from scripts.test_video_batch import WORKER_BUSY_EXIT_CODE, run_queue

    queue_path = tmp_path / "batch_queue.json"
    lease_path = tmp_path / "batch_worker.lock"
    append_queue_job(str(tmp_path / "videos"), path=queue_path)
    calls = []
    owner = WorkerLease(lease_path, mode="direct").acquire()
    try:
        result = run_queue(
            str(queue_path),
            process_job=lambda job: calls.append(job) or 0,
            worker_lease_file=lease_path,
            pid_file=tmp_path / "batch.pid",
        )
    finally:
        owner.release()

    assert result == WORKER_BUSY_EXIT_CODE
    assert calls == []


def test_direct_worker_refuses_lease_owned_by_queue_worker(tmp_path):
    from app.services.batch_queue import WorkerLease
    from scripts.test_video_batch import WORKER_BUSY_EXIT_CODE, run_direct

    lease_path = tmp_path / "batch_worker.lock"
    calls = []
    owner = WorkerLease(lease_path, mode="queue").acquire()
    try:
        result = run_direct(
            "C:/videos",
            0,
            ".mp4",
            False,
            worker_lease_file=lease_path,
            pid_file=tmp_path / "batch.pid",
            process_directory=lambda *_args: calls.append(True) or 0,
        )
    finally:
        owner.release()

    assert result == WORKER_BUSY_EXIT_CODE
    assert calls == []


def test_queue_child_claims_launch_token_and_pid_before_processing(tmp_path):
    from app.services.batch_queue import append_queue_job, load_queue_state, save_queue_state
    from scripts.test_video_batch import run_queue

    queue_path = tmp_path / "batch_queue.json"
    state_path = tmp_path / "batch_queue_state.json"
    pid_file = tmp_path / "batch.pid"
    lease_path = tmp_path / "batch_worker.lock"
    append_queue_job(str(tmp_path / "videos"), path=queue_path)
    save_queue_state(
        {
            "status": "starting",
            "current_job_id": None,
            "launch_token": "launch-1",
            "launcher_pid": 123,
            "jobs": {},
        },
        state_path,
    )

    def process(_job):
        state = load_queue_state(state_path)
        assert state["status"] == "running"
        assert state["launch_token"] == "launch-1"
        assert state["worker_pid"] == int(pid_file.read_text(encoding="ascii"))
        return 0

    result = run_queue(
        str(queue_path),
        process_job=process,
        worker_lease_file=lease_path,
        pid_file=pid_file,
        launch_token="launch-1",
    )

    state = load_queue_state(state_path)
    assert result == 0
    assert state["status"] == "idle"
    assert state["completed_launch_token"] == "launch-1"
    assert state["previous_worker_pid"] > 0
    assert not pid_file.exists()


def test_queue_child_rejects_mismatched_launch_without_clearing_newer_state(tmp_path):
    from app.services.batch_queue import append_queue_job, load_queue_state, save_queue_state
    from scripts.test_video_batch import LAUNCH_REJECTED_EXIT_CODE, run_queue

    queue_path = tmp_path / "batch_queue.json"
    state_path = tmp_path / "batch_queue_state.json"
    append_queue_job(str(tmp_path / "videos"), path=queue_path)
    original = {
        "status": "starting",
        "current_job_id": None,
        "launch_token": "new-launch",
        "launcher_pid": 999,
        "jobs": {},
    }
    save_queue_state(original, state_path)
    calls = []

    result = run_queue(
        str(queue_path),
        process_job=lambda job: calls.append(job) or 0,
        worker_lease_file=tmp_path / "batch_worker.lock",
        pid_file=tmp_path / "batch.pid",
        launch_token="stale-launch",
    )

    assert result == LAUNCH_REJECTED_EXIT_CODE
    assert calls == []
    assert load_queue_state(state_path) == original


def test_fingerprint_error_logs_failed_terminal_and_continues_next_video(
    monkeypatch, tmp_path, capsys
):
    from scripts import test_video_batch

    video_dir = tmp_path / "videos"
    video_dir.mkdir()
    first = video_dir / "a.mp4"
    second = video_dir / "b.mp4"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    monkeypatch.setattr(test_video_batch, "CHECKPOINT_FILE", str(tmp_path / "checkpoint.json"))

    def fingerprint(path):
        if path == str(first):
            raise OSError("ffprobe unavailable")
        return None

    class Result:
        returncode = 0

    monkeypatch.setattr(test_video_batch, "compute_fingerprint", fingerprint)
    monkeypatch.setattr(test_video_batch.subprocess, "run", lambda *_args, **_kwargs: Result())

    result = test_video_batch.run_single_directory(str(video_dir), 0, ".mp4", False)

    output = capsys.readouterr().out
    assert result == 1
    assert f"[VIDEO] status=START path={first}" in output
    assert f"[VIDEO] status=FAILED path={first}" in output
    assert "ffprobe unavailable" in output
    assert f"[VIDEO] status=OK path={second}" in output
    run_status = test_video_batch.load_checkpoint()["last_run"]
    assert run_status["planned"] == 2
    assert run_status["processed"] == 2
    assert run_status["succeeded"] == 1
    assert run_status["failed"] == 1


def test_post_success_fingerprint_error_is_failed_only(monkeypatch, tmp_path, capsys):
    from app.ui.candidate_review import summarize_checkpoint_status
    from scripts import test_video_batch

    video_dir = tmp_path / "videos"
    video_dir.mkdir()
    video = video_dir / "post-success-fingerprint.mp4"
    video.write_bytes(b"video")
    checkpoint = tmp_path / "checkpoint.json"
    monkeypatch.setattr(test_video_batch, "CHECKPOINT_FILE", str(checkpoint))

    fingerprint_calls = 0

    def fingerprint(_path):
        nonlocal fingerprint_calls
        fingerprint_calls += 1
        if fingerprint_calls == 1:
            return None
        raise OSError("fingerprint failed after adaptive success")

    class Result:
        returncode = 0

    monkeypatch.setattr(test_video_batch, "compute_fingerprint", fingerprint)
    monkeypatch.setattr(test_video_batch.subprocess, "run", lambda *_args, **_kwargs: Result())

    result = test_video_batch.run_single_directory(str(video_dir), 0, ".mp4", False)

    output = capsys.readouterr().out
    run_status = test_video_batch.load_checkpoint()["last_run"]
    status = summarize_checkpoint_status({"last_run": run_status})
    video_key = test_video_batch.checkpoint_key(str(video))

    assert result == 1
    assert fingerprint_calls == 2
    assert run_status["planned"] == 1
    assert run_status["processed"] == 1
    assert run_status["succeeded"] == 0
    assert run_status["failed"] == 1
    assert status["total"] == 1
    assert status["completed"] + status["failed"] == 1
    assert video_key not in test_video_batch.load_checkpoint()["completed"]
    assert test_video_batch.load_checkpoint()["retryable"][video_key]["status"] == "failed"
    assert "status=FAILED" in output
    assert "fingerprint failed after adaptive success" in output


def test_reusable_dedup_and_timeout_videos_have_full_terminal_logs(
    monkeypatch, tmp_path, capsys
):
    import subprocess

    from scripts import test_video_batch

    video_dir = tmp_path / "same-name-parent"
    video_dir.mkdir()
    reusable = video_dir / "reusable.mp4"
    duplicate = video_dir / "duplicate.mp4"
    timeout_video = video_dir / "timeout.mp4"
    for video in (reusable, duplicate, timeout_video):
        video.write_bytes(b"video")

    checkpoint_path = tmp_path / "checkpoint.json"
    monkeypatch.setattr(test_video_batch, "CHECKPOINT_FILE", str(checkpoint_path))
    checkpoint = {
        "completed": {
            test_video_batch.checkpoint_key(str(reusable)): {
                "status": "ok",
                "source_path": test_video_batch.normalized_source_path(str(reusable)),
            }
        },
        "retryable": {},
        "started_at": None,
        "last_run": None,
    }
    test_video_batch.save_checkpoint(checkpoint)

    monkeypatch.setattr(
        test_video_batch,
        "compute_fingerprint",
        lambda path: {"path": path},
    )
    monkeypatch.setattr(
        test_video_batch,
        "find_duplicate_in_checkpoint",
        lambda fingerprint, _cp: "path:C:/source/original.mp4"
        if fingerprint["path"] == str(duplicate)
        else None,
    )
    monkeypatch.setattr(
        test_video_batch.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("adaptive", 14400)
        ),
    )

    result = test_video_batch.run_single_directory(str(video_dir), 0, ".mp4", False)

    output = capsys.readouterr().out
    assert result == 1
    assert f"[VIDEO] status=START path={reusable}" in output
    assert f"[VIDEO] status=OK path={reusable} outcome=SKIPPED" in output
    assert f"[VIDEO] status=START path={duplicate}" in output
    assert f"[VIDEO] status=OK path={duplicate} outcome=DEDUP_SKIPPED" in output
    assert f"[VIDEO] status=START path={timeout_video}" in output
    assert f"[VIDEO] status=FAILED path={timeout_video} reason=timeout" in output
    run_status = test_video_batch.load_checkpoint()["last_run"]
    assert run_status["planned"] == 3
    assert run_status["processed"] == 3
    from app.ui.candidate_review import summarize_checkpoint_status

    status = summarize_checkpoint_status({"last_run": run_status})
    assert status["completed"] == 2
    assert status["total"] == 3
    assert status["completed"] <= status["total"]

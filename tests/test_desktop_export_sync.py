"""Tests for Favorite-GIF + PBF desktop export synchronization."""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from app.services.desktop_export_sync import (
    DesktopSyncReport,
    DesktopSyncScheduler,
    get_config,
    run_reconciliation,
)


ENV_LIBRARY_DB = "GIFAGENT_LIBRARY_DB"
ENV_ADAPTIVE_SOURCE_ROOT = "GIFAGENT_ADAPTIVE_SOURCE_ROOT"
ENV_FAVORITE_GIF_DEST = "GIFAGENT_FAVORITE_GIF_DEST"
ENV_PBF_DEST = "GIFAGENT_PBF_DEST"


def _write_test_file(path: Path, data: bytes, mtime: float = 1_700_000_000.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def _make_library_db(path: Path, rows: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE favorite_gifs (
            favorite_id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL UNIQUE,
            full_path TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    for idx, (candidate_id, full_path) in enumerate(rows):
        conn.execute(
            "INSERT INTO favorite_gifs (favorite_id, candidate_id, full_path)"
            " VALUES (?, ?, ?)",
            (f"fav-{idx}", candidate_id, full_path),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def sync_env(
    monkeypatch, tmp_path
) -> dict[str, Path]:
    root = tmp_path / "adaptive_test"
    fav_dest = tmp_path / "desktop" / "entertainment" / "favorite_gifs"
    pbf_dest = tmp_path / "desktop" / "entertainment" / "bookmarks" / "PBF"
    db_path = tmp_path / "library.db"
    monkeypatch.setenv(ENV_LIBRARY_DB, str(db_path))
    monkeypatch.setenv(ENV_ADAPTIVE_SOURCE_ROOT, str(root))
    monkeypatch.setenv(ENV_FAVORITE_GIF_DEST, str(fav_dest))
    monkeypatch.setenv(ENV_PBF_DEST, str(pbf_dest))
    return {
        "root": root,
        "fav_dest": fav_dest,
        "pbf_dest": pbf_dest,
        "db": db_path,
    }


# ---------------------------------------------------------------------------
# Service behavior
# ---------------------------------------------------------------------------


def test_favorite_selection_exports_only_existing_gifs_under_adaptive_root(
    sync_env, tmp_path
):
    inside = _write_test_file(
        sync_env["root"] / "folder-a" / "keep.gif", b"keep"
    )
    outside = _write_test_file(tmp_path / "elsewhere" / "outside.gif", b"outside")
    missing = sync_env["root"] / "folder-b" / "gone.gif"
    _make_library_db(
        sync_env["db"],
        [
            ("cand-keep", str(inside)),
            ("cand-outside", str(outside)),
            ("cand-missing", str(missing)),
        ],
    )

    report = run_reconciliation()

    dest = sync_env["fav_dest"] / "keep.gif"
    assert dest.read_bytes() == b"keep"
    assert not (sync_env["fav_dest"] / "outside.gif").exists()
    assert not (sync_env["fav_dest"] / "gone.gif").exists()
    assert report.gif_summary["copied"] == 1
    assert report.gif_summary["missing"] == 2
    missing_reasons = {e.reason for e in report.gifs["missing"]}
    assert "source not found" in missing_reasons
    assert "source outside adaptive export root" in missing_reasons


def test_stale_absolute_favorite_path_is_relocated_to_configured_root(
    sync_env, tmp_path
):
    actual = _write_test_file(
        sync_env["root"] / "folder-a" / "moved.gif", b"moved"
    )
    stale = (
        tmp_path
        / "old-checkout"
        / "data"
        / "exports"
        / "adaptive_test"
        / "folder-a"
        / "moved.gif"
    )
    assert not stale.exists()
    _make_library_db(sync_env["db"], [("cand-moved", str(stale))])

    report = run_reconciliation()

    assert (sync_env["fav_dest"] / actual.name).read_bytes() == b"moved"
    assert report.gif_summary["copied"] == 1
    assert report.gif_summary["missing"] == 0


def test_nested_gifs_and_pbfs_flatten_to_original_basenames(sync_env):
    gif_a = _write_test_file(
        sync_env["root"] / "folder-a" / "clip-one.gif", b"gif-a"
    )
    gif_b = _write_test_file(
        sync_env["root"] / "folder-a" / "nested" / "clip-two.gif", b"gif-b"
    )
    pbf_a = _write_test_file(
        sync_env["root"] / "folder-a" / "clip-one.pbf", b"pbf-a"
    )
    pbf_b = _write_test_file(
        sync_env["root"] / "folder-b" / "deep" / "deeper" / "clip-two.pbf",
        b"pbf-b",
    )
    _make_library_db(
        sync_env["db"],
        [("cand-a", str(gif_a)), ("cand-b", str(gif_b))],
    )

    report = run_reconciliation()

    assert (sync_env["fav_dest"] / "clip-one.gif").read_bytes() == b"gif-a"
    assert (sync_env["fav_dest"] / "clip-two.gif").read_bytes() == b"gif-b"
    assert (sync_env["pbf_dest"] / "clip-one.pbf").read_bytes() == b"pbf-a"
    assert (sync_env["pbf_dest"] / "clip-two.pbf").read_bytes() == b"pbf-b"
    assert report.gif_summary["copied"] == 2
    assert report.pbf_summary["copied"] == 2


def test_unchanged_files_are_skipped_and_changed_files_are_updated(sync_env):
    gif = _write_test_file(sync_env["root"] / "a.gif", b"version-1")
    pbf = _write_test_file(sync_env["root"] / "a.pbf", b"pbf-1")
    _make_library_db(sync_env["db"], [("cand-a", str(gif))])

    first = run_reconciliation()
    assert first.gif_summary["copied"] == 1
    assert first.pbf_summary["copied"] == 1

    second = run_reconciliation()
    assert second.gif_summary["skipped"] == 1
    assert second.pbf_summary["skipped"] == 1
    assert second.gif_summary["copied"] == 0
    assert second.pbf_summary["copied"] == 0
    assert (sync_env["fav_dest"] / "a.gif").read_bytes() == b"version-1"

    _write_test_file(sync_env["root"] / "a.gif", b"version-2-larger")
    _write_test_file(sync_env["root"] / "a.pbf", b"pbf-2-larger")

    third = run_reconciliation()
    assert third.gif_summary["updated"] == 1
    assert third.pbf_summary["updated"] == 1
    assert (sync_env["fav_dest"] / "a.gif").read_bytes() == b"version-2-larger"
    assert (sync_env["pbf_dest"] / "a.pbf").read_bytes() == b"pbf-2-larger"


def test_case_insensitive_collisions_reported_and_not_overwritten(sync_env):
    gif_a = _write_test_file(sync_env["root"] / "one" / "same.gif", b"first")
    gif_b = _write_test_file(sync_env["root"] / "two" / "SAME.GIF", b"second")
    pbf_a = _write_test_file(sync_env["root"] / "one" / "book.pbf", b"pbf-1")
    pbf_b = _write_test_file(sync_env["root"] / "two" / "BOOK.PBF", b"pbf-2")
    gif_ok = _write_test_file(sync_env["root"] / "three" / "ok.gif", b"ok")
    pbf_ok = _write_test_file(sync_env["root"] / "three" / "ok.pbf", b"pbf-ok")
    _make_library_db(
        sync_env["db"],
        [
            ("cand-a", str(gif_a)),
            ("cand-b", str(gif_b)),
            ("cand-ok", str(gif_ok)),
        ],
    )

    report = run_reconciliation()

    # Every source in a colliding group is marked conflicted before any copy.
    assert len(report.gifs["conflicts"]) == 2
    assert len(report.pbfs["conflicts"]) == 2
    assert {Path(e.source).name for e in report.gifs["conflicts"]} == {
        "same.gif",
        "SAME.GIF",
    }
    assert {Path(e.source).name for e in report.pbfs["conflicts"]} == {
        "book.pbf",
        "BOOK.PBF",
    }
    # No ambiguous destination is created for either colliding group.
    assert not (sync_env["fav_dest"] / "same.gif").exists()
    assert not (sync_env["fav_dest"] / "SAME.GIF").exists()
    assert not (sync_env["pbf_dest"] / "book.pbf").exists()
    assert not (sync_env["pbf_dest"] / "BOOK.PBF").exists()
    # Unrelated files still sync.
    assert (sync_env["fav_dest"] / "ok.gif").read_bytes() == b"ok"
    assert (sync_env["pbf_dest"] / "ok.pbf").read_bytes() == b"pbf-ok"


def test_missing_favorites_and_copy_errors_do_not_abort_other_copies(
    sync_env, monkeypatch
):
    good = _write_test_file(sync_env["root"] / "good.gif", b"good")
    failing = _write_test_file(sync_env["root"] / "failing.gif", b"failing")
    good_pbf = _write_test_file(sync_env["root"] / "good.pbf", b"good-pbf")
    _make_library_db(
        sync_env["db"],
        [
            ("cand-good", str(good)),
            ("cand-failing", str(failing)),
            ("cand-missing", str(sync_env["root"] / "missing.gif")),
        ],
    )
    from app.services import desktop_export_sync as sync_module

    def flaky_copy2(source, destination, *args, **kwargs):
        if Path(source).name == "failing.gif":
            raise OSError("simulated copy failure")
        return original_copy2(source, destination, *args, **kwargs)

    original_copy2 = sync_module.shutil.copy2
    monkeypatch.setattr(sync_module.shutil, "copy2", flaky_copy2)

    report = run_reconciliation()

    assert (sync_env["fav_dest"] / "good.gif").read_bytes() == b"good"
    assert (sync_env["pbf_dest"] / "good.pbf").read_bytes() == b"good-pbf"
    assert not (sync_env["fav_dest"] / "failing.gif").exists()
    assert report.gif_summary["copied"] == 1
    assert report.gif_summary["missing"] == 1
    assert len(report.gifs["errors"]) == 1
    assert "simulated copy failure" in report.gifs["errors"][0].reason


def test_destination_directories_are_created(sync_env):
    gif = _write_test_file(sync_env["root"] / "nested" / "a.gif", b"a")
    pbf = _write_test_file(sync_env["root"] / "nested" / "a.pbf", b"pbf")
    _make_library_db(sync_env["db"], [("cand-a", str(gif))])
    assert not sync_env["fav_dest"].exists()
    assert not sync_env["pbf_dest"].exists()

    run_reconciliation()

    assert sync_env["fav_dest"].is_dir()
    assert sync_env["pbf_dest"].is_dir()
    assert (sync_env["fav_dest"] / "a.gif").exists()
    assert (sync_env["pbf_dest"] / "a.pbf").exists()


def test_config_defaults_are_overridable_via_environment(monkeypatch, tmp_path):
    root = tmp_path / "src"
    fav = tmp_path / "fav"
    pbf = tmp_path / "pbf"
    db = tmp_path / "db.sqlite"
    monkeypatch.setenv(ENV_ADAPTIVE_SOURCE_ROOT, str(root))
    monkeypatch.setenv(ENV_FAVORITE_GIF_DEST, str(fav))
    monkeypatch.setenv(ENV_PBF_DEST, str(pbf))
    monkeypatch.setenv(ENV_LIBRARY_DB, str(db))

    config = get_config()

    assert config["source_root"] == root
    assert config["favorite_dest"] == fav
    assert config["pbf_dest"] == pbf
    assert config["library_db"] == db


def test_invalid_library_database_raises_and_cli_exits_nonzero(
    monkeypatch, tmp_path, capsys
):
    from app.services import desktop_export_sync

    bad_db = tmp_path / "bad.db"
    bad_db.write_bytes(b"this is not a sqlite database")
    monkeypatch.setenv(ENV_LIBRARY_DB, str(bad_db))
    monkeypatch.setenv(ENV_ADAPTIVE_SOURCE_ROOT, str(tmp_path / "src"))
    monkeypatch.setenv(ENV_FAVORITE_GIF_DEST, str(tmp_path / "fav"))
    monkeypatch.setenv(ENV_PBF_DEST, str(tmp_path / "pbf"))

    with pytest.raises(sqlite3.DatabaseError):
        run_reconciliation()

    exit_code = desktop_export_sync.main([])
    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FATAL" in output


def test_database_without_favorite_gifs_table_raises(sync_env):
    conn = sqlite3.connect(sync_env["db"])
    conn.execute("CREATE TABLE unrelated (id INTEGER)")
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.DatabaseError, match="favorite_gifs"):
        run_reconciliation()


def test_missing_individual_gif_is_report_only_and_cli_exits_zero(
    monkeypatch, tmp_path, capsys
):
    from app.services import desktop_export_sync

    db_path = tmp_path / "library.db"
    root = tmp_path / "src"
    fav = tmp_path / "fav"
    pbf = tmp_path / "pbf"
    monkeypatch.setenv(ENV_LIBRARY_DB, str(db_path))
    monkeypatch.setenv(ENV_ADAPTIVE_SOURCE_ROOT, str(root))
    monkeypatch.setenv(ENV_FAVORITE_GIF_DEST, str(fav))
    monkeypatch.setenv(ENV_PBF_DEST, str(pbf))
    _make_library_db(db_path, [("cand-missing", str(root / "gone.gif"))])

    report = run_reconciliation()
    assert report.gif_summary["missing"] == 1

    exit_code = desktop_export_sync.main([])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "gifs copied=0 updated=0 skipped=0 missing=1" in output


# ---------------------------------------------------------------------------
# Background scheduler: serialized + coalesced
# ---------------------------------------------------------------------------


def test_background_triggers_are_serialized_and_coalesced():
    started = threading.Event()
    proceed = threading.Event()
    sync_runs = []
    max_concurrent = {"value": 0}
    current = {"value": 0}

    def callback():
        current["value"] += 1
        max_concurrent["value"] = max(max_concurrent["value"], current["value"])
        sync_runs.append(threading.get_ident())
        try:
            if len(sync_runs) == 1:
                started.set()
                assert proceed.wait(timeout=5)
            return DesktopSyncReport()
        finally:
            current["value"] -= 1

    scheduler = DesktopSyncScheduler(callback)
    stop_event = threading.Event()
    thread = scheduler.start(stop_event)

    try:
        scheduler.request_sync()
        assert started.wait(timeout=5)
        # Two triggers while the first run is in flight -> one follow-up.
        scheduler.request_sync()
        scheduler.request_sync()
        proceed.set()
        deadline = time.monotonic() + 5
        while len(sync_runs) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        stop_event.set()
        thread.join(timeout=3)

    assert len(sync_runs) == 2
    assert max_concurrent["value"] == 1


# ---------------------------------------------------------------------------
# Legacy queue completion hook
# ---------------------------------------------------------------------------


def test_legacy_queue_successful_completion_schedules_sync_once(tmp_path):
    from app.services.batch_queue import append_queue_job
    from scripts import test_video_batch

    queue_path = tmp_path / "batch_queue.json"
    append_queue_job(str(tmp_path / "videos"), path=queue_path)
    sync_calls = []

    result = test_video_batch.run_queue(
        str(queue_path),
        process_job=lambda job: 0,
        sync_on_success=True,
        sync_callback=lambda: sync_calls.append("run") or DesktopSyncReport(),
    )

    assert result == 0
    assert sync_calls == ["run"]


def test_legacy_queue_failure_does_not_schedule_sync(tmp_path):
    from app.services.batch_queue import append_queue_job
    from scripts import test_video_batch

    queue_path = tmp_path / "batch_queue.json"
    append_queue_job(str(tmp_path / "videos"), path=queue_path)
    sync_calls = []

    result = test_video_batch.run_queue(
        str(queue_path),
        process_job=lambda job: 1,
        sync_on_success=True,
        sync_callback=lambda: sync_calls.append("run") or DesktopSyncReport(),
    )

    assert result == 1
    assert sync_calls == []


def test_busy_queue_does_not_start_or_leak_scheduler(tmp_path):
    from app.services.batch_queue import (
        WorkerLease,
        append_queue_job,
    )
    from scripts import test_video_batch

    queue_path = tmp_path / "batch_queue.json"
    lease_path = tmp_path / "batch_worker.lock"
    append_queue_job(str(tmp_path / "videos"), path=queue_path)
    sync_calls = []
    owner = WorkerLease(lease_path, mode="direct").acquire()
    try:
        result = test_video_batch.run_queue(
            str(queue_path),
            process_job=lambda job: 0,
            worker_lease_file=lease_path,
            pid_file=tmp_path / "batch.pid",
            sync_on_success=True,
            sync_callback=lambda: sync_calls.append("run") or DesktopSyncReport(),
        )
    finally:
        owner.release()

    assert result == test_video_batch.WORKER_BUSY_EXIT_CODE
    assert sync_calls == []
    assert not any(
        t.name == "desktop-export-sync" and t.is_alive()
        for t in threading.enumerate()
    )


def test_rejected_queue_launch_does_not_start_or_leak_scheduler(tmp_path):
    from app.services.batch_queue import (
        append_queue_job,
        save_queue_state,
    )
    from scripts import test_video_batch

    queue_path = tmp_path / "batch_queue.json"
    state_path = tmp_path / "batch_queue_state.json"
    append_queue_job(str(tmp_path / "videos"), path=queue_path)
    save_queue_state(
        {
            "status": "starting",
            "current_job_id": None,
            "launch_token": "current-token",
            "launcher_pid": 123,
            "jobs": {},
        },
        state_path,
    )
    sync_calls = []

    result = test_video_batch.run_queue(
        str(queue_path),
        process_job=lambda job: 0,
        worker_lease_file=tmp_path / "batch_worker.lock",
        pid_file=tmp_path / "batch.pid",
        launch_token="stale-token",
        sync_on_success=True,
        sync_callback=lambda: sync_calls.append("run") or DesktopSyncReport(),
    )

    assert result == test_video_batch.LAUNCH_REJECTED_EXIT_CODE
    assert sync_calls == []
    assert not any(
        t.name == "desktop-export-sync" and t.is_alive()
        for t in threading.enumerate()
    )


def test_worker_lease_held_while_final_sync_callback_is_blocked(tmp_path):
    import time

    from app.services.batch_queue import (
        WorkerLease,
        WorkerLeaseBusyError,
        append_queue_job,
    )
    from scripts import test_video_batch

    queue_path = tmp_path / "batch_queue.json"
    lease_path = tmp_path / "batch_worker.lock"
    append_queue_job(str(tmp_path / "videos"), path=queue_path)
    callback_running = threading.Event()
    release_callback = threading.Event()
    results = {}

    def sync_callback():
        callback_running.set()
        assert release_callback.wait(timeout=10)
        return DesktopSyncReport()

    def run_worker():
        results["result"] = test_video_batch.run_queue(
            str(queue_path),
            process_job=lambda job: 0,
            worker_lease_file=lease_path,
            pid_file=tmp_path / "batch.pid",
            sync_on_success=True,
            sync_callback=sync_callback,
        )

    worker = threading.Thread(target=run_worker)
    worker.start()
    try:
        assert callback_running.wait(timeout=10)
        # The worker lease must still be held while the final reconciliation
        # callback is blocked, so no other worker can overlap it.
        with pytest.raises(WorkerLeaseBusyError):
            WorkerLease(lease_path, mode="queue").acquire()
    finally:
        release_callback.set()
        worker.join(timeout=10)

    assert not worker.is_alive()
    assert results["result"] == 0
    # Lease is released only after the sync drain completed.
    probe = WorkerLease(lease_path, mode="queue").acquire()
    probe.release()


def test_legacy_queue_two_successes_run_background_serialized_and_coalesced(
    tmp_path,
):
    import threading
    import time

    from app.services.batch_queue import append_queue_job
    from scripts import test_video_batch

    queue_path = tmp_path / "batch_queue.json"
    append_queue_job(str(tmp_path / "folder-one"), path=queue_path)
    append_queue_job(str(tmp_path / "folder-two"), path=queue_path)

    sync_started = threading.Event()
    release_first = threading.Event()
    sync2_started = threading.Event()
    events = []
    events_lock = threading.Lock()
    max_concurrent = {"value": 0}
    current = {"value": 0}
    order = []

    def record(name):
        with events_lock:
            events.append(name)

    def sync_callback():
        current["value"] += 1
        max_concurrent["value"] = max(max_concurrent["value"], current["value"])
        try:
            record("sync-start")
            if len([e for e in events if e == "sync-start"]) == 1:
                sync_started.set()
                assert release_first.wait(timeout=10)
            else:
                sync2_started.set()
            return DesktopSyncReport()
        finally:
            current["value"] -= 1

    def process_job(job):
        record(Path(job["directory"]).name)
        if job["directory"].endswith("folder-two"):
            # Queue keeps running; the first sync is already in flight.
            assert sync_started.wait(timeout=10)
        return 0

    result = test_video_batch.run_queue(
        str(queue_path),
        process_job=process_job,
        sync_on_success=True,
        sync_callback=sync_callback,
    )

    assert result == 0
    assert sync2_started.wait(timeout=10)
    with events_lock:
        order = list(events)
    assert order.count("folder-one") == 1
    assert order.count("folder-two") == 1
    assert order.count("sync-start") == 2
    # process_job for folder-two only returned after the first sync was
    # already in flight, so the queue demonstrably continued while the
    # reconciliation ran in the background.
    assert order.index("folder-one") < order.index("sync-start")
    # The second trigger (raised while the first run was active) caused
    # exactly one follow-up run, never a concurrent one.
    assert max_concurrent["value"] == 1


def test_wait_until_idle_cannot_return_before_consumed_callback_completes():
    import time

    from app.services.desktop_export_sync import DesktopSyncScheduler

    callback_started = threading.Event()
    release_callback = threading.Event()
    callback_done = threading.Event()

    def callback():
        callback_started.set()
        try:
            assert release_callback.wait(timeout=10)
            return DesktopSyncReport()
        finally:
            callback_done.set()

    scheduler = DesktopSyncScheduler(callback)
    stop_event = threading.Event()
    thread = scheduler.start(stop_event)
    wait_results = {}

    def wait_idle():
        scheduler.wait_until_idle(timeout=10)
        wait_results["returned"] = True

    try:
        scheduler.request_sync()
        assert callback_started.wait(timeout=10)
        waiter = threading.Thread(target=wait_idle)
        waiter.start()
        time.sleep(0.2)
        # The consumed callback is still blocked, so the waiter must not
        # have returned yet.
        assert waiter.is_alive()
        assert not wait_results.get("returned", False)
        release_callback.set()
        waiter.join(timeout=10)
        assert callback_done.wait(timeout=10)
        assert wait_results.get("returned") is True
    finally:
        release_callback.set()
        stop_event.set()
        thread.join(timeout=3)


def test_background_failure_logs_warning_without_propagating(caplog):
    import time

    from app.services.desktop_export_sync import DesktopSyncScheduler

    def failing_callback():
        raise OSError("simulated background sync failure")

    scheduler = DesktopSyncScheduler(failing_callback)
    stop_event = threading.Event()
    thread = scheduler.start(stop_event)
    try:
        scheduler.request_sync()
        deadline = time.monotonic() + 5
        while (
            not any("simulated background sync failure" in r.message for r in caplog.records)
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
    finally:
        stop_event.set()
        thread.join(timeout=3)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        "simulated background sync failure" in r.message for r in warnings
    )


def test_direct_mode_success_runs_sync_and_failure_does_not(tmp_path):
    from scripts import test_video_batch

    sync_calls = []
    processed = []

    def fake_process(video_dir, limit, extensions, force):
        processed.append(video_dir)
        return 0

    result = test_video_batch.run_direct(
        str(tmp_path / "videos"),
        0,
        ".mp4",
        False,
        worker_lease_file=tmp_path / "direct.lock",
        pid_file=tmp_path / "direct.pid",
        process_directory=fake_process,
        sync_on_success=True,
        sync_callback=lambda: sync_calls.append("run") or DesktopSyncReport(),
    )

    assert result == 0
    assert processed == [str(tmp_path / "videos")]
    assert sync_calls == ["run"]

    sync_calls.clear()

    def fake_failure(video_dir, limit, extensions, force):
        processed.append(video_dir)
        return 1

    result = test_video_batch.run_direct(
        str(tmp_path / "videos"),
        0,
        ".mp4",
        False,
        worker_lease_file=tmp_path / "direct.lock",
        pid_file=tmp_path / "direct.pid",
        process_directory=fake_failure,
        sync_on_success=True,
        sync_callback=lambda: sync_calls.append("run") or DesktopSyncReport(),
    )

    assert result == 1
    assert sync_calls == []


# ---------------------------------------------------------------------------
# Task-engine first-transition hook
# ---------------------------------------------------------------------------


def test_task_engine_first_succeeded_transition_schedules_once(monkeypatch, tmp_path):
    from app.task_engine.models import CreateJob
    from app.task_engine.orchestrator import (
        _set_job_status,
        advance_job,
        initialize_job,
    )
    from app.task_engine.repository import TaskRepository
    from app.task_engine.schema import connect_task_db

    conn = connect_task_db(tmp_path / "task.db")
    repo = TaskRepository(conn)
    empty = tmp_path / "empty"
    empty.mkdir()
    job = repo.create_job(CreateJob(directory=str(empty), config_json="{}"))

    requested = []
    monkeypatch.setattr(
        "app.task_engine.orchestrator.request_background_sync",
        lambda: requested.append(1),
    )

    videos = initialize_job(repo, job.job_id)
    assert videos == []
    row = repo.conn.execute(
        "SELECT status FROM task_jobs WHERE job_id=?", (job.job_id,)
    ).fetchone()
    assert row["status"] == "succeeded"
    assert len(requested) == 1

    # Terminal re-entry must not schedule again.
    status = advance_job(repo, job.job_id)
    assert status == "succeeded"
    assert len(requested) == 1
    conn.close()


def test_task_engine_non_success_terminal_does_not_schedule(monkeypatch, tmp_path):
    from app.task_engine.models import CreateJob
    from app.task_engine.orchestrator import initialize_job
    from app.task_engine.repository import TaskRepository
    from app.task_engine.schema import connect_task_db

    conn = connect_task_db(tmp_path / "task.db")
    repo = TaskRepository(conn)
    empty = tmp_path / "empty"
    empty.mkdir()
    job = repo.create_job(CreateJob(directory=str(empty), config_json="{}"))
    requested = []
    monkeypatch.setattr(
        "app.task_engine.orchestrator.request_background_sync",
        lambda: requested.append(1),
    )

    repo.conn.execute(
        "UPDATE task_jobs SET status='needs_attention' WHERE job_id=?",
        (job.job_id,),
    )
    repo.conn.commit()
    initialize_job(repo, job.job_id)

    assert requested == []
    conn.close()


# ---------------------------------------------------------------------------
# Launcher startup integration
# ---------------------------------------------------------------------------


def _fake_webview_start():
    raise SystemExit(0)


def _install_fake_webview(monkeypatch):
    """Install a fake ``webview`` module before launcher.main() imports it."""
    import sys
    import types

    class FakeClosedEvent:
        def __init__(self):
            self.callback = None

        def __iadd__(self, callback):
            self.callback = callback
            return self

    class FakeWindow:
        def __init__(self):
            self.events = types.SimpleNamespace(closed=FakeClosedEvent())

    fake = types.ModuleType("webview")
    fake.create_window = lambda *args, **kwargs: FakeWindow()
    fake.start = _fake_webview_start
    sys.modules["webview"] = fake


def _install_os_exit_guard(monkeypatch):
    """Convert ``os._exit`` into SystemExit so pytest survives shutdown."""

    def fake_os_exit(code):
        raise SystemExit(code)

    monkeypatch.setattr("os._exit", fake_os_exit)


def test_startup_runs_initial_sync_after_db_init_before_task_worker(
    monkeypatch, tmp_path, capsys
):
    from app.ui import launcher

    db_path = tmp_path / "library.db"
    monkeypatch.setenv(ENV_LIBRARY_DB, str(db_path))
    monkeypatch.setenv(ENV_ADAPTIVE_SOURCE_ROOT, str(tmp_path / "src"))
    monkeypatch.setenv(ENV_FAVORITE_GIF_DEST, str(tmp_path / "fav"))
    monkeypatch.setenv(ENV_PBF_DEST, str(tmp_path / "pbf"))
    _make_library_db(db_path, [])

    calls = []
    monkeypatch.setattr(launcher, "_init_database", lambda: calls.append("db"))
    monkeypatch.setattr(launcher, "_start_task_worker", lambda: (None, None))
    monkeypatch.setattr(launcher, "start_api_server", lambda: None)
    monkeypatch.setattr(launcher, "_wait_for_url", lambda *a, **k: True)
    monkeypatch.setattr(launcher, "launch_gradio_app", lambda app: None)
    monkeypatch.setattr(
        launcher, "start_background_sync", lambda event: calls.append("bg")
    )
    monkeypatch.setattr(launcher, "stop_background_sync", lambda event=None: None)
    _install_fake_webview(monkeypatch)
    _install_os_exit_guard(monkeypatch)

    with pytest.raises(SystemExit):
        launcher.main()

    assert calls.index("db") < calls.index("bg")
    output = capsys.readouterr().out
    assert "[desktop-sync]" in output


def test_startup_sync_failure_logs_warning_and_startup_continues(
    monkeypatch, capsys
):
    from app.ui import launcher

    calls = []
    monkeypatch.setattr(launcher, "_init_database", lambda: calls.append("db"))
    monkeypatch.setattr(launcher, "_start_task_worker", lambda: (None, None))

    def failing_sync():
        calls.append("sync")
        raise OSError("sync exploded")

    monkeypatch.setattr(launcher, "_run_startup_sync", failing_sync)
    monkeypatch.setattr(launcher, "start_api_server", lambda: None)
    monkeypatch.setattr(launcher, "_wait_for_url", lambda *a, **k: True)
    monkeypatch.setattr(launcher, "launch_gradio_app", lambda app: None)
    monkeypatch.setattr(
        launcher, "start_background_sync", lambda event: calls.append("bg")
    )
    monkeypatch.setattr(launcher, "stop_background_sync", lambda event=None: None)
    _install_fake_webview(monkeypatch)
    _install_os_exit_guard(monkeypatch)

    with pytest.raises(SystemExit):
        launcher.main()

    assert calls.index("db") < calls.index("sync") < calls.index("bg")
    output = capsys.readouterr().out
    assert "WARNING: desktop export synchronization failed" in output


def test_window_close_forces_process_exit_when_graceful_shutdown_blocks():
    from app.ui import launcher

    class FakeClosedEvent:
        def __init__(self):
            self.callback = None

        def __iadd__(self, callback):
            self.callback = callback
            return self

    class FakeWindow:
        def __init__(self):
            self.events = type("Events", (), {"closed": FakeClosedEvent()})()

    window = FakeWindow()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    exit_called = threading.Event()
    exit_codes = []

    def blocking_cleanup():
        cleanup_started.set()
        release_cleanup.wait(timeout=1.0)

    def record_exit(code):
        exit_codes.append(code)
        exit_called.set()

    launcher._register_window_shutdown(
        window,
        blocking_cleanup,
        force_timeout=0.02,
        exit_process=record_exit,
    )

    callback_thread = threading.Thread(target=window.events.closed.callback)
    callback_thread.start()
    try:
        assert cleanup_started.wait(timeout=0.5)
        assert exit_called.wait(timeout=0.5)
        assert exit_codes[0] == 0
    finally:
        release_cleanup.set()
        callback_thread.join(timeout=1.0)


def test_graceful_shutdown_stops_ollama_runtime(monkeypatch, capsys):
    """Desktop shutdown must invoke the Ollama runtime shutdown exactly once."""
    from app.ui import launcher

    calls = []
    monkeypatch.setattr(launcher, "_init_database", lambda: calls.append("db"))
    monkeypatch.setattr(launcher, "_run_startup_sync", lambda: calls.append("sync"))
    monkeypatch.setattr(launcher, "_start_task_worker", lambda: (None, None))
    monkeypatch.setattr(launcher, "start_api_server", lambda: None)
    monkeypatch.setattr(launcher, "_wait_for_url", lambda *a, **k: True)
    monkeypatch.setattr(launcher, "launch_gradio_app", lambda app: None)
    monkeypatch.setattr(
        launcher, "start_background_sync", lambda event: calls.append("bg")
    )
    monkeypatch.setattr(launcher, "stop_background_sync", lambda event=None: None)

    shutdown_calls = []
    monkeypatch.setattr(
        launcher.ollama_runtime,
        "shutdown_runtime",
        lambda: shutdown_calls.append(1),
    )
    _install_fake_webview(monkeypatch)
    _install_os_exit_guard(monkeypatch)

    with pytest.raises(SystemExit):
        launcher.main()

    assert shutdown_calls == [1]

"""W4 kill tests for desktop_export_sync mutation survivors.

Targets survivors from the cosmic-ray run recorded in
``<sprint-root>/w4/mutation_scores/cosmic-ray/desktop_export_sync.json``:

- ``_is_inside`` boundary operators: paths lexicographically before/after the
  root and a favorite path equal to the root must be classified exactly.
- ``_relocate_favorite_source``: root-name match at the first path segment,
  and rejection of ``..`` candidates escaping the configured root.
- ``_needs_copy``/``_sha256_file`` hash fallback: equal-size files with
  different mtimes are decided by content hashing, not size or mtime.
- case-collision report reasons must name the other colliding file.
- favorite row scan continues past missing/outside rows regardless of row
  order (``continue`` vs ``break``).
- scheduler loop: exits after the stop event, still drains a trigger that was
  coalesced before shutdown, and logs the report line on success.
- module-level default scheduler wires ``request_background_sync`` to a real
  reconciliation run.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from app.services import desktop_export_sync as sync_module
from app.services.desktop_export_sync import (
    DesktopSyncReport,
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
def sync_env(monkeypatch, tmp_path) -> dict[str, Path]:
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
        "tmp": tmp_path,
    }


# ---------------------------------------------------------------------------
# _is_inside boundary classification
# ---------------------------------------------------------------------------


def test_outside_paths_sorting_before_or_after_root_stay_outside(sync_env):
    """``_is_inside`` must use the path-prefix rule, not lexicographic order.

    Kills ``p == r`` -> ``<``/``<=`` (outside path sorting before the root
    would be classified inside) and ``>``/``>=`` (outside path sorting after
    the root would be classified inside).
    """
    before = _write_test_file(sync_env["tmp"] / "aaa-before" / "b.gif", b"before")
    after = _write_test_file(sync_env["tmp"] / "zzz-after" / "c.gif", b"after")
    assert str(before) < str(sync_env["root"]) < str(after)
    _make_library_db(
        sync_env["db"],
        [("cand-before", str(before)), ("cand-after", str(after))],
    )

    report = run_reconciliation()

    assert report.gif_summary["copied"] == 0
    assert report.gif_summary["missing"] == 2
    reasons = {e.reason for e in report.gifs["missing"]}
    assert reasons == {"source outside adaptive export root"}
    assert not (sync_env["fav_dest"] / "b.gif").exists()
    assert not (sync_env["fav_dest"] / "c.gif").exists()


def test_favorite_path_equal_to_source_root_is_inside(sync_env):
    """A favorite whose full_path equals the source root itself (a file) is
    the ``p == r`` branch of ``_is_inside`` and must be classified inside."""
    root_file = sync_env["tmp"] / "root-is-the-file.gif"
    _write_test_file(root_file, b"self")
    _make_library_db(sync_env["db"], [("cand-self", str(root_file))])

    report = run_reconciliation(source_root=root_file)

    assert report.gif_summary["copied"] == 1
    assert (sync_env["fav_dest"] / "root-is-the-file.gif").read_bytes() == b"self"


# ---------------------------------------------------------------------------
# _relocate_favorite_source
# ---------------------------------------------------------------------------


def test_stale_path_with_root_name_at_first_segment_is_relocated(sync_env):
    """The relocation scan must also accept a match at parts[0]; mutating the
    range stop misses it and reports the favorite as missing."""
    actual = _write_test_file(sync_env["root"] / "first.gif", b"first")
    stale = Path(sync_env["root"].name) / "first.gif"
    assert stale.parts[0].casefold() == sync_env["root"].name.casefold()
    _make_library_db(sync_env["db"], [("cand-first", str(stale))])

    report = run_reconciliation()

    assert report.gif_summary["copied"] == 1
    assert report.gif_summary["missing"] == 0
    assert (sync_env["fav_dest"] / actual.name).read_bytes() == b"first"


def test_relocation_rejects_candidate_escaping_source_root(sync_env):
    """A stale path whose only root-name match yields a candidate outside the
    configured root (via ``..``) must not be relocated or copied."""
    escape = sync_env["tmp"] / "escape.gif"
    escape.write_bytes(b"escape")
    stale = (
        sync_env["tmp"]
        / "old-checkout"
        / "data"
        / "exports"
        / sync_env["root"].name
        / ".."
        / ".."
        / "escape.gif"
    )
    assert ".." in stale.parts
    _make_library_db(sync_env["db"], [("cand-escape", str(stale))])

    report = run_reconciliation()

    assert report.gif_summary["copied"] == 0
    assert report.gif_summary["missing"] == 1
    assert not (sync_env["fav_dest"] / "escape.gif").exists()


# ---------------------------------------------------------------------------
# _needs_copy hash fallback (equal size, mtime inconclusive)
# ---------------------------------------------------------------------------


def test_equal_size_different_content_is_updated_via_hash(sync_env):
    """Equal file sizes with different mtimes must fall back to content
    hashing; a changed file is updated, never skipped."""
    src = _write_test_file(sync_env["root"] / "hash.gif", b"AAAA")
    _make_library_db(sync_env["db"], [("cand-hash", str(src))])
    assert run_reconciliation().gif_summary["copied"] == 1

    _write_test_file(src, b"AAAB", mtime=1_700_000_500.0)
    assert src.stat().st_size == (sync_env["fav_dest"] / "hash.gif").stat().st_size

    report = run_reconciliation()

    assert report.gif_summary["updated"] == 1
    assert report.gif_summary["skipped"] == 0
    assert (sync_env["fav_dest"] / "hash.gif").read_bytes() == b"AAAB"


def test_equal_size_identical_content_is_skipped_via_hash(sync_env):
    """Equal sizes with different mtimes but identical bytes must hash equal
    and be skipped, never rewritten."""
    src = _write_test_file(sync_env["root"] / "same.gif", b"SAME")
    _make_library_db(sync_env["db"], [("cand-same", str(src))])
    assert run_reconciliation().gif_summary["copied"] == 1

    _write_test_file(src, b"SAME", mtime=1_700_000_500.0)
    report = run_reconciliation()

    assert report.gif_summary["skipped"] == 1
    assert report.gif_summary["updated"] == 0
    assert report.gif_summary["copied"] == 0


# ---------------------------------------------------------------------------
# Collision report reasons
# ---------------------------------------------------------------------------


def test_collision_reason_names_the_other_colliding_file(sync_env):
    """Every conflict entry must name the OTHER member of the colliding
    group; an empty/self-only reason list is a mutation tell."""
    gif_a = _write_test_file(sync_env["root"] / "one" / "same.gif", b"first")
    gif_b = _write_test_file(sync_env["root"] / "two" / "SAME.GIF", b"second")
    _make_library_db(
        sync_env["db"], [("cand-a", str(gif_a)), ("cand-b", str(gif_b))]
    )

    report = run_reconciliation()

    assert len(report.gifs["conflicts"]) == 2
    for entry in report.gifs["conflicts"]:
        other = gif_b if Path(entry.source) == gif_a else gif_a
        assert Path(other).name in entry.reason


# ---------------------------------------------------------------------------
# Favorite row scan: continue past problem rows in any order
# ---------------------------------------------------------------------------


def test_favorite_scan_continues_past_missing_row_before_good_row(sync_env):
    """A not-found row that sorts BEFORE the good row must not stop the row
    scan (``continue``, not ``break``)."""
    missing = sync_env["root"] / "000-missing.gif"
    good = _write_test_file(sync_env["root"] / "111-good.gif", b"good")
    _make_library_db(
        sync_env["db"],
        [("cand-missing", str(missing)), ("cand-good", str(good))],
    )

    report = run_reconciliation()

    assert report.gif_summary["copied"] == 1
    assert (sync_env["fav_dest"] / "111-good.gif").read_bytes() == b"good"
    assert report.gif_summary["missing"] == 1
    assert report.gifs["missing"][0].reason == "source not found"


def test_favorite_scan_continues_past_outside_row_before_good_row(
    monkeypatch, sync_env
):
    """An outside-root row that sorts BEFORE the good row must not stop the
    row scan; the outside directory name sorts before the root directory."""
    root = sync_env["tmp"] / "adaptive_test"
    outside_dir = sync_env["tmp"] / "adapt"
    assert str(outside_dir) < str(root)
    outside = _write_test_file(outside_dir / "000-outside.gif", b"outside")
    good = _write_test_file(root / "111-good.gif", b"good")
    assert str(outside) < str(good)
    monkeypatch.setenv(ENV_ADAPTIVE_SOURCE_ROOT, str(root))
    _make_library_db(
        sync_env["db"],
        [("cand-outside", str(outside)), ("cand-good", str(good))],
    )

    report = run_reconciliation()

    assert report.gif_summary["copied"] == 1
    assert (sync_env["fav_dest"] / "111-good.gif").read_bytes() == b"good"
    assert report.gif_summary["missing"] == 1
    assert report.gifs["missing"][0].reason == (
        "source outside adaptive export root"
    )


# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------


def test_default_destination_roots_resolve_under_home(monkeypatch, tmp_path):
    """With the destination env overrides unset, defaults resolve under HOME."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(ENV_FAVORITE_GIF_DEST, raising=False)
    monkeypatch.delenv(ENV_PBF_DEST, raising=False)

    config = get_config()

    assert config["favorite_dest"] == (
        Path(tmp_path) / "Desktop" / "entertainment" / "favorite_gifs"
    )
    assert config["pbf_dest"] == (
        Path(tmp_path) / "Desktop" / "entertainment" / "bookmarks" / "PBF"
    )


# ---------------------------------------------------------------------------
# Background scheduler loop
# ---------------------------------------------------------------------------


def test_scheduler_thread_exits_once_stop_event_is_set():
    runs = []

    def callback():
        runs.append(1)
        return DesktopSyncReport()

    scheduler = sync_module.DesktopSyncScheduler(callback)
    stop_event = threading.Event()
    thread = scheduler.start(stop_event)
    try:
        scheduler.request_sync()
        deadline = time.monotonic() + 5
        while not runs and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runs
    finally:
        stop_event.set()
        thread.join(timeout=5)

    assert not thread.is_alive()


def test_stop_event_with_pending_trigger_still_drains_final_run():
    """A trigger coalesced while a run is active must still be consumed after
    the stop event is set — shutdown drains the final reconciliation."""
    started = threading.Event()
    release = threading.Event()
    runs = []

    def callback():
        runs.append(len(runs))
        if len(runs) == 1:
            started.set()
            assert release.wait(timeout=10)
        return DesktopSyncReport()

    scheduler = sync_module.DesktopSyncScheduler(callback)
    stop_event = threading.Event()
    thread = scheduler.start(stop_event)
    try:
        scheduler.request_sync()
        assert started.wait(timeout=5)
        scheduler.request_sync()  # coalesced while the first run is active
        stop_event.set()
        release.set()
        deadline = time.monotonic() + 5
        while len(runs) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        release.set()
        stop_event.set()
        thread.join(timeout=5)

    assert len(runs) == 2
    assert not thread.is_alive()


def test_successful_background_run_logs_report_line(caplog):
    caplog.set_level(logging.INFO)

    def callback():
        return DesktopSyncReport()

    scheduler = sync_module.DesktopSyncScheduler(callback)
    stop_event = threading.Event()
    thread = scheduler.start(stop_event)
    try:
        scheduler.request_sync()
        scheduler.wait_until_idle(timeout=10)
    finally:
        stop_event.set()
        thread.join(timeout=5)

    info_lines = [
        r.getMessage() for r in caplog.records if r.levelname == "INFO"
    ]
    assert any("[desktop-sync]" in line for line in info_lines)


# ---------------------------------------------------------------------------
# Module-level default scheduler wiring
# ---------------------------------------------------------------------------


def test_module_level_background_sync_runs_requested_reconciliation(
    monkeypatch, sync_env
):
    """``request_background_sync`` + ``wait_until_idle`` drive the shared
    default scheduler through a real (env-scoped) reconciliation."""
    gif = _write_test_file(sync_env["root"] / "bg.gif", b"bg")
    _make_library_db(sync_env["db"], [("cand-bg", str(gif))])

    stop_event = threading.Event()
    try:
        thread = sync_module.start_background_sync(stop_event)
        assert thread is not None
        sync_module.request_background_sync()
        sync_module.wait_until_idle(timeout=30)
        assert (sync_env["fav_dest"] / "bg.gif").read_bytes() == b"bg"
    finally:
        sync_module.stop_background_sync(stop_event)

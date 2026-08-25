"""Task 8: shared parallel frame extraction service.

Every test injects a fake ``runner`` so no real ffmpeg is required; the
production default (``runner=subprocess.run``) is exercised only by the
call sites that migrate to this module.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

from app.services.frame_extract import FrameExtractResult, extract_frames


class _FakeCompleted:
    def __init__(self, returncode: int, stderr: bytes = b""):
        self.returncode = returncode
        self.stdout = b""
        self.stderr = stderr


def _ss_value(cmd: list[str]) -> float:
    """Pull the ``-ss`` argument back out of an emitted ffmpeg command."""
    return float(cmd[cmd.index("-ss") + 1])


def test_results_are_ordered_by_timestamp_regardless_of_completion_order(tmp_path):
    # The lowest timestamp sleeps the longest, so it would finish LAST if
    # results simply followed completion order.
    def runner(cmd, **kwargs):
        if _ss_value(cmd) == 1.0:
            time.sleep(0.2)
        return _FakeCompleted(0)

    results = extract_frames(
        "video.mp4", [3.0, 1.0, 2.0], str(tmp_path),
        workers=3, runner=runner,
    )
    assert [r.timestamp_s for r in results] == [1.0, 2.0, 3.0]
    assert all(isinstance(r, FrameExtractResult) for r in results)


def test_workers_equal_one_issues_calls_in_ascending_timestamp_order(tmp_path):
    seen: list[float] = []

    def runner(cmd, **kwargs):
        seen.append(_ss_value(cmd))
        return _FakeCompleted(0)

    extract_frames(
        "video.mp4", [5.0, 1.0, 3.0], str(tmp_path),
        workers=1, runner=runner,
    )
    assert seen == [1.0, 3.0, 5.0]


def test_nonzero_returncode_yields_failure_with_code_preserved(tmp_path):
    def runner(cmd, **kwargs):
        return _FakeCompleted(7, stderr=b"boom")

    results = extract_frames("video.mp4", [1.0], str(tmp_path), runner=runner)

    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].returncode == 7
    assert "7" in results[0].error
    assert "boom" in results[0].error


def test_timeout_yields_failure_with_an_attributable_error(tmp_path):
    def runner(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

    results = extract_frames(
        "video.mp4", [2.5], str(tmp_path), timeout_s=9.0, runner=runner,
    )

    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].returncode is None
    assert "2.5" in results[0].error
    assert "timed out" in results[0].error


def test_workers_equal_one_command_matches_legacy_shape_plus_new_flags(tmp_path):
    captured = {}

    def runner(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeCompleted(0)

    results = extract_frames(
        "video.mp4", [10], str(tmp_path), workers=1, runner=runner,
    )

    out_path = results[0].path
    assert captured["cmd"] == [
        "ffmpeg", "-y", "-ss", "10", "-i", "video.mp4",
        "-vf", "scale=640:-1", "-vframes", "1",
        "-an", "-sn", "-q:v", "3",
        out_path,
    ]
    assert captured["kwargs"] == {"capture_output": True, "timeout": 15.0}
    assert os.path.isabs(out_path)


def test_custom_width_and_jpeg_quality_flow_into_the_command(tmp_path):
    captured = {}

    def runner(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompleted(0)

    extract_frames(
        "video.mp4", [1.0], str(tmp_path),
        width=720, jpeg_quality=5, runner=runner,
    )
    assert "scale=720:-1" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("-q:v") + 1] == "5"


def test_workers_greater_than_timestamp_count_does_not_oversubscribe(tmp_path):
    active = 0
    peak = 0
    lock = threading.Lock()

    def runner(cmd, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return _FakeCompleted(0)

    results = extract_frames(
        "video.mp4", [1.0, 2.0], str(tmp_path),
        workers=8, runner=runner,
    )

    assert len(results) == 2
    assert peak <= 2, f"only 2 timestamps but saw {peak} concurrent calls"


def test_default_runner_is_resolved_at_call_time(tmp_path, monkeypatch):
    seen: list[list[str]] = []

    def patched_run(cmd, **kwargs):
        seen.append(cmd)
        return _FakeCompleted(0)

    monkeypatch.setattr(
        "app.services.frame_extract.subprocess.run", patched_run
    )
    extract_frames("video.mp4", [1.0], str(tmp_path))
    assert seen and seen[0][0] == "ffmpeg"


def test_empty_timestamps_returns_empty_list_without_touching_out_dir(tmp_path):
    missing_dir = tmp_path / "does_not_exist_yet"
    results = extract_frames(
        "video.mp4", [], str(missing_dir), runner=lambda *a, **k: _FakeCompleted(0),
    )
    assert results == []
    assert not missing_dir.exists()


def test_accurate_seek_places_ss_after_input(tmp_path):
    captured = {}

    def runner(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompleted(0)

    results = extract_frames(
        "video.mp4", [10], str(tmp_path),
        workers=1, runner=runner, accurate_seek=True,
    )
    cmd = captured["cmd"]
    assert cmd.index("-i") < cmd.index("-ss")
    assert cmd[cmd.index("-ss") + 1] == "10"
    assert results[0].ok is True


def test_out_dir_is_created_when_missing(tmp_path):
    target_dir = tmp_path / "fresh" / "nested"
    extract_frames(
        "video.mp4", [1.0], str(target_dir),
        runner=lambda *a, **k: _FakeCompleted(0),
    )
    assert target_dir.exists()

from app.ui.candidate_review import is_batch_command_line


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


def test_batch_command_line_matches_frozen_batch_runner():
    command_line = (
        r'"C:\app\GifAgentUI.exe" --run-script '
        r'"C:\app\_internal\scripts\test_video_batch.py" --dir C:\videos'
    )

    assert is_batch_command_line(command_line)


def test_batch_command_line_matches_source_batch_runner():
    command_line = r"uv run python -u scripts/test_video_batch.py --dir C:\videos"

    assert is_batch_command_line(command_line)


def test_batch_command_line_rejects_gui_and_unrelated_processes():
    assert not is_batch_command_line(r'"C:\app\GifAgentUI.exe"')
    assert not is_batch_command_line(r"C:\Windows\System32\notepad.exe")
    assert not is_batch_command_line("")
    assert not is_batch_command_line(None)


def test_format_batch_status_keeps_persisted_queue_error_visible():
    from app.ui.candidate_review import format_batch_status

    text = format_batch_status({
        "running": False,
        "queue_state": "starting",
        "queue_worker_pid": 555,
        "cleanup_pending": True,
        "last_error": "worker handshake failed",
    })

    assert "Queue State: starting" in text
    assert "Queue Worker PID: 555" in text
    assert "Cleanup Pending: YES" in text
    assert "Last Error: worker handshake failed" in text

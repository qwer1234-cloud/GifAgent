from app.ui.candidate_review import is_batch_command_line


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

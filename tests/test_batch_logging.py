def test_format_gif_export_line_contains_video_gif_path_and_result():
    from app.services.batch_logging import format_gif_export_line

    line = format_gif_export_line(
        video_name="VideoA",
        index=1,
        total=3,
        output_path="data/exports/VideoA@@@001.gif",
        status="OK",
        worthiness=0.91,
        duration_s=4.5,
        timestamp_s=27,
        merged=True,
        frame_count=2,
        size_bytes=2048,
        emotional_core="laugh",
    )

    assert "[GIF 1/3]" in line
    assert "VideoA" in line
    assert "OK" in line
    assert "data/exports/VideoA@@@001.gif" in line
    assert "score=0.91" in line
    assert "size=2KB" in line


def test_read_batch_log_returns_full_utf8_content(tmp_path):
    from app.services.batch_logging import read_batch_log

    path = tmp_path / "batch_subprocess.log"
    path.write_text("[VIDEO] A\n[GIF 1/1] OK: 浣犲ソ.gif\n", encoding="utf-8")

    assert read_batch_log(path) == "[VIDEO] A\n[GIF 1/1] OK: 浣犲ソ.gif\n"


def test_format_gif_export_line_includes_failed_status():
    from app.services.batch_logging import format_gif_export_line

    line = format_gif_export_line(
        video_name="VideoA",
        index=2,
        total=3,
        output_path="data/exports/VideoA@@@002.gif",
        status="FAILED",
        worthiness=0.45,
        duration_s=3.0,
        timestamp_s=42,
        merged=False,
        frame_count=1,
        emotional_core="sadness",
    )

    assert "[GIF 2/3]" in line
    assert "status=FAILED" in line
    assert "data/exports/VideoA@@@002.gif" in line


def test_read_batch_log_replaces_malformed_utf8_bytes(tmp_path):
    from app.services.batch_logging import read_batch_log

    path = tmp_path / "batch_subprocess.log"
    path.write_bytes(b"[GIF 1/1] status=OK \xff\n")

    assert read_batch_log(path) == "[GIF 1/1] status=OK \ufffd\n"


def test_failed_ffmpeg_with_existing_output_is_not_a_success():
    from app.services.batch_logging import is_successful_gif_export

    assert not is_successful_gif_export(
        ffmpeg_failed=True,
        output_exists=True,
    )


def test_gif_attempt_rejects_unchanged_stale_output(tmp_path):
    from app.services.batch_logging import run_gif_export_attempt

    output = tmp_path / "clip.gif"
    palette = tmp_path / "palette.png"
    output.write_bytes(b"old gif")

    class Result:
        returncode = 0

    result = run_gif_export_attempt(
        palette_command=["palette"],
        gif_command=["gif"],
        palette_path=palette,
        output_path=output,
        runner=lambda *_args, **_kwargs: Result(),
    )

    assert not result.success
    assert result.size_bytes == 0
    assert result.error == "output file was not created"
    assert not output.exists()


def test_gif_attempt_rejects_new_zero_length_output(tmp_path):
    from app.services.batch_logging import run_gif_export_attempt

    output = tmp_path / "clip.gif"
    palette = tmp_path / "palette.png"
    calls = []

    class Result:
        returncode = 0

    def runner(*_args, **_kwargs):
        calls.append(True)
        if len(calls) == 2:
            output.write_bytes(b"")
        return Result()

    result = run_gif_export_attempt(
        palette_command=["palette"],
        gif_command=["gif"],
        palette_path=palette,
        output_path=output,
        runner=runner,
    )

    assert not result.success
    assert result.size_bytes == 0
    assert result.error == "output file is empty"
    assert not output.exists()


def test_gif_attempt_accepts_only_new_nonempty_output(tmp_path):
    from app.services.batch_logging import run_gif_export_attempt

    output = tmp_path / "clip.gif"
    palette = tmp_path / "palette.png"
    calls = []

    class Result:
        returncode = 0

    def runner(*_args, **_kwargs):
        calls.append(True)
        if len(calls) == 2:
            output.write_bytes(b"GIF89a")
        return Result()

    result = run_gif_export_attempt(
        palette_command=["palette"],
        gif_command=["gif"],
        palette_path=palette,
        output_path=output,
        runner=runner,
    )

    assert result.success
    assert result.size_bytes == 6
    assert result.error == ""


def test_failed_gif_log_includes_failure_reason():
    from app.services.batch_logging import format_gif_export_line

    line = format_gif_export_line(
        video_name="clip",
        index=1,
        total=1,
        output_path="C:/exports/clip.gif",
        status="FAILED",
        worthiness=0.5,
        duration_s=2.0,
        timestamp_s=10,
        merged=False,
        frame_count=1,
        error="output file is empty",
    )

    assert "status=FAILED" in line
    assert "error=output file is empty" in line


def test_adaptive_exporter_uses_attempt_result_counts_and_nonzero_failure_exit():
    from pathlib import Path

    source = Path("scripts/test_video_adaptive.py").read_text(encoding="utf-8")

    assert "run_gif_export_attempt(" in source
    assert '"gif_attempted": gif_attempted' in source
    assert '"gif_succeeded": gif_succeeded' in source
    assert '"gif_failed": gif_failed' in source
    assert "if gif_failed:" in source
    assert "raise SystemExit(1)" in source

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

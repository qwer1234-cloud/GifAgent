def test_cleanup_adaptive_export_dir_removes_generated_outputs(tmp_path):
    from app.services.export_cleanup import cleanup_adaptive_export_dir

    export_dir = tmp_path / "VideoA"
    sample_dir = export_dir / "Sample"
    sample_dir.mkdir(parents=True)

    generated_gif = export_dir / "VideoA@@@001_10s-15s.gif"
    generated_gif.write_bytes(b"gif")
    palette = export_dir / "pal_001.png"
    palette.write_bytes(b"png")
    pbf = export_dir / "VideoA.pbf"
    pbf.write_bytes(b"pbf")
    sample = sample_dir / "VideoA_sample_01_10s_w0.90.jpg"
    sample.write_bytes(b"jpg")
    grid = sample_dir / "VideoA_grid.jpg"
    grid.write_bytes(b"jpg")

    note = export_dir / "keep.txt"
    note.write_text("keep", encoding="utf-8")
    other_sample = sample_dir / "OtherVideo_grid.jpg"
    other_sample.write_bytes(b"jpg")

    removed = cleanup_adaptive_export_dir(export_dir, video_name="VideoA")

    assert removed == 5
    assert not generated_gif.exists()
    assert not palette.exists()
    assert not pbf.exists()
    assert not sample.exists()
    assert not grid.exists()
    assert note.exists()
    assert other_sample.exists()

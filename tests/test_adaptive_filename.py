def test_build_gif_filename_uses_absolute_millisecond_offsets():
    from app.services.gif_naming import build_gif_filename

    assert build_gif_filename("movie", 1, 12.345, 17.89) == "movie@@@001_12345ms-17890ms.gif"


def test_parse_clip_filename_supports_milliseconds_and_legacy_seconds():
    from app.services.gif_naming import parse_clip_filename

    assert parse_clip_filename("movie@@@001_12345ms-17890ms.gif") == (12.345, 17.89)
    assert parse_clip_filename("movie@@@001_12s-17s.gif") == (12.0, 17.0)

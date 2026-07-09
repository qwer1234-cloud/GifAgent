from app.services.potplayer_bookmarks import (
    PotPlayerBookmark,
    build_pbf_text,
    format_bookmark_title,
    write_pbf_file,
)


def test_build_pbf_text_uses_potplayer_bookmark_shape():
    bookmarks = [
        PotPlayerBookmark(
            start_s=12.34,
            end_s=18.9,
            rank=1,
            score=0.7,
            merged=True,
            caption="close shot * with newline\ncaption",
        ),
        PotPlayerBookmark(
            start_s=61,
            end_s=64,
            rank=2,
            score=0.5,
            merged=False,
            caption="",
        ),
    ]

    text = build_pbf_text(bookmarks)

    assert text.splitlines() == [
        "[Bookmark]",
        "0=12340*#001 00:12-00:18 w=0.70 merged close shot - with newline caption*",
        "1=61000*#002 01:01-01:04 w=0.50 single*",
    ]


def test_write_pbf_file_uses_utf16_for_potplayer(tmp_path):
    out_path = tmp_path / "sample.pbf"
    write_pbf_file(
        out_path,
        [
            PotPlayerBookmark(
                start_s=1,
                end_s=2,
                rank=1,
                score=0.6,
                merged=False,
                caption="测试",
            )
        ],
    )

    raw = out_path.read_bytes()
    assert raw.startswith(b"\xff\xfe")
    assert "[Bookmark]" in raw.decode("utf-16")
    assert "测试" in raw.decode("utf-16")


def test_format_bookmark_title_limits_long_captions():
    title = format_bookmark_title(
        PotPlayerBookmark(
            start_s=0,
            end_s=5,
            rank=12,
            score=0.6,
            merged=False,
            caption="x" * 300,
        )
    )

    assert len(title) <= 180
    assert "*" not in title

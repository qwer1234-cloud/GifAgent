"""PotPlayer bookmark file helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class PotPlayerBookmark:
    start_s: float
    end_s: float
    rank: int
    score: float
    merged: bool
    caption: str = ""


def seconds_to_ms(seconds: float) -> int:
    return max(0, int(round(float(seconds) * 1000)))


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def sanitize_title(value: str | None) -> str:
    text = re.sub(r"[\r\n\t]+", " ", str(value or ""))
    text = text.replace("*", "-")
    return re.sub(r"\s+", " ", text).strip()


def format_bookmark_title(bookmark: PotPlayerBookmark, *, max_len: int = 180) -> str:
    kind = "merged" if bookmark.merged else "single"
    title = (
        f"#{bookmark.rank:03d} "
        f"{format_timestamp(bookmark.start_s)}-{format_timestamp(bookmark.end_s)} "
        f"w={bookmark.score:.2f} {kind}"
    )
    caption = sanitize_title(bookmark.caption)
    if caption:
        title = f"{title} {caption}"
    if len(title) > max_len:
        title = f"{title[: max_len - 3].rstrip()}..."
    return title.replace("*", "-")


def build_pbf_text(bookmarks: list[PotPlayerBookmark]) -> str:
    lines = ["[Bookmark]"]
    for idx, bookmark in enumerate(bookmarks):
        ms = seconds_to_ms(bookmark.start_s)
        title = format_bookmark_title(bookmark)
        lines.append(f"{idx}={ms}*{title}*")
    return "\r\n".join(lines) + "\r\n"


def write_pbf_file(path: str | Path, bookmarks: list[PotPlayerBookmark]) -> str:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = build_pbf_text(bookmarks)
    out_path.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    return str(out_path)

"""GIF export filename helpers with backwards-compatible parsing."""

from __future__ import annotations

import re
from pathlib import Path


_MILLISECOND_FILENAME_RE = re.compile(
    r"@@@\d+_(?P<start>\d+)ms-(?P<end>\d+)ms\.gif$", re.IGNORECASE
)
_SECOND_FILENAME_RE = re.compile(
    r"@@@\d+_(?P<start>\d+(?:\.\d+)?)s-(?P<end>\d+(?:\.\d+)?)s\.gif$",
    re.IGNORECASE,
)


def build_gif_filename(video_name: str, rank: int, start_sec: float, end_sec: float) -> str:
    """Build a GIF name using absolute offsets from the video start in ms."""
    start_ms = int(round(float(start_sec) * 1000))
    end_ms = int(round(float(end_sec) * 1000))
    return f"{video_name}@@@{int(rank):03d}_{start_ms}ms-{end_ms}ms.gif"


def parse_clip_filename(path: str | Path) -> tuple[float, float]:
    """Parse new millisecond names and legacy second names into seconds."""
    name = Path(path).name
    match = _MILLISECOND_FILENAME_RE.search(name)
    if match:
        return int(match.group("start")) / 1000.0, int(match.group("end")) / 1000.0
    match = _SECOND_FILENAME_RE.search(name)
    if match:
        return float(match.group("start")), float(match.group("end"))
    return 0.0, 0.0

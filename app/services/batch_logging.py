"""Formatting and reading helpers for adaptive GIF batch logs."""

from pathlib import Path


def format_gif_export_line(
    *,
    video_name: str,
    index: int,
    total: int,
    output_path: str,
    status: str,
    worthiness: float,
    duration_s: float,
    timestamp_s: int,
    merged: bool,
    frame_count: int,
    size_bytes: int = 0,
    emotional_core: str = "?",
) -> str:
    """Return the stable, human-readable log line for one GIF attempt."""
    size_kb = size_bytes // 1024
    merge_state = "merged" if merged else "single"
    return (
        f"[GIF {index}/{total}] video={video_name} status={status} "
        f"path={output_path} score={worthiness:.2f} duration={duration_s:.1f}s "
        f"timestamp={timestamp_s}s merge={merge_state} frames={frame_count} "
        f"size={size_kb}KB emotion={emotional_core}"
    )


def read_batch_log(path: str | Path) -> str:
    """Read all available batch log text, tolerating malformed UTF-8 bytes."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""

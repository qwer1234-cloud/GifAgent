"""Formatting and reading helpers for adaptive GIF batch logs."""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


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
    error: str = "",
) -> str:
    """Return the stable, human-readable log line for one GIF attempt."""
    size_kb = size_bytes // 1024
    merge_state = "merged" if merged else "single"
    line = (
        f"[GIF {index}/{total}] video={video_name} status={status} "
        f"path={output_path} score={worthiness:.2f} duration={duration_s:.1f}s "
        f"timestamp={timestamp_s}s merge={merge_state} frames={frame_count} "
        f"size={size_kb}KB emotion={emotional_core}"
    )
    if error:
        line += f" error={str(error).replace(chr(10), ' ')}"
    return line


def read_batch_log(path: str | Path) -> str:
    """Read all available batch log text, tolerating malformed UTF-8 bytes."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def is_successful_gif_export(
    *,
    ffmpeg_failed: bool,
    output_exists: bool,
    output_size: int = 1,
    output_is_new: bool = True,
) -> bool:
    """Return whether an export can be logged as successful."""
    return not ffmpeg_failed and output_exists and output_size > 0 and output_is_new


@dataclass(frozen=True)
class GifExportAttemptResult:
    success: bool
    size_bytes: int = 0
    error: str = ""


def run_gif_export_attempt(
    *,
    palette_command: Sequence[str],
    gif_command: Sequence[str],
    palette_path: str | Path,
    output_path: str | Path,
    runner: Callable = subprocess.run,
    timeout: float = 60,
) -> GifExportAttemptResult:
    """Run one GIF attempt and accept only a newly-created nonempty target."""
    palette = Path(palette_path)
    output = Path(output_path)

    def failed(error: str) -> GifExportAttemptResult:
        try:
            if output.exists() or output.is_symlink():
                output.unlink()
        except OSError:
            pass
        return GifExportAttemptResult(False, error=error)

    try:
        if output.exists() or output.is_symlink():
            output.unlink()
    except OSError as exc:
        return failed(f"cannot prepare output: {exc}")

    try:
        palette_result = runner(
            list(palette_command), capture_output=True, timeout=timeout
        )
        if palette_result.returncode != 0:
            return failed(f"palette ffmpeg exited {palette_result.returncode}")
        gif_result = runner(list(gif_command), capture_output=True, timeout=timeout)
        if gif_result.returncode != 0:
            return failed(f"GIF ffmpeg exited {gif_result.returncode}")
    except subprocess.TimeoutExpired:
        return failed("ffmpeg timed out")
    except OSError as exc:
        return failed(f"cannot run ffmpeg: {exc}")
    except Exception as exc:
        return failed(f"unexpected ffmpeg error: {type(exc).__name__}: {exc}")
    finally:
        try:
            if palette.exists() or palette.is_symlink():
                palette.unlink()
        except OSError:
            pass

    try:
        if not output.is_file():
            return failed("output file was not created")
        size_bytes = output.stat().st_size
    except OSError as exc:
        return failed(f"cannot inspect output: {exc}")
    if size_bytes <= 0:
        return failed("output file is empty")
    return GifExportAttemptResult(True, size_bytes=size_bytes)

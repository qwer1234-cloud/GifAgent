"""Shared parallel frame extraction for the adaptive GIF pipeline.

Six near-identical ``subprocess.run(["ffmpeg", ...])`` call sites (Direct
coarse/refine/action-rescore, Staged sample/refine/rank_dedup action
rescore) used to hand-roll the same one-frame-per-timestamp ffmpeg
invocation.  Consolidating them here means the command shape, timeout
handling, and error attribution can never drift between call sites again,
and gives every call site optional ``ThreadPoolExecutor`` concurrency for
free.

Callers keep their own domain-specific post-conditions (the ``> 500`` byte
check, the ``min_brightness`` grayscale filter, failure counters): this
module only reports what ffmpeg itself did.
"""

from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Sequence


@dataclass(frozen=True)
class FrameExtractResult:
    timestamp_s: float
    path: str
    ok: bool
    returncode: int | None
    error: str


def _format_timestamp(value: float) -> str:
    """Render *value* the way the pre-refactor call sites did.

    Whole-number timestamps (the common case: coarse/refine sampling)
    render without a decimal point, so the emitted ffmpeg command does not
    gain a spurious ``.0`` and stays diffable against historical logs.
    """
    numeric = float(value)
    if numeric.is_integer():
        return str(int(numeric))
    return str(numeric)


def _frame_filename(timestamp_s: float) -> str:
    """Millisecond-precision name: collision-free for whole-second
    coarse/refine timestamps and sub-second action-rescore timestamps
    alike, within one ``extract_frames`` call."""
    millis = round(float(timestamp_s) * 1000)
    return f"frame_{millis:012d}.jpg"


def _build_command(
    video_path: str,
    timestamp_s: float,
    out_path: str,
    *,
    width: int,
    jpeg_quality: int,
    accurate_seek: bool = False,
) -> list[str]:
    # Fast seek (-ss before -i) is the historical coarse/refine path.
    # Accurate seek (-ss after -i) is for short Quality windows where a
    # keyframe miss would land outside the candidate interval.
    seek = ["-ss", _format_timestamp(timestamp_s)]
    input_args = ["-i", video_path]
    prefix = ["ffmpeg", "-y"]
    if accurate_seek:
        positioned = prefix + input_args + seek
    else:
        positioned = prefix + seek + input_args
    return [
        *positioned,
        "-vf", f"scale={width}:-1",
        "-vframes", "1",
        # Previously missing: skip demuxing audio/subtitle streams and pin
        # JPEG quality instead of relying on the encoder's own default.
        "-an", "-sn",
        "-q:v", str(jpeg_quality),
        out_path,
    ]


def _extract_one(
    video_path: str,
    timestamp_s: float,
    out_dir: str,
    *,
    width: int,
    jpeg_quality: int,
    timeout_s: float,
    runner: Callable,
    accurate_seek: bool = False,
) -> FrameExtractResult:
    out_path = os.path.abspath(
        os.path.join(out_dir, _frame_filename(timestamp_s))
    )
    cmd = _build_command(
        video_path, timestamp_s, out_path,
        width=width, jpeg_quality=jpeg_quality,
        accurate_seek=accurate_seek,
    )
    try:
        completed = runner(cmd, capture_output=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return FrameExtractResult(
            timestamp_s=timestamp_s, path=out_path, ok=False,
            returncode=None,
            error=f"ffmpeg timed out after {timeout_s}s at ts={timestamp_s}",
        )
    except Exception as exc:  # defensive: a launch failure is still a result
        return FrameExtractResult(
            timestamp_s=timestamp_s, path=out_path, ok=False,
            returncode=None, error=f"ffmpeg failed to launch: {exc}",
        )
    returncode = getattr(completed, "returncode", None)
    if returncode != 0:
        stderr = getattr(completed, "stderr", b"") or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return FrameExtractResult(
            timestamp_s=timestamp_s, path=out_path, ok=False,
            returncode=returncode,
            error=(
                f"ffmpeg exited {returncode} at ts={timestamp_s}: "
                f"{str(stderr)[:200]}"
            ),
        )
    return FrameExtractResult(
        timestamp_s=timestamp_s, path=out_path, ok=True,
        returncode=returncode, error="",
    )


def extract_frames(
    video_path: str,
    timestamps: Sequence[float],
    out_dir: str,
    *,
    width: int = 640,
    jpeg_quality: int = 3,
    workers: int = 1,
    timeout_s: float = 15.0,
    runner: Callable | None = None,
    accurate_seek: bool = False,
) -> list[FrameExtractResult]:
    """Extract one frame per timestamp, optionally with bounded concurrency.

    ``-ss`` stays before ``-i`` (unchanged fast-seek semantics -- this
    function must not alter which frame ffmpeg produces).

    Results are always returned sorted by ``timestamp_s`` ascending,
    independent of submission or completion order, so a manifest built
    from them is reproducible regardless of ``workers``.  With
    ``workers=1`` the underlying ffmpeg calls themselves are also issued
    in ascending timestamp order.
    """
    ts_list = sorted(float(t) for t in timestamps)
    if not ts_list:
        return []
    if runner is None:
        runner = subprocess.run

    os.makedirs(out_dir, exist_ok=True)
    worker_count = max(1, min(int(workers), len(ts_list)))

    def _run(ts: float) -> FrameExtractResult:
        return _extract_one(
            video_path, ts, out_dir,
            width=width, jpeg_quality=jpeg_quality,
            timeout_s=timeout_s, runner=runner,
            accurate_seek=accurate_seek,
        )

    if worker_count == 1:
        return [_run(ts) for ts in ts_list]

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        results = list(pool.map(_run, ts_list))
    return sorted(results, key=lambda r: r.timestamp_s)

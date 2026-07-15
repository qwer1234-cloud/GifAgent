"""Cleanup helpers for generated adaptive export artifacts."""

from __future__ import annotations

import os
from pathlib import Path


def _process_is_alive(pid: int) -> bool:
    """Check a PID without sending a signal (os.kill(pid, 0) is unsafe on Windows)."""
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() not in {6, 87}

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class ExportDirectoryBusyError(RuntimeError):
    """Raised when another process is exporting the same video directory."""


class ExportDirectoryLock:
    """Cross-process lock for one adaptive-video export directory."""

    def __init__(self, export_dir: str | Path):
        self.root = Path(export_dir)
        self.lock_path = self.root / ".adaptive_export.lock"
        self._owned = False

    def acquire(self) -> "ExportDirectoryLock":
        self.root.mkdir(parents=True, exist_ok=True)
        payload = f"{os.getpid()}\n"

        for attempt in range(2):
            try:
                fd = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                if attempt == 0 and self._remove_stale_lock():
                    continue
                raise ExportDirectoryBusyError(
                    f"Adaptive export is already running for {self.root}"
                )

            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(payload)
            self._owned = True
            return self

        raise ExportDirectoryBusyError(f"Adaptive export is already running for {self.root}")

    def _remove_stale_lock(self) -> bool:
        try:
            owner_pid = int(self.lock_path.read_text(encoding="ascii").strip())
        except (FileNotFoundError, ValueError, OSError):
            owner_pid = None

        if owner_pid is not None:
            if _process_is_alive(owner_pid):
                return False

        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            return False
        return True

    def release(self) -> None:
        if not self._owned:
            return
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self._owned = False

    def __enter__(self) -> "ExportDirectoryLock":
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


def _unlink_if_file(path: Path) -> int:
    if not path.is_file():
        return 0
    path.unlink()
    return 1


def cleanup_adaptive_export_dir(export_dir: str | Path, *, video_name: str) -> int:
    """Remove generated files for one adaptive video export directory."""
    root = Path(export_dir)
    if not root.exists():
        return 0

    removed = 0
    for pattern in ("*.gif", "pal_*.png", f"{video_name}.pbf"):
        for path in root.glob(pattern):
            removed += _unlink_if_file(path)

    sample_dir = root / "Sample"
    if sample_dir.is_dir():
        sample_patterns = [
            f"{video_name}_grid.*",
            f"{video_name}_sample_*.*",
        ]
        for pattern in sample_patterns:
            for path in sample_dir.glob(pattern):
                removed += _unlink_if_file(path)

    return removed

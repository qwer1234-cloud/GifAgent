"""Cleanup helpers for generated adaptive export artifacts."""

from __future__ import annotations

from pathlib import Path


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

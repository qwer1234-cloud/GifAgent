"""Stage 1: discover -- ffprobe + metadata only."""
from __future__ import annotations

import os
import subprocess

from app.pipeline.stage_io import _make_artifact, _save_manifest


def _stage_discover(video_path: str, work_dir: str, cfg: dict) -> dict:
    """Probe the video with ffprobe and write a video-metadata manifest.

    Does NOT sample frames, call VLM, call LLM, or export GIFs.
    """
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ],
        capture_output=True, text=True,
    )
    total_duration = float(probe.stdout.strip())
    print(f"  Duration: {total_duration:.0f}s ({total_duration / 60:.0f} min)")

    manifest = {
        "schema_version": 1,
        "stage": "discover",
        "video_path": os.path.abspath(video_path),
        "video_name": os.path.splitext(os.path.basename(video_path))[0],
        "duration_s": total_duration,
        "output_key": "discover",
    }
    manifest_path = _save_manifest(work_dir, "discover", manifest)

    return {
        "output_key": "discover",
        "duration_s": total_duration,
        "_artifacts": [_make_artifact(manifest_path, "discover_manifest")],
    }

"""Video fingerprinting — duration + keyframe pHash for near-duplicate detection.

Robust to re-encoding, container changes, resolution changes, filename changes.
Not robust to cropping, watermarks, or aspect ratio changes (use Chromaprint for those).
"""
from __future__ import annotations

import subprocess
import tempfile
import os
from typing import Optional

from PIL import Image
import imagehash


KEYFRAME_POSITIONS = [0.1, 0.3, 0.5, 0.7, 0.9]
DEDUP_HAMMING_THRESHOLD = 5


def get_video_duration(video_path: str) -> float:
    """Return duration in seconds via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        capture_output=True, text=True, timeout=30,
    )
    return float(result.stdout.strip())


def extract_keyframes(video_path: str, duration: float, out_dir: str) -> list[str]:
    """Extract 5 keyframes at fixed percentages of duration. Returns list of JPEG paths."""
    paths = []
    for i, pos in enumerate(KEYFRAME_POSITIONS):
        t = duration * pos
        out_path = os.path.join(out_dir, f"keyframe_{i}.jpg")
        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", video_path,
             "-frames:v", "1", "-q:v", "2", out_path],
            capture_output=True, timeout=30,
        )
        if os.path.exists(out_path):
            paths.append(out_path)
    return paths


def compute_fingerprint(video_path: str) -> Optional[dict]:
    """Compute video fingerprint: {duration, phashes: [hex strings]}.

    Returns None if ffprobe/ffmpeg fails (e.g., corrupt video).
    """
    try:
        duration = get_video_duration(video_path)
    except Exception:
        return None

    with tempfile.TemporaryDirectory() as tmp:
        keyframes = extract_keyframes(video_path, duration, tmp)
        if len(keyframes) < 3:
            return None

        phashes = []
        for kf in keyframes:
            try:
                with Image.open(kf) as img:
                    phashes.append(str(imagehash.phash(img)))
            except Exception:
                continue

        if len(phashes) < 3:
            return None

        return {"duration": round(duration, 1), "phashes": phashes}


def hamming_distance(a: str, b: str) -> int:
    """Hamming distance between two pHash hex strings."""
    try:
        return int(imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b))
    except Exception:
        return 64


def is_duplicate(fp_a: dict, fp_b: dict, duration_tolerance: float = 2.0) -> bool:
    """Check if two fingerprints are near-duplicates.

    Matches if durations are within tolerance AND all keyframe pHashes
    have Hamming distance below threshold.
    """
    if not fp_a or not fp_b:
        return False
    if abs(fp_a.get("duration", 0) - fp_b.get("duration", 0)) > duration_tolerance:
        return False

    phashes_a = fp_a.get("phashes", [])
    phashes_b = fp_b.get("phashes", [])
    if len(phashes_a) != len(phashes_b) or len(phashes_a) < 3:
        return False

    max_dist = max(hamming_distance(a, b) for a, b in zip(phashes_a, phashes_b))
    return max_dist <= DEDUP_HAMMING_THRESHOLD


def find_duplicate_in_checkpoint(fingerprint: dict, checkpoint: dict) -> Optional[str]:
    """Check if fingerprint matches any already-processed video in checkpoint.

    Returns the matching video name, or None if no duplicate found.
    """
    for video_name, info in checkpoint.get("completed", {}).items():
        existing_fp = info.get("fingerprint")
        if existing_fp and is_duplicate(fingerprint, existing_fp):
            return video_name
    return None

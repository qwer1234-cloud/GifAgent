"""Build real-media manifests for the continuity spike (milestone 0B).

Subcommands:

``videos``  pre-scan a directory of source videos, select windows that cover
            the fifteen required scenario buckets, and write a
            source_video manifest. Scenario tags are provisional sampling
            labels derived from measured signals (motion energy, brightness,
            person count, face size); they are not human verdicts, and every
            candidate is emitted with ``human_label=UNLABELED``.

``gifs``    select stratified historical GIFs from the packaged library
            database, carrying each GIF's real preference label
            (favorite/like/dislike/neutral) as ``feedback_label``.

All database access is read-only and fully parameter-bound.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.continuity_spike_backend_real import (  # noqa: E402
    ANALYSIS_WIDTH,
    RealSpikeBackend,
    _box_iou,
)

SCHEMA_VERSION = "continuity-spike-input-v1"
DEFAULT_DB = REPO / "dist" / "GifAgentUI" / "data" / "library.db"
WINDOW_S = 3.0
CONTEXT_MARGIN_S = 2.0
SHOT_MARGIN_S = 3.0
PROBE_SAMPLES = 10
MIN_VIDEO_S = 30.0
TARGET_CANDIDATES = 50
TARGET_GIFS = 200
MAX_GIF_BYTES = 64 * 1024 * 1024
PER_FILE_LIMIT = 4

MOTION_TAGS = ("subtle_motion", "moderate_motion", "large_motion")
_FACE_SET = {"face_visible", "face_occluded", "face_not_visible"}
_MOTION_SET = set(MOTION_TAGS)

REQUIRED_BUCKETS = {
    "single_subject", "co_primary_subjects", "partial_body", "headless",
    "close_contact", "occlusion", "fast_motion", "subtle_motion",
    "moderate_motion", "large_motion", "face_visible", "face_occluded",
    "face_not_visible", "low_light", "media_edge",
}


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _ffprobe_duration(path: Path) -> float | None:
    completed = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    try:
        return float(completed.stdout.strip())
    except ValueError:
        return None


def _probe_frames(path: Path, duration: float, count: int) -> list[np.ndarray]:
    """Decode `count` single probe frames via fast `-ss` seeks."""

    frames: list[np.ndarray] = []
    with tempfile.TemporaryDirectory(prefix="continuity_probe_") as tmp:
        for index in range(count):
            stamp = (index + 0.5) * duration / count
            out = Path(tmp) / f"f{index:04d}.jpg"
            completed = subprocess.run(
                [
                    _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                    "-hwaccel", "auto",
                    "-ss", f"{stamp:.3f}",
                    "-i", str(path),
                    "-frames:v", "1",
                    "-vf", f"scale={ANALYSIS_WIDTH}:-2",
                    "-q:v", "2",
                    str(out),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if out.is_file():
                image = cv2.imread(str(out))
                if image is not None:
                    frames.append(image)
            if completed.returncode != 0 and index == 0:
                return []
    return frames


def _probe_file(backend: RealSpikeBackend, path: Path) -> dict | None:
    duration = _ffprobe_duration(path)
    if duration is None or duration < MIN_VIDEO_S:
        return None
    frames = _probe_frames(path, duration, PROBE_SAMPLES)
    if len(frames) < 4:
        return None
    height, width = frames[0].shape[:2]
    luma = [float(np.mean(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY))) for f in frames]
    diffs = []
    for first, second in zip(frames, frames[1:]):
        g0 = cv2.cvtColor(first, cv2.COLOR_BGR2GRAY).astype(np.float32)
        g1 = cv2.cvtColor(second, cv2.COLOR_BGR2GRAY).astype(np.float32)
        diffs.append(float(np.mean(np.abs(g1 - g0))))
    motion = float(np.mean(diffs)) if diffs else 0.0
    persons_per_frame = [backend._detect_persons(f) for f in frames]
    person_counts = [len(p) for p in persons_per_frame]
    multi = sum(1 for p in persons_per_frame if len(p) >= 2)
    overlap = sum(
        1
        for p in persons_per_frame
        if len(p) >= 2 and _box_iou(p[0].box, p[1].box) >= 0.10
    )
    border = 0
    for p in persons_per_frame:
        if not p:
            continue
        x, y, w, h = p[0].box
        if (
            x <= 2 or y <= 2 or x + w >= width - 2 or y + h >= height - 2
        ) and h > 0.6 * height:
            border += 1
    faces = backend._face_stats(frames, persons_per_frame)
    return {
        "duration": duration,
        "luma_median": float(np.median(luma)),
        "motion": motion,
        "person_counts": person_counts,
        "multi_ratio": multi / len(frames),
        "overlap_ratio": overlap / len(frames),
        "border_ratio": border / len(frames),
        "face_visible": faces["visible"] >= len(frames) * 0.4,
        "face_any": faces["visible"] > 0,
        "face_short_side": faces["short_side"],
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


_LIVE_DRIVES: set[str] = set()
_DEAD_DRIVES: set[str] = set()


def _drive_alive(path: str) -> bool:
    drive = os.path.splitdrive(path)[0]
    if drive in _LIVE_DRIVES:
        return True
    if drive in _DEAD_DRIVES:
        return False
    try:
        os.stat(drive + os.sep)
    except OSError:
        _DEAD_DRIVES.add(drive)
        return False
    _LIVE_DRIVES.add(drive)
    return True


def _path_exists_fast(path: str) -> bool:
    return _drive_alive(path) and os.path.exists(path)


def _window_face_tags(
    backend: RealSpikeBackend,
    proposal: dict,
) -> tuple[str, bool]:
    """Per-window face scan: (face_tag, headless)."""

    start = proposal["window_start_s"]
    duration = max(proposal["window_end_s"] - start, 0.25)
    frames: list[np.ndarray] = []
    with tempfile.TemporaryDirectory(prefix="continuity_winface_") as tmp:
        for index in range(4):
            stamp = start + duration * (index + 0.5) / 4
            out = Path(tmp) / f"f{index}.jpg"
            subprocess.run(
                [
                    _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                    "-hwaccel", "auto",
                    "-ss", f"{stamp:.3f}",
                    "-i", proposal["source_path"],
                    "-frames:v", "1",
                    "-vf", f"scale={ANALYSIS_WIDTH}:-2",
                    "-q:v", "2",
                    str(out),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if out.is_file():
                image = cv2.imread(str(out))
                if image is not None:
                    frames.append(image)
    if not frames:
        return "face_not_visible", False
    persons = [backend._detect_persons(frame) for frame in frames]
    stats = backend._face_stats(frames, persons)
    if stats["usable"] >= 2:
        return "face_visible", False
    if stats["visible"] >= 1:
        return "face_occluded", False
    headless = any(len(p) > 0 for p in persons)
    return "face_not_visible", headless


def build_video_manifest(directory: Path, output: Path, per_file: int) -> None:
    cache_path = output.parent / "probe_cache.json"
    cache: dict[str, dict] = {}
    if cache_path.is_file():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        print(f"probe cache: {len(cache)} files")

    files = sorted(
        p for p in directory.iterdir()
        if p.suffix.lower() in (".mp4", ".mkv", ".ts", ".webm", ".avi")
    )
    backend = RealSpikeBackend()
    proposals: list[dict] = []
    scanned = 0
    first_file_key: str | None = None
    last_file_key: str | None = None
    for path in files:
        if len(proposals) >= TARGET_CANDIDATES * 2:
            break
        key = path.name
        if key in cache:
            entry = cache[key]
            if entry.get("probe") is None:
                continue
            probe, digest = entry["probe"], entry["digest"]
        else:
            probe = _probe_file(backend, path)
            digest = _sha256(path) if probe else ""
            cache[key] = {"probe": probe, "digest": digest}
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
        scanned += 1
        summary = f"{probe['duration']:.0f}s" if probe else "skip"
        print(f"probe {scanned:02d}: {path.name[:56]} -> {summary}", flush=True)
        if probe is None:
            continue
        if first_file_key is None:
            first_file_key = key
        last_file_key = key
        windows = _candidate_windows(probe["duration"], per_file)
        for index, (start, end) in enumerate(windows):
            proposals.append(
                {
                    "candidate_id": f"video-{scanned:03d}-{index:02d}",
                    "media_kind": "source_video",
                    "source_path": str(path.resolve()),
                    "source_sha256": digest,
                    "media_duration_s": probe["duration"],
                    "window_start_s": start,
                    "window_end_s": end,
                    "probe": probe,
                    "file_key": key,
                    "at_media_edge": False,
                }
            )

    # True media-edge candidates: the first window of the first scanned file
    # sits at the file head, the last window of the last scanned file at the
    # file tail, with context truncated by the media boundary.
    if proposals:
        duration = proposals[0]["media_duration_s"]
        if duration > 8.0:
            for proposal in proposals:
                if proposal["file_key"] == first_file_key:
                    proposal["window_start_s"] = 0.3
                    proposal["window_end_s"] = 3.3
                    proposal["at_media_edge"] = True
                    break
        for proposal in reversed(proposals):
            if proposal["file_key"] == last_file_key:
                duration = proposal["media_duration_s"]
                if duration > 8.0:
                    proposal["window_end_s"] = duration - 0.3
                    proposal["window_start_s"] = duration - 3.3
                    proposal["at_media_edge"] = True
                break

    motion_values = sorted(p["probe"]["motion"] for p in proposals)
    tercile_low = motion_values[len(motion_values) // 3]
    tercile_high = motion_values[2 * len(motion_values) // 3]
    fast_cutoff = motion_values[int(len(motion_values) * 0.90)]
    print(f"motion terciles: {tercile_low:.2f} / {tercile_high:.2f} | fast cutoff: {fast_cutoff:.2f}")

    for proposal in proposals:
        window_start = time.perf_counter_ns()
        face_tag, headless = _window_face_tags(backend, proposal)
        proposal["window_face_tag"] = face_tag
        proposal["window_headless"] = headless
        print(
            f"  window {proposal['candidate_id']}: face={face_tag}"
            f" headless={headless} ({(time.perf_counter_ns() - window_start) / 1e6:.0f}ms)",
            flush=True,
        )

    for proposal in proposals:
        proposal["scenario_tags"] = _assign_tags(proposal, tercile_low, tercile_high, fast_cutoff)

    selected = _select_covering(proposals, TARGET_CANDIDATES)
    candidates = []
    for proposal in selected:
        start = proposal["window_start_s"]
        end = proposal["window_end_s"]
        duration = proposal["media_duration_s"]
        candidates.append(
            {
                "candidate_id": proposal["candidate_id"],
                "media_kind": "source_video",
                "source_path": proposal["source_path"],
                "source_sha256": proposal["source_sha256"],
                "media_duration_s": round(duration, 3),
                "confirmed_shot_start_s": round(max(0.0, start - SHOT_MARGIN_S), 3),
                "context_start_s": round(max(0.0, start - CONTEXT_MARGIN_S), 3),
                "window_start_s": round(start, 3),
                "window_end_s": round(end, 3),
                "context_end_s": round(min(duration, end + CONTEXT_MARGIN_S), 3),
                "confirmed_shot_end_s": round(min(duration, end + SHOT_MARGIN_S), 3),
                "scenario_tags": proposal["scenario_tags"],
                "data_origin": "real",
                "human_label": "UNLABELED",
                "feedback_label": "unknown",
            }
        )
    _validate_ordering(candidates)
    _write_manifest(output, candidates)
    covered: set[str] = set()
    for candidate in candidates:
        covered.update(candidate["scenario_tags"])
    print(f"candidates: {len(candidates)} | buckets covered: {len(covered & REQUIRED_BUCKETS)}/15")
    missing = REQUIRED_BUCKETS - covered
    if missing:
        print("MISSING BUCKETS:", sorted(missing))


def _candidate_windows(duration: float, count: int) -> list[tuple[float, float]]:
    usable_start = min(0.10 * duration, duration - WINDOW_S)
    usable_end = max(duration - 0.10 * duration, usable_start + WINDOW_S)
    span = usable_end - usable_start - WINDOW_S
    if count <= 1 or span <= 0:
        return [(usable_start, min(usable_start + WINDOW_S, duration))]
    step = span / (count - 1)
    return [
        (usable_start + index * step, min(usable_start + index * step + WINDOW_S, duration))
        for index in range(count)
    ]


def _assign_tags(
    proposal: dict,
    tercile_low: float,
    tercile_high: float,
    fast_cutoff: float,
) -> list[str]:
    probe = proposal["probe"]
    tags: set[str] = set()
    if proposal["at_media_edge"]:
        tags.add("media_edge")
    if probe["motion"] <= tercile_low:
        tags.add("subtle_motion")
    elif probe["motion"] <= tercile_high:
        tags.add("moderate_motion")
    else:
        tags.add("large_motion")
    if probe["motion"] >= fast_cutoff:
        tags.add("fast_motion")
    if probe["luma_median"] < 70:
        tags.add("low_light")
    if probe["multi_ratio"] >= 0.55:
        tags.add("co_primary_subjects")
        if probe["overlap_ratio"] >= 0.4:
            tags.add("close_contact")
    else:
        tags.add("single_subject")
    if probe["border_ratio"] >= 0.5:
        tags.add("partial_body")
    if max(probe["person_counts"]) != min(probe["person_counts"]):
        tags.add("occlusion")
    tags.add(proposal["window_face_tag"])
    if proposal["window_headless"] and probe["person_counts"] and max(probe["person_counts"]) > 0:
        tags.add("headless")
    return sorted(tags)


def _select_covering(proposals: list[dict], target: int) -> list[dict]:
    """Greedy selection: cover every required bucket first, then fill."""

    selected: list[dict] = []
    used: set[int] = set()
    for bucket in sorted(REQUIRED_BUCKETS):
        for index, proposal in enumerate(proposals):
            if index in used:
                continue
            if bucket in proposal["scenario_tags"]:
                selected.append(proposal)
                used.add(index)
                break

    per_source: dict[str, int] = {}
    for proposal in selected:
        per_source[proposal["source_path"]] = (
            per_source.get(proposal["source_path"], 0) + 1
        )
    for index, proposal in enumerate(proposals):
        if len(selected) >= target:
            break
        if index in used:
            continue
        if per_source.get(proposal["source_path"], 0) >= PER_FILE_LIMIT:
            continue
        selected.append(proposal)
        used.add(index)
        per_source[proposal["source_path"]] = (
            per_source.get(proposal["source_path"], 0) + 1
        )
    return selected[:target]


def _validate_ordering(candidates: list[dict]) -> None:
    for index, candidate in enumerate(candidates):
        chain = (
            0.0,
            candidate["confirmed_shot_start_s"],
            candidate["context_start_s"],
            candidate["window_start_s"],
            candidate["window_end_s"],
            candidate["context_end_s"],
            candidate["confirmed_shot_end_s"],
            candidate["media_duration_s"],
        )
        assert chain[3] < chain[4], f"candidates[{index}]: window must be non-empty"
        for earlier, later in zip(chain, chain[1:]):
            assert earlier <= later, f"candidates[{index}]: chain broken {chain}"


def build_gif_manifest(output: Path, db_path: Path) -> None:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    query = """
        SELECT cg.candidate_id, cg.artifact_path, cg.start_sec, cg.end_sec,
               pe.rating
        FROM preference_events pe
        JOIN candidate_gifs cg ON cg.candidate_id = pe.target_id
        WHERE pe.target_type = ? AND pe.rating IN (?, ?, ?, ?)
        ORDER BY cg.candidate_id
    """
    rows = conn.execute(
        query, ("candidate_gif", "favorite", "like", "dislike", "neutral")
    ).fetchall()
    conn.close()
    print(f"labeled rows: {len(rows)}")

    buckets: dict[tuple[str, str], list[dict]] = {}
    seen_files: set[str] = set()
    dead_drive_skips = 0
    for count, row in enumerate(rows):
        artifact = row["artifact_path"]
        if not artifact or not artifact.lower().endswith(".gif"):
            continue
        if artifact in seen_files or not _path_exists_fast(artifact):
            if artifact and not _drive_alive(artifact):
                dead_drive_skips += 1
            continue
        if (count + 1) % 100 == 0:
            print(
                f"  scanned {count + 1}/{len(rows)} rows, kept {len(seen_files)} files",
                flush=True,
            )
        if os.stat(artifact).st_size > MAX_GIF_BYTES:
            continue
        try:
            with Image.open(artifact) as image:
                width, height = image.size
                frames = getattr(image, "n_frames", 1)
                per_frame_ms = image.info.get("duration", 100) or 100
        except Exception:
            continue
        duration_s = frames * per_frame_ms / 1000.0
        if duration_s <= 0:
            continue
        longest = max(width, height)
        resolution = "le_256" if longest <= 256 else "257_480" if longest <= 480 else "gt_480"
        duration_bucket = (
            "lt_2s" if duration_s < 2.0 else "2_5s" if duration_s <= 5.0 else "gt_5s"
        )
        seen_files.add(artifact)
        buckets.setdefault((resolution, duration_bucket), []).append(
            {
                "artifact": artifact,
                "duration": round(duration_s, 3),
                "rating": row["rating"],
            }
        )

    print("bucket sizes:", {f"{k[0]}/{k[1]}": len(v) for k, v in sorted(buckets.items())})
    per_bucket = math.ceil(TARGET_GIFS / 9)
    selection: list[dict] = []
    for key in sorted(buckets):
        entries = buckets[key]
        ratings: dict[str, list[dict]] = {
            "favorite": [], "like": [], "dislike": [], "neutral": [],
        }
        for entry in entries:
            ratings[entry["rating"]].append(entry)
        ordered: list[dict] = []
        round_index = 0
        while len(ordered) < per_bucket:
            added = False
            for rating in ("favorite", "like", "dislike", "neutral"):
                if round_index < len(ratings[rating]):
                    ordered.append(ratings[rating][round_index])
                    added = True
                    if len(ordered) >= per_bucket:
                        break
            if not added:
                break
            round_index += 1
        selection.extend(ordered[:per_bucket])

    # Fill up to the target from whichever buckets still have entries.
    overflow = [
        entry
        for key in sorted(buckets)
        for entry in buckets[key][per_bucket:]
    ]
    for entry in overflow:
        if len(selection) >= TARGET_GIFS:
            break
        selection.append(entry)

    selection = selection[:TARGET_GIFS]
    candidates = []
    for index, entry in enumerate(selection):
        candidates.append(
            {
                "candidate_id": f"gif-{index:04d}-{entry['rating']}",
                "media_kind": "historical_gif",
                "source_path": str(Path(entry["artifact"]).resolve()),
                "source_sha256": _sha256(Path(entry["artifact"])),
                "media_duration_s": entry["duration"],
                "confirmed_shot_start_s": 0.0,
                "context_start_s": 0.0,
                "window_start_s": 0.0,
                "window_end_s": entry["duration"],
                "context_end_s": entry["duration"],
                "confirmed_shot_end_s": entry["duration"],
                "scenario_tags": ["historical_gif"],
                "data_origin": "real",
                "human_label": "UNLABELED",
                "feedback_label": entry["rating"],
            }
        )
    _write_manifest(output, candidates)
    ratings: dict[str, int] = {}
    for candidate in candidates:
        ratings[candidate["feedback_label"]] = (
            ratings.get(candidate["feedback_label"], 0) + 1
        )
    print(f"gif candidates: {len(candidates)} | ratings: {ratings}")


def _write_manifest(output: Path, candidates: list[dict]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": SCHEMA_VERSION, "candidates": candidates}
    output.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"wrote {output} ({len(candidates)} candidates)")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    videos = sub.add_parser("videos")
    videos.add_argument("--dir", required=True, type=Path)
    videos.add_argument("--output", required=True, type=Path)
    videos.add_argument("--per-file", type=int, default=4)

    gifs = sub.add_parser("gifs")
    gifs.add_argument("--output", required=True, type=Path)
    gifs.add_argument("--db", type=Path, default=DEFAULT_DB)

    args = parser.parse_args()
    if args.command == "videos":
        build_video_manifest(args.dir, args.output, args.per_file)
    else:
        build_gif_manifest(args.output, args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

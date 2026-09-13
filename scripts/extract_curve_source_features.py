"""Curve source feature worker -- runs INSIDE the ``.venv-spike`` environment.

Reads one JSON request file (one source video, multiple snap-frozen windows),
runs pose-based body-curve feature extraction out of process, and writes one
JSON response file atomically. The main app never imports onnxruntime, cv2
inference backends, or this spike environment; it only talks the protocol.

Usage:
    python scripts/extract_curve_source_features.py --request req.json --response resp.json
    python scripts/extract_curve_source_features.py --self-test-real

The self-test renders synthetic pose fixtures in-memory (never user media)
and exercises the real model package; it may legitimately report
``pose_coverage_low`` but must never report a protocol/hash/runtime error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.services.curve_source_features import (  # noqa: E402
    CURVE_SOURCE_FEATURE_NAMES,
    CURVE_SOURCE_FEATURE_VERSION,
    CURVE_SOURCE_RUNTIME_PROTOCOL_VERSION,
    POSE_BACKEND_ID,
    SOURCE_DECODE_PROFILE_V1,
    CurveWindow,
    validate_window_feature_values,
    window_input_hash,
)
from app.services.curve_source_runtime import (  # noqa: E402
    LEASE_FILE_NAME,  # noqa: F401  (protocol parity constant)
    model_package_sha256,
)

MIN_VALID_FRAMES = 3
KEYPOINTS = 17
_SHOULDERS = (5, 6)
_HIPS = (11, 12)
_LIMB_TIPS = (9, 10, 15, 16)
_SEGMENT_MERGE_GAP_S = 0.5


def _ffmpeg() -> str:
    return os.environ.get("GIFAGENT_FFMPEG", "ffmpeg")


def _ffprobe() -> str:
    return os.environ.get("GIFAGENT_FFPROBE", "ffprobe")


# --------------------------------------------------------------------------- #
# pure curve aggregation (ported from scripts/curve_feature_backfill.py)      #
# --------------------------------------------------------------------------- #


def _primary_valid_person(persons: list[Any]) -> Any | None:
    if not persons:
        return None
    from scripts.continuity_spike_backend_real import _pose_valid

    primary = persons[0]
    return primary if _pose_valid(primary) else None


def _torso_border_clipped(person: Any, width: int, height: int) -> bool:
    from scripts.continuity_spike_backend_real import KEYPOINT_SCORE_MIN

    for index in _SHOULDERS + _HIPS:
        x, y, conf = person.keypoint(index)
        if conf >= KEYPOINT_SCORE_MIN and (
            x <= 2 or y <= 2 or x >= width - 2 or y >= height - 2
        ):
            return True
    return False


def aggregate_curve_features(
    valid_people: list[Any],
    total_frame_count: int,
    width: int,
    height: int,
) -> tuple[dict[str, float] | None, str | None]:
    """Aggregate the ten ``curve-source-v1`` features from per-frame persons.

    Keypoints live in the analysis-frame coordinate system, so ``width`` and
    ``height`` must be the analysis dimensions; real source dimensions are
    reported separately as metadata.
    """
    if total_frame_count < MIN_VALID_FRAMES:
        return None, "window_too_short"
    if len(valid_people) < MIN_VALID_FRAMES:
        return None, "pose_coverage_low"

    torso_scales: list[float] = []
    extensions: list[float] = []
    visibilities: list[float] = []
    centers: list[tuple[float, float]] = []
    verticalities: list[float] = []
    from scripts.continuity_spike_backend_real import KEYPOINT_SCORE_MIN

    for person in valid_people:
        sx = (person.keypoint(_SHOULDERS[0])[0] + person.keypoint(_SHOULDERS[1])[0]) / 2
        sy = (person.keypoint(_SHOULDERS[0])[1] + person.keypoint(_SHOULDERS[1])[1]) / 2
        hx = (person.keypoint(_HIPS[0])[0] + person.keypoint(_HIPS[1])[0]) / 2
        hy = (person.keypoint(_HIPS[0])[1] + person.keypoint(_HIPS[1])[1]) / 2
        torso = float(np.hypot(sx - hx, sy - hy))
        if torso < 4.0:
            continue
        torso_scales.append(torso)
        centers.append(((sx + hx) / 2, (sy + hy) / 2))
        verticalities.append(abs(float(np.arctan2(sx - hx, hy - sy))))

        tips = [
            person.keypoint(index)
            for index in _LIMB_TIPS
            if person.keypoint(index)[2] >= KEYPOINT_SCORE_MIN
        ]
        if len(tips) >= 2:
            xs = [tip[0] for tip in tips]
            extensions.append((max(xs) - min(xs)) / torso)
        visibilities.append(
            sum(
                1 for index in range(KEYPOINTS)
                if person.keypoint(index)[2] >= KEYPOINT_SCORE_MIN
            ) / KEYPOINTS
        )
    clipped = sum(
        1 for person in valid_people if _torso_border_clipped(person, width, height)
    )

    if len(torso_scales) < MIN_VALID_FRAMES:
        return None, "pose_coverage_low"

    torso_scale = float(np.median(torso_scales))
    speeds = [
        float(np.hypot(x1 - x0, y1 - y0) / torso_scale)
        for (x0, y0), (x1, y1) in zip(centers, centers[1:])
    ]
    speed_arr = np.asarray(speeds, dtype=np.float64) if speeds else np.zeros(1)
    peak_speed = float(np.percentile(speed_arr, 95))
    if len(speed_arr) >= 2:
        jerk = float(np.mean(np.abs(np.diff(speed_arr))))
        smoothness = float(np.clip(1.0 - jerk / (peak_speed + 1e-6), 0.0, 1.0))
    else:
        smoothness = 0.0

    ratios = [
        person.area / max(width * height, 1.0) for person in valid_people
    ]
    features = {
        "pose_valid_ratio": round(len(valid_people) / max(total_frame_count, 1), 6),
        "subject_area_ratio": round(float(np.median(ratios)), 6),
        "extension_mean": round(float(np.mean(extensions)), 6) if extensions else 0.0,
        "extension_peak": round(float(np.percentile(extensions, 90)), 6) if extensions else 0.0,
        "extension_range": round(
            float(np.percentile(extensions, 90) - np.percentile(extensions, 10)), 6
        ) if len(extensions) >= 2 else 0.0,
        "torso_speed_p95": round(peak_speed, 6),
        "motion_smoothness": round(smoothness, 6),
        "visibility_ratio": round(float(np.mean(visibilities)), 6) if visibilities else 0.0,
        "border_clip_ratio": round(clipped / len(valid_people), 6),
        "verticality_mean": round(float(np.mean(verticalities)), 6) if verticalities else 0.0,
    }
    return features, None


# --------------------------------------------------------------------------- #
# decoding                                                                    #
# --------------------------------------------------------------------------- #


def extract_segment_frames(
    source_path: Path,
    start_s: float,
    duration_s: float,
    fps: int,
    max_width: int,
) -> list[tuple[float, np.ndarray]]:
    """Decode one contiguous segment at the decode profile.

    Returns ``(timestamp_s, frame)`` pairs; timestamps are derived from the
    segment start and the fixed sample fps so overlapping windows can reuse
    the same decoded frames.
    """
    import cv2

    with tempfile.TemporaryDirectory(prefix="curve_source_worker_") as tmp:
        pattern = str(Path(tmp) / "f%05d.png")
        vf = f"fps={fps},scale='min({max_width},iw)':-2"
        completed = subprocess.run(
            [
                _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                "-hwaccel", "auto",
                "-ss", f"{max(start_s, 0.0):.3f}",
                "-t", f"{max(duration_s, 0.25):.3f}",
                "-i", str(source_path),
                "-vf", vf,
                "-start_number", "0",
                pattern,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        frame_paths = sorted(Path(tmp).glob("f*.png"))
        frames: list[tuple[float, np.ndarray]] = []
        if completed.returncode == 0:
            for index, frame_path in enumerate(frame_paths):
                image = cv2.imread(str(frame_path))
                if image is None:
                    continue
                frames.append((start_s + index / float(fps), image))
        if len(frames) == 0:
            raise RuntimeError(
                f"ffmpeg decode failed rc={completed.returncode}"
            )
        return frames


def _segments_for(
    windows: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Group windows into contiguous decode segments (minimal re-decoding)."""
    ordered = sorted(windows, key=lambda w: w["start_us"])
    segments: list[list[dict[str, Any]]] = []
    for window in ordered:
        if segments:
            last = segments[-1][-1]
            gap_s = window["start_us"] / 1e6 - last["end_us"] / 1e6
            if gap_s <= _SEGMENT_MERGE_GAP_S:
                segments[-1].append(window)
                continue
        segments.append([window])
    return segments


def _source_media_dims(source_path: Path) -> tuple[int, int] | None:
    try:
        from scripts.continuity_spike_backend_real import source_media_dims

        return source_media_dims(source_path)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# protocol handling                                                           #
# --------------------------------------------------------------------------- #


def _canonical_request_hash(request: dict[str, Any]) -> str:
    body = {k: v for k, v in request.items() if k != "request_sha256"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def write_response(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write the UTF-8 JSON response file."""
    temp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temp.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    temp.replace(path)


def _unavailable_entry(window: dict[str, Any], reasons: list[str], t0: float) -> dict[str, Any]:
    return {
        "preexport_key": window["preexport_key"],
        "input_hash": window.get("input_hash", ""),
        "status": "UNAVAILABLE",
        "features": None,
        "actual_timestamps": [],
        "missing_reasons": reasons,
        "wall_clock_ms": round((time.perf_counter() - t0) * 1000.0, 2),
    }


def _streaming_sha256(path: Path) -> str:
    """Content hash of the source file, verified before any inference."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _worker_extractor_identity(model_hash: str | None) -> str:
    """This worker's own extractor identity: model package + this script's
    code + the pose backend's REAL source (continuity_spike_backend_real.py,
    whose preprocessing/thresholds/NMS define the detections). Must match the
    client's expectation exactly."""
    from app.services.curve_source_features import compute_extractor_identity

    code_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    backend_path = REPO / "scripts" / "continuity_spike_backend_real.py"
    backend_sha = (
        hashlib.sha256(backend_path.read_bytes()).hexdigest()
        if backend_path.is_file()
        else ""
    )
    return compute_extractor_identity(
        model_hash or "",
        code_sha,
        POSE_BACKEND_ID,
        backend_code_sha256=backend_sha,
    )


def process_request(request: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Process one valid protocol request; returns (results, video_metadata)."""
    t0 = time.perf_counter()
    source_path = Path(str(request.get("source_path") or ""))
    source_sha256 = str(request.get("source_sha256") or "")
    windows = list(request.get("windows") or [])
    profile = dict(SOURCE_DECODE_PROFILE_V1)
    provided_profile = request.get("decode_profile")
    if isinstance(provided_profile, dict):
        profile.update(provided_profile)
    fps = int(profile.get("fps", 8))
    max_width = int(profile.get("max_width", 320))

    model_hash = model_package_sha256(Path(str(request.get("model_dir") or "")))
    extractor_identity = _worker_extractor_identity(model_hash)
    metadata: dict[str, Any] = {
        "source_media_width_px": None,
        "source_media_height_px": None,
        "model_package_sha256": model_hash,
        "extractor_sha256": extractor_identity,
    }
    if not source_path.is_file():
        return (
            [_unavailable_entry(w, ["source_file_unreadable"], t0) for w in windows],
            metadata,
        )

    # The claimed source identity is REQUIRED and must match the bytes on
    # disk: a missing, stale, or wrong source_sha256 is a hard UNAVAILABLE,
    # never silently scored.
    if not source_sha256:
        return (
            [_unavailable_entry(w, ["source_sha256_missing"], t0) for w in windows],
            metadata,
        )
    actual_sha = _streaming_sha256(source_path)
    if actual_sha != source_sha256:
        return (
            [
                _unavailable_entry(w, ["source_sha256_mismatch"], t0)
                for w in windows
            ],
            metadata,
        )

    # The client's expected extractor identity must match this worker's
    # (model package + implementation + backend) or nothing gets scored.
    requested_extractor = str(request.get("extractor_sha256") or "")
    if requested_extractor and requested_extractor != extractor_identity:
        return (
            [
                _unavailable_entry(w, ["extractor_identity_mismatch"], t0)
                for w in windows
            ],
            metadata,
        )

    dims = _source_media_dims(source_path)
    if dims:
        metadata["source_media_width_px"] = int(dims[0])
        metadata["source_media_height_px"] = int(dims[1])

    from scripts.continuity_spike_backend_real import RealSpikeBackend

    backend = RealSpikeBackend()

    results_by_key: dict[str, dict[str, Any]] = {}
    for segment in _segments_for(windows):
        seg_start_us = min(w["start_us"] for w in segment)
        seg_end_us = max(w["end_us"] for w in segment)
        try:
            frames = extract_segment_frames(
                source_path,
                seg_start_us / 1e6,
                (seg_end_us - seg_start_us) / 1e6,
                fps=fps,
                max_width=max_width,
            )
        except Exception:
            for window in segment:
                results_by_key[window["preexport_key"]] = _unavailable_entry(
                    window, ["decode_failed"], t0
                )
            continue

        analysis_height, analysis_width = frames[0][1].shape[:2]
        frame_persons = [backend._detect_persons(frame) for _ts, frame in frames]

        for window in segment:
            window_t0 = time.perf_counter()
            # Verify the per-window input hash (it binds source identity,
            # window, feature version, decode profile, and extractor
            # identity): a mismatch is structured UNAVAILABLE, never scored.
            expected_input_hash = window_input_hash(
                source_sha256,
                CurveWindow(
                    preexport_key=str(window["preexport_key"]),
                    start_us=int(window["start_us"]),
                    end_us=int(window["end_us"]),
                ),
                decode_profile=profile,
                extractor_sha256=extractor_identity,
            )
            if str(window.get("input_hash") or "") != expected_input_hash:
                results_by_key[window["preexport_key"]] = _unavailable_entry(
                    window, ["input_hash_mismatch"], t0
                )
                continue
            start_s = window["start_us"] / 1e6
            end_s = window["end_us"] / 1e6
            window_frames = [
                (ts, frame)
                for ts, frame in frames
                if start_s - 1e-6 <= ts <= end_s + 1e-6
            ]
            primary_people = []
            frame_indices = []
            for index, (ts, _frame) in enumerate(frames):
                if start_s - 1e-6 <= ts <= end_s + 1e-6:
                    primary_people.append(_primary_valid_person(frame_persons[index]))
                    frame_indices.append(ts)
            valid_people = [p for p in primary_people if p is not None]
            features, reason = aggregate_curve_features(
                valid_people, len(window_frames), analysis_width, analysis_height
            )
            reasons: list[str] = [reason] if reason else []
            if not reasons:
                strict = validate_window_feature_values(
                    features, frame_indices,
                    None,
                )
                if strict:
                    features, reasons = None, strict
            if features is not None:
                results_by_key[window["preexport_key"]] = {
                    "preexport_key": window["preexport_key"],
                    "input_hash": window.get("input_hash", ""),
                    "status": "AVAILABLE",
                    "features": features,
                    "actual_timestamps": [round(float(ts), 6) for ts in frame_indices],
                    "missing_reasons": [],
                    "wall_clock_ms": round(
                        (time.perf_counter() - window_t0) * 1000.0, 2
                    ),
                }
            else:
                entry = _unavailable_entry(window, reasons, window_t0)
                results_by_key[window["preexport_key"]] = entry

    results = []
    for window in windows:
        entry = results_by_key.get(window["preexport_key"])
        if entry is None:
            entry = _unavailable_entry(window, ["window_inference_failed"], t0)
        results.append(entry)
    return results, metadata


def run_worker(request_path: Path, response_path: Path) -> int:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if request.get("protocol_version") != CURVE_SOURCE_RUNTIME_PROTOCOL_VERSION:
        return 2
    recomputed = _canonical_request_hash(request)
    claimed = request.get("request_sha256")
    if claimed != recomputed:
        # The client verifies the echo; a mismatched echo is reported back
        # rather than crashing so the failure stays structured.
        claimed = recomputed
        echo_ok = False
    else:
        echo_ok = True

    try:
        results, metadata = process_request(request)
    except Exception:
        windows = list(request.get("windows") or [])
        results = [
            _unavailable_entry(w, ["worker_internal_error"], time.perf_counter())
            for w in windows
        ]
        metadata = {}

    response = {
        "protocol_version": CURVE_SOURCE_RUNTIME_PROTOCOL_VERSION,
        "request_sha256": recomputed,
        "model_package_sha256": metadata.get("model_package_sha256"),
        "feature_version": CURVE_SOURCE_FEATURE_VERSION,
        "extractor_sha256": metadata.get("extractor_sha256"),
        "source_media_width_px": metadata.get("source_media_width_px"),
        "source_media_height_px": metadata.get("source_media_height_px"),
        "results": results,
    }
    write_response(response_path, response)
    _ = claimed
    return 0


# --------------------------------------------------------------------------- #
# real-model smoke test on synthetic fixtures (never user media)              #
# --------------------------------------------------------------------------- #


def _synthetic_pose_frames(count: int = 12, size: int = 320) -> list[np.ndarray]:
    """Render a moving stick figure; deterministic synthetic fixture frames."""
    import cv2

    frames = []
    for index in range(count):
        frame = np.full((size, size, 3), 235, dtype=np.uint8)
        phase = index / float(max(count - 1, 1))
        hip = (int(size * 0.5), int(size * 0.62))
        shoulder = (int(size * 0.5 + 14 * math.sin(phase * math.pi)), int(size * 0.36))
        arm_sw = int(26 * math.sin(phase * 2 * math.pi))
        color = (30, 30, 30)
        cv2.circle(frame, (shoulder[0], shoulder[1] - 22), 14, color, 2)
        cv2.line(frame, shoulder, hip, color, 2)
        cv2.line(
            frame, shoulder, (shoulder[0] + arm_sw, shoulder[1] + 34), color, 2
        )
        cv2.line(
            frame, shoulder, (shoulder[0] - arm_sw, shoulder[1] + 34), color, 2
        )
        cv2.line(frame, hip, (hip[0] - 18, hip[1] + 48), color, 2)
        cv2.line(frame, hip, (hip[0] + 18, hip[1] + 48), color, 2)
        frames.append(frame)
    return frames


def self_test_real() -> int:
    from scripts.continuity_spike_backend_real import RealSpikeBackend

    frames = _synthetic_pose_frames()
    backend = RealSpikeBackend()
    persons = [backend._detect_persons(frame) for frame in frames]
    primary = [_primary_valid_person(p) for p in persons]
    valid_people = [p for p in primary if p is not None]
    height, width = frames[0].shape[:2]
    features, reason = aggregate_curve_features(valid_people, len(frames), width, height)
    summary = {
        "self_test": "real",
        "frame_count": len(frames),
        "detected_frames": sum(1 for p in persons if p),
        "pose_valid_frames": len(valid_people),
        "status": "AVAILABLE" if features is not None else "UNAVAILABLE",
        "missing_reasons": [reason] if reason else [],
        "features": features,
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", help="UTF-8 JSON request file")
    parser.add_argument("--response", help="UTF-8 JSON response file")
    parser.add_argument(
        "--self-test-real",
        action="store_true",
        help="run the real model against synthetic pose fixtures",
    )
    args = parser.parse_args()

    if args.self_test_real:
        return self_test_real()

    if not args.request or not args.response:
        parser.error("--request and --response are required")
    return run_worker(Path(args.request), Path(args.response))


if __name__ == "__main__":
    raise SystemExit(main())

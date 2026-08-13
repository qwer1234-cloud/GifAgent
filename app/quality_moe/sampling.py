"""Bounded, deterministic frame sampling for a single candidate interval."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import cv2
import numpy as np

from app.quality_moe.models import EvidenceStatus


_MAX_LONGEST_SIDE = 640
_DEFAULT_SAMPLE_COUNT = 6
_MIN_SAMPLE_COUNT = 6
_MAX_SAMPLE_COUNT = 8


def _finite_timestamp(value: float, *, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    return parsed


def _frozen_diagnostics(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({str(key): item for key, item in value.items()})


@dataclass(frozen=True)
class SampledClip:
    """Frame evidence restricted to exactly one candidate's time interval."""

    candidate_id: str
    video_path: str | Path
    start_ts: float
    end_ts: float
    timestamps: tuple[float, ...] = ()
    frames: tuple[np.ndarray, ...] = ()
    status: EvidenceStatus = EvidenceStatus.AVAILABLE
    diagnostics: Mapping[str, object] = field(default_factory=dict)
    semantic_labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not self.candidate_id:
            raise ValueError("candidate_id must be a non-empty string")
        if not isinstance(self.video_path, (str, Path)):
            raise ValueError("video_path must be a string or Path")
        start_ts = _finite_timestamp(self.start_ts, name="start_ts")
        end_ts = _finite_timestamp(self.end_ts, name="end_ts")
        if start_ts < 0 or end_ts <= start_ts:
            raise ValueError("candidate interval must satisfy 0 <= start_ts < end_ts")
        timestamps = tuple(_finite_timestamp(value, name="timestamp") for value in self.timestamps)
        frames = tuple(self.frames)
        status = EvidenceStatus(self.status)
        if status is EvidenceStatus.AVAILABLE:
            if not frames or len(timestamps) != len(frames):
                raise ValueError("available sampled clips need matching frames and timestamps")
            if not _MIN_SAMPLE_COUNT <= len(frames) <= _MAX_SAMPLE_COUNT:
                raise ValueError("available sampled clips require six to eight frames")
            if any(timestamp < start_ts or timestamp > end_ts for timestamp in timestamps):
                raise ValueError("sample timestamp lies outside the candidate interval")
            for frame in frames:
                if not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.shape[2] != 3:
                    raise ValueError("sample frames must be BGR image arrays")
                if frame.dtype != np.uint8 or frame.size == 0:
                    raise ValueError("sample frames must be non-empty uint8 arrays")
        elif frames or timestamps:
            raise ValueError("unavailable sampled clips must not contain frames or timestamps")
        if not isinstance(self.diagnostics, Mapping):
            raise ValueError("diagnostics must be a mapping")
        if not all(isinstance(label, str) for label in self.semantic_labels):
            raise ValueError("semantic_labels must contain strings")
        object.__setattr__(self, "video_path", str(self.video_path))
        object.__setattr__(self, "start_ts", start_ts)
        object.__setattr__(self, "end_ts", end_ts)
        object.__setattr__(self, "timestamps", timestamps)
        object.__setattr__(self, "frames", frames)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "diagnostics", _frozen_diagnostics(self.diagnostics))
        object.__setattr__(self, "semantic_labels", tuple(self.semantic_labels))

    @property
    def input_hash(self) -> str:
        """Hash only scored pixels and interval facts; labels are never score input."""
        digest = hashlib.sha256()
        digest.update(self.candidate_id.encode("utf-8"))
        digest.update(self.video_path.encode("utf-8"))
        digest.update(repr((self.start_ts, self.end_ts, self.timestamps, self.status.value)).encode("ascii"))
        for frame in self.frames:
            digest.update(repr((frame.shape, frame.dtype.str)).encode("ascii"))
            digest.update(np.ascontiguousarray(frame).tobytes())
        return digest.hexdigest()


def _unavailable(
    *, candidate_id: str, video_path: str | Path, start_ts: float, end_ts: float, code: str
) -> SampledClip:
    return SampledClip(
        candidate_id=candidate_id,
        video_path=video_path,
        start_ts=start_ts,
        end_ts=end_ts,
        status=EvidenceStatus.UNAVAILABLE,
        diagnostics={"code": code},
    )


def _resize(frame: np.ndarray) -> np.ndarray:
    height, width = frame.shape[:2]
    longest_side = max(height, width)
    if longest_side <= _MAX_LONGEST_SIDE:
        return frame
    scale = _MAX_LONGEST_SIDE / longest_side
    return cv2.resize(
        frame,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )


def sample_clip_frames(
    video_path: str | Path,
    start_ts: float,
    end_ts: float,
    candidate_id: str,
    *,
    sample_count: int = _DEFAULT_SAMPLE_COUNT,
) -> SampledClip:
    """Random-access sample one media file without reading neighbouring files.

    The generated timestamps are validated before and after frame decoding, so a
    caller cannot accidentally submit frames outside its exact candidate range.
    """
    start_ts = _finite_timestamp(start_ts, name="start_ts")
    end_ts = _finite_timestamp(end_ts, name="end_ts")
    if start_ts < 0 or end_ts <= start_ts:
        raise ValueError("candidate interval must satisfy 0 <= start_ts < end_ts")
    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or not _MIN_SAMPLE_COUNT <= sample_count <= _MAX_SAMPLE_COUNT
    ):
        raise ValueError("sample_count must be an integer from six to eight")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("candidate_id must be a non-empty string")

    timestamps = tuple(float(value) for value in np.linspace(start_ts, end_ts, sample_count))
    if any(timestamp < start_ts or timestamp > end_ts for timestamp in timestamps):
        raise ValueError("sample timestamp lies outside the candidate interval")
    path = Path(video_path)
    if not path.is_file():
        return _unavailable(
            candidate_id=candidate_id, video_path=video_path, start_ts=start_ts,
            end_ts=end_ts, code="media_unavailable",
        )

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        capture.release()
        return _unavailable(
            candidate_id=candidate_id, video_path=video_path, start_ts=start_ts,
            end_ts=end_ts, code="media_unavailable",
        )
    try:
        frames: list[np.ndarray] = []
        decoded_timestamps: list[float] = []
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        tolerance = max(0.05, 1.0 / fps) if math.isfinite(fps) and fps > 0 else 0.05
        for timestamp in timestamps:
            if not capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0):
                return _unavailable(
                    candidate_id=candidate_id, video_path=video_path, start_ts=start_ts,
                    end_ts=end_ts, code="random_access_unavailable",
                )
            decoded, frame = capture.read()
            if not decoded or frame is None:
                return _unavailable(
                    candidate_id=candidate_id, video_path=video_path, start_ts=start_ts,
                    end_ts=end_ts, code="frame_decode_failed",
                )
            actual_timestamp = float(capture.get(cv2.CAP_PROP_POS_MSEC)) / 1000.0
            if not math.isfinite(actual_timestamp) or actual_timestamp < start_ts or actual_timestamp > end_ts:
                return _unavailable(
                    candidate_id=candidate_id, video_path=video_path, start_ts=start_ts,
                    end_ts=end_ts, code="decoded_timestamp_outside_interval",
                )
            if abs(actual_timestamp - timestamp) > tolerance:
                return _unavailable(
                    candidate_id=candidate_id, video_path=video_path, start_ts=start_ts,
                    end_ts=end_ts, code="decoded_timestamp_mismatch",
                )
            frames.append(_resize(frame))
            decoded_timestamps.append(actual_timestamp)
    finally:
        capture.release()
    return SampledClip(
        candidate_id=candidate_id,
        video_path=video_path,
        start_ts=start_ts,
        end_ts=end_ts,
        timestamps=tuple(decoded_timestamps),
        frames=tuple(frames),
        diagnostics={"code": "sampled", "sample_count": len(frames)},
    )

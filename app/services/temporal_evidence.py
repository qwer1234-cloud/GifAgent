"""Reusable, cached low-resolution temporal evidence for video analysis."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path

import cv2
import numpy as np


class TemporalMediaError(ValueError):
    """The requested temporal samples could not be decoded from media."""


@dataclass(frozen=True)
class TemporalScanConfig:
    fps: float = 8.0
    width: int = 320
    motion_compensation: bool = True


@dataclass(frozen=True)
class TemporalFrame:
    sample_index: int
    timestamp_s: float
    gray: np.ndarray
    hsv: np.ndarray


@dataclass(frozen=True)
class TemporalPairEvidence:
    timestamp_s: float
    previous_gray: np.ndarray
    gray: np.ndarray
    histogram_distance: float
    edge_distance: float
    luma_change: float
    compensated_residual: float
    inlier_ratio: float
    translate_x: float
    translate_y: float
    scale: float
    residual_map: np.ndarray


@dataclass(frozen=True)
class TemporalEvidence:
    start_s: float
    end_s: float
    fps: float
    width: int
    frames: tuple[TemporalFrame, ...]
    pairs: tuple[TemporalPairEvidence, ...]

    def slice(self, start_s: float, end_s: float) -> "TemporalEvidence":
        frames = tuple(frame for frame in self.frames if start_s - 1e-6 <= frame.timestamp_s <= end_s + 1e-6)
        pairs = tuple(pair for pair in self.pairs if start_s - 1e-6 <= pair.timestamp_s <= end_s + 1e-6)
        return TemporalEvidence(start_s, end_s, self.fps, self.width, frames, pairs)

    def resample(self, fps: float) -> "TemporalEvidence":
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError("fps must be finite and positive")
        indexes = {int(round(index * self.fps / fps)) for index in range(int(math.floor(self.start_s * fps)), int(math.ceil(self.end_s * fps)) + 1)}
        frames = tuple(frame for frame in self.frames if frame.sample_index in indexes)
        retained = {frame.sample_index for frame in frames}
        pairs = tuple(pair for pair in self.pairs if int(round(pair.timestamp_s * self.fps)) in retained)
        return TemporalEvidence(self.start_s, self.end_s, fps, self.width, frames, pairs)


def _identity(path: str | Path) -> tuple[str, int, int]:
    resolved = Path(path).resolve()
    stat = resolved.stat()
    return str(resolved), stat.st_size, stat.st_mtime_ns


def _resize(frame: np.ndarray, width: int) -> tuple[np.ndarray, np.ndarray]:
    height = max(1, round(frame.shape[0] * width / frame.shape[1]))
    small = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), cv2.cvtColor(small, cv2.COLOR_BGR2HSV)


def _pair(previous: TemporalFrame, current: TemporalFrame, motion_compensation: bool) -> TemporalPairEvidence:
    hist_a = cv2.calcHist([previous.hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
    hist_b = cv2.calcHist([current.hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
    cv2.normalize(hist_a, hist_a)
    cv2.normalize(hist_b, hist_b)
    histogram_distance = float(cv2.compareHist(hist_a, hist_b, cv2.HISTCMP_BHATTACHARYYA))
    edge_distance = float(np.mean(cv2.Canny(previous.gray, 60, 140) != cv2.Canny(current.gray, 60, 140)))
    luma_change = float(np.mean(cv2.absdiff(previous.gray, current.gray)) / 255.0)
    transform: np.ndarray | None = None
    inlier_ratio = 0.0
    if motion_compensation:
        points = cv2.goodFeaturesToTrack(previous.gray, maxCorners=160, qualityLevel=0.01, minDistance=5, blockSize=5)
        if points is not None and len(points) >= 8:
            next_points, status, _ = cv2.calcOpticalFlowPyrLK(previous.gray, current.gray, points, None)
            if next_points is not None and status is not None:
                good = status.ravel().astype(bool)
                if int(good.sum()) >= 6:
                    transform, inliers = cv2.estimateAffinePartial2D(points[good], next_points[good], method=cv2.RANSAC)
                    if transform is not None and inliers is not None:
                        inlier_ratio = float(np.mean(inliers.ravel().astype(bool)))
    if transform is None:
        transform = np.float32([[1, 0, 0], [0, 1, 0]])
    warped = cv2.warpAffine(previous.gray, transform, (current.gray.shape[1], current.gray.shape[0]), borderMode=cv2.BORDER_REFLECT)
    residual_map = cv2.absdiff(warped, current.gray)
    return TemporalPairEvidence(
        current.timestamp_s, previous.gray, current.gray, histogram_distance, edge_distance, luma_change,
        float(np.mean(residual_map) / 255.0), inlier_ratio, float(transform[0, 2]), float(transform[1, 2]),
        float(math.hypot(transform[0, 0], transform[1, 0])), residual_map,
    )


class TemporalEvidenceCache:
    """Cache sampled frames and pair metrics by immutable video identity."""

    def __init__(self) -> None:
        self._frames: dict[tuple[tuple[str, int, int], float, int, int], TemporalFrame] = {}
        self._pairs: dict[tuple[tuple[str, int, int], float, int, bool, int], TemporalPairEvidence] = {}
        self.decoded_frame_count = 0

    def _decode_range(self, path: Path, indexes: list[int], config: TemporalScanConfig) -> None:
        for attempt in range(2):
            capture = cv2.VideoCapture(str(path))
            if not capture.isOpened():
                capture.release()
                if attempt == 1:
                    raise TemporalMediaError("OpenCV could not open the source video")
                continue
            try:
                source_fps = capture.get(cv2.CAP_PROP_FPS)
                if not math.isfinite(source_fps) or source_fps <= 0:
                    source_fps = config.fps
                frame_count = int(round(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
                source_indexes = {
                    int(round(index * source_fps / config.fps))
                    for index in indexes
                }
                if frame_count > 0:
                    source_indexes = {index for index in source_indexes if 0 <= index < frame_count}
                if not source_indexes:
                    raise TemporalMediaError("requested temporal range has no decodable source frames")
                wanted = {
                    int(round(index * config.fps / source_fps))
                    for index in source_indexes
                }
                first_source_index = min(source_indexes)
                capture.set(cv2.CAP_PROP_POS_FRAMES, first_source_index)
                while wanted:
                    ok, image = capture.read()
                    if not ok:
                        break
                    source_index = int(round(capture.get(cv2.CAP_PROP_POS_FRAMES) - 1))
                    if source_index > max(source_indexes):
                        break
                    sample_index = int(round(source_index * config.fps / source_fps))
                    if sample_index not in wanted:
                        continue
                    gray, hsv = _resize(image, config.width)
                    key = (_identity(path), config.fps, config.width, sample_index)
                    is_new = key not in self._frames
                    self._frames[key] = TemporalFrame(sample_index, sample_index / config.fps, gray, hsv)
                    if is_new:
                        self.decoded_frame_count += 1
                    wanted.remove(sample_index)
                if not wanted:
                    return
                if attempt == 1:
                    raise TemporalMediaError("OpenCV could not decode the requested source video samples")
            finally:
                capture.release()
        raise TemporalMediaError("OpenCV could not decode the requested source video samples")

    def scan(self, video_path: str | Path, start_s: float, end_s: float, config: TemporalScanConfig) -> TemporalEvidence:
        if not (math.isfinite(start_s) and math.isfinite(end_s) and end_s >= start_s and math.isfinite(config.fps) and config.fps > 0 and config.width > 0):
            raise ValueError("invalid temporal scan range or configuration")
        path = Path(video_path)
        identity = _identity(path)
        indexes = list(range(int(round(start_s * config.fps)), int(round(end_s * config.fps)) + 1))
        missing = [index for index in indexes if (identity, config.fps, config.width, index) not in self._frames]
        for index in range(0, len(missing)):
            if index == 0 or missing[index] != missing[index - 1] + 1:
                run: list[int] = []
                cursor = index
                while cursor < len(missing) and (cursor == index or missing[cursor] == missing[cursor - 1] + 1):
                    run.append(missing[cursor]); cursor += 1
                self._decode_range(path, run, config)
        frames = tuple(self._frames[key] for index in indexes if (key := (identity, config.fps, config.width, index)) in self._frames)
        if not frames:
            raise TemporalMediaError("OpenCV could not decode requested source video samples")
        pairs: list[TemporalPairEvidence] = []
        for previous, current in zip(frames, frames[1:]):
            key = (identity, config.fps, config.width, config.motion_compensation, current.sample_index)
            if key not in self._pairs:
                self._pairs[key] = _pair(previous, current, config.motion_compensation)
            pairs.append(self._pairs[key])
        return TemporalEvidence(start_s, end_s, config.fps, config.width, frames, tuple(pairs))

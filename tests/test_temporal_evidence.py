"""Shared cached temporal scan coverage."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from app.services.temporal_evidence import (
    TemporalEvidenceCache,
    TemporalMediaError,
    TemporalScanConfig,
)
from app.services.transition_guard import guard_candidate_window
from tests.test_transition_guard import BASE_CFG


FPS = 8
FRAME_SIZE = (160, 90)


def _frame(seed: int, index: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = rng.integers(20, 220, (FRAME_SIZE[1], FRAME_SIZE[0], 3), dtype=np.uint8)
    for x in range(0, FRAME_SIZE[0], 16):
        cv2.line(image, (x, 0), (x, FRAME_SIZE[1] - 1), (255, 255, 255), 1)
    for y in range(0, FRAME_SIZE[1], 15):
        cv2.line(image, (0, y), (FRAME_SIZE[0] - 1, y), (0, 0, 0), 1)
    cv2.rectangle(image, (index % 100, 24), (index % 100 + 24, 58), (0, 255, 255), -1)
    return image


def _write_cache_video(path: Path, *, hard_cut: bool) -> Path:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, FRAME_SIZE)
    assert writer.isOpened()
    try:
        for index in range(48):
            writer.write(_frame(1 if not hard_cut or index < 24 else 2, index))
    finally:
        writer.release()
    return path


def test_overlapping_scans_decode_only_missing_samples(tmp_path: Path) -> None:
    video = _write_cache_video(tmp_path / "motion.mp4", hard_cut=False)
    cache = TemporalEvidenceCache()
    config = TemporalScanConfig(fps=8.0, width=160, motion_compensation=True)

    first = cache.scan(video, 0.0, 4.0, config)
    decoded_after_first = cache.decoded_frame_count
    second = cache.scan(video, 2.0, 6.0, config)

    assert first.frames
    assert second.frames
    assert cache.decoded_frame_count < decoded_after_first * 2
    assert len({frame.sample_index for frame in second.frames}) == len(second.frames)


def test_precomputed_evidence_matches_direct_transition_scan(tmp_path: Path) -> None:
    video = _write_cache_video(tmp_path / "hard-cut.mp4", hard_cut=True)
    cache = TemporalEvidenceCache()
    evidence = cache.scan(video, 0.0, 6.0, TemporalScanConfig(fps=8.0, width=320, motion_compensation=True))

    direct = guard_candidate_window(video, 0.0, 6.0, 1.0, BASE_CFG)
    shared = guard_candidate_window(video, 0.0, 6.0, 1.0, BASE_CFG, temporal_evidence=evidence)

    assert shared.transition_action == direct.transition_action
    assert shared.hard_cut_count == direct.hard_cut_count
    assert shared.segments == direct.segments


def test_slice_keeps_only_frames_and_pairs_in_requested_window(tmp_path: Path) -> None:
    evidence = TemporalEvidenceCache().scan(
        _write_cache_video(tmp_path / "slice.mp4", hard_cut=False), 0.0, 6.0,
        TemporalScanConfig(fps=8.0, width=160),
    ).slice(2.0, 4.0)

    assert all(2.0 - 1e-6 <= item.timestamp_s <= 4.0 + 1e-6 for item in evidence.frames)
    assert all(2.0 - 1e-6 <= item.timestamp_s <= 4.0 + 1e-6 for item in evidence.pairs)


def test_resample_selects_deterministic_source_indexes(tmp_path: Path) -> None:
    evidence = TemporalEvidenceCache().scan(
        _write_cache_video(tmp_path / "resample.mp4", hard_cut=False), 0.0, 6.0,
        TemporalScanConfig(fps=8.0, width=160),
    )
    sampled = evidence.resample(4.0)

    assert [item.sample_index for item in sampled.frames] == list(range(0, 48, 2))


def test_scan_retries_one_failed_open(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    video = _write_cache_video(tmp_path / "retry.mp4", hard_cut=False)
    real_capture = cv2.VideoCapture
    calls = 0

    class FailedCapture:
        def isOpened(self) -> bool:
            return False

        def release(self) -> None:
            pass

    def capture(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return FailedCapture() if calls == 1 else real_capture(*args, **kwargs)

    monkeypatch.setattr(cv2, "VideoCapture", capture)
    result = TemporalEvidenceCache().scan(video, 0.0, 1.0, TemporalScanConfig(fps=8.0, width=160))
    assert result.frames
    assert calls >= 2


def test_scan_raises_typed_error_after_two_unreadable_attempts(tmp_path: Path) -> None:
    path = tmp_path / "not-a-video.mp4"
    path.write_text("not media", encoding="utf-8")
    with pytest.raises(TemporalMediaError):
        TemporalEvidenceCache().scan(path, 0.0, 1.0, TemporalScanConfig())

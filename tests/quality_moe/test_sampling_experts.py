from __future__ import annotations

import math

import cv2
import numpy as np

from app.quality_moe.experts import (
    CinematicExpert,
    TechnicalAestheticExpert,
    TemporalExpert,
)
from app.quality_moe.models import EvidenceStatus
from app.quality_moe.sampling import SampledClip, sample_clip_frames


def sampled_clip(
    frames: tuple[np.ndarray, ...], *, semantic_labels: tuple[str, ...] = ()
) -> SampledClip:
    return SampledClip(
        candidate_id="candidate-1",
        video_path="synthetic://candidate-1",
        start_ts=10.0,
        end_ts=12.0,
        timestamps=tuple(10.0 + index * 0.25 for index in range(len(frames))),
        frames=frames,
        semantic_labels=semantic_labels,
    )


def test_underexposed_clip_reports_repairable_exposure_issue():
    frames = tuple(np.full((90, 160, 3), 12, dtype=np.uint8) for _ in range(6))

    evidence = TechnicalAestheticExpert().evaluate(sampled_clip(frames))

    assert evidence.status is EvidenceStatus.AVAILABLE
    assert evidence.scores["technical_integrity"] < 0.5
    assert any(finding["code"] == "underexposed_subject" for finding in evidence.findings)


def test_single_flash_frame_is_temporal_negative_signal():
    frames = [np.full((90, 160, 3), 80, np.uint8) for _ in range(6)]
    frames[3] = np.full((90, 160, 3), 250, np.uint8)

    evidence = TemporalExpert().evaluate(sampled_clip(tuple(frames)))

    assert evidence.scores["temporal_coherence"] < 0.7
    assert evidence.signal_family == "deterministic_temporal"


def test_identical_pixels_ignore_semantic_labels():
    frames = tuple(np.full((90, 160, 3), 100, np.uint8) for _ in range(6))
    night = sampled_clip(frames, semantic_labels=("low-key", "handheld"))
    daylight = sampled_clip(frames, semantic_labels=("daylight", "tripod"))

    for expert in (TechnicalAestheticExpert(), TemporalExpert(), CinematicExpert()):
        assert expert.evaluate(night).scores == expert.evaluate(daylight).scores


def test_expert_scores_are_always_finite_and_bounded():
    frames = tuple(
        np.full((90, 160, 3), index * 50, dtype=np.uint8)
        for index in range(6)
    )
    clip = sampled_clip(frames)

    for expert in (TechnicalAestheticExpert(), TemporalExpert(), CinematicExpert()):
        evidence = expert.evaluate(clip)
        assert evidence.status is EvidenceStatus.AVAILABLE
        assert all(math.isfinite(score) and 0.0 <= score <= 1.0 for score in evidence.scores.values())


def test_missing_or_corrupt_media_is_typed_unavailable(tmp_path):
    corrupt_path = tmp_path / "corrupt.mp4"
    corrupt_path.write_bytes(b"not a video")

    for media_path in (tmp_path / "does-not-exist.mp4", corrupt_path):
        result = sample_clip_frames(
            video_path=media_path,
            start_ts=1.0,
            end_ts=2.0,
            candidate_id="candidate-missing",
        )

        assert result.status is EvidenceStatus.UNAVAILABLE
        assert result.frames == ()
        assert result.diagnostics["code"] == "media_unavailable"


def test_sampling_is_deterministic_bounded_and_stays_in_candidate_interval(tmp_path):
    video_path = tmp_path / "source.avi"
    writer = cv2.VideoWriter(
        str(video_path), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (800, 400)
    )
    assert writer.isOpened()
    for value in range(20):
        writer.write(np.full((400, 800, 3), value * 10, dtype=np.uint8))
    writer.release()

    first = sample_clip_frames(video_path, 0.4, 1.3, "candidate-video")
    second = sample_clip_frames(video_path, 0.4, 1.3, "candidate-video")

    assert first.status is EvidenceStatus.AVAILABLE
    assert first.timestamps == second.timestamps
    assert len(first.frames) == 6
    assert all(0.4 <= timestamp <= 1.3 for timestamp in first.timestamps)
    assert all(max(frame.shape[:2]) <= 640 for frame in first.frames)
    assert all(np.array_equal(left, right) for left, right in zip(first.frames, second.frames))

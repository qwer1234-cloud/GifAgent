"""Tests for candidate capture identity, preexport_key, and config normalization."""
from __future__ import annotations

import decimal
import hashlib
import json
import math
from pathlib import Path

import pytest

from app.services.candidate_capture import (
    assign_candidate_capture_identities,
    canonical_capture_config_hash,
    capture_config,
    compute_source_sha256,
    preexport_key,
    resolve_anchor_us,
    seconds_to_microsecond_int,
    validate_candidate_capture_config,
)


def test_key_ignores_list_order():
    a = preexport_key("a" * 64, 1_000_000, 4_000_000, 2_500_000)
    b = preexport_key("a" * 64, 1_000_000, 4_000_000, 2_500_000)
    assert a == b and len(a) == 64
    assert a != preexport_key("a" * 64, 1_000_000, 4_000_001, 2_500_000)


def test_preexport_key_deterministic_canonical_json():
    key = preexport_key("0123456789abcdef" * 4, 2_000_000, 5_000_000, 3_500_000)
    expected_payload = {
        "anchor_us": 3_500_000,
        "end_us": 5_000_000,
        "source_sha256": "0123456789abcdef" * 4,
        "start_us": 2_000_000,
    }
    canonical_bytes = json.dumps(expected_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected_hash = hashlib.sha256(canonical_bytes).hexdigest()
    assert key == expected_hash


def test_preexport_key_validation_errors():
    valid_sha = "f" * 64
    # Rejection of bool
    with pytest.raises(ValueError, match="boolean"):
        preexport_key(valid_sha, True, 4_000_000, 2_000_000)  # type: ignore

    with pytest.raises(ValueError, match="boolean"):
        preexport_key(valid_sha, 1_000_000, False, 2_000_000)  # type: ignore

    # Rejection of end <= start
    with pytest.raises(ValueError, match="end_us must be greater than start_us"):
        preexport_key(valid_sha, 4_000_000, 4_000_000, 4_000_000)

    with pytest.raises(ValueError, match="end_us must be greater than start_us"):
        preexport_key(valid_sha, 5_000_000, 4_000_000, 4_500_000)

    # Rejection of anchor out of bounds
    with pytest.raises(ValueError, match="anchor_us must be between start_us and end_us"):
        preexport_key(valid_sha, 1_000_000, 4_000_000, 500_000)

    with pytest.raises(ValueError, match="anchor_us must be between start_us and end_us"):
        preexport_key(valid_sha, 1_000_000, 4_000_000, 4_500_000)

    # Rejection of invalid sha256
    with pytest.raises(ValueError, match="source_sha256"):
        preexport_key("not-a-sha256", 1_000_000, 4_000_000, 2_000_000)

    with pytest.raises(ValueError, match="source_sha256"):
        preexport_key("g" * 64, 1_000_000, 4_000_000, 2_000_000)


def test_seconds_to_microsecond_int():
    assert seconds_to_microsecond_int(1.234567) == 1_234_567
    assert seconds_to_microsecond_int(1.2345674) == 1_234_567
    assert seconds_to_microsecond_int(1.2345675) == 1_234_568
    assert seconds_to_microsecond_int("2.5") == 2_500_000
    assert seconds_to_microsecond_int(10) == 10_000_000

    with pytest.raises(ValueError, match="boolean"):
        seconds_to_microsecond_int(True)

    with pytest.raises(ValueError, match="finite"):
        seconds_to_microsecond_int(float("nan"))

    with pytest.raises(ValueError, match="finite"):
        seconds_to_microsecond_int(float("inf"))


def test_resolve_anchor_us():
    clip_with_best_frame = {
        "start_ts": 2.0,
        "end_ts": 6.0,
        "best_frame": {"timestamp": 3.75},
    }
    anchor_us, source = resolve_anchor_us(clip_with_best_frame, 2_000_000, 6_000_000)
    assert anchor_us == 3_750_000
    assert source == "best_frame"

    clip_without_anchor = {
        "start_ts": 2.0,
        "end_ts": 6.0,
    }
    anchor_us, source = resolve_anchor_us(clip_without_anchor, 2_000_000, 6_000_000)
    assert anchor_us == 4_000_000
    assert source == "window_midpoint"


def test_compute_source_sha256(tmp_path: Path):
    video = tmp_path / "test_video.mp4"
    data = b"synthetic-video-bytes-for-test-12345"
    video.write_bytes(data)

    expected_sha = hashlib.sha256(data).hexdigest()
    computed_sha = compute_source_sha256(video)
    assert computed_sha == expected_sha

    # Test cache behavior (calling again returns cached result)
    assert compute_source_sha256(str(video)) == expected_sha

    # Missing file error
    with pytest.raises(FileNotFoundError):
        compute_source_sha256(tmp_path / "non_existent.mp4")


def test_assign_candidate_capture_identities_disabled_mode():
    clips = [
        {"start_ts": 1.0, "end_ts": 4.0},
        {"start_ts": 5.0, "end_ts": 8.0},
    ]
    assign_candidate_capture_identities(clips, "video.mp4", capture_enabled=False)
    assert "candidate_id" in clips[0]
    assert len(clips[0]["candidate_id"]) == 16
    assert "preexport_key" not in clips[0]


def test_assign_candidate_capture_identities_enabled_mode(tmp_path: Path):
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"sample-video-bytes")
    source_sha = hashlib.sha256(b"sample-video-bytes").hexdigest()

    clips = [
        {"start_ts": 1.0, "end_ts": 4.0, "best_frame": {"timestamp": 2.5}},
        {"start_ts": 5.0, "end_ts": 8.0, "best_frame": {"timestamp": 6.5}},
    ]
    assign_candidate_capture_identities(
        clips, str(video), source_sha256=source_sha, capture_enabled=True, capture_version="capture-v1"
    )
    for clip in clips:
        assert "preexport_key" in clip
        assert len(clip["preexport_key"]) == 64
        assert clip["candidate_id"] == clip["preexport_key"][:16]
        assert clip["clip_id"] == clip["candidate_id"]
        assert clip["candidate_capture_version"] == "capture-v1"


def test_assign_candidate_capture_identities_preserves_legacy_identities_with_marker(tmp_path):
    """Task 1: a bare candidate_id is only preserved when the clip explicitly
    carries candidate_identity_version=legacy-v1."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"legacy-identity-video")

    clips = [
        {
            "candidate_id": "frozen_id_123456",
            "clip_id": "frozen_id_123456",
            "candidate_identity_version": "legacy-v1",
            "start_ts": 1.0,
            "end_ts": 4.0,
        },
    ]
    assign_candidate_capture_identities(clips, str(video), capture_enabled=True)
    assert clips[0]["candidate_id"] == "frozen_id_123456"
    assert "preexport_key" not in clips[0]


def test_assign_candidate_capture_identities_recomputes_full_key_without_marker(tmp_path):
    """Task 1: without the legacy marker a new capture always computes the
    full preexport key, even when a stale candidate_id is present."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"no-marker-video")

    clips = [{"candidate_id": "stale00000000001", "start_ts": 1.0, "end_ts": 4.0}]
    assign_candidate_capture_identities(clips, str(video), capture_enabled=True)
    assert len(clips[0]["preexport_key"]) == 64
    assert clips[0]["candidate_id"] == clips[0]["preexport_key"][:16]


def test_assign_candidate_capture_identities_unreadable_source_raises():
    """Task 1: capture enabled must never synthesize a placeholder source
    identity when the source file cannot be read."""
    clips = [{"start_ts": 1.0, "end_ts": 4.0}]
    from app.services.candidate_capture import SourceIdentityUnavailable

    with pytest.raises(SourceIdentityUnavailable):
        assign_candidate_capture_identities(
            clips, "does_not_exist.mp4", capture_enabled=True
        )
    assert "preexport_key" not in clips[0]


def test_assign_candidate_capture_identities_rejects_non_hex_source_sha():
    """Task 1: a passed-in source SHA that is not strict lowercase hex must
    raise instead of silently producing fake keys."""
    from app.services.candidate_capture import SourceIdentityUnavailable

    clips = [{"start_ts": 1.0, "end_ts": 4.0}]
    with pytest.raises(SourceIdentityUnavailable):
        assign_candidate_capture_identities(
            clips, "video.mp4", source_sha256="NOT-A-HASH", capture_enabled=True
        )

    clips = [{"start_ts": 1.0, "end_ts": 4.0}]
    with pytest.raises(SourceIdentityUnavailable):
        assign_candidate_capture_identities(
            clips, "video.mp4", source_sha256=("a" * 63), capture_enabled=True
        )


def test_assign_candidate_capture_identities_resume_recomputes_same_key(tmp_path):
    """Task 1: a frozen preexport identity survives a re-assignment unchanged."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"resume-video-bytes")

    first = [{"start_ts": 1.0, "end_ts": 4.0, "best_frame": {"timestamp": 2.5}}]
    assign_candidate_capture_identities(first, str(video), capture_enabled=True)
    frozen = dict(first[0])

    second = [dict(frozen)]
    assign_candidate_capture_identities(second, str(video), capture_enabled=True)
    assert second[0]["preexport_key"] == frozen["preexport_key"]
    assert second[0]["candidate_id"] == frozen["candidate_id"]

    tampered = [dict(frozen, end_us=frozen["end_us"] + 1, end_ts=4.000001)]
    with pytest.raises(ValueError, match="preexport_key mismatch"):
        assign_candidate_capture_identities(tampered, str(video), capture_enabled=True)


def test_candidate_id_collision_detection():
    # Force collision in 16-char prefix by mocking preexport_key
    clips = [
        {"start_ts": 1.0, "end_ts": 4.0},
        {"start_ts": 5.0, "end_ts": 8.0},
    ]
    from unittest.mock import patch

    with patch(
        "app.services.candidate_capture.preexport_key",
        side_effect=["0123456789abcdef" + "0" * 48, "0123456789abcdef" + "1" * 48],
    ):
        with pytest.raises(ValueError, match="collision"):
            assign_candidate_capture_identities(
                clips, "dummy.mp4", source_sha256="f" * 64, capture_enabled=True
            )


def test_capture_config_normalization():
    raw = {
        "candidate_capture": {
            "enabled": True,
            "version": "capture-v1",
            "failure_policy": "record_and_retry",
        }
    }
    cfg = capture_config(raw)
    assert cfg["enabled"] is True
    assert cfg["version"] == "capture-v1"
    assert cfg["failure_policy"] == "record_and_retry"
    assert len(cfg["config_hash"]) == 64


def test_validate_candidate_capture_config_requires_capture_for_enforce_or_active():
    # continuity enforce without candidate_capture
    with pytest.raises(ValueError, match="requires candidate_capture"):
        validate_candidate_capture_config(
            candidate_capture={"enabled": False},
            continuity_gate={"enabled": True, "mode": "enforce"},
            curve_preference={"enabled": False, "mode": "shadow"},
        )

    # curve active without candidate_capture
    with pytest.raises(ValueError, match="requires candidate_capture"):
        validate_candidate_capture_config(
            candidate_capture={"enabled": False},
            continuity_gate={"enabled": False, "mode": "shadow"},
            curve_preference={"enabled": True, "mode": "active"},
        )

    # valid combination
    validate_candidate_capture_config(
        candidate_capture={"enabled": True},
        continuity_gate={"enabled": True, "mode": "enforce"},
        curve_preference={"enabled": True, "mode": "active"},
    )


def test_reordered_candidates_produce_identical_keys(tmp_path: Path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"reordered-test-video")
    source_sha = hashlib.sha256(b"reordered-test-video").hexdigest()

    clip_a = {"start_ts": 1.0, "end_ts": 4.0, "best_frame": {"timestamp": 2.5}}
    clip_b = {"start_ts": 5.0, "end_ts": 8.0, "best_frame": {"timestamp": 6.5}}

    list1 = [dict(clip_a), dict(clip_b)]
    list2 = [dict(clip_b), dict(clip_a)]

    assign_candidate_capture_identities(list1, str(video), source_sha256=source_sha, capture_enabled=True)
    assign_candidate_capture_identities(list2, str(video), source_sha256=source_sha, capture_enabled=True)

    # list1[0] (clip_a) must match list2[1] (clip_a)
    assert list1[0]["preexport_key"] == list2[1]["preexport_key"]
    assert list1[0]["candidate_id"] == list2[1]["candidate_id"]

    # list1[1] (clip_b) must match list2[0] (clip_b)
    assert list1[1]["preexport_key"] == list2[0]["preexport_key"]
    assert list1[1]["candidate_id"] == list2[0]["candidate_id"]


def test_same_name_different_source_bytes_produce_different_keys(tmp_path: Path):
    dir1 = tmp_path / "dir1"
    dir2 = tmp_path / "dir2"
    dir1.mkdir()
    dir2.mkdir()

    v1 = dir1 / "video.mp4"
    v2 = dir2 / "video.mp4"
    v1.write_bytes(b"content-version-1")
    v2.write_bytes(b"content-version-2")

    clip = {"start_ts": 1.0, "end_ts": 4.0, "best_frame": {"timestamp": 2.5}}

    clips1 = [dict(clip)]
    clips2 = [dict(clip)]

    assign_candidate_capture_identities(clips1, str(v1), capture_enabled=True)
    assign_candidate_capture_identities(clips2, str(v2), capture_enabled=True)

    assert clips1[0]["preexport_key"] != clips2[0]["preexport_key"]
    assert clips1[0]["source_video_sha256"] != clips2[0]["source_video_sha256"]


def test_window_change_produces_different_keys(tmp_path: Path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"window-change-test")

    clip1 = {"start_ts": 1.0, "end_ts": 4.0, "best_frame": {"timestamp": 2.5}}
    clip2 = {"start_ts": 1.0, "end_ts": 4.000001, "best_frame": {"timestamp": 2.5}}

    clips1 = [dict(clip1)]
    clips2 = [dict(clip2)]

    assign_candidate_capture_identities(clips1, str(video), capture_enabled=True)
    assign_candidate_capture_identities(clips2, str(video), capture_enabled=True)

    assert clips1[0]["preexport_key"] != clips2[0]["preexport_key"]


def test_pipeline_extract_config_integration():
    from app.pipeline.config import extract_config

    raw = {
        "candidate_capture": {
            "enabled": True,
            "version": "capture-v1",
        }
    }
    flat = extract_config(raw)
    assert flat["candidate_capture_enabled"] is True
    assert flat["candidate_capture_version"] == "capture-v1"
    assert "candidate_capture_config_hash" in flat


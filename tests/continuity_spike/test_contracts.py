"""Contract tests for the continuity spike manifest and probe results."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from scripts.continuity_spike_contracts import (
    CandidateProbeResult,
    ManifestError,
    ResultValidationError,
    SourceHashMismatchError,
    SourceUnavailableError,
    load_manifest,
    make_error_result,
    validate_probe_result,
    verify_source_identity,
)

SCHEMA_VERSION = "continuity-spike-input-v1"


def _write_source(
    tmp_path: Path,
    name: str = "source.mp4",
    content: bytes = b"synthetic-video",
) -> Path:
    video = tmp_path / name
    video.write_bytes(content)
    return video


def _source_payload(video: Path, digest: str) -> dict:
    return {
        "candidate_id": "synthetic-001",
        "media_kind": "source_video",
        "source_path": str(video.resolve()),
        "source_sha256": digest,
        "media_duration_s": 8.0,
        "confirmed_shot_start_s": 0.5,
        "context_start_s": 0.5,
        "window_start_s": 1.0,
        "window_end_s": 3.5,
        "context_end_s": 5.5,
        "confirmed_shot_end_s": 6.0,
        "scenario_tags": [
            "single_subject",
            "subtle_motion",
            "face_visible",
            "media_edge",
        ],
        "data_origin": "synthetic",
        "human_label": "UNLABELED",
        "feedback_label": "unknown",
    }


def _write_manifest_file(
    tmp_path: Path,
    candidates: list[dict],
    *,
    top_level_extra: dict | None = None,
) -> Path:
    payload: dict = {"schema_version": SCHEMA_VERSION, "candidates": candidates}
    if top_level_extra:
        payload.update(top_level_extra)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return manifest_path


def _single_manifest(
    tmp_path: Path,
    **overrides,
) -> tuple[Path, Path, str]:
    video = _write_source(tmp_path)
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    payload = _source_payload(video, digest)
    for key, value in overrides.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    return _write_manifest_file(tmp_path, [payload]), video, digest


def _historical_payload(tmp_path: Path) -> tuple[dict, Path, str]:
    gif = _write_source(tmp_path, name="clip.gif", content=b"gif-bytes")
    digest = hashlib.sha256(gif.read_bytes()).hexdigest()
    payload = {
        "candidate_id": "hist-001",
        "media_kind": "historical_gif",
        "source_path": str(gif.resolve()),
        "source_sha256": digest,
        "media_duration_s": 3.0,
        "confirmed_shot_start_s": 0.0,
        "context_start_s": 0.0,
        "window_start_s": 0.0,
        "window_end_s": 3.0,
        "context_end_s": 3.0,
        "confirmed_shot_end_s": 3.0,
        "scenario_tags": ["subtle_motion", "face_visible"],
        "data_origin": "real",
        "human_label": "UNLABELED",
        "feedback_label": "favorite",
    }
    return payload, gif, digest


def test_load_manifest_freezes_explicit_source_window(tmp_path):
    video = tmp_path / "source.mp4"
    video.write_bytes(b"synthetic-video")
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "candidates": [_source_payload(video, digest)],
            }
        ),
        encoding="utf-8",
    )

    manifest = load_manifest(manifest_path)

    assert manifest.schema_version == SCHEMA_VERSION
    assert manifest.candidates[0].duration_s == 2.5
    assert verify_source_identity(manifest.candidates[0]) == digest


def test_load_manifest_accepts_historical_gif_bounds(tmp_path):
    payload, _gif, digest = _historical_payload(tmp_path)
    manifest_path = _write_manifest_file(tmp_path, [payload])

    manifest = load_manifest(manifest_path)

    candidate = manifest.candidates[0]
    assert candidate.media_kind == "historical_gif"
    assert candidate.duration_s == 3.0
    assert candidate.source_sha256 == digest
    assert candidate.human_label == "UNLABELED"


def test_load_manifest_rejects_duplicate_candidate_id(tmp_path):
    video = _write_source(tmp_path)
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    payload = _source_payload(video, digest)
    duplicate = dict(payload, candidate_id="synthetic-001")
    second = dict(payload, candidate_id="synthetic-001")
    manifest_path = _write_manifest_file(tmp_path, [duplicate, second])

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(manifest_path)
    assert "candidate_id" in str(excinfo.value)


def test_load_manifest_rejects_relative_source_path(tmp_path):
    video = _write_source(tmp_path)
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    payload = _source_payload(video, digest)
    payload["source_path"] = "relative/source.mp4"
    manifest_path = _write_manifest_file(tmp_path, [payload])

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(manifest_path)
    assert "candidates[0].source_path" in str(excinfo.value)


def test_load_manifest_rejects_malformed_sha256(tmp_path):
    video = _write_source(tmp_path)
    payload = _source_payload(video, "SHA256:not-a-hash")
    manifest_path = _write_manifest_file(tmp_path, [payload])

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(manifest_path)
    assert "candidates[0].source_sha256" in str(excinfo.value)


def test_load_manifest_rejects_uppercase_sha256(tmp_path):
    video = _write_source(tmp_path)
    digest = hashlib.sha256(video.read_bytes()).hexdigest().upper()
    payload = _source_payload(video, digest)
    manifest_path = _write_manifest_file(tmp_path, [payload])

    with pytest.raises(ManifestError):
        load_manifest(manifest_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("window_end_s", 1.0),
        ("context_end_s", 0.75),
        ("confirmed_shot_end_s", 0.25),
    ],
)
def test_load_manifest_rejects_end_not_after_start(tmp_path, field, value):
    manifest_path, _video, _digest = _single_manifest(tmp_path, **{field: value})

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(manifest_path)
    assert f"candidates[0].{field}" in str(excinfo.value)


def test_load_manifest_rejects_window_before_context(tmp_path):
    manifest_path, _video, _digest = _single_manifest(
        tmp_path, window_start_s=0.25
    )

    with pytest.raises(ManifestError):
        load_manifest(manifest_path)


def test_load_manifest_rejects_empty_scenario_tags(tmp_path):
    manifest_path, _video, _digest = _single_manifest(tmp_path, scenario_tags=[])

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(manifest_path)
    assert "candidates[0].scenario_tags" in str(excinfo.value)


def test_load_manifest_rejects_non_snake_case_tag(tmp_path):
    manifest_path, _video, _digest = _single_manifest(
        tmp_path, scenario_tags=["single_subject", "Subtle-Motion", "face_visible"]
    )

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(manifest_path)
    assert "scenario_tags" in str(excinfo.value)


def test_load_manifest_rejects_duplicate_tags(tmp_path):
    manifest_path, _video, _digest = _single_manifest(
        tmp_path,
        scenario_tags=["single_subject", "subtle_motion", "subtle_motion"],
    )

    with pytest.raises(ManifestError):
        load_manifest(manifest_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("media_kind", "webm"),
        ("data_origin", "simulated"),
        ("human_label", "GOOD"),
        ("feedback_label", "loved_it"),
    ],
)
def test_load_manifest_rejects_unsupported_enum_values(tmp_path, field, value):
    manifest_path, _video, _digest = _single_manifest(tmp_path, **{field: value})

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(manifest_path)
    assert f"candidates[0].{field}" in str(excinfo.value)


def test_load_manifest_rejects_missing_source_file(tmp_path):
    video = tmp_path / "gone.mp4"
    payload = _source_payload(video, "0" * 64)
    manifest_path = _write_manifest_file(tmp_path, [payload])

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(manifest_path)
    assert "candidates[0].source_path" in str(excinfo.value)


def test_verify_source_identity_detects_changed_source(tmp_path):
    manifest_path, video, _digest = _single_manifest(tmp_path)
    manifest = load_manifest(manifest_path)

    video.write_bytes(b"mutated-content")

    with pytest.raises(SourceHashMismatchError):
        verify_source_identity(manifest.candidates[0])


def test_verify_source_identity_detects_missing_source(tmp_path):
    manifest_path, video, _digest = _single_manifest(tmp_path)
    manifest = load_manifest(manifest_path)

    video.unlink()

    with pytest.raises(SourceUnavailableError):
        verify_source_identity(manifest.candidates[0])


def test_load_manifest_rejects_boolean_where_number_expected(tmp_path):
    video = _write_source(tmp_path)
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    payload = _source_payload(video, digest)
    payload["media_duration_s"] = True
    manifest_path = _write_manifest_file(tmp_path, [payload])

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(manifest_path)
    assert "candidates[0].media_duration_s" in str(excinfo.value)


def test_load_manifest_rejects_non_finite_number(tmp_path):
    video = _write_source(tmp_path)
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    payload = _source_payload(video, digest)
    payload["window_end_s"] = float("inf")
    manifest_path = _write_manifest_file(tmp_path, [payload])

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(manifest_path)
    assert "candidates[0].window_end_s" in str(excinfo.value)


def test_load_manifest_rejects_non_positive_media_duration(tmp_path):
    manifest_path, _video, _digest = _single_manifest(tmp_path, media_duration_s=0.0)

    with pytest.raises(ManifestError):
        load_manifest(manifest_path)


def test_load_manifest_rejects_unknown_top_level_key(tmp_path):
    manifest_path, _video, _digest = _single_manifest(tmp_path)

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["extra_section"] = {"oops": True}
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(manifest_path)
    assert "extra_section" in str(excinfo.value)


def test_load_manifest_rejects_unknown_candidate_key(tmp_path):
    manifest_path, _video, _digest = _single_manifest(tmp_path)

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["candidates"][0]["backend_hint"] = "rtmpose"
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(manifest_path)
    assert "candidates[0].backend_hint" in str(excinfo.value)


def test_load_manifest_rejects_wrong_schema_version(tmp_path):
    manifest_path, _video, _digest = _single_manifest(tmp_path)

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["schema_version"] = "continuity-spike-input-v9"
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(manifest_path)
    assert "schema_version" in str(excinfo.value)


def test_load_manifest_rejects_source_video_without_motion_tag(tmp_path):
    manifest_path, _video, _digest = _single_manifest(
        tmp_path,
        scenario_tags=["single_subject", "face_visible", "media_edge"],
    )

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(manifest_path)
    assert "candidates[0].scenario_tags" in str(excinfo.value)


def test_load_manifest_rejects_source_video_with_two_face_tags(tmp_path):
    manifest_path, _video, _digest = _single_manifest(
        tmp_path,
        scenario_tags=[
            "single_subject",
            "subtle_motion",
            "face_visible",
            "face_occluded",
        ],
    )

    with pytest.raises(ManifestError):
        load_manifest(manifest_path)


def test_load_manifest_rejects_historical_gif_with_open_window(tmp_path):
    payload, _gif, _digest = _historical_payload(tmp_path)
    payload["window_end_s"] = 2.0
    manifest_path = _write_manifest_file(tmp_path, [payload])

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(manifest_path)
    assert "candidates[0].window_end_s" in str(excinfo.value)


def test_load_manifest_rejects_historical_gif_with_labeled_human_label(tmp_path):
    payload, _gif, _digest = _historical_payload(tmp_path)
    payload["human_label"] = "ACCEPTABLE"
    manifest_path = _write_manifest_file(tmp_path, [payload])

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(manifest_path)
    assert "candidates[0].human_label" in str(excinfo.value)


def test_source_video_candidate_requires_motion_and_face_bucket(tmp_path):
    manifest_path, _video, _digest = _single_manifest(tmp_path)
    manifest = load_manifest(manifest_path)

    tags = set(manifest.candidates[0].scenario_tags)
    motion = {"subtle_motion", "moderate_motion", "large_motion"}
    face = {"face_visible", "face_occluded", "face_not_visible"}
    assert len(tags & motion) == 1
    assert len(tags & face) == 1


def _source_candidate(tmp_path: Path) -> object:
    manifest_path, _video, _digest = _single_manifest(tmp_path)
    return load_manifest(manifest_path).candidates[0]


def _historical_candidate(tmp_path: Path) -> object:
    payload, _gif, _digest = _historical_payload(tmp_path)
    manifest_path = _write_manifest_file(tmp_path, [payload])
    return load_manifest(manifest_path).candidates[0]


def _passing_result(candidate) -> CandidateProbeResult:
    return CandidateProbeResult(
        candidate_id=candidate.candidate_id,
        source_sha256=candidate.source_sha256,
        backend_id="test-backend-v1",
        disposition="AUTO_PASS",
        total_frame_count=20,
        detected_frame_count=20,
        pose_valid_frame_count=20,
        max_evidence_gap_s=0.1,
        subject_set_ambiguous=False,
        tracking_loss_count=0,
        amplitude_status="PASS",
        amplitude_class="subtle",
        normalized_amplitude_peak=0.30,
        normalized_amplitude_noise_floor=0.03,
        amplitude_noise_reference="combined",
        background_coverage=0.55,
        affine_inlier_ratio=0.72,
        camera_compensation_status="AVAILABLE",
        amplitude_phase_complete=True,
        body_identity_status="CONSISTENT",
        face_presence_status="USABLE",
        face_identity_status="CONSISTENT",
        face_visible_frame_count=20,
        face_usable_frame_count=12,
        face_usable_coverage=0.60,
        min_face_short_side_px=96.0,
        mean_face_landmark_confidence=0.90,
        facial_landmark_status="AVAILABLE",
        facial_expression_status="AVAILABLE",
        curve_feature_status="NOT_RUN",
        curve_feature_missing_reason=None,
        source_media_width_px=320,
        source_media_height_px=180,
        primary_subject_area_ratio=0.30,
        face_decode_width_px=320,
        face_evidence_wall_clock_ms=150.0,
        curve_feature_wall_clock_ms=None,
        confirming_independence_groups=(),
        vlm_status="COMPLETED",
        wall_clock_ms=900.0,
        reason_codes=(),
    )


def _mutated(candidate, **changes) -> CandidateProbeResult:
    return dataclasses.replace(_passing_result(candidate), **changes)


def test_validate_probe_result_accepts_consistent_pass(tmp_path):
    candidate = _source_candidate(tmp_path)

    assert validate_probe_result(candidate, _passing_result(candidate)) is None


def test_validate_probe_result_rejects_candidate_id_mismatch(tmp_path):
    candidate = _source_candidate(tmp_path)

    with pytest.raises(ResultValidationError):
        validate_probe_result(
            candidate, _mutated(candidate, candidate_id="other-id")
        )


def test_validate_probe_result_rejects_source_hash_mismatch(tmp_path):
    candidate = _source_candidate(tmp_path)

    with pytest.raises(ResultValidationError):
        validate_probe_result(
            candidate, _mutated(candidate, source_sha256="0" * 64)
        )


def test_validate_probe_result_rejects_negative_amplitude_peak(tmp_path):
    candidate = _source_candidate(tmp_path)

    with pytest.raises(ResultValidationError):
        validate_probe_result(
            candidate, _mutated(candidate, normalized_amplitude_peak=-0.1)
        )


def test_validate_probe_result_rejects_pass_at_or_below_noise_floor(tmp_path):
    candidate = _source_candidate(tmp_path)

    with pytest.raises(ResultValidationError):
        validate_probe_result(
            candidate,
            _mutated(candidate, normalized_amplitude_peak=0.03),
        )


def test_validate_probe_result_rejects_pass_with_unavailable_reference(tmp_path):
    candidate = _source_candidate(tmp_path)

    with pytest.raises(ResultValidationError):
        validate_probe_result(
            candidate,
            _mutated(candidate, amplitude_noise_reference="unavailable"),
        )


def test_validate_probe_result_rejects_pass_with_incomplete_phase(tmp_path):
    candidate = _source_candidate(tmp_path)

    with pytest.raises(ResultValidationError):
        validate_probe_result(
            candidate, _mutated(candidate, amplitude_phase_complete=False)
        )


def test_validate_probe_result_rejects_pass_with_low_confidence_compensation(
    tmp_path,
):
    candidate = _source_candidate(tmp_path)

    with pytest.raises(ResultValidationError):
        validate_probe_result(
            candidate,
            _mutated(candidate, camera_compensation_status="LOW_CONFIDENCE"),
        )


def test_validate_probe_result_requires_low_confidence_below_coverage_floor(
    tmp_path,
):
    candidate = _source_candidate(tmp_path)

    with pytest.raises(ResultValidationError):
        validate_probe_result(
            candidate, _mutated(candidate, background_coverage=0.10)
        )


def test_validate_probe_result_requires_unavailable_amplitude_when_compensation_low(
    tmp_path,
):
    candidate = _source_candidate(tmp_path)
    result = _mutated(
        candidate,
        camera_compensation_status="LOW_CONFIDENCE",
        disposition="REVIEW_REQUIRED",
        amplitude_status="UNAVAILABLE",
        amplitude_class="unobservable",
        normalized_amplitude_peak=None,
        normalized_amplitude_noise_floor=None,
        amplitude_noise_reference="unavailable",
        background_coverage=0.10,
        affine_inlier_ratio=0.80,
        amplitude_phase_complete=None,
    )

    assert validate_probe_result(candidate, result) is None


def test_validate_probe_result_rejects_low_confidence_with_auto_pass(tmp_path):
    candidate = _source_candidate(tmp_path)
    result = _mutated(
        candidate,
        camera_compensation_status="LOW_CONFIDENCE",
        amplitude_status="UNAVAILABLE",
        amplitude_class="unobservable",
        normalized_amplitude_peak=None,
        normalized_amplitude_noise_floor=None,
        amplitude_noise_reference="unavailable",
        background_coverage=0.10,
        affine_inlier_ratio=0.80,
        amplitude_phase_complete=None,
    )

    with pytest.raises(ResultValidationError):
        validate_probe_result(candidate, result)


def test_validate_probe_result_rejects_body_conflict_with_auto_pass(tmp_path):
    candidate = _source_candidate(tmp_path)

    with pytest.raises(ResultValidationError):
        validate_probe_result(
            candidate, _mutated(candidate, body_identity_status="CONFLICT")
        )


def test_validate_probe_result_rejects_face_conflict_with_auto_pass(tmp_path):
    candidate = _source_candidate(tmp_path)

    with pytest.raises(ResultValidationError):
        validate_probe_result(
            candidate, _mutated(candidate, face_identity_status="CONFLICT")
        )


@pytest.mark.parametrize(
    ("changes", "violation"),
    [
        ({"face_usable_frame_count": 2}, "usable frames"),
        ({"min_face_short_side_px": 32.0}, "short side"),
        ({"mean_face_landmark_confidence": 0.50}, "confidence"),
        ({"face_usable_coverage": 0.30}, "coverage"),
    ],
)
def test_validate_probe_result_rejects_unusable_usable_face(
    tmp_path, changes, violation
):
    candidate = _source_candidate(tmp_path)

    with pytest.raises(ResultValidationError) as excinfo:
        validate_probe_result(candidate, _mutated(candidate, **changes))
    assert violation in str(excinfo.value)


def test_validate_probe_result_rejects_identity_status_on_absent_face(tmp_path):
    candidate = _source_candidate(tmp_path)
    result = _mutated(
        candidate,
        face_presence_status="NOT_DETECTED",
        face_identity_status="CONSISTENT",
        face_visible_frame_count=0,
        face_usable_frame_count=0,
        face_usable_coverage=None,
        min_face_short_side_px=None,
        mean_face_landmark_confidence=None,
    )

    with pytest.raises(ResultValidationError):
        validate_probe_result(candidate, result)


def test_validate_probe_result_rejects_dynamics_status_on_unusable_face(tmp_path):
    candidate = _source_candidate(tmp_path)
    result = _mutated(
        candidate,
        face_presence_status="DETECTED_UNUSABLE",
        facial_expression_status="AVAILABLE",
        face_identity_status="NOT_APPLICABLE",
        facial_landmark_status="NOT_APPLICABLE",
        face_visible_frame_count=4,
        face_usable_frame_count=1,
        face_usable_coverage=0.25,
        min_face_short_side_px=48.0,
        mean_face_landmark_confidence=0.45,
    )

    with pytest.raises(ResultValidationError):
        validate_probe_result(candidate, result)


def test_validate_probe_result_allows_advisory_alert_with_auto_pass(tmp_path):
    candidate = _source_candidate(tmp_path)
    result = _mutated(
        candidate,
        facial_expression_status="ABSTAIN",
        facial_landmark_status="TEMPORAL_ALERT",
    )

    assert validate_probe_result(candidate, result) is None


def test_validate_probe_result_accepts_hard_reject_with_scene_boundary_only(
    tmp_path,
):
    candidate = _source_candidate(tmp_path)
    result = _mutated(
        candidate,
        disposition="HARD_REJECT",
        confirming_independence_groups=("scene_boundary",),
        reason_codes=("shot_boundary_conflict",),
    )

    assert validate_probe_result(candidate, result) is None


def test_validate_probe_result_accepts_hard_reject_with_two_groups(tmp_path):
    candidate = _source_candidate(tmp_path)
    result = _mutated(
        candidate,
        disposition="HARD_REJECT",
        confirming_independence_groups=("pose_geometry", "semantic_vlm"),
        reason_codes=("limb_topology_jump", "contact_transition_unexplained"),
    )

    assert validate_probe_result(candidate, result) is None


def test_validate_probe_result_rejects_hard_reject_with_one_non_scene_group(
    tmp_path,
):
    candidate = _source_candidate(tmp_path)
    result = _mutated(
        candidate,
        disposition="HARD_REJECT",
        confirming_independence_groups=("pose_geometry",),
    )

    with pytest.raises(ResultValidationError):
        validate_probe_result(candidate, result)


def test_validate_probe_result_rejects_duplicate_confirming_groups(tmp_path):
    candidate = _source_candidate(tmp_path)
    result = _mutated(
        candidate,
        disposition="HARD_REJECT",
        confirming_independence_groups=(
            "appearance_identity",
            "appearance_identity",
        ),
    )

    with pytest.raises(ResultValidationError):
        validate_probe_result(candidate, result)


def test_validate_probe_result_rejects_confirming_groups_without_hard_reject(
    tmp_path,
):
    candidate = _source_candidate(tmp_path)

    with pytest.raises(ResultValidationError):
        validate_probe_result(
            candidate,
            _mutated(
                candidate,
                disposition="REVIEW_REQUIRED",
                confirming_independence_groups=("appearance_identity",),
            ),
        )


def test_validate_probe_result_requires_gif_only_disposition_for_historical_gif(
    tmp_path,
):
    candidate = _historical_candidate(tmp_path)
    result = _passing_result(candidate)

    with pytest.raises(ResultValidationError):
        validate_probe_result(candidate, result)


def test_validate_probe_result_requires_not_applicable_amplitude_for_historical(
    tmp_path,
):
    candidate = _historical_candidate(tmp_path)
    result = _mutated(
        _passing_result(candidate),
        disposition="GIF_ONLY_PASS",
        amplitude_status="PASS",
    )

    with pytest.raises(ResultValidationError):
        validate_probe_result(candidate, result)


def test_validate_probe_result_accepts_valid_historical_gif_result(tmp_path):
    candidate = _historical_candidate(tmp_path)
    result = _mutated(
        _passing_result(candidate),
        disposition="GIF_ONLY_PASS",
        amplitude_status="NOT_APPLICABLE",
        amplitude_class="unobservable",
        normalized_amplitude_peak=None,
        normalized_amplitude_noise_floor=None,
        amplitude_noise_reference="unavailable",
        background_coverage=None,
        affine_inlier_ratio=None,
        camera_compensation_status="NOT_APPLICABLE",
        amplitude_phase_complete=None,
        curve_feature_status="AVAILABLE",
        curve_feature_wall_clock_ms=800.0,
        primary_subject_area_ratio=0.50,
        face_evidence_wall_clock_ms=None,
        vlm_status="SKIPPED",
    )

    assert validate_probe_result(candidate, result) is None


def test_validate_probe_result_rejects_gif_only_disposition_for_source_video(
    tmp_path,
):
    candidate = _source_candidate(tmp_path)

    with pytest.raises(ResultValidationError):
        validate_probe_result(
            candidate,
            _mutated(_passing_result(candidate), disposition="GIF_ONLY_PASS"),
        )


def test_validate_probe_result_rejects_non_positive_dimensions(tmp_path):
    candidate = _source_candidate(tmp_path)

    with pytest.raises(ResultValidationError):
        validate_probe_result(
            candidate, _mutated(candidate, source_media_width_px=0)
        )


def test_validate_probe_result_rejects_non_positive_wall_clock(tmp_path):
    candidate = _source_candidate(tmp_path)

    with pytest.raises(ResultValidationError):
        validate_probe_result(candidate, _mutated(candidate, wall_clock_ms=0.0))


def test_validate_probe_result_rejects_out_of_range_subject_ratio(tmp_path):
    candidate = _source_candidate(tmp_path)

    with pytest.raises(ResultValidationError):
        validate_probe_result(
            candidate, _mutated(candidate, primary_subject_area_ratio=1.5)
        )


def test_validate_probe_result_requires_dimensions_for_available_curve_features(
    tmp_path,
):
    candidate = _source_candidate(tmp_path)

    with pytest.raises(ResultValidationError):
        validate_probe_result(
            candidate,
            _mutated(
                candidate,
                curve_feature_status="AVAILABLE",
                curve_feature_wall_clock_ms=700.0,
                source_media_width_px=None,
            ),
        )


def test_validate_probe_result_requires_stable_reason_codes(tmp_path):
    candidate = _source_candidate(tmp_path)

    with pytest.raises(ResultValidationError):
        validate_probe_result(
            candidate,
            _mutated(
                candidate,
                disposition="REVIEW_REQUIRED",
                reason_codes=("Not-Stable",),
                vlm_status="ABSTAIN",
            ),
        )


def test_make_error_result_preserves_candidate_identity(tmp_path):
    candidate = _source_candidate(tmp_path)

    result = make_error_result(
        candidate,
        backend_id="test-backend-v1",
        reason_code="decode_failed",
        wall_clock_ms=12.5,
    )

    assert result.candidate_id == candidate.candidate_id
    assert result.source_sha256 == candidate.source_sha256
    assert result.backend_id == "test-backend-v1"
    assert result.disposition == "ERROR"
    assert result.amplitude_status == "UNAVAILABLE"
    assert result.camera_compensation_status == "NOT_APPLICABLE"
    assert result.body_identity_status == "NOT_APPLICABLE"
    assert result.face_identity_status == "NOT_APPLICABLE"
    assert result.facial_landmark_status == "NOT_APPLICABLE"
    assert result.facial_expression_status == "NOT_APPLICABLE"
    assert result.curve_feature_status == "NOT_RUN"
    assert result.vlm_status == "ERROR"
    assert result.total_frame_count == 0
    assert result.detected_frame_count == 0
    assert result.pose_valid_frame_count == 0
    assert result.tracking_loss_count == 0
    assert result.confirming_independence_groups == ()
    assert result.reason_codes == ("decode_failed",)
    assert result.wall_clock_ms == 12.5

    assert validate_probe_result(candidate, result) is None


def test_make_error_result_keeps_only_supplied_reason_code(tmp_path):
    candidate = _source_candidate(tmp_path)

    result = make_error_result(
        candidate,
        backend_id="b",
        reason_code="model_unavailable",
        wall_clock_ms=1.0,
    )

    assert len(result.reason_codes) == 1

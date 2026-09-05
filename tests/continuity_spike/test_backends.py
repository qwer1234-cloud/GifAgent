"""Tests for the fake backend and the explicit plugin backend loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.continuity_spike_backends import (
    BackendLoadError,
    DeterministicFakeBackend,
    load_backend,
)
from scripts.continuity_spike_contracts import (
    SpikeCandidate,
    load_manifest,
    validate_probe_result,
)

from tests.continuity_spike.test_contracts import (
    _historical_payload,
    _single_manifest,
    _write_manifest_file,
)


def _load_candidates(tmp_path: Path, payloads: list[dict]) -> tuple[SpikeCandidate, ...]:
    manifest_path = _write_manifest_file(tmp_path, payloads)
    return load_manifest(manifest_path).candidates


def _source_payloads(tmp_path: Path, count: int) -> tuple[list[dict], list[Path]]:
    payloads: list[dict] = []
    videos: list[Path] = []
    motion_tags = ["subtle_motion", "moderate_motion", "large_motion"]
    face_tags = ["face_visible", "face_occluded", "face_not_visible"]
    for index in range(count):
        video = tmp_path / f"source-{index}.mp4"
        video.write_bytes(f"video-bytes-{index}".encode("utf-8"))
        import hashlib

        digest = hashlib.sha256(video.read_bytes()).hexdigest()
        payload = {
            "candidate_id": f"video-{index:03d}",
            "media_kind": "source_video",
            "source_path": str(video.resolve()),
            "source_sha256": digest,
            "media_duration_s": 8.0,
            "confirmed_shot_start_s": 0.0,
            "context_start_s": 0.0,
            "window_start_s": 1.0,
            "window_end_s": 4.0,
            "context_end_s": 6.0,
            "confirmed_shot_end_s": 7.0,
            "scenario_tags": [
                "single_subject",
                motion_tags[index % 3],
                face_tags[index % 3],
            ],
            "data_origin": "synthetic",
            "human_label": "UNLABELED",
            "feedback_label": "unknown",
        }
        payloads.append(payload)
        videos.append(video)
    return payloads, videos


def test_fake_backend_is_always_marked_synthetic(tmp_path):
    payloads, _videos = _source_payloads(tmp_path, 1)
    candidate = _load_candidates(tmp_path, payloads)[0]

    backend = load_backend("fake")
    result = backend.evaluate_source((candidate,))[0]

    assert backend.synthetic is True
    assert result.backend_id == "deterministic-fake-v1"
    assert result.candidate_id == candidate.candidate_id
    assert result.source_sha256 == candidate.source_sha256
    assert result.amplitude_class == "subtle"
    assert result.face_presence_status == "USABLE"
    assert result.face_identity_status == "CONSISTENT"
    assert result.facial_expression_status == "AVAILABLE"


def test_fake_backend_satisfies_probe_contract_for_all_variants(tmp_path):
    payloads, _videos = _source_payloads(tmp_path, 3)
    hist_payload, _gif, _digest = _historical_payload(tmp_path)
    payloads.append(hist_payload)
    candidates = _load_candidates(tmp_path, payloads)

    backend = load_backend("fake")
    results = backend.evaluate_source(candidates)

    assert len(results) == len(candidates)
    for candidate, result in zip(candidates, results):
        validate_probe_result(candidate, result)


def test_fake_backend_scales_amplitude_with_motion_tag(tmp_path):
    payloads, _videos = _source_payloads(tmp_path, 3)
    candidates = _load_candidates(tmp_path, payloads)

    results = DeterministicFakeBackend().evaluate_source(candidates)

    by_id = {result.candidate_id: result for result in results}
    assert by_id["video-000"].amplitude_class == "subtle"
    assert by_id["video-000"].normalized_amplitude_peak == 0.15
    assert by_id["video-001"].amplitude_class == "moderate"
    assert by_id["video-001"].normalized_amplitude_peak == 0.45
    assert by_id["video-002"].amplitude_class == "large"
    assert by_id["video-002"].normalized_amplitude_peak == 0.90
    for result in results:
        assert result.normalized_amplitude_noise_floor == 0.03
        assert result.amplitude_noise_reference == "combined"
        assert result.background_coverage == 0.60
        assert result.affine_inlier_ratio == 0.80
        assert result.camera_compensation_status == "AVAILABLE"
        assert result.amplitude_phase_complete is True


def test_fake_backend_reports_face_variants(tmp_path):
    payloads, _videos = _source_payloads(tmp_path, 3)
    candidates = _load_candidates(tmp_path, payloads)

    results = DeterministicFakeBackend().evaluate_source(candidates)

    by_id = {result.candidate_id: result for result in results}
    visible = by_id["video-000"]
    assert visible.face_presence_status == "USABLE"
    assert visible.face_usable_frame_count == 6
    assert visible.face_visible_frame_count == 8
    assert visible.min_face_short_side_px == 96.0
    assert visible.mean_face_landmark_confidence == 0.90
    assert visible.facial_landmark_status == "AVAILABLE"

    occluded = by_id["video-001"]
    assert occluded.face_presence_status == "DETECTED_UNUSABLE"
    assert occluded.face_usable_frame_count == 1
    assert occluded.face_visible_frame_count == 4
    assert occluded.min_face_short_side_px == 48.0
    assert occluded.mean_face_landmark_confidence == 0.45
    assert occluded.face_identity_status == "NOT_APPLICABLE"
    assert occluded.facial_expression_status == "NOT_APPLICABLE"

    absent = by_id["video-002"]
    assert absent.face_presence_status == "NOT_DETECTED"
    assert absent.face_visible_frame_count == 0
    assert absent.face_usable_frame_count == 0
    assert absent.face_identity_status == "NOT_APPLICABLE"


def test_fake_backend_reports_historical_gif_yield_fields(tmp_path):
    hist_payload, _gif, _digest = _historical_payload(tmp_path)
    candidate = _load_candidates(tmp_path, [hist_payload])[0]

    result = DeterministicFakeBackend().evaluate_source((candidate,))[0]

    assert result.disposition == "GIF_ONLY_PASS"
    assert result.amplitude_status == "NOT_APPLICABLE"
    assert result.camera_compensation_status == "NOT_APPLICABLE"
    assert result.curve_feature_status == "AVAILABLE"
    assert result.curve_feature_wall_clock_ms is not None
    assert result.primary_subject_area_ratio == 0.50
    assert result.source_media_width_px is not None
    assert result.source_media_height_px is not None


def test_fake_backend_reports_common_video_evidence(tmp_path):
    payloads, _videos = _source_payloads(tmp_path, 1)
    candidate = _load_candidates(tmp_path, payloads)[0]

    result = DeterministicFakeBackend().evaluate_source((candidate,))[0]

    assert result.disposition == "AUTO_PASS"
    assert result.total_frame_count == 8
    assert result.detected_frame_count == 8
    assert result.pose_valid_frame_count == 8
    assert result.max_evidence_gap_s == 0.125
    assert result.tracking_loss_count == 0
    assert result.subject_set_ambiguous is False
    assert result.body_identity_status == "CONSISTENT"
    assert result.vlm_status == "SKIPPED"
    assert result.confirming_independence_groups == ()
    assert result.curve_feature_status == "NOT_RUN"


def test_plugin_loader_accepts_module_factory(tmp_path, monkeypatch):
    plugin = tmp_path / "spike_plugin_under_test.py"
    plugin.write_text(
        "class PluginBackend:\n"
        "    backend_id = 'plugin-test-v1'\n"
        "    synthetic = True\n"
        "\n"
        "    def evaluate_source(self, candidates):\n"
        "        return ()\n"
        "\n"
        "\n"
        "def create_backend():\n"
        "    return PluginBackend()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    backend = load_backend("spike_plugin_under_test:create_backend")

    assert backend.backend_id == "plugin-test-v1"
    assert backend.synthetic is True


def test_plugin_loader_rejects_missing_module():
    with pytest.raises(BackendLoadError):
        load_backend("definitely_not_a_real_module:create_backend")


def test_plugin_loader_rejects_non_callable_attribute(monkeypatch):
    import scripts.continuity_spike_backends as backends_module

    monkeypatch.setattr(
        backends_module, "SPIKE_TEST_NON_CALLABLE", "not-callable", raising=False
    )

    with pytest.raises(BackendLoadError):
        load_backend("scripts.continuity_spike_backends:SPIKE_TEST_NON_CALLABLE")


def test_plugin_loader_rejects_object_without_protocol_surface(tmp_path, monkeypatch):
    plugin = tmp_path / "spike_plugin_incomplete.py"
    plugin.write_text(
        "class NotABackend:\n"
        "    pass\n"
        "\n"
        "\n"
        "def create_backend():\n"
        "    return NotABackend()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(BackendLoadError):
        load_backend("spike_plugin_incomplete:create_backend")


@pytest.mark.parametrize(
    "spec",
    [
        "fake:extra",
        "no_separator_at_all",
        "",
    ],
)
def test_plugin_loader_rejects_malformed_specs(spec):
    with pytest.raises(BackendLoadError):
        load_backend(spec)


def test_plugin_loader_rejects_empty_backend_id(tmp_path, monkeypatch):
    plugin = tmp_path / "spike_plugin_empty_id.py"
    plugin.write_text(
        "class EmptyIdBackend:\n"
        "    backend_id = ''\n"
        "    synthetic = False\n"
        "\n"
        "    def evaluate_source(self, candidates):\n"
        "        return ()\n"
        "\n"
        "\n"
        "def create_backend():\n"
        "    return EmptyIdBackend()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(BackendLoadError):
        load_backend("spike_plugin_empty_id:create_backend")


def test_plugin_loader_rejects_non_boolean_synthetic(tmp_path, monkeypatch):
    plugin = tmp_path / "spike_plugin_bad_flag.py"
    plugin.write_text(
        "class BadFlagBackend:\n"
        "    backend_id = 'bad-flag'\n"
        "    synthetic = 1\n"
        "\n"
        "    def evaluate_source(self, candidates):\n"
        "        return ()\n"
        "\n"
        "\n"
        "def create_backend():\n"
        "    return BadFlagBackend()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(BackendLoadError):
        load_backend("spike_plugin_bad_flag:create_backend")

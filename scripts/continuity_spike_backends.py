"""Deterministic fake backend and explicit plugin loader for the spike.

`load_backend("fake")` returns the built-in deterministic backend whose
``synthetic=True`` flag keeps every fake run statistically ineligible. Any
other spec must be ``module:function`` naming a zero-argument factory that
returns an object satisfying ``ContinuitySpikeBackend``.
"""

from __future__ import annotations

import importlib
from typing import cast

from scripts.continuity_spike_contracts import (
    AmplitudeClass,
    AmplitudeStatus,
    BackendLoadError,
    CandidateProbeResult,
    ContinuitySpikeBackend,
    Disposition,
    FacePresenceStatus,
    FacialDynamicsStatus,
    IdentityEvidenceStatus,
    SpikeCandidate,
    VlmStatus,
)

FAKE_BACKEND_ID = "deterministic-fake-v1"

_MOTION_AMPLITUDE: dict[str, tuple[AmplitudeClass, float]] = {
    "subtle_motion": ("subtle", 0.15),
    "moderate_motion": ("moderate", 0.45),
    "large_motion": ("large", 0.90),
}
_FAKE_NOISE_FLOOR = 0.03


class DeterministicFakeBackend:
    """Synthetic backend that exercises the report contract deterministically."""

    backend_id: str = FAKE_BACKEND_ID
    synthetic: bool = True

    def evaluate_source(
        self,
        candidates: tuple[SpikeCandidate, ...],
    ) -> tuple[CandidateProbeResult, ...]:
        return tuple(self._evaluate_one(candidate) for candidate in candidates)

    def _evaluate_one(self, candidate: SpikeCandidate) -> CandidateProbeResult:
        if candidate.media_kind == "historical_gif":
            return self._historical_result(candidate)
        return self._video_result(candidate)

    def _video_result(self, candidate: SpikeCandidate) -> CandidateProbeResult:
        amplitude_class: AmplitudeClass = "subtle"
        peak: float = 0.15
        for tag in ("subtle_motion", "moderate_motion", "large_motion"):
            if tag in candidate.scenario_tags:
                amplitude_class, peak = _MOTION_AMPLITUDE[tag]
                break

        face_presence: FacePresenceStatus
        face_identity: IdentityEvidenceStatus
        landmark_status: FacialDynamicsStatus
        expression_status: FacialDynamicsStatus
        face_visible = 0
        face_usable = 0
        face_coverage: float | None = None
        face_short_side: float | None = None
        landmark_confidence: float | None = None
        face_cost_ms: float | None = None
        face_decode_width: int | None = None
        if "face_visible" in candidate.scenario_tags:
            face_presence = "USABLE"
            face_identity = "CONSISTENT"
            landmark_status = "AVAILABLE"
            expression_status = "AVAILABLE"
            face_visible = 8
            face_usable = 6
            face_coverage = 0.75
            face_short_side = 96.0
            landmark_confidence = 0.90
            face_cost_ms = 120.0
            face_decode_width = 320
        elif "face_occluded" in candidate.scenario_tags:
            face_presence = "DETECTED_UNUSABLE"
            face_identity = "NOT_APPLICABLE"
            landmark_status = "NOT_APPLICABLE"
            expression_status = "NOT_APPLICABLE"
            face_visible = 4
            face_usable = 1
            face_coverage = 0.25
            face_short_side = 48.0
            landmark_confidence = 0.45
            face_cost_ms = 90.0
            face_decode_width = 320
        else:
            face_presence = "NOT_DETECTED"
            face_identity = "NOT_APPLICABLE"
            landmark_status = "NOT_APPLICABLE"
            expression_status = "NOT_APPLICABLE"

        amplitude_status: AmplitudeStatus = "PASS"
        disposition: Disposition = "AUTO_PASS"
        vlm_status: VlmStatus = "SKIPPED"

        return CandidateProbeResult(
            candidate_id=candidate.candidate_id,
            source_sha256=candidate.source_sha256,
            backend_id=self.backend_id,
            disposition=disposition,
            total_frame_count=8,
            detected_frame_count=8,
            pose_valid_frame_count=8,
            max_evidence_gap_s=0.125,
            subject_set_ambiguous=False,
            tracking_loss_count=0,
            amplitude_status=amplitude_status,
            amplitude_class=amplitude_class,
            normalized_amplitude_peak=peak,
            normalized_amplitude_noise_floor=_FAKE_NOISE_FLOOR,
            amplitude_noise_reference="combined",
            background_coverage=0.60,
            affine_inlier_ratio=0.80,
            camera_compensation_status="AVAILABLE",
            amplitude_phase_complete=True,
            body_identity_status="CONSISTENT",
            face_presence_status=face_presence,
            face_identity_status=face_identity,
            face_visible_frame_count=face_visible,
            face_usable_frame_count=face_usable,
            face_usable_coverage=face_coverage,
            min_face_short_side_px=face_short_side,
            mean_face_landmark_confidence=landmark_confidence,
            facial_landmark_status=landmark_status,
            facial_expression_status=expression_status,
            curve_feature_status="NOT_RUN",
            curve_feature_missing_reason=None,
            source_media_width_px=320,
            source_media_height_px=180,
            primary_subject_area_ratio=None,
            face_decode_width_px=face_decode_width,
            face_evidence_wall_clock_ms=face_cost_ms,
            curve_feature_wall_clock_ms=None,
            confirming_independence_groups=(),
            vlm_status=vlm_status,
            wall_clock_ms=250.0,
            reason_codes=(),
        )

    def _historical_result(self, candidate: SpikeCandidate) -> CandidateProbeResult:
        return CandidateProbeResult(
            candidate_id=candidate.candidate_id,
            source_sha256=candidate.source_sha256,
            backend_id=self.backend_id,
            disposition="GIF_ONLY_PASS",
            total_frame_count=8,
            detected_frame_count=8,
            pose_valid_frame_count=8,
            max_evidence_gap_s=0.125,
            subject_set_ambiguous=False,
            tracking_loss_count=0,
            amplitude_status="NOT_APPLICABLE",
            amplitude_class="unobservable",
            normalized_amplitude_peak=None,
            normalized_amplitude_noise_floor=None,
            amplitude_noise_reference="unavailable",
            background_coverage=None,
            affine_inlier_ratio=None,
            camera_compensation_status="NOT_APPLICABLE",
            amplitude_phase_complete=None,
            body_identity_status="CONSISTENT",
            face_presence_status="USABLE",
            face_identity_status="NOT_APPLICABLE",
            face_visible_frame_count=8,
            face_usable_frame_count=6,
            face_usable_coverage=0.75,
            min_face_short_side_px=96.0,
            mean_face_landmark_confidence=0.90,
            facial_landmark_status="AVAILABLE",
            facial_expression_status="AVAILABLE",
            curve_feature_status="AVAILABLE",
            curve_feature_missing_reason=None,
            source_media_width_px=480,
            source_media_height_px=270,
            primary_subject_area_ratio=0.50,
            face_decode_width_px=None,
            face_evidence_wall_clock_ms=None,
            curve_feature_wall_clock_ms=850.0,
            confirming_independence_groups=(),
            vlm_status="SKIPPED",
            wall_clock_ms=900.0,
            reason_codes=(),
        )


def _validate_backend(backend: object) -> ContinuitySpikeBackend:
    backend_id = getattr(backend, "backend_id", None)
    synthetic = getattr(backend, "synthetic", None)
    evaluate = getattr(backend, "evaluate_source", None)
    if not isinstance(backend_id, str) or not backend_id:
        raise BackendLoadError(
            "backend must expose a non-empty string backend_id"
        )
    if not isinstance(synthetic, bool):
        raise BackendLoadError("backend must expose a boolean synthetic flag")
    if not callable(evaluate):
        raise BackendLoadError(
            "backend must expose a callable evaluate_source(candidates) method"
        )
    return cast(ContinuitySpikeBackend, backend)


def load_backend(spec: str) -> ContinuitySpikeBackend:
    """Resolve a backend spec to a validated backend object."""

    if spec == "fake":
        return DeterministicFakeBackend()

    if spec.count(":") != 1:
        raise BackendLoadError(
            "backend spec must be 'fake' or 'module:function' with exactly "
            f"one colon, got {spec!r}"
        )
    module_name, _, attribute = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise BackendLoadError(
            f"backend module {module_name!r} could not be imported: {exc}"
        ) from exc
    factory = getattr(module, attribute, None)
    if factory is None or not callable(factory):
        raise BackendLoadError(
            f"attribute {attribute!r} of module {module_name!r} is not callable"
        )
    try:
        backend = factory()
    except Exception as exc:
        raise BackendLoadError(
            f"backend factory {spec!r} raised: {exc}"
        ) from exc
    return _validate_backend(backend)

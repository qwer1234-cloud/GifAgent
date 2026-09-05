"""Immutable contracts for the continuity feasibility spike (milestone 0A).

The spike is a read-only, model-neutral harness. These types freeze the
manifest input shape, the per-candidate probe result shape, and the backend
protocol shared by the deterministic fake and future experimental backends.
Production pipeline code must not import from this module.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

SCHEMA_VERSION = "continuity-spike-input-v1"

DataOrigin = Literal["real", "synthetic"]
MediaKind = Literal["source_video", "historical_gif"]
HumanLabel = Literal["ACCEPTABLE", "INVALID", "UNLABELED"]
FeedbackLabel = Literal["favorite", "like", "dislike", "neutral", "unknown"]
Disposition = Literal[
    "AUTO_PASS",
    "REVIEW_REQUIRED",
    "HARD_REJECT",
    "GIF_ONLY_PASS",
    "GIF_ONLY_REVIEW",
    "GIF_ONLY_REJECT",
    "ERROR",
]
VlmStatus = Literal["COMPLETED", "ABSTAIN", "SKIPPED", "ERROR"]
AmplitudeStatus = Literal["PASS", "REVIEW", "UNAVAILABLE", "NOT_APPLICABLE"]
AmplitudeClass = Literal["unobservable", "subtle", "moderate", "large"]
NoiseReferenceSource = Literal[
    "static_context", "background_residual", "combined", "unavailable"
]
CameraCompensationStatus = Literal["AVAILABLE", "LOW_CONFIDENCE", "NOT_APPLICABLE"]
FacePresenceStatus = Literal["NOT_DETECTED", "DETECTED_UNUSABLE", "USABLE"]
IdentityEvidenceStatus = Literal[
    "NOT_APPLICABLE", "CONSISTENT", "CONFLICT", "UNAVAILABLE"
]
FacialDynamicsStatus = Literal[
    "NOT_APPLICABLE", "AVAILABLE", "TEMPORAL_ALERT", "ABSTAIN", "UNAVAILABLE"
]
CurveFeatureStatus = Literal["AVAILABLE", "UNAVAILABLE", "NOT_RUN"]
IndependenceGroup = Literal[
    "scene_boundary",
    "pose_geometry",
    "appearance_identity",
    "photometric_motion",
    "semantic_vlm",
]

MIN_BACKGROUND_COVERAGE = 0.30
MIN_AFFINE_INLIER_RATIO = 0.50
MIN_FACE_USABLE_FRAMES = 3
MIN_FACE_SHORT_SIDE_PX = 64.0
MIN_FACE_LANDMARK_CONFIDENCE = 0.70
MIN_FACE_USABLE_COVERAGE = 0.60

_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_LOWERCASE_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_CANDIDATE_FIELDS = frozenset(
    {
        "candidate_id",
        "media_kind",
        "source_path",
        "source_sha256",
        "media_duration_s",
        "confirmed_shot_start_s",
        "context_start_s",
        "window_start_s",
        "window_end_s",
        "context_end_s",
        "confirmed_shot_end_s",
        "scenario_tags",
        "data_origin",
        "human_label",
        "feedback_label",
    }
)


class ContinuitySpikeError(Exception):
    """Base class for continuity spike contract failures."""


class ManifestError(ContinuitySpikeError):
    """A spike manifest is structurally invalid."""


class SourceIdentityError(ContinuitySpikeError):
    """A spike source file could not be verified against its declared hash."""


class SourceUnavailableError(SourceIdentityError):
    """The declared source file is missing or unreadable."""


class SourceHashMismatchError(SourceIdentityError):
    """The source file bytes no longer match the declared SHA-256."""


class ResultValidationError(ContinuitySpikeError):
    """A backend probe result violates the cross-field result contract."""


class BackendLoadError(ContinuitySpikeError):
    """A backend spec could not be resolved to a valid backend object."""


@dataclass(frozen=True)
class SpikeCandidate:
    candidate_id: str
    media_kind: MediaKind
    source_path: Path
    source_sha256: str
    media_duration_s: float
    confirmed_shot_start_s: float
    context_start_s: float
    window_start_s: float
    window_end_s: float
    context_end_s: float
    confirmed_shot_end_s: float
    scenario_tags: tuple[str, ...]
    data_origin: DataOrigin
    human_label: HumanLabel
    feedback_label: FeedbackLabel

    @property
    def duration_s(self) -> float:
        return self.window_end_s - self.window_start_s


@dataclass(frozen=True)
class SpikeManifest:
    schema_version: str
    candidates: tuple[SpikeCandidate, ...]


@dataclass(frozen=True)
class CandidateProbeResult:
    candidate_id: str
    source_sha256: str
    backend_id: str
    disposition: Disposition
    total_frame_count: int
    detected_frame_count: int
    pose_valid_frame_count: int
    max_evidence_gap_s: float | None
    subject_set_ambiguous: bool
    tracking_loss_count: int
    amplitude_status: AmplitudeStatus
    amplitude_class: AmplitudeClass
    normalized_amplitude_peak: float | None
    normalized_amplitude_noise_floor: float | None
    amplitude_noise_reference: NoiseReferenceSource
    background_coverage: float | None
    affine_inlier_ratio: float | None
    camera_compensation_status: CameraCompensationStatus
    amplitude_phase_complete: bool | None
    body_identity_status: IdentityEvidenceStatus
    face_presence_status: FacePresenceStatus
    face_identity_status: IdentityEvidenceStatus
    face_visible_frame_count: int
    face_usable_frame_count: int
    face_usable_coverage: float | None
    min_face_short_side_px: float | None
    mean_face_landmark_confidence: float | None
    facial_landmark_status: FacialDynamicsStatus
    facial_expression_status: FacialDynamicsStatus
    curve_feature_status: CurveFeatureStatus
    curve_feature_missing_reason: str | None
    source_media_width_px: int | None
    source_media_height_px: int | None
    primary_subject_area_ratio: float | None
    face_decode_width_px: int | None
    face_evidence_wall_clock_ms: float | None
    curve_feature_wall_clock_ms: float | None
    confirming_independence_groups: tuple[IndependenceGroup, ...]
    vlm_status: VlmStatus
    wall_clock_ms: float
    reason_codes: tuple[str, ...]


class ContinuitySpikeBackend(Protocol):
    backend_id: str
    synthetic: bool

    def evaluate_source(
        self,
        candidates: tuple[SpikeCandidate, ...],
    ) -> tuple[CandidateProbeResult, ...]:
        raise NotImplementedError


def _fail(path: str, message: str) -> ManifestError:
    return ManifestError(f"{path}: {message}")


def _number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(path, "expected a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise _fail(path, "expected a finite number")
    return number


def _literal(value: object, path: str, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _fail(path, f"expected one of {sorted(allowed)}")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise SourceUnavailableError(f"{path}: source file unavailable ({exc})") from exc
    return digest.hexdigest()


def _parse_candidate(raw: object, index: int) -> SpikeCandidate:
    prefix = f"candidates[{index}]"
    if not isinstance(raw, dict):
        raise _fail(prefix, "expected a JSON object")
    for key in sorted(set(raw) - _CANDIDATE_FIELDS):
        raise _fail(f"{prefix}.{key}", "unknown candidate field")
    for key in sorted(_CANDIDATE_FIELDS - set(raw)):
        raise _fail(f"{prefix}.{key}", "missing required candidate field")

    candidate_id = raw["candidate_id"]
    if not isinstance(candidate_id, str) or not candidate_id:
        raise _fail(f"{prefix}.candidate_id", "expected a non-empty string")

    media_kind = cast(
        "MediaKind",
        _literal(
            raw["media_kind"],
            f"{prefix}.media_kind",
            ("source_video", "historical_gif"),
        ),
    )

    raw_path = raw["source_path"]
    if not isinstance(raw_path, str) or not raw_path:
        raise _fail(f"{prefix}.source_path", "expected a non-empty path string")
    source_path = Path(raw_path)
    if not source_path.is_absolute():
        raise _fail(f"{prefix}.source_path", "source path must be absolute")
    if not source_path.is_file():
        raise _fail(f"{prefix}.source_path", "source path must be an existing file")

    source_sha256 = raw["source_sha256"]
    if not isinstance(source_sha256, str) or not _LOWERCASE_SHA256.fullmatch(
        source_sha256
    ):
        raise _fail(
            f"{prefix}.source_sha256",
            "expected 64 lowercase hexadecimal characters",
        )

    media_duration_s = _number(raw["media_duration_s"], f"{prefix}.media_duration_s")
    if media_duration_s <= 0:
        raise _fail(f"{prefix}.media_duration_s", "media duration must be positive")

    bounds = {}
    for field in (
        "confirmed_shot_start_s",
        "context_start_s",
        "window_start_s",
        "window_end_s",
        "context_end_s",
        "confirmed_shot_end_s",
    ):
        bounds[field] = _number(raw[field], f"{prefix}.{field}")

    if media_kind == "source_video":
        chain = (
            0.0,
            bounds["confirmed_shot_start_s"],
            bounds["context_start_s"],
            bounds["window_start_s"],
            bounds["window_end_s"],
            bounds["context_end_s"],
            bounds["confirmed_shot_end_s"],
            media_duration_s,
        )
        for earlier, later, earlier_name, later_name in zip(
            chain,
            chain[1:],
            (
                "zero",
                "confirmed_shot_start_s",
                "context_start_s",
                "window_start_s",
                "window_end_s",
                "context_end_s",
                "confirmed_shot_end_s",
            ),
            (
                "confirmed_shot_start_s",
                "context_start_s",
                "window_start_s",
                "window_end_s",
                "context_end_s",
                "confirmed_shot_end_s",
                "media_duration_s",
            ),
        ):
            strict = later_name == "window_end_s"
            if (strict and not earlier < later) or (not strict and earlier > later):
                relation = "<" if strict else "<="
                raise _fail(
                    f"{prefix}.{later_name}",
                    f"expected {earlier_name} {relation} {later_name}",
                )

    if media_kind == "historical_gif":
        for field in ("confirmed_shot_start_s", "context_start_s", "window_start_s"):
            if bounds[field] != 0.0:
                raise _fail(f"{prefix}.{field}", "historical GIF bounds must start at 0")
        for field in ("window_end_s", "context_end_s", "confirmed_shot_end_s"):
            if bounds[field] != media_duration_s:
                raise _fail(
                    f"{prefix}.{field}",
                    "historical GIF bounds must end at media_duration_s",
                )

    raw_tags = raw["scenario_tags"]
    if not isinstance(raw_tags, list) or not raw_tags:
        raise _fail(f"{prefix}.scenario_tags", "expected a non-empty list of tags")
    tags: list[str] = []
    for tag_index, tag in enumerate(raw_tags):
        tag_path = f"{prefix}.scenario_tags[{tag_index}]"
        if not isinstance(tag, str) or not _SNAKE_CASE.fullmatch(tag):
            raise _fail(tag_path, "expected a snake_case tag")
        if tag in tags:
            raise _fail(tag_path, "duplicate scenario tag")
        tags.append(tag)
    tag_set = frozenset(tags)

    if media_kind == "source_video":
        motion = tag_set & {"subtle_motion", "moderate_motion", "large_motion"}
        if len(motion) != 1:
            raise _fail(
                f"{prefix}.scenario_tags",
                "source video needs exactly one of subtle/moderate/large_motion",
            )
        face = tag_set & {"face_visible", "face_occluded", "face_not_visible"}
        if len(face) != 1:
            raise _fail(
                f"{prefix}.scenario_tags",
                "source video needs exactly one of face_visible/occluded/not_visible",
            )

    data_origin = cast(
        "DataOrigin",
        _literal(raw["data_origin"], f"{prefix}.data_origin", ("real", "synthetic")),
    )
    human_label = cast(
        "HumanLabel",
        _literal(
            raw["human_label"],
            f"{prefix}.human_label",
            ("ACCEPTABLE", "INVALID", "UNLABELED"),
        ),
    )
    feedback_label = cast(
        "FeedbackLabel",
        _literal(
            raw["feedback_label"],
            f"{prefix}.feedback_label",
            ("favorite", "like", "dislike", "neutral", "unknown"),
        ),
    )
    if media_kind == "historical_gif" and human_label != "UNLABELED":
        raise _fail(
            f"{prefix}.human_label", "historical GIF must use human_label=UNLABELED"
        )

    return SpikeCandidate(
        candidate_id=candidate_id,
        media_kind=media_kind,
        source_path=source_path,
        source_sha256=source_sha256,
        media_duration_s=media_duration_s,
        confirmed_shot_start_s=bounds["confirmed_shot_start_s"],
        context_start_s=bounds["context_start_s"],
        window_start_s=bounds["window_start_s"],
        window_end_s=bounds["window_end_s"],
        context_end_s=bounds["context_end_s"],
        confirmed_shot_end_s=bounds["confirmed_shot_end_s"],
        scenario_tags=tuple(tags),
        data_origin=data_origin,
        human_label=human_label,
        feedback_label=feedback_label,
    )


def load_manifest(path: Path) -> SpikeManifest:
    """Load and strictly validate a spike input manifest."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"{path}: manifest unreadable ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path}: invalid JSON ({exc})") from exc

    if not isinstance(payload, dict):
        raise ManifestError(f"{path}: top level must be a JSON object")
    for key in sorted(set(payload) - {"schema_version", "candidates"}):
        raise ManifestError(f"{key}: unknown top-level field")
    if "schema_version" not in payload:
        raise ManifestError("schema_version: missing required field")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise ManifestError(
            f"schema_version: expected {SCHEMA_VERSION!r}, "
            f"got {payload['schema_version']!r}"
        )
    if "candidates" not in payload:
        raise ManifestError("candidates: missing required field")
    raw_candidates = payload["candidates"]
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ManifestError("candidates: expected a non-empty list")

    seen_ids: set[str] = set()
    candidates: list[SpikeCandidate] = []
    for index, raw in enumerate(raw_candidates):
        candidate = _parse_candidate(raw, index)
        if candidate.candidate_id in seen_ids:
            raise _fail(
                f"candidates[{index}].candidate_id",
                f"duplicate candidate_id {candidate.candidate_id!r}",
            )
        seen_ids.add(candidate.candidate_id)
        candidates.append(candidate)

    return SpikeManifest(
        schema_version=SCHEMA_VERSION, candidates=tuple(candidates)
    )


def verify_source_identity(candidate: SpikeCandidate) -> str:
    """Recompute the source SHA-256 and require it to match the manifest."""

    digest = _sha256_file(candidate.source_path)
    if digest != candidate.source_sha256:
        raise SourceHashMismatchError(
            f"{candidate.source_path}: source hash changed since manifest "
            f"creation (declared {candidate.source_sha256}, found {digest})"
        )
    return digest


def _optional_non_negative(value: float | None, description: str) -> None:
    if value is None:
        return
    if not math.isfinite(value) or value < 0:
        raise ResultValidationError(f"{description} must be finite and non-negative")


def _positive(value: float | None, description: str) -> None:
    if value is None:
        return
    if not math.isfinite(value) or value <= 0:
        raise ResultValidationError(f"{description} must be positive when present")


def validate_probe_result(
    candidate: SpikeCandidate,
    result: CandidateProbeResult,
) -> None:
    """Enforce cross-field probe result rules; raise on any violation."""

    if result.candidate_id != candidate.candidate_id:
        raise ResultValidationError(
            "candidate_id must match the requested candidate "
            f"({result.candidate_id!r} != {candidate.candidate_id!r})"
        )
    if result.source_sha256 != candidate.source_sha256:
        raise ResultValidationError(
            "source_sha256 must match the requested candidate"
        )

    _optional_non_negative(
        result.normalized_amplitude_peak, "normalized_amplitude_peak"
    )
    _optional_non_negative(
        result.normalized_amplitude_noise_floor,
        "normalized_amplitude_noise_floor",
    )
    _optional_non_negative(result.max_evidence_gap_s, "max_evidence_gap_s")
    _optional_non_negative(result.face_usable_coverage, "face_usable_coverage")
    _optional_non_negative(
        result.mean_face_landmark_confidence, "mean_face_landmark_confidence"
    )
    _optional_non_negative(result.min_face_short_side_px, "min_face_short_side_px")
    _positive(result.face_evidence_wall_clock_ms, "face_evidence_wall_clock_ms")
    _positive(result.curve_feature_wall_clock_ms, "curve_feature_wall_clock_ms")
    if not math.isfinite(result.wall_clock_ms) or result.wall_clock_ms <= 0:
        raise ResultValidationError("wall_clock_ms must be positive and finite")

    if result.source_media_width_px is not None and result.source_media_width_px <= 0:
        raise ResultValidationError("source_media_width_px must be positive")
    if (
        result.source_media_height_px is not None
        and result.source_media_height_px <= 0
    ):
        raise ResultValidationError("source_media_height_px must be positive")
    if result.primary_subject_area_ratio is not None and not (
        0.0 <= result.primary_subject_area_ratio <= 1.0
    ):
        raise ResultValidationError(
            "primary_subject_area_ratio must be within [0, 1]"
        )

    if result.curve_feature_status == "AVAILABLE":
        if (
            result.source_media_width_px is None
            or result.source_media_height_px is None
            or result.primary_subject_area_ratio is None
            or result.curve_feature_wall_clock_ms is None
        ):
            raise ResultValidationError(
                "curve_feature_status=AVAILABLE requires media dimensions, "
                "primary subject area ratio, and curve feature wall clock"
            )

    for reason in result.reason_codes:
        if not _SNAKE_CASE.fullmatch(reason):
            raise ResultValidationError(
                f"reason code {reason!r} must be stable snake_case"
            )

    if result.amplitude_status == "PASS":
        if (
            result.normalized_amplitude_peak is None
            or result.normalized_amplitude_noise_floor is None
        ):
            raise ResultValidationError(
                "amplitude PASS requires peak and independent noise floor"
            )
        if result.normalized_amplitude_peak <= result.normalized_amplitude_noise_floor:
            raise ResultValidationError(
                "amplitude PASS requires peak above the independent noise floor"
            )
        if result.amplitude_noise_reference == "unavailable":
            raise ResultValidationError(
                "amplitude PASS requires a non-unavailable noise reference"
            )
        if result.amplitude_phase_complete is not True:
            raise ResultValidationError(
                "amplitude PASS requires amplitude_phase_complete=true"
            )
        if (
            result.amplitude_noise_reference
            in ("background_residual", "combined")
            and result.camera_compensation_status != "AVAILABLE"
        ):
            raise ResultValidationError(
                "background-compensated amplitude PASS requires camera "
                "compensation AVAILABLE"
            )

    if candidate.media_kind == "source_video":
        low_confidence_metrics = (
            result.background_coverage is not None
            and result.background_coverage < MIN_BACKGROUND_COVERAGE
        ) or (
            result.affine_inlier_ratio is not None
            and result.affine_inlier_ratio < MIN_AFFINE_INLIER_RATIO
        )
        if low_confidence_metrics and (
            result.camera_compensation_status != "LOW_CONFIDENCE"
        ):
            raise ResultValidationError(
                "background coverage or affine inlier ratio below threshold "
                "requires camera_compensation_status=LOW_CONFIDENCE"
            )
        if result.camera_compensation_status == "LOW_CONFIDENCE":
            if result.amplitude_status != "UNAVAILABLE":
                raise ResultValidationError(
                    "LOW_CONFIDENCE camera compensation requires amplitude "
                    "UNAVAILABLE"
                )
            if result.disposition not in (
                "REVIEW_REQUIRED",
                "HARD_REJECT",
                "ERROR",
            ):
                raise ResultValidationError(
                    "LOW_CONFIDENCE camera compensation requires disposition "
                    "at least REVIEW_REQUIRED"
                )

    if result.body_identity_status == "CONFLICT" and (
        result.disposition == "AUTO_PASS"
    ):
        raise ResultValidationError(
            "body identity CONFLICT cannot have disposition AUTO_PASS"
        )
    if result.face_identity_status == "CONFLICT" and (
        result.disposition == "AUTO_PASS"
    ):
        raise ResultValidationError(
            "face identity CONFLICT cannot have disposition AUTO_PASS"
        )

    if result.face_presence_status == "USABLE":
        if result.face_usable_frame_count < MIN_FACE_USABLE_FRAMES:
            raise ResultValidationError(
                "USABLE face requires at least 3 usable frames"
            )
        if (
            result.min_face_short_side_px is None
            or result.min_face_short_side_px < MIN_FACE_SHORT_SIDE_PX
        ):
            raise ResultValidationError(
                "USABLE face requires face short side at least 64 px"
            )
        if (
            result.mean_face_landmark_confidence is None
            or result.mean_face_landmark_confidence < MIN_FACE_LANDMARK_CONFIDENCE
        ):
            raise ResultValidationError(
                "USABLE face requires mean landmark confidence at least 0.70"
            )
        if (
            result.face_usable_coverage is None
            or result.face_usable_coverage < MIN_FACE_USABLE_COVERAGE
        ):
            raise ResultValidationError(
                "USABLE face requires usable coverage at least 0.60"
            )
    if result.face_presence_status in ("NOT_DETECTED", "DETECTED_UNUSABLE"):
        if (
            result.face_identity_status != "NOT_APPLICABLE"
            or result.facial_landmark_status != "NOT_APPLICABLE"
            or result.facial_expression_status != "NOT_APPLICABLE"
        ):
            raise ResultValidationError(
                "absent or unusable face requires identity and facial "
                "dynamics statuses NOT_APPLICABLE"
            )

    groups = result.confirming_independence_groups
    if len(set(groups)) != len(groups):
        raise ResultValidationError(
            "confirming_independence_groups must not contain duplicates"
        )
    if result.disposition == "HARD_REJECT":
        if "scene_boundary" not in groups and len(groups) < 2:
            raise ResultValidationError(
                "HARD_REJECT requires scene_boundary or at least two distinct "
                "confirming independence groups"
            )
    elif groups:
        raise ResultValidationError(
            "confirming_independence_groups must be empty without HARD_REJECT"
        )

    if candidate.media_kind == "historical_gif":
        if result.amplitude_status != "NOT_APPLICABLE":
            raise ResultValidationError(
                "historical GIF must use amplitude status NOT_APPLICABLE"
            )
        if result.disposition not in (
            "GIF_ONLY_PASS",
            "GIF_ONLY_REVIEW",
            "GIF_ONLY_REJECT",
            "ERROR",
        ):
            raise ResultValidationError(
                "historical GIF must use a GIF_ONLY_* or ERROR disposition"
            )
    else:
        if result.disposition in (
            "GIF_ONLY_PASS",
            "GIF_ONLY_REVIEW",
            "GIF_ONLY_REJECT",
        ):
            raise ResultValidationError(
                "source video cannot use GIF-only dispositions"
            )


def make_error_result(
    candidate: SpikeCandidate,
    backend_id: str,
    reason_code: str,
    wall_clock_ms: float,
) -> CandidateProbeResult:
    """Build the canonical ERROR result for a failed probe."""

    return CandidateProbeResult(
        candidate_id=candidate.candidate_id,
        source_sha256=candidate.source_sha256,
        backend_id=backend_id,
        disposition="ERROR",
        total_frame_count=0,
        detected_frame_count=0,
        pose_valid_frame_count=0,
        max_evidence_gap_s=None,
        subject_set_ambiguous=False,
        tracking_loss_count=0,
        amplitude_status="UNAVAILABLE",
        amplitude_class="unobservable",
        normalized_amplitude_peak=None,
        normalized_amplitude_noise_floor=None,
        amplitude_noise_reference="unavailable",
        background_coverage=None,
        affine_inlier_ratio=None,
        camera_compensation_status="NOT_APPLICABLE",
        amplitude_phase_complete=None,
        body_identity_status="NOT_APPLICABLE",
        face_presence_status="NOT_DETECTED",
        face_identity_status="NOT_APPLICABLE",
        face_visible_frame_count=0,
        face_usable_frame_count=0,
        face_usable_coverage=None,
        min_face_short_side_px=None,
        mean_face_landmark_confidence=None,
        facial_landmark_status="NOT_APPLICABLE",
        facial_expression_status="NOT_APPLICABLE",
        curve_feature_status="NOT_RUN",
        curve_feature_missing_reason=None,
        source_media_width_px=None,
        source_media_height_px=None,
        primary_subject_area_ratio=None,
        face_decode_width_px=None,
        face_evidence_wall_clock_ms=None,
        curve_feature_wall_clock_ms=None,
        confirming_independence_groups=(),
        vlm_status="ERROR",
        wall_clock_ms=wall_clock_ms,
        reason_codes=(reason_code,),
    )

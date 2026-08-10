"""Deterministic, content-neutral quality evidence from sampled clip pixels."""

from __future__ import annotations

import math

import cv2
import numpy as np

from app.quality_moe.config import QualityMoeConfig
from app.quality_moe.models import EvidencePolarity, EvidenceStatus, ExpertEvidence
from app.quality_moe.sampling import SampledClip


def _bounded(value: float) -> float:
    return max(0.0, min(1.0, float(value))) if math.isfinite(value) else 0.0


def _luma(frame: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float64) / 255.0


def _sharpness_score(frame: np.ndarray) -> float:
    """Use native 8-bit variance so the fixed 250 scale is meaningful."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    laplacian_variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    return _bounded(1.0 - math.exp(-laplacian_variance / 250.0))


_MAX_MOTION_ESTIMATION_SIDE = 160
# Sampled frames are capped at 640px, so thumbnail quantization is at most 4px.
_MAX_MOTION_REFINEMENT_RADIUS = 4


def _overlap_bounds(
    width: int, height: int, dx: int, dy: int
) -> tuple[slice, slice, slice, slice]:
    return (
        slice(max(0, -dy), min(height, height - dy)),
        slice(max(0, -dx), min(width, width - dx)),
        slice(max(0, dy), min(height, height + dy)),
        slice(max(0, dx), min(width, width + dx)),
    )


def _grayscale_overlap_difference(
    left: np.ndarray, right: np.ndarray, dx: int, dy: int
) -> float:
    height, width = left.shape[:2]
    left_y, left_x, right_y, right_x = _overlap_bounds(width, height, dx, dy)
    return cv2.mean(cv2.absdiff(left[left_y, left_x], right[right_y, right_x]))[0] / 255.0


def _bgr_overlap_difference(left: np.ndarray, right: np.ndarray, dx: int, dy: int) -> float:
    height, width = left.shape[:2]
    left_y, left_x, right_y, right_x = _overlap_bounds(width, height, dx, dy)
    channel_means = cv2.mean(cv2.absdiff(left[left_y, left_x], right[right_y, right_x]))[:3]
    return sum(channel_means) / (len(channel_means) * 255.0)


def _motion_thumbnail(gray: np.ndarray) -> np.ndarray:
    height, width = gray.shape[:2]
    longest_side = max(height, width)
    if longest_side <= _MAX_MOTION_ESTIMATION_SIDE:
        return gray
    scale = _MAX_MOTION_ESTIMATION_SIDE / longest_side
    return cv2.resize(
        gray,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )


def _least_motion_near_best(
    candidates: list[tuple[float, tuple[int, int]]],
) -> tuple[float, tuple[int, int]]:
    best_residual = min(residual for residual, _shift in candidates)
    near_best = [
        item for item in candidates if item[0] <= best_residual + 1.0 / 255.0
    ]
    return min(
        near_best,
        key=lambda item: (
            math.hypot(*item[1]),
            abs(item[1][1]),
            abs(item[1][0]),
            item[1],
        ),
    )


def _motion_search_limit(frame: np.ndarray) -> int:
    height, width = frame.shape[:2]
    return min(16, max(4, round(min(height, width) * 0.05)), min(height, width) - 1)


def _motion_compensated_difference(
    left: np.ndarray, right: np.ndarray
) -> tuple[float, tuple[int, int]]:
    """Estimate coarsely, refine a bounded full-size window, then score BGR once."""
    full_height, full_width = left.shape[:2]
    left_gray = cv2.cvtColor(left, cv2.COLOR_BGR2GRAY)
    right_gray = cv2.cvtColor(right, cv2.COLOR_BGR2GRAY)
    left_thumbnail = _motion_thumbnail(left_gray)
    right_thumbnail = _motion_thumbnail(right_gray)
    thumbnail_height, thumbnail_width = left_thumbnail.shape[:2]
    max_shift = _motion_search_limit(left)
    max_dx = min(thumbnail_width - 1, max(1, math.ceil(max_shift * thumbnail_width / full_width)))
    max_dy = min(thumbnail_height - 1, max(1, math.ceil(max_shift * thumbnail_height / full_height)))
    coarse_candidates = [
        (
            _grayscale_overlap_difference(left_thumbnail, right_thumbnail, dx, dy),
            (
                round(dx * full_width / thumbnail_width),
                round(dy * full_height / thumbnail_height),
            ),
        )
        for dy in range(-max_dy, max_dy + 1)
        for dx in range(-max_dx, max_dx + 1)
    ]
    _coarse_residual, (coarse_dx, coarse_dy) = _least_motion_near_best(coarse_candidates)
    refine_radius_x = min(
        _MAX_MOTION_REFINEMENT_RADIUS,
        max_shift,
        math.ceil(full_width / thumbnail_width),
    )
    refine_radius_y = min(
        _MAX_MOTION_REFINEMENT_RADIUS,
        max_shift,
        math.ceil(full_height / thumbnail_height),
    )
    refined_candidates = [
        (_grayscale_overlap_difference(left_gray, right_gray, dx, dy), (dx, dy))
        for dy in range(
            max(-max_shift, coarse_dy - refine_radius_y),
            min(max_shift, coarse_dy + refine_radius_y) + 1,
        )
        for dx in range(
            max(-max_shift, coarse_dx - refine_radius_x),
            min(max_shift, coarse_dx + refine_radius_x) + 1,
        )
    ]
    _refined_residual, shift = _least_motion_near_best(refined_candidates)
    return _bgr_overlap_difference(left, right, *shift), shift


def _unavailable_evidence(
    sampled_clip: SampledClip, *, expert_id: str, signal_family: str, config: QualityMoeConfig
) -> ExpertEvidence:
    return ExpertEvidence(
        candidate_id=sampled_clip.candidate_id,
        evaluation_version=config.evaluation_version,
        expert_id=expert_id,
        expert_version="deterministic-v1",
        signal_family=signal_family,
        status=sampled_clip.status,
        findings=(dict(sampled_clip.diagnostics),),
        summary="Sampled clip is unavailable; no quality score was inferred.",
        input_hash=sampled_clip.input_hash,
        config_hash=config.config_hash,
    )


class _BaseExpert:
    expert_id = ""
    signal_family = ""

    def __init__(self, config: QualityMoeConfig | None = None) -> None:
        self._config = config or QualityMoeConfig.defaults()

    def _evidence(
        self,
        sampled_clip: SampledClip,
        *,
        scores: dict[str, float],
        findings: tuple[dict[str, object], ...] = (),
        polarity: EvidencePolarity = EvidencePolarity.NEUTRAL,
        summary: str,
    ) -> ExpertEvidence:
        if sampled_clip.status is not EvidenceStatus.AVAILABLE:
            return _unavailable_evidence(
                sampled_clip, expert_id=self.expert_id,
                signal_family=self.signal_family, config=self._config,
            )
        return ExpertEvidence(
            candidate_id=sampled_clip.candidate_id,
            evaluation_version=self._config.evaluation_version,
            expert_id=self.expert_id,
            expert_version="deterministic-v1",
            signal_family=self.signal_family,
            status=EvidenceStatus.AVAILABLE,
            scores={key: _bounded(value) for key, value in scores.items()},
            findings=findings,
            summary=summary,
            input_hash=sampled_clip.input_hash,
            config_hash=self._config.config_hash,
            polarity=polarity,
        )


class TechnicalAestheticExpert(_BaseExpert):
    """Measures only technical pixel characteristics, never semantic labels."""

    expert_id = "technical_aesthetic"
    signal_family = "nr_vqa"

    def evaluate(self, sampled_clip: SampledClip) -> ExpertEvidence:
        if sampled_clip.status is not EvidenceStatus.AVAILABLE:
            return self._evidence(sampled_clip, scores={}, summary="No technical assessment.")
        lumas = np.concatenate([_luma(frame).ravel() for frame in sampled_clip.frames])
        median_luma = float(np.median(lumas))
        exposure_score = _bounded(1.0 - min(1.0, abs(median_luma - 0.5) / 0.5))
        sharpness_values = [_sharpness_score(frame) for frame in sampled_clip.frames]
        sharpness_score = _bounded(float(np.median(sharpness_values)))
        shadow_clip = float(np.mean(lumas <= 1.0 / 255.0))
        highlight_clip = float(np.mean(lumas >= 254.0 / 255.0))
        clipping_penalty = _bounded(shadow_clip + highlight_clip)
        technical_integrity = _bounded(
            0.60 * exposure_score + 0.25 * sharpness_score + 0.15 * (1.0 - clipping_penalty)
        )
        findings: tuple[dict[str, object], ...] = ()
        polarity = EvidencePolarity.NEUTRAL
        detail_preservation = sharpness_score
        if exposure_score < 0.4 and shadow_clip > 0.5 and detail_preservation < 0.1:
            findings = ({
                "code": "underexposed_subject",
                "severity": "repairable",
                "metric": "median_luma",
                "value": median_luma,
                "shadow_clipping": shadow_clip,
                "detail_preservation": detail_preservation,
            },)
            polarity = EvidencePolarity.NEGATIVE
        elif clipping_penalty > 0.5:
            findings = ({
                "code": "severe_luminance_clipping",
                "severity": "technical_failure",
                "value": clipping_penalty,
            },)
            polarity = EvidencePolarity.NEGATIVE
        return self._evidence(
            sampled_clip,
            scores={
                "technical_integrity": technical_integrity,
                "exposure_balance": exposure_score,
                "sharpness": sharpness_score,
                "clipping": 1.0 - clipping_penalty,
            },
            findings=findings,
            polarity=polarity,
            summary="Technical assessment uses measured exposure, detail, and clipping only.",
        )


class TemporalExpert(_BaseExpert):
    """Detects measurable temporal disruptions and loop continuity."""

    expert_id = "temporal"
    signal_family = "deterministic_temporal"

    def evaluate(self, sampled_clip: SampledClip) -> ExpertEvidence:
        if sampled_clip.status is not EvidenceStatus.AVAILABLE:
            return self._evidence(sampled_clip, scores={}, summary="No temporal assessment.")
        frames = list(sampled_clip.frames)
        comparisons = [
            _motion_compensated_difference(left, right)
            for left, right in zip(frames, frames[1:])
        ]
        differences = [residual for residual, _shift in comparisons]
        max_difference = max(differences, default=0.0)
        mean_difference = float(np.mean(differences)) if differences else 0.0
        loop_difference, _loop_shift = _motion_compensated_difference(frames[0], frames[-1])
        temporal_coherence = _bounded(1.0 - max_difference)
        loop_score = _bounded(1.0 - loop_difference)
        findings: tuple[dict[str, object], ...] = ()
        polarity = EvidencePolarity.NEUTRAL
        if max_difference > 0.3:
            findings = ({
                "code": "luminance_flash_or_discontinuity",
                "severity": "technical_failure",
                "max_frame_difference": max_difference,
            },)
            polarity = EvidencePolarity.NEGATIVE
        elif any(dx or dy for _residual, (dx, dy) in comparisons):
            findings = ({
                "code": "camera_motion",
                "severity": "descriptive",
                "max_translation_pixels": max(
                    math.hypot(dx, dy) for _residual, (dx, dy) in comparisons
                ),
            },)
        return self._evidence(
            sampled_clip,
            scores={
                "temporal_coherence": temporal_coherence,
                "loop_continuity": loop_score,
                "mean_frame_difference": _bounded(1.0 - mean_difference),
            },
            findings=findings,
            polarity=polarity,
            summary="Temporal assessment reports only measured frame discontinuities.",
        )


class CinematicExpert(_BaseExpert):
    """Produces descriptive film-style evidence without content or style penalties."""

    expert_id = "cinematic"
    signal_family = "cinematic_classifier"

    def evaluate(self, sampled_clip: SampledClip) -> ExpertEvidence:
        if sampled_clip.status is not EvidenceStatus.AVAILABLE:
            return self._evidence(sampled_clip, scores={}, summary="No cinematic assessment.")
        lumas = np.concatenate([_luma(frame).ravel() for frame in sampled_clip.frames])
        median_luma = float(np.median(lumas))
        channel_means = np.mean(
            np.concatenate([frame.reshape(-1, 3) for frame in sampled_clip.frames], axis=0), axis=0
        ) / 255.0
        color_balance = _bounded(1.0 - float(np.max(channel_means) - np.min(channel_means)))
        findings: tuple[dict[str, object], ...] = ()
        if median_luma < 0.35:
            findings = ({
                "code": "low_key_lighting",
                "severity": "descriptive",
                "median_luma": median_luma,
            },)
        return self._evidence(
            sampled_clip,
            scores={"color_balance": color_balance},
            findings=findings,
            summary="Film-style attributes are descriptive and do not create a negative signal.",
        )

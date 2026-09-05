"""Experimental real-media backend for the continuity spike (milestone 0B).

Disposable measurement backend: YOLOv8n-pose (ONNX, DirectML or CPU) for
person detection and COCO-17 keypoints, YuNet (OpenCV FaceDetectorYN) for
face boxes, greedy subject tracking, background-affine camera compensation
with torso-scale-normalized amplitude, and pose-derived curve-feature
availability for historical GIFs.

Load with the 0A CLI via::

    --backend scripts.continuity_spike_backend_real:create_backend

It reports what it measures and nothing more: no ReID or face-embedding
model exists here (identity statuses stay UNAVAILABLE/NOT_APPLICABLE), no
VLM runs (vlm_status=SKIPPED), and it never emits HARD_REJECT because it has
no second independent evidence family to confirm one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from scripts.continuity_spike_contracts import (
    AmplitudeClass,
    AmplitudeStatus,
    CandidateProbeResult,
    Disposition,
    FacePresenceStatus,
    FacialDynamicsStatus,
    SpikeCandidate,
    make_error_result,
)

BACKEND_ID = "real-yolov8pose-yunet-v1"

_REPO = Path(__file__).resolve().parents[1]
MODEL_DIR = Path(
    os.environ.get(
        "CONTINUITY_SPIKE_MODEL_DIR", str(_REPO / "data" / "continuity_spike_models")
    )
)
POSE_MODEL = MODEL_DIR / "yolov8n-pose.onnx"
FACE_MODEL = MODEL_DIR / "face_detection_yunet_2023mar.onnx"

ANALYSIS_WIDTH = 960
SAMPLE_FPS = 8
POSE_INPUT = 640
PERSON_SCORE_MIN = 0.35
KEYPOINT_SCORE_MIN = 0.50
VALID_KEYPOINT_MIN = 6
MIN_BACKGROUND_POINTS = 40
MIN_FACE_SCORE = 0.60
MIN_FACE_SHORT_SIDE = 64.0
MIN_FACE_CONF = 0.70
MIN_USABLE_FACES = 3
MIN_FACE_COVERAGE = 0.60
MIN_BACKGROUND_COVERAGE = 0.30
MIN_AFFINE_INLIER_RATIO = 0.50
MIN_POSE_COVERAGE = 0.95
MAX_EVIDENCE_GAP_S = 0.25
MIN_SUBJECT_AREA_RATIO = 0.02
CO_PRIMARY_AREA_RATIO = 0.55
AMBIGUOUS_AREA_RATIO = 0.35
OBSERVABLE_FLOOR_FACTOR = 3.0
OBSERVABLE_ABS_MIN = 0.03
_FALLBACK_FLOOR = 0.015
_BORDER_PX = 2
_SHOULDERS = (5, 6)
_HIPS = (11, 12)
_LIMB_TIPS = (9, 10, 15, 16)
_TORSO = (5, 6, 11, 12)

_PROVIDERS = ("DmlExecutionProvider", "CPUExecutionProvider")


class DecodeFailure(Exception):
    """Media could not be decoded into usable frames."""


@dataclass
class _Person:
    box: tuple[float, float, float, float]
    score: float
    keypoints: np.ndarray  # (17, 3) x, y, conf in analysis pixels

    @property
    def area(self) -> float:
        return max(self.box[2], 1.0) * max(self.box[3], 1.0)

    def keypoint(self, index: int) -> tuple[float, float, float]:
        x, y, conf = self.keypoints[index]
        return float(x), float(y), float(conf)


class RealSpikeBackend:
    backend_id: str = BACKEND_ID
    synthetic: bool = False

    def __init__(self) -> None:
        if not POSE_MODEL.is_file():
            raise RuntimeError(f"pose model missing: {POSE_MODEL}")
        import onnxruntime as ort

        providers = [p for p in _PROVIDERS if p in ort.get_available_providers()]
        self._pose = ort.InferenceSession(str(POSE_MODEL), providers=providers)
        self._pose_input = self._pose.get_inputs()[0].name
        self._face_ready = FACE_MODEL.is_file()
        if self._face_ready:
            self._face = cv2.FaceDetectorYN.create(
                str(FACE_MODEL), "", (ANALYSIS_WIDTH, 720), score_threshold=MIN_FACE_SCORE
            )

    def evaluate_source(
        self,
        candidates: tuple[SpikeCandidate, ...],
    ) -> tuple[CandidateProbeResult, ...]:
        results: list[CandidateProbeResult] = []
        for candidate in candidates:
            start_ns = time.perf_counter_ns()
            try:
                results.append(self._evaluate_one(candidate, start_ns))
            except DecodeFailure as exc:
                elapsed = (time.perf_counter_ns() - start_ns) / 1_000_000
                results.append(
                    make_error_result(
                        candidate,
                        backend_id=self.backend_id,
                        reason_code="decode_failed",
                        wall_clock_ms=max(elapsed, 1e-6),
                    )
                )
        return tuple(results)

    def _evaluate_one(
        self,
        candidate: SpikeCandidate,
        start_ns: int,
    ) -> CandidateProbeResult:
        if candidate.media_kind == "historical_gif":
            return self._evaluate_gif(candidate, start_ns)
        return self._evaluate_video(candidate, start_ns)

    # ------------------------------------------------------------------ #
    # source video                                                        #
    # ------------------------------------------------------------------ #

    def _evaluate_video(
        self,
        candidate: SpikeCandidate,
        start_ns: int,
    ) -> CandidateProbeResult:
        window_frames = self._extract_frames(
            candidate.source_path,
            candidate.window_start_s,
            candidate.window_end_s - candidate.window_start_s,
        )
        context_frames = self._extract_frames(
            candidate.source_path,
            candidate.context_start_s,
            candidate.context_end_s - candidate.context_start_s,
        )
        total = len(window_frames)
        if total < 4:
            raise DecodeFailure(f"only {total} window frames decoded")
        height, width = window_frames[0].shape[:2]

        persons = [self._detect_persons(frame) for frame in window_frames]
        context_persons = [self._detect_persons(frame) for frame in context_frames]

        valid_flags = [
            _pose_valid(frame_persons[0] if frame_persons else None)
            for frame_persons in persons
        ]
        pose_valid = sum(valid_flags)
        max_gap = _max_gap_s(valid_flags, SAMPLE_FPS)
        tracking_loss = _tracking_loss_runs(valid_flags)
        subjects = _subject_set_stats(persons)
        area_ratio = _subject_area_ratio(persons, width, height)
        torso_clipped = _torso_border_clipped(persons, width, height)

        face_start = time.perf_counter_ns()
        face_stats = self._face_stats(window_frames, persons)
        face_ms = (time.perf_counter_ns() - face_start) / 1_000_000

        amplitude = self._amplitude(window_frames, persons, context_frames, context_persons)

        disposition, reasons = self._video_disposition(
            pose_valid=pose_valid,
            total=total,
            max_gap=max_gap,
            tracking_loss=tracking_loss,
            subjects=subjects,
            area_ratio=area_ratio,
            torso_clipped=torso_clipped,
            amplitude=amplitude,
        )

        return CandidateProbeResult(
            candidate_id=candidate.candidate_id,
            source_sha256=candidate.source_sha256,
            backend_id=self.backend_id,
            disposition=disposition,
            total_frame_count=total,
            detected_frame_count=sum(1 for p in persons if p),
            pose_valid_frame_count=pose_valid,
            max_evidence_gap_s=max_gap,
            subject_set_ambiguous=subjects["ambiguous"],
            tracking_loss_count=tracking_loss,
            amplitude_status=amplitude["status"],
            amplitude_class=amplitude["class"],
            normalized_amplitude_peak=amplitude["peak"],
            normalized_amplitude_noise_floor=amplitude["floor"],
            amplitude_noise_reference=amplitude["reference"],
            background_coverage=amplitude["coverage"],
            affine_inlier_ratio=amplitude["inlier"],
            camera_compensation_status=amplitude["compensation"],
            amplitude_phase_complete=amplitude["phase_complete"],
            body_identity_status="UNAVAILABLE",
            face_presence_status=face_stats["presence"],
            face_identity_status=(
                "UNAVAILABLE" if face_stats["presence"] == "USABLE" else "NOT_APPLICABLE"
            ),
            face_visible_frame_count=face_stats["visible"],
            face_usable_frame_count=face_stats["usable"],
            face_usable_coverage=face_stats["coverage"],
            min_face_short_side_px=face_stats["short_side"],
            mean_face_landmark_confidence=face_stats["confidence"],
            facial_landmark_status=face_stats["landmark_status"],
            facial_expression_status=face_stats["expression_status"],
            curve_feature_status="NOT_RUN",
            curve_feature_missing_reason=None,
            source_media_width_px=width,
            source_media_height_px=height,
            primary_subject_area_ratio=area_ratio,
            face_decode_width_px=ANALYSIS_WIDTH,
            face_evidence_wall_clock_ms=face_ms,
            curve_feature_wall_clock_ms=None,
            confirming_independence_groups=(),
            vlm_status="SKIPPED",
            wall_clock_ms=(time.perf_counter_ns() - start_ns) / 1_000_000,
            reason_codes=reasons,
        )

    # ------------------------------------------------------------------ #
    # historical GIF                                                      #
    # ------------------------------------------------------------------ #

    def _evaluate_gif(
        self,
        candidate: SpikeCandidate,
        start_ns: int,
    ) -> CandidateProbeResult:
        frames = self._extract_gif_frames(candidate.source_path)
        if len(frames) < 3:
            raise DecodeFailure(f"only {len(frames)} GIF frames decoded")
        height, width = frames[0].shape[:2]

        curve_start = time.perf_counter_ns()
        persons = [self._detect_persons(frame) for frame in frames]
        face_stats = self._face_stats(frames, persons)
        valid = sum(
            1
            for frame_persons in persons
            if _pose_valid(frame_persons[0] if frame_persons else None)
        )
        area_ratio = _subject_area_ratio(persons, width, height)
        spread = _pose_extension(persons)
        curve_ms = (time.perf_counter_ns() - curve_start) / 1_000_000

        curve_available = valid >= 3 and spread is not None
        reasons = ["historical_gif_without_source_context"]
        if not curve_available:
            reasons.append("pose_coverage_low")
        disposition: Disposition = "GIF_ONLY_PASS" if curve_available else "GIF_ONLY_REVIEW"

        presence = face_stats["presence"]
        return CandidateProbeResult(
            candidate_id=candidate.candidate_id,
            source_sha256=candidate.source_sha256,
            backend_id=self.backend_id,
            disposition=disposition,
            total_frame_count=len(frames),
            detected_frame_count=sum(1 for p in persons if p),
            pose_valid_frame_count=valid,
            max_evidence_gap_s=None,
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
            body_identity_status="UNAVAILABLE",
            face_presence_status=face_stats["presence"],
            face_identity_status=(
                "UNAVAILABLE" if presence == "USABLE" else "NOT_APPLICABLE"
            ),
            face_visible_frame_count=face_stats["visible"],
            face_usable_frame_count=face_stats["usable"],
            face_usable_coverage=face_stats["coverage"],
            min_face_short_side_px=face_stats["short_side"],
            mean_face_landmark_confidence=face_stats["confidence"],
            facial_landmark_status=face_stats["landmark_status"],
            facial_expression_status=face_stats["expression_status"],
            curve_feature_status="AVAILABLE" if curve_available else "UNAVAILABLE",
            curve_feature_missing_reason=None if curve_available else "pose_coverage_low",
            source_media_width_px=width,
            source_media_height_px=height,
            primary_subject_area_ratio=area_ratio,
            face_decode_width_px=ANALYSIS_WIDTH,
            face_evidence_wall_clock_ms=None,
            curve_feature_wall_clock_ms=curve_ms,
            confirming_independence_groups=(),
            vlm_status="SKIPPED",
            wall_clock_ms=(time.perf_counter_ns() - start_ns) / 1_000_000,
            reason_codes=tuple(reasons),
        )

    # ------------------------------------------------------------------ #
    # models                                                              #
    # ------------------------------------------------------------------ #

    def _detect_persons(self, frame: np.ndarray) -> list[_Person]:
        scale, pad_x, pad_y = _letterbox_params(frame, POSE_INPUT)
        image = _letterbox(frame, POSE_INPUT)
        blob = cv2.dnn.blobFromImage(
            image, 1.0 / 255.0, (POSE_INPUT, POSE_INPUT), swapRB=True, crop=False
        )
        output = self._pose.run(None, {self._pose_input: blob})[0][0]  # (56, anchors)
        boxes = output[0:4, :]
        scores = output[4, :]
        keypoints = output[5:, :].reshape(17, 3, -1)

        detected: list[_Person] = []
        for index in np.flatnonzero(scores >= PERSON_SCORE_MIN):
            cx, cy, w, h = (float(v) for v in boxes[:, index])
            x0 = (cx - w / 2 - pad_x) / scale
            y0 = (cy - h / 2 - pad_y) / scale
            raw = keypoints[:, :, index]  # (17, 3): x, y, conf
            kps = np.stack(
                [
                    (raw[:, 0] - pad_x) / scale,
                    (raw[:, 1] - pad_y) / scale,
                    raw[:, 2],
                ],
                axis=1,
            )
            detected.append(
                _Person(
                    box=(x0, y0, w / scale, h / scale),
                    score=float(scores[index]),
                    keypoints=kps,
                )
            )
        return _nms_persons(detected)

    def _face_stats(
        self,
        frames: list[np.ndarray],
        persons: list[list[_Person]],
    ) -> dict[str, object]:
        empty = {
            "presence": "NOT_DETECTED",
            "visible": 0,
            "usable": 0,
            "coverage": None,
            "short_side": None,
            "confidence": None,
            "landmark_status": "NOT_APPLICABLE",
            "expression_status": "NOT_APPLICABLE",
        }
        if not self._face_ready:
            return empty
        visible = 0
        usable = 0
        short_sides: list[float] = []
        usable_short_sides: list[float] = []
        confidences: list[float] = []
        usable_confidences: list[float] = []
        for frame, frame_persons in zip(frames, persons):
            height, width = frame.shape[:2]
            self._face.setInputSize((width, height))
            _, faces = self._face.detect(frame)
            if faces is None or len(faces) == 0:
                continue
            visible += 1
            face = faces[0]
            if frame_persons:
                primary = frame_persons[0]
                for candidate_face in faces:
                    x, y, w, h = candidate_face[:4]
                    cx, cy = x + w / 2, y + h / 2
                    px, py, pw, ph = primary.box
                    if px <= cx <= px + pw and py <= cy <= py + ph:
                        face = candidate_face
                        break
            x, y, w, h = face[:4]
            score = float(face[-1])
            short_side = float(min(w, h))
            short_sides.append(short_side)
            confidences.append(score)
            if short_side >= MIN_FACE_SHORT_SIDE and score >= MIN_FACE_CONF:
                usable += 1
                usable_confidences.append(score)
                usable_short_sides.append(short_side)

        total = len(frames)
        if not total:
            return empty
        coverage = usable / total
        if usable >= MIN_USABLE_FACES and coverage >= MIN_FACE_COVERAGE:
            presence: FacePresenceStatus = "USABLE"
        elif visible:
            presence = "DETECTED_UNUSABLE"
        else:
            presence = "NOT_DETECTED"
        if presence == "USABLE":
            # YuNet provides 5 landmarks only: not a landmark-dynamics or
            # expression backend, so advisory statuses stay UNAVAILABLE.
            landmark_status: FacialDynamicsStatus = "UNAVAILABLE"
            expression_status: FacialDynamicsStatus = "UNAVAILABLE"
        else:
            landmark_status = "NOT_APPLICABLE"
            expression_status = "NOT_APPLICABLE"
        return {
            "presence": presence,
            "visible": visible,
            "usable": usable,
            "coverage": coverage,
            "short_side": min(usable_short_sides)
            if usable_short_sides
            else (min(short_sides) if short_sides else None),
            # Confidence of the usable-face evidence population; the
            # USABLE contract requires this mean at or above 0.70.
            "confidence": float(np.mean(usable_confidences))
            if usable_confidences
            else (float(np.mean(confidences)) if confidences else None),
            "landmark_status": landmark_status,
            "expression_status": expression_status,
        }

    # ------------------------------------------------------------------ #
    # amplitude with camera compensation                                  #
    # ------------------------------------------------------------------ #

    def _amplitude(
        self,
        window_frames: list[np.ndarray],
        window_persons: list[list[_Person]],
        context_frames: list[np.ndarray],
        context_persons: list[list[_Person]],
    ) -> dict[str, object]:
        window_result = _stabilized_speeds(window_frames, window_persons)
        context_result = _stabilized_speeds(context_frames, context_persons)

        coverage = window_result["coverage"]
        inlier = window_result["inlier"]
        compensation = (
            "AVAILABLE"
            if coverage is not None
            and inlier is not None
            and coverage >= MIN_BACKGROUND_COVERAGE
            and inlier >= MIN_AFFINE_INLIER_RATIO
            else "LOW_CONFIDENCE"
        )
        if compensation == "LOW_CONFIDENCE":
            return {
                "status": "UNAVAILABLE",
                "class": "unobservable",
                "peak": None,
                "floor": None,
                "reference": "unavailable",
                "coverage": coverage,
                "inlier": inlier,
                "compensation": "LOW_CONFIDENCE",
                "phase_complete": None,
            }
        speeds = window_result["speeds"]
        if speeds is None or len(speeds) < 3:
            return {
                "status": "UNAVAILABLE",
                "class": "unobservable",
                "peak": None,
                "floor": None,
                "reference": "unavailable",
                "coverage": coverage,
                "inlier": inlier,
                "compensation": "AVAILABLE",
                "phase_complete": None,
            }

        reference: str = "unavailable"
        floors: list[float] = [_FALLBACK_FLOOR]
        context_speeds = context_result["speeds"]
        if context_speeds is not None and len(context_speeds) >= 2:
            floors.append(float(np.percentile(context_speeds, 50)))
            reference = "static_context"
        if window_result["residual_floor"] is not None:
            floors.append(window_result["residual_floor"])
            reference = (
                "combined" if reference == "static_context" else "background_residual"
            )
        floor = max(floors)

        speeds_arr = np.asarray(speeds, dtype=np.float64)
        peak = float(np.percentile(speeds_arr, 95))
        median_speed = float(np.median(speeds_arr))
        observable = peak > max(OBSERVABLE_FLOOR_FACTOR * floor, OBSERVABLE_ABS_MIN)
        spike = peak > 12.0 * max(median_speed, 1e-6)
        phase_complete = _phase_complete(speeds_arr, peak)

        if not observable:
            status: AmplitudeStatus = "REVIEW"
        elif spike:
            status = "REVIEW"
        elif phase_complete is not True:
            status = "REVIEW"
        else:
            status = "PASS"
        amplitude_class: AmplitudeClass = (
            "unobservable"
            if not observable
            else "subtle"
            if peak < 0.15
            else "moderate"
            if peak < 0.45
            else "large"
        )
        return {
            "status": status,
            "class": amplitude_class,
            "peak": peak,
            "floor": floor,
            "reference": reference,
            "coverage": coverage,
            "inlier": inlier,
            "compensation": "AVAILABLE",
            "phase_complete": phase_complete,
        }

    # ------------------------------------------------------------------ #
    # media decoding                                                      #
    # ------------------------------------------------------------------ #

    def _extract_frames(
        self,
        path: Path,
        start_s: float,
        duration_s: float,
    ) -> list[np.ndarray]:
        duration_s = max(duration_s, 0.25)
        with tempfile.TemporaryDirectory(prefix="continuity_spike_") as tmp:
            pattern = str(Path(tmp) / "f%05d.jpg")
            command = [
                _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                "-hwaccel", "auto",
                "-ss", f"{max(start_s, 0.0):.3f}",
                "-t", f"{duration_s:.3f}",
                "-i", str(path),
                "-vf", f"fps={SAMPLE_FPS},scale={ANALYSIS_WIDTH}:-2",
                "-q:v", "2",
                "-start_number", "0",
                pattern,
            ]
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=300
            )
            frames = _load_frame_dir(Path(tmp))
            if len(frames) < 2:
                raise DecodeFailure(
                    f"ffmpeg rc={completed.returncode}: {completed.stderr[-200:]}"
                )
            return frames

    def _extract_gif_frames(self, path: Path) -> list[np.ndarray]:
        with tempfile.TemporaryDirectory(prefix="continuity_spike_gif_") as tmp:
            pattern = str(Path(tmp) / "f%05d.jpg")
            command = [
                _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(path),
                "-vf", f"fps={SAMPLE_FPS},scale={ANALYSIS_WIDTH}:-2",
                "-q:v", "2",
                "-start_number", "0",
                pattern,
            ]
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=120
            )
            frames = _load_frame_dir(Path(tmp))
            if len(frames) < 2:
                raise DecodeFailure(
                    f"gif rc={completed.returncode}: {completed.stderr[-200:]}"
                )
            return frames

    # ------------------------------------------------------------------ #
    # disposition policy                                                  #
    # ------------------------------------------------------------------ #

    def _video_disposition(
        self,
        *,
        pose_valid: int,
        total: int,
        max_gap: float,
        tracking_loss: int,
        subjects: dict[str, bool],
        area_ratio: float | None,
        torso_clipped: bool,
        amplitude: dict[str, object],
    ) -> tuple[Disposition, list[str]]:
        reasons: list[str] = []
        coverage = pose_valid / total if total else 0.0
        if subjects["ambiguous"]:
            reasons.append("primary_subject_ambiguous")
        if tracking_loss:
            reasons.append("tracking_gap")
        if max_gap > MAX_EVIDENCE_GAP_S or coverage < MIN_POSE_COVERAGE:
            reasons.append("pose_coverage_low")
        if area_ratio is not None and area_ratio < MIN_SUBJECT_AREA_RATIO:
            reasons.append("subject_too_small")
        if torso_clipped:
            reasons.append("critical_joint_clipped")
        if amplitude["compensation"] == "LOW_CONFIDENCE":
            reasons.append("camera_compensation_low_confidence")
        elif amplitude["status"] == "REVIEW":
            if amplitude["phase_complete"] is not True:
                reasons.append("amplitude_phase_incomplete")
            elif amplitude["class"] == "unobservable":
                reasons.append("motion_amplitude_below_observable_floor")
            else:
                reasons.append("motion_amplitude_discontinuity")
        if reasons:
            return "REVIEW_REQUIRED", reasons
        return "AUTO_PASS", []


def create_backend() -> RealSpikeBackend:
    return RealSpikeBackend()


def _stabilized_speeds(
    frames: list[np.ndarray],
    persons: list[list[_Person]],
) -> dict[str, object]:
    """Camera-compensated per-interval torso speeds, torso-scale normalized.

    Returns speeds (per frame interval), background point coverage, affine
    inlier ratio, and the background-residual noise floor contribution.
    """

    torso_scale = _median_torso_scale(persons)
    if torso_scale is None or torso_scale < 8.0 or len(frames) < 4:
        return {"speeds": None, "coverage": None, "inlier": None, "residual_floor": None}

    half_width = frames[0].shape[1] // 2
    half_height = frames[0].shape[0] // 2
    centers: list[tuple[float, float]] = []
    coverages: list[float] = []
    inliers: list[float] = []
    residuals: list[float] = []
    prev_gray: np.ndarray | None = None
    prev_boxes: list[tuple[float, float, float, float]] = []
    cumulative = np.eye(2, 3, dtype=np.float64)

    for index, frame in enumerate(frames):
        gray = cv2.cvtColor(
            cv2.resize(frame, (half_width, half_height)), cv2.COLOR_BGR2GRAY
        )
        frame_persons = persons[index]
        if frame_persons and _pose_valid(frame_persons[0]):
            person = frame_persons[0]
            sx = (person.keypoint(_SHOULDERS[0])[0] + person.keypoint(_SHOULDERS[1])[0]) / 2
            sy = (person.keypoint(_SHOULDERS[0])[1] + person.keypoint(_SHOULDERS[1])[1]) / 2
            hx = (person.keypoint(_HIPS[0])[0] + person.keypoint(_HIPS[1])[0]) / 2
            hy = (person.keypoint(_HIPS[0])[1] + person.keypoint(_HIPS[1])[1]) / 2
            centers.append(((sx + hx) / 2, (sy + hy) / 2))
        else:
            centers.append((float("nan"), float("nan")))

        if prev_gray is not None:
            points = cv2.goodFeaturesToTrack(
                prev_gray,
                maxCorners=400,
                qualityLevel=0.01,
                minDistance=8,
                mask=_background_mask(
                    (half_height, half_width), prev_boxes, (half_width, half_height)
                ),
            )
            if points is not None and len(points) >= MIN_BACKGROUND_POINTS:
                matched, status, _ = cv2.calcOpticalFlowPyrLK(
                    prev_gray, gray, points, None
                )
                good = status.ravel() == 1
                if int(good.sum()) >= MIN_BACKGROUND_POINTS:
                    transform, inlier_mask = cv2.estimateAffinePartial2D(
                        points[good],
                        matched[good],
                        method=cv2.RANSAC,
                        ransacReprojThreshold=2.0,
                    )
                    if transform is not None:
                        inliers.append(float(inlier_mask.mean()))
                        coverages.append(1.0)
                        inverse = cv2.invertAffineTransform(transform)
                        residual = inverse @ np.array([0.0, 0.0, 1.0])
                        residuals.append(float(np.linalg.norm(residual[:2])))
                        cumulative = _compose_affine(cumulative, inverse)
                    else:
                        coverages.append(0.0)
                else:
                    coverages.append(0.0)
            else:
                coverages.append(0.0)
        prev_gray = gray
        prev_boxes = [p.box for p in frame_persons]

    valid_centers = [(c for c in [centers[t]]) for t in range(len(centers))]
    stabilized: list[tuple[int, float, float]] = []
    for t, (cx, cy) in enumerate(centers):
        if cx == cx:  # not NaN
            moved = cumulative @ np.array([cx, cy, 1.0])
            stabilized.append((t, float(moved[0]), float(moved[1])))
    del valid_centers

    if len(stabilized) < 4:
        return {
            "speeds": None,
            "coverage": _mean_or_none(coverages),
            "inlier": _mean_or_none(inliers),
            "residual_floor": None,
        }
    speeds = [
        float(
            np.hypot(x1 - x0, y1 - y0) / torso_scale
        )
        for (_, x0, y0), (_, x1, y1) in zip(stabilized, stabilized[1:])
        if x0 == x0 and x1 == x1
    ]
    return {
        "speeds": speeds,
        "coverage": _mean_or_none(coverages),
        "inlier": _mean_or_none(inliers),
        "residual_floor": (float(np.median(residuals)) / torso_scale)
        if residuals
        else None,
    }


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _letterbox(frame: np.ndarray, target: int) -> np.ndarray:
    height, width = frame.shape[:2]
    scale = min(target / width, target / height)
    resized = cv2.resize(frame, (int(width * scale), int(height * scale)))
    canvas = np.full((target, target, 3), 114, dtype=np.uint8)
    pad_x = (target - resized.shape[1]) // 2
    pad_y = (target - resized.shape[0]) // 2
    canvas[pad_y : pad_y + resized.shape[0], pad_x : pad_x + resized.shape[1]] = resized
    return canvas


def _letterbox_params(frame: np.ndarray, target: int) -> tuple[float, float, float]:
    height, width = frame.shape[:2]
    scale = min(target / width, target / height)
    pad_x = (target - int(width * scale)) / 2
    pad_y = (target - int(height * scale)) / 2
    return scale, pad_x, pad_y


def _nms_persons(persons: list[_Person], iou: float = 0.5) -> list[_Person]:
    ordered = sorted(persons, key=lambda p: p.score, reverse=True)
    kept: list[_Person] = []
    for person in ordered:
        if all(_box_iou(person.box, other.box) < iou for other in kept):
            kept.append(person)
    return kept


def _box_iou(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax0 + aw, bx0 + bw), min(ay0 + ah, by0 + bh)
    inter = max(ix1 - ix0, 0.0) * max(iy1 - iy0, 0.0)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _pose_valid(person: _Person | None) -> bool:
    if person is None:
        return False
    confident = sum(
        1 for index in range(17) if person.keypoint(index)[2] >= KEYPOINT_SCORE_MIN
    )
    if confident < VALID_KEYPOINT_MIN:
        return False
    torso = sum(
        1 for index in _TORSO if person.keypoint(index)[2] >= KEYPOINT_SCORE_MIN
    )
    return torso >= 2


def _subject_set_stats(persons: list[list[_Person]]) -> dict[str, bool]:
    total = len(persons)
    if not total:
        return {"ambiguous": False, "co_primary": False, "close_contact": False}
    co_primary = 0
    ambiguous = 0
    contact = 0
    for frame_persons in persons:
        if not frame_persons:
            continue
        primary_area = frame_persons[0].area
        others = frame_persons[1:]
        if any(other.area >= CO_PRIMARY_AREA_RATIO * primary_area for other in others):
            co_primary += 1
        if len(frame_persons) >= 3 and any(
            other.area >= AMBIGUOUS_AREA_RATIO * primary_area for other in others
        ):
            ambiguous += 1
        if others and _box_iou(frame_persons[0].box, others[0].box) >= 0.10:
            contact += 1
    return {
        "ambiguous": ambiguous / total >= 0.30,
        "co_primary": co_primary / total >= 0.55,
        "close_contact": contact / total >= 0.55,
    }


def _tracking_loss_runs(valid_flags: list[bool]) -> int:
    runs = 0
    previous = False
    started = False
    for flag in valid_flags:
        if flag:
            if not previous and started:
                runs += 1
            started = True
        previous = flag
    return runs


def _max_gap_s(valid_flags: list[bool], fps: float) -> float:
    longest = 0
    current = 0
    for flag in valid_flags:
        if not flag:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest / fps


def _subject_area_ratio(
    persons: list[list[_Person]],
    width: int,
    height: int,
) -> float | None:
    ratios = [
        frame_persons[0].area / max(width * height, 1.0)
        for frame_persons in persons
        if frame_persons
    ]
    if not ratios:
        return None
    return float(np.median(ratios))


def _torso_border_clipped(
    persons: list[list[_Person]],
    width: int,
    height: int,
) -> bool:
    clipped = 0
    counted = 0
    for frame_persons in persons:
        if not frame_persons or not _pose_valid(frame_persons[0]):
            continue
        counted += 1
        person = frame_persons[0]
        for index in _TORSO:
            x, y, conf = person.keypoint(index)
            if conf < KEYPOINT_SCORE_MIN:
                continue
            if x <= _BORDER_PX or y <= _BORDER_PX or x >= width - _BORDER_PX or y >= height - _BORDER_PX:
                clipped += 1
                break
    if counted < 4:
        return False
    return clipped / counted >= 0.80


def _median_torso_scale(persons: list[list[_Person]]) -> float | None:
    scales: list[float] = []
    for frame_persons in persons:
        if not frame_persons or not _pose_valid(frame_persons[0]):
            continue
        person = frame_persons[0]
        sx = (person.keypoint(_SHOULDERS[0])[0] + person.keypoint(_SHOULDERS[1])[0]) / 2
        sy = (person.keypoint(_SHOULDERS[0])[1] + person.keypoint(_SHOULDERS[1])[1]) / 2
        hx = (person.keypoint(_HIPS[0])[0] + person.keypoint(_HIPS[1])[0]) / 2
        hy = (person.keypoint(_HIPS[0])[1] + person.keypoint(_HIPS[1])[1]) / 2
        length = float(np.hypot(sx - hx, sy - hy))
        if length > 4.0:
            scales.append(length)
    if not scales:
        return None
    return float(np.median(scales))


def _pose_extension(persons: list[list[_Person]]) -> float | None:
    spreads: list[float] = []
    for frame_persons in persons:
        if not frame_persons or not _pose_valid(frame_persons[0]):
            continue
        person = frame_persons[0]
        sx = (person.keypoint(_SHOULDERS[0])[0] + person.keypoint(_SHOULDERS[1])[0]) / 2
        sy = (person.keypoint(_SHOULDERS[0])[1] + person.keypoint(_SHOULDERS[1])[1]) / 2
        hx = (person.keypoint(_HIPS[0])[0] + person.keypoint(_HIPS[1])[0]) / 2
        hy = (person.keypoint(_HIPS[0])[1] + person.keypoint(_HIPS[1])[1]) / 2
        torso = float(np.hypot(sx - hx, sy - hy))
        if torso < 4.0:
            continue
        tips = [
            person.keypoint(index)
            for index in _LIMB_TIPS
            if person.keypoint(index)[2] >= KEYPOINT_SCORE_MIN
        ]
        if len(tips) < 2:
            continue
        xs = [tip[0] for tip in tips]
        spreads.append((max(xs) - min(xs)) / torso)
    if not spreads:
        return None
    return float(np.mean(spreads))


def _background_mask(
    shape: tuple[int, ...],
    boxes: list[tuple[float, float, float, float]],
    full_size: tuple[int, int],
) -> np.ndarray:
    height, width = shape[:2]
    full_w, full_h = full_size
    scale_x = width / max(full_w, 1)
    scale_y = height / max(full_h, 1)
    mask = np.full((height, width), 255, dtype=np.uint8)
    for x, y, w, h in boxes:
        x0 = int(x * scale_x) - 20
        y0 = int(y * scale_y) - 20
        x1 = int((x + w) * scale_x) + 20
        y1 = int((y + h) * scale_y) + 20
        mask[max(y0, 0) : max(y1, 0), max(x0, 0) : max(x1, 0)] = 0
    return mask


def _compose_affine(cumulative: np.ndarray, inverse: np.ndarray) -> np.ndarray:
    a1, b1 = cumulative[:, :2], cumulative[:, 2:3]
    a2, b2 = inverse[:, :2], inverse[:, 2:3]
    composed_a = a2 @ a1
    composed_b = a2 @ b1 + b2
    return np.hstack([composed_a, composed_b])


def _phase_complete(speeds: np.ndarray, peak: float) -> bool | None:
    if len(speeds) < 4 or peak <= 0:
        return None
    quiet = peak * 0.35
    if speeds[0] <= quiet and speeds[-1] <= quiet:
        return True
    above = speeds >= peak * 0.60
    peaks = 0
    in_peak = False
    for flag in above:
        if flag and not in_peak:
            peaks += 1
            in_peak = True
        elif not flag:
            in_peak = False
    if peaks >= 2:
        between = speeds[~above]
        if len(between) and float(between.max()) <= peak * 0.45:
            return True
    return False


def _mean_or_none(values: list[float]) -> float | None:
    return float(np.mean(values)) if values else None


def _load_frame_dir(directory: Path) -> list[np.ndarray]:
    frames = []
    for path in sorted(directory.glob("f*.jpg")):
        image = cv2.imread(str(path))
        if image is not None:
            frames.append(image)
    return frames

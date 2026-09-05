"""Statistical aggregation and eligibility tests for the spike report."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from scripts.continuity_spike_contracts import (
    SCHEMA_VERSION,
    CandidateProbeResult,
    SpikeCandidate,
    SpikeManifest,
)
from scripts.continuity_spike_metrics import (
    REQUIRED_SOURCE_VIDEO_SCENARIOS,
    build_spike_report,
    nearest_rank_percentile,
)

EXTRA_SCENARIO_TAGS = (
    "single_subject",
    "co_primary_subjects",
    "partial_body",
    "headless",
    "close_contact",
    "occlusion",
    "fast_motion",
    "low_light",
    "media_edge",
)
MOTION_TAGS = ("subtle_motion", "moderate_motion", "large_motion")
FACE_TAGS = ("face_visible", "face_occluded", "face_not_visible")
HIST_RESOLUTIONS = (200, 400, 600)
HIST_DURATIONS = (1.5, 3.0, 6.0)
HIST_RATIOS = (0.10, 0.40, 0.80)
HIST_FEEDBACK = ("favorite", "like", "dislike", "neutral")


def _source_candidate(
    candidate_id: str,
    tags: tuple[str, ...],
    *,
    data_origin: str = "real",
    human_label: str = "ACCEPTABLE",
    media_duration_s: float = 8.0,
    window_start_s: float = 1.0,
    window_end_s: float = 4.0,
) -> SpikeCandidate:
    return SpikeCandidate(
        candidate_id=candidate_id,
        media_kind="source_video",
        source_path=Path(f"Z:/spike-media/{candidate_id}.mp4"),
        source_sha256="a" * 64,
        media_duration_s=media_duration_s,
        confirmed_shot_start_s=0.0,
        context_start_s=0.0,
        window_start_s=window_start_s,
        window_end_s=window_end_s,
        context_end_s=6.0,
        confirmed_shot_end_s=7.0,
        scenario_tags=tags,
        data_origin=data_origin,  # type: ignore[arg-type]
        human_label=human_label,  # type: ignore[arg-type]
        feedback_label="unknown",
    )


def _historical_candidate(
    candidate_id: str,
    *,
    feedback_label: str = "favorite",
    media_duration_s: float = 3.0,
    data_origin: str = "real",
) -> SpikeCandidate:
    return SpikeCandidate(
        candidate_id=candidate_id,
        media_kind="historical_gif",
        source_path=Path(f"Z:/spike-media/{candidate_id}.gif"),
        source_sha256="b" * 64,
        media_duration_s=media_duration_s,
        confirmed_shot_start_s=0.0,
        context_start_s=0.0,
        window_start_s=0.0,
        window_end_s=media_duration_s,
        context_end_s=media_duration_s,
        confirmed_shot_end_s=media_duration_s,
        scenario_tags=("subtle_motion", "face_visible"),
        data_origin=data_origin,  # type: ignore[arg-type]
        human_label="UNLABELED",
        feedback_label=feedback_label,  # type: ignore[arg-type]
    )


def _video_result(
    candidate: SpikeCandidate,
    **overrides: object,
) -> CandidateProbeResult:
    base = CandidateProbeResult(
        candidate_id=candidate.candidate_id,
        source_sha256=candidate.source_sha256,
        backend_id="metrics-test-backend",
        disposition="AUTO_PASS",
        total_frame_count=24,
        detected_frame_count=24,
        pose_valid_frame_count=24,
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
        face_visible_frame_count=24,
        face_usable_frame_count=20,
        face_usable_coverage=0.85,
        min_face_short_side_px=96.0,
        mean_face_landmark_confidence=0.90,
        facial_landmark_status="AVAILABLE",
        facial_expression_status="AVAILABLE",
        curve_feature_status="NOT_RUN",
        curve_feature_missing_reason=None,
        source_media_width_px=320,
        source_media_height_px=180,
        primary_subject_area_ratio=None,
        face_decode_width_px=320,
        face_evidence_wall_clock_ms=150.0,
        curve_feature_wall_clock_ms=None,
        confirming_independence_groups=(),
        vlm_status="COMPLETED",
        wall_clock_ms=1000.0,
        reason_codes=(),
    )
    return dataclasses.replace(  # type: ignore[arg-type]
        base, **overrides  # type: ignore[arg-type]
    )


def _gif_result(
    candidate: SpikeCandidate,
    *,
    width: int,
    duration_s: float,
    ratio: float,
    **overrides: object,
) -> CandidateProbeResult:
    base = CandidateProbeResult(
        candidate_id=candidate.candidate_id,
        source_sha256=candidate.source_sha256,
        backend_id="metrics-test-backend",
        disposition="GIF_ONLY_PASS",
        total_frame_count=16,
        detected_frame_count=16,
        pose_valid_frame_count=16,
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
        face_presence_status="NOT_DETECTED",
        face_identity_status="NOT_APPLICABLE",
        face_visible_frame_count=0,
        face_usable_frame_count=0,
        face_usable_coverage=None,
        min_face_short_side_px=None,
        mean_face_landmark_confidence=None,
        facial_landmark_status="NOT_APPLICABLE",
        facial_expression_status="NOT_APPLICABLE",
        curve_feature_status="AVAILABLE",
        curve_feature_missing_reason=None,
        source_media_width_px=width,
        source_media_height_px=max(64, width * 9 // 16),
        primary_subject_area_ratio=ratio,
        face_decode_width_px=None,
        face_evidence_wall_clock_ms=None,
        curve_feature_wall_clock_ms=500.0,
        confirming_independence_groups=(),
        vlm_status="SKIPPED",
        wall_clock_ms=400.0,
        reason_codes=(),
    )
    return dataclasses.replace(  # type: ignore[arg-type]
        base, **overrides  # type: ignore[arg-type]
    )


def _full_population() -> tuple[SpikeManifest, list[CandidateProbeResult]]:
    candidates: list[SpikeCandidate] = []
    results: list[CandidateProbeResult] = []
    for index in range(50):
        tags = (
            EXTRA_SCENARIO_TAGS[index % len(EXTRA_SCENARIO_TAGS)],
            MOTION_TAGS[index % len(MOTION_TAGS)],
            FACE_TAGS[index % len(FACE_TAGS)],
        )
        candidate = _source_candidate(f"video-{index:03d}", tags)
        candidates.append(candidate)
        results.append(_video_result(candidate))
    for index in range(200):
        candidate = _historical_candidate(
            f"gif-{index:03d}",
            feedback_label=HIST_FEEDBACK[index % len(HIST_FEEDBACK)],
            media_duration_s=HIST_DURATIONS[(index // 3) % len(HIST_DURATIONS)],
        )
        candidates.append(candidate)
        results.append(
            _gif_result(
                candidate,
                width=HIST_RESOLUTIONS[index % len(HIST_RESOLUTIONS)],
                duration_s=candidate.duration_s,
                ratio=HIST_RATIOS[(index // 9) % len(HIST_RATIOS)],
            )
        )
    return SpikeManifest(SCHEMA_VERSION, tuple(candidates)), results


def _ready_runtime() -> list[dict[str, object]]:
    return [
        {
            "kind": "telemetry",
            "gpu": [
                {
                    "memory_used_mib": 1000.0,
                    "memory_total_mib": 16384.0,
                }
            ],
        },
        {
            "kind": "source_batch_timing",
            "source_key": "a" * 64,
            "media_kind": "source_video",
            "candidate_count": 50,
            "wall_clock_ms": 60000.0,
        },
    ]


def test_nearest_rank_percentile_uses_observed_value():
    assert nearest_rank_percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0


def test_nearest_rank_percentile_single_value():
    assert nearest_rank_percentile([5.0], 0.5) == 5.0


def test_nearest_rank_percentile_rejects_empty_and_invalid_input():
    with pytest.raises(ValueError):
        nearest_rank_percentile([], 0.5)
    with pytest.raises(ValueError):
        nearest_rank_percentile([1.0], 0.0)
    with pytest.raises(ValueError):
        nearest_rank_percentile([1.0], 1.5)


def test_synthetic_backend_can_never_reach_go_no_go():
    manifest, results = _full_population()
    report = build_spike_report(
        manifest,
        [results[0]],
        [{"gpu": [{"memory_used_mib": 1000, "memory_total_mib": 16384}]}],
        backend_synthetic=True,
    )
    assert report["go_no_go"]["state"] == "INSUFFICIENT_REAL_DATA"
    assert "synthetic_backend" in report["go_no_go"]["reasons"]


def test_full_real_population_passes_all_thresholds():
    manifest, results = _full_population()

    report = build_spike_report(
        manifest, results, _ready_runtime(), backend_synthetic=False
    )

    assert report["schema"] == "continuity-spike-report-v1"
    for key in (
        "population",
        "scenario_coverage",
        "evidence_independence",
        "motion_amplitude",
        "face_identity",
        "facial_advisory",
        "historical_curve_features",
        "performance",
        "position_quintiles",
        "vlm",
        "threshold_checks",
        "go_no_go",
    ):
        assert key in report
    assert report["go_no_go"]["state"] == "READY_FOR_HUMAN_GO_NO_GO_REVIEW"
    assert report["go_no_go"]["reasons"] == []
    assert report["scenario_coverage"]["required_missing"] == []
    assert all(
        check["passed"] for check in report["threshold_checks"].values()
    )


def test_candidate_p50_threshold_failure_is_isolated():
    manifest, results = _full_population()
    slow = [
        _video_result(candidate, wall_clock_ms=11000.0 if index < 49 else 12000.0)
        for index, candidate in enumerate(
            [c for c in manifest.candidates if c.media_kind == "source_video"]
        )
    ]
    gif_results = [
        result for result in results if result.disposition == "GIF_ONLY_PASS"
    ]

    report = build_spike_report(
        manifest, slow + gif_results, _ready_runtime(), backend_synthetic=False
    )

    assert report["go_no_go"]["state"] == "NO_GO"
    assert report["go_no_go"]["reasons"] == [
        "threshold_failed:candidate_p50_ms_max"
    ]


def test_candidate_p95_threshold_failure_is_isolated():
    manifest, results = _full_population()
    slow = [
        _video_result(candidate, wall_clock_ms=1000.0 if index < 47 else 35000.0)
        for index, candidate in enumerate(
            [c for c in manifest.candidates if c.media_kind == "source_video"]
        )
    ]
    gif_results = [
        result for result in results if result.disposition == "GIF_ONLY_PASS"
    ]

    report = build_spike_report(
        manifest, slow + gif_results, _ready_runtime(), backend_synthetic=False
    )

    assert report["go_no_go"]["reasons"] == [
        "threshold_failed:candidate_p95_ms_max"
    ]


def test_source_video_p95_threshold_failure_is_isolated():
    manifest, results = _full_population()
    runtime = [
        {
            "kind": "source_batch_timing",
            "source_key": "a" * 64,
            "media_kind": "source_video",
            "candidate_count": 50,
            "wall_clock_ms": 1_300_000.0,
        },
        {
            "kind": "telemetry",
            "gpu": [{"memory_used_mib": 1000.0, "memory_total_mib": 16384.0}],
        },
    ]

    report = build_spike_report(
        manifest, results, runtime, backend_synthetic=False
    )

    assert report["go_no_go"]["reasons"] == [
        "threshold_failed:source_video_p95_ms_max"
    ]


def test_peak_vram_threshold_failure_is_isolated():
    manifest, results = _full_population()
    runtime = [
        {
            "kind": "telemetry",
            "gpu": [{"memory_used_mib": 15000.0, "memory_total_mib": 16384.0}],
        },
        {
            "kind": "source_batch_timing",
            "source_key": "a" * 64,
            "media_kind": "source_video",
            "candidate_count": 50,
            "wall_clock_ms": 60000.0,
        },
    ]

    report = build_spike_report(
        manifest, results, runtime, backend_synthetic=False
    )

    assert report["go_no_go"]["reasons"] == ["threshold_failed:peak_vram_mib_max"]


def test_missing_vram_measurement_fails_closed():
    manifest, results = _full_population()
    runtime = [
        {
            "kind": "source_batch_timing",
            "source_key": "a" * 64,
            "media_kind": "source_video",
            "candidate_count": 50,
            "wall_clock_ms": 60000.0,
        },
    ]

    report = build_spike_report(manifest, results, runtime, backend_synthetic=False)

    assert report["go_no_go"]["reasons"] == ["threshold_failed:peak_vram_mib_max"]


def test_auto_pass_rate_threshold_failure_is_isolated():
    manifest, results = _full_population()
    source_candidates = [
        c for c in manifest.candidates if c.media_kind == "source_video"
    ]
    mixed: list[CandidateProbeResult] = []
    for index, candidate in enumerate(source_candidates):
        if index < 15:
            mixed.append(_video_result(candidate))
        elif index < 40:
            mixed.append(
                _video_result(candidate, disposition="REVIEW_REQUIRED")
            )
        else:
            mixed.append(
                _video_result(
                    candidate,
                    disposition="HARD_REJECT",
                    confirming_independence_groups=("scene_boundary",),
                    reason_codes=("shot_boundary_conflict",),
                )
            )
    gif_results = [
        result for result in results if result.disposition == "GIF_ONLY_PASS"
    ]

    report = build_spike_report(
        manifest, mixed + gif_results, _ready_runtime(), backend_synthetic=False
    )

    assert report["go_no_go"]["reasons"] == ["threshold_failed:auto_pass_rate_min"]


def test_review_required_rate_threshold_failure_is_isolated():
    manifest, results = _full_population()
    source_candidates = [
        c for c in manifest.candidates if c.media_kind == "source_video"
    ]
    gif_candidates = [
        c for c in manifest.candidates if c.media_kind == "historical_gif"
    ]
    rebuilt = [
        _source_candidate(
            c.candidate_id,
            c.scenario_tags,
            human_label="INVALID" if index >= 40 else "ACCEPTABLE",
        )
        for index, c in enumerate(source_candidates)
    ]
    outcomes = [
        _video_result(
            candidate,
            disposition="AUTO_PASS" if index < 19 else "REVIEW_REQUIRED",
        )
        for index, candidate in enumerate(rebuilt)
    ]
    gif_results = [
        result for result in results if result.disposition == "GIF_ONLY_PASS"
    ]

    report = build_spike_report(
        SpikeManifest(SCHEMA_VERSION, tuple(rebuilt) + tuple(gif_candidates)),
        outcomes + gif_results,
        _ready_runtime(),
        backend_synthetic=False,
    )

    assert report["go_no_go"]["reasons"] == [
        "threshold_failed:review_required_rate_max"
    ]


def test_mixed_real_synthetic_input_is_insufficient():
    manifest, results = _full_population()
    synthetic = _source_candidate(
        "video-synthetic",
        ("single_subject", "subtle_motion", "face_visible"),
        data_origin="synthetic",
    )
    rebuilt_manifest = SpikeManifest(
        SCHEMA_VERSION, manifest.candidates + (synthetic,)
    )
    outcomes = results + [_video_result(synthetic)]

    report = build_spike_report(
        rebuilt_manifest,
        outcomes,
        _ready_runtime(),
        backend_synthetic=False,
    )

    assert report["go_no_go"]["state"] == "INSUFFICIENT_REAL_DATA"
    assert "synthetic_candidates" in report["go_no_go"]["reasons"]


def test_unlabeled_source_video_is_insufficient():
    manifest, results = _full_population()
    source_candidates = [
        c for c in manifest.candidates if c.media_kind == "source_video"
    ]
    unlabeled = _source_candidate(
        "video-unlabeled",
        ("single_subject", "subtle_motion", "face_visible"),
        human_label="UNLABELED",
    )
    outcomes = [_video_result(c) for c in source_candidates]
    outcomes.append(_video_result(unlabeled))

    report = build_spike_report(
        SpikeManifest(
            SCHEMA_VERSION, tuple(source_candidates) + (unlabeled,)
        ),
        outcomes,
        _ready_runtime(),
        backend_synthetic=False,
    )

    assert "unlabeled_source_video_candidates" in report["go_no_go"]["reasons"]


def test_unknown_historical_feedback_is_insufficient():
    manifest, results = _full_population()
    gif_candidates = [
        c for c in manifest.candidates if c.media_kind == "historical_gif"
    ]
    gif_results = [
        r for r in results if r.disposition == "GIF_ONLY_PASS"
    ]
    unknown = _historical_candidate("gif-unknown", feedback_label="unknown")
    gif_candidates.append(unknown)
    gif_results.append(
        _gif_result(unknown, width=200, duration_s=3.0, ratio=0.40)
    )

    report = build_spike_report(
        SpikeManifest(SCHEMA_VERSION, tuple(gif_candidates)),
        gif_results,
        _ready_runtime(),
        backend_synthetic=False,
    )

    assert "unknown_historical_feedback" in report["go_no_go"]["reasons"]


def test_missing_scenario_bucket_is_insufficient():
    manifest, results = _full_population()
    source_candidates = [
        c for c in manifest.candidates if c.media_kind == "source_video"
    ]
    stripped = [
        _source_candidate(
            c.candidate_id,
            tuple(tag for tag in c.scenario_tags if tag != "low_light"),
            human_label=c.human_label,
        )
        for c in source_candidates
    ]
    outcomes = [_video_result(c) for c in stripped]

    report = build_spike_report(
        SpikeManifest(SCHEMA_VERSION, tuple(stripped)),
        outcomes,
        _ready_runtime(),
        backend_synthetic=False,
    )

    assert report["go_no_go"]["state"] == "INSUFFICIENT_REAL_DATA"
    assert "missing_scenario_bucket:low_light" in report["go_no_go"]["reasons"]


def test_empty_historical_bucket_is_insufficient():
    candidates: list[SpikeCandidate] = []
    outcomes: list[CandidateProbeResult] = []
    for index in range(60):
        candidate = _historical_candidate(
            f"gif-uniform-{index:03d}",
            feedback_label=HIST_FEEDBACK[index % len(HIST_FEEDBACK)],
            media_duration_s=1.5,
        )
        candidates.append(candidate)
        outcomes.append(
            _gif_result(candidate, width=200, duration_s=1.5, ratio=0.10)
        )

    report = build_spike_report(
        SpikeManifest(SCHEMA_VERSION, tuple(candidates)),
        outcomes,
        _ready_runtime(),
        backend_synthetic=False,
    )

    assert report["go_no_go"]["state"] == "INSUFFICIENT_REAL_DATA"
    assert "empty_historical_bucket:resolution_257_480" in report["go_no_go"]["reasons"]
    assert "empty_historical_bucket:duration_gt_5s" in report["go_no_go"]["reasons"]
    assert (
        "empty_historical_bucket:subject_area_gt_0.60"
        in report["go_no_go"]["reasons"]
    )


def test_missing_feedback_label_is_insufficient():
    manifest, results = _full_population()
    gif_candidates = [
        c
        for c in manifest.candidates
        if c.media_kind == "historical_gif" and c.feedback_label != "dislike"
    ]
    gif_results = [
        r for r in results if r.candidate_id in {c.candidate_id for c in gif_candidates}
    ]

    report = build_spike_report(
        SpikeManifest(SCHEMA_VERSION, tuple(gif_candidates)),
        gif_results,
        _ready_runtime(),
        backend_synthetic=False,
    )

    assert "missing_feedback_label:dislike" in report["go_no_go"]["reasons"]


def test_candidate_result_mismatch_is_insufficient():
    manifest, results = _full_population()

    report = build_spike_report(
        manifest, results[:10], _ready_runtime(), backend_synthetic=False
    )

    assert report["go_no_go"]["state"] == "INSUFFICIENT_REAL_DATA"
    assert "candidate_result_mismatch" in report["go_no_go"]["reasons"]


def test_motion_amplitude_aggregation():
    manifest, results = _full_population()
    source_candidates = [
        c for c in manifest.candidates if c.media_kind == "source_video"
    ]
    amplitudes: list[CandidateProbeResult] = []
    peaks = (0.15, 0.20, 0.45, 0.90)
    classes = ("subtle", "subtle", "moderate", "large")
    references = ("combined", "combined", "combined", "static_context")
    for index, candidate in enumerate(source_candidates):
        slot = index % 4
        amplitudes.append(
            _video_result(
                candidate,
                amplitude_class=classes[slot],
                normalized_amplitude_peak=peaks[slot],
                amplitude_noise_reference=references[slot],
            )
        )

    report = build_spike_report(
        SpikeManifest(SCHEMA_VERSION, tuple(source_candidates)),
        amplitudes,
        _ready_runtime(),
        backend_synthetic=False,
    )

    amplitude = report["motion_amplitude"]
    assert amplitude["class_counts"] == {
        "unobservable": 0,
        "subtle": 26,
        "moderate": 12,
        "large": 12,
    }
    assert amplitude["normalized_peak_p50"] == pytest.approx(0.20)
    assert amplitude["normalized_peak_p95"] == pytest.approx(0.90)
    assert amplitude["noise_reference_counts"]["static_context"] == 12
    assert amplitude["low_confidence_compensation_rate"] == 0.0
    assert amplitude["phase_incomplete_rate"] == 0.0


def test_low_confidence_compensation_and_amplitude_review_rates():
    manifest, results = _full_population()
    source_candidates = [
        c for c in manifest.candidates if c.media_kind == "source_video"
    ]
    flagged = _video_result(
        source_candidates[0],
        disposition="REVIEW_REQUIRED",
        amplitude_status="UNAVAILABLE",
        amplitude_class="unobservable",
        normalized_amplitude_peak=None,
        normalized_amplitude_noise_floor=None,
        amplitude_noise_reference="unavailable",
        background_coverage=0.10,
        affine_inlier_ratio=0.80,
        camera_compensation_status="LOW_CONFIDENCE",
        amplitude_phase_complete=None,
    )

    report = build_spike_report(
        SpikeManifest(SCHEMA_VERSION, tuple(source_candidates)),
        [flagged] + [_video_result(c) for c in source_candidates[1:]],
        _ready_runtime(),
        backend_synthetic=False,
    )

    amplitude = report["motion_amplitude"]
    assert amplitude["low_confidence_compensation_rate"] == pytest.approx(1 / 50)
    assert amplitude["amplitude_review_rate"] == pytest.approx(1 / 50)


def test_face_identity_marginal_catch_count():
    manifest, results = _full_population()
    source_candidates = [
        c for c in manifest.candidates if c.media_kind == "source_video"
    ]
    invalid_a = _source_candidate(
        "video-invalid-a", ("occlusion", "subtle_motion", "face_visible"),
        human_label="INVALID",
    )
    invalid_b = _source_candidate(
        "video-invalid-b", ("occlusion", "moderate_motion", "face_visible"),
        human_label="INVALID",
    )
    invalid_c = _source_candidate(
        "video-invalid-c", ("occlusion", "large_motion", "face_visible"),
        human_label="INVALID",
    )
    face_catch = _video_result(
        invalid_a,
        disposition="REVIEW_REQUIRED",
        face_identity_status="CONFLICT",
        confirming_independence_groups=(),
        reason_codes=("face_embedding_identity_conflict",),
    )
    body_catch = _video_result(
        invalid_b,
        disposition="REVIEW_REQUIRED",
        body_identity_status="CONFLICT",
    )
    double_confirm = _video_result(
        invalid_c,
        disposition="HARD_REJECT",
        face_identity_status="CONFLICT",
        confirming_independence_groups=(
            "appearance_identity",
            "photometric_motion",
        ),
    )
    population = list(source_candidates) + [invalid_a, invalid_b, invalid_c]
    outcomes = [_video_result(c) for c in source_candidates] + [
        face_catch,
        body_catch,
        double_confirm,
    ]

    report = build_spike_report(
        SpikeManifest(SCHEMA_VERSION, tuple(population)),
        outcomes,
        _ready_runtime(),
        backend_synthetic=False,
    )

    face = report["face_identity"]
    assert face["marginal_true_catch_count"] == 1
    assert face["face_identity_counts"]["CONFLICT"] == 2
    assert face["body_identity_counts"]["CONFLICT"] == 1


def test_advisory_expression_abstain_does_not_alter_disposition():
    manifest, results = _full_population()
    source_candidates = [
        c for c in manifest.candidates if c.media_kind == "source_video"
    ]
    abstaining = _video_result(
        source_candidates[0],
        facial_expression_status="ABSTAIN",
        facial_landmark_status="TEMPORAL_ALERT",
    )
    outcomes = [abstaining] + [_video_result(c) for c in source_candidates[1:]]

    report = build_spike_report(
        SpikeManifest(SCHEMA_VERSION, tuple(source_candidates)),
        outcomes,
        _ready_runtime(),
        backend_synthetic=False,
    )

    assert report["facial_advisory"]["marginal_alert_count"] == 1
    assert report["facial_advisory"]["expression_status_counts"]["ABSTAIN"] == 1
    assert report["population"]["disposition_counts"]["AUTO_PASS"] == 50
    assert report["threshold_checks"]["auto_pass_rate_min"]["passed"] is True


def test_invalid_same_group_hard_reject_is_reported():
    manifest, results = _full_population()
    source_candidates = [
        c for c in manifest.candidates if c.media_kind == "source_video"
    ]
    invalid_reject = _video_result(
        source_candidates[0],
        disposition="HARD_REJECT",
        confirming_independence_groups=("pose_geometry",),
    )

    report = build_spike_report(
        SpikeManifest(SCHEMA_VERSION, tuple(source_candidates)),
        [invalid_reject] + [_video_result(c) for c in source_candidates[1:]],
        _ready_runtime(),
        backend_synthetic=False,
    )

    independence = report["evidence_independence"]
    assert independence["invalid_hard_rejects"] == [source_candidates[0].candidate_id]
    assert independence["valid_hard_reject_count"] == 0


def test_historical_curve_feature_yield_and_projection():
    manifest, results = _full_population()
    gif_candidates = [
        c for c in manifest.candidates if c.media_kind == "historical_gif"
    ][:3]
    gif_results = [
        _gif_result(gif_candidates[0], width=200, duration_s=1.5, ratio=0.10),
        _gif_result(
            gif_candidates[1],
            width=400,
            duration_s=3.0,
            ratio=0.40,
            curve_feature_wall_clock_ms=1700.0,
        ),
        _gif_result(
            gif_candidates[2],
            width=600,
            duration_s=6.0,
            ratio=0.80,
            curve_feature_status="UNAVAILABLE",
            curve_feature_missing_reason="pose_coverage_low",
            curve_feature_wall_clock_ms=None,
        ),
    ]

    report = build_spike_report(
        SpikeManifest(SCHEMA_VERSION, tuple(gif_candidates)),
        gif_results,
        _ready_runtime(),
        backend_synthetic=False,
    )

    curve = report["historical_curve_features"]
    assert curve["available"] == 2
    assert curve["unavailable"] == 1
    assert curve["missing_reasons"] == {"pose_coverage_low": 1}
    assert curve["effective_training_rows"] == {
        "favorite": 1,
        "like": 1,
        "dislike": 0,
    }
    assert curve["cost_mean_ms"] == pytest.approx(1100.0)
    expected_hours = 1100.0 * 16894 / 3_600_000
    assert curve["projected_full_inventory_gpu_hours"] == pytest.approx(expected_hours)


def test_vlm_abstain_reporting():
    manifest, results = _full_population()
    source_candidates = [
        c for c in manifest.candidates if c.media_kind == "source_video"
    ]
    outcomes = [_video_result(c) for c in source_candidates]
    outcomes[0] = _video_result(source_candidates[0], vlm_status="ABSTAIN")
    outcomes[1] = _video_result(source_candidates[1], vlm_status="ERROR")

    report = build_spike_report(
        SpikeManifest(SCHEMA_VERSION, tuple(source_candidates)),
        outcomes,
        _ready_runtime(),
        backend_synthetic=False,
    )

    vlm = report["vlm"]
    assert vlm["status_counts"]["ABSTAIN"] == 1
    assert vlm["status_counts"]["ERROR"] == 1
    assert vlm["status_counts"]["COMPLETED"] == 48
    assert vlm["abstain_rate"] == pytest.approx(1 / 50)


def test_error_rate_and_disposition_counts():
    manifest, results = _full_population()
    source_candidates = [
        c for c in manifest.candidates if c.media_kind == "source_video"
    ]
    outcomes = [_video_result(c) for c in source_candidates]
    outcomes[3] = _video_result(
        source_candidates[3],
        disposition="ERROR",
        reason_codes=("decode_failed",),
        amplitude_status="UNAVAILABLE",
        amplitude_class="unobservable",
        normalized_amplitude_peak=None,
        normalized_amplitude_noise_floor=None,
        amplitude_noise_reference="unavailable",
        camera_compensation_status="NOT_APPLICABLE",
        amplitude_phase_complete=None,
        face_presence_status="NOT_DETECTED",
        face_identity_status="NOT_APPLICABLE",
        facial_landmark_status="NOT_APPLICABLE",
        facial_expression_status="NOT_APPLICABLE",
        face_visible_frame_count=0,
        face_usable_frame_count=0,
        face_usable_coverage=None,
        min_face_short_side_px=None,
        mean_face_landmark_confidence=None,
        face_decode_width_px=None,
        face_evidence_wall_clock_ms=None,
    )

    report = build_spike_report(
        SpikeManifest(SCHEMA_VERSION, tuple(source_candidates)),
        outcomes,
        _ready_runtime(),
        backend_synthetic=False,
    )

    population = report["population"]
    assert population["disposition_counts"]["ERROR"] == 1
    assert population["error_rate"] == pytest.approx(1 / 50)


def test_position_quintiles_report_dispositions_and_budget_exhaustion():
    candidates: list[SpikeCandidate] = []
    outcomes: list[CandidateProbeResult] = []
    for index, window in enumerate(((0.0, 2.0), (2.0, 4.0), (4.0, 6.0), (6.0, 8.0), (8.0, 10.0))):
        candidate = _source_candidate(
            f"video-position-{index}",
            ("single_subject", "subtle_motion", "face_visible"),
            media_duration_s=10.0,
            window_start_s=window[0],
            window_end_s=window[1],
        )
        candidates.append(candidate)
        if index == 3:
            outcomes.append(
                _video_result(
                    candidate,
                    disposition="REVIEW_REQUIRED",
                    reason_codes=("continuity_budget_exhausted",),
                )
            )
        else:
            outcomes.append(_video_result(candidate))

    report = build_spike_report(
        SpikeManifest(SCHEMA_VERSION, tuple(candidates)),
        outcomes,
        _ready_runtime(),
        backend_synthetic=False,
    )

    quintiles = report["position_quintiles"]
    assert len(quintiles) == 5
    assert quintiles[0]["bucket"] == "0_20"
    assert quintiles[0]["candidate_count"] == 1
    assert quintiles[3]["disposition_counts"]["REVIEW_REQUIRED"] == 1
    assert quintiles[3]["budget_exhausted_count"] == 1
    assert quintiles[4]["bucket"] == "80_100"


def test_source_batch_wall_clock_p95_is_measured_from_runtime():
    manifest, results = _full_population()
    source_candidates = [
        c for c in manifest.candidates if c.media_kind == "source_video"
    ]
    runtime = [
        {
            "kind": "source_batch_timing",
            "source_key": "a" * 64,
            "media_kind": "source_video",
            "candidate_count": 25,
            "wall_clock_ms": 60000.0,
        },
        {
            "kind": "source_batch_timing",
            "source_key": "b" * 64,
            "media_kind": "source_video",
            "candidate_count": 25,
            "wall_clock_ms": 120000.0,
        },
        {
            "kind": "source_batch_timing",
            "source_key": "c" * 64,
            "media_kind": "historical_gif",
            "candidate_count": 200,
            "wall_clock_ms": 9_999_999.0,
        },
        {
            "kind": "telemetry",
            "gpu": [{"memory_used_mib": 1000.0, "memory_total_mib": 16384.0}],
        },
    ]

    report = build_spike_report(
        SpikeManifest(SCHEMA_VERSION, tuple(source_candidates)),
        [_video_result(c) for c in source_candidates],
        runtime,
        backend_synthetic=False,
    )

    performance = report["performance"]
    assert performance["source_video_p95_ms"] == pytest.approx(120000.0)
    assert performance["candidate_p50_ms"] == pytest.approx(1000.0)
    assert performance["candidate_p95_ms"] == pytest.approx(1000.0)
    assert performance["component_sum_p50_ms"] == pytest.approx(150.0)


def test_required_scenarios_cover_all_fifteen_buckets():
    manifest, results = _full_population()
    source_candidates = [
        c for c in manifest.candidates if c.media_kind == "source_video"
    ]

    report = build_spike_report(
        SpikeManifest(SCHEMA_VERSION, tuple(source_candidates)),
        [_video_result(c) for c in source_candidates],
        _ready_runtime(),
        backend_synthetic=False,
    )

    covered = set(report["scenario_coverage"]["covered"])
    assert covered == REQUIRED_SOURCE_VIDEO_SCENARIOS
    assert len(REQUIRED_SOURCE_VIDEO_SCENARIOS) == 15


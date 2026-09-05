"""Deterministic aggregation and go/no-go gating for the continuity spike.

The report summarizes probe results without touching media or services. A
report produced here is never a qualification: synthetic data, incomplete
populations, or any threshold failure keeps the run out of the real-data
go/no-go review, and no state in this module can unlock production enforce.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from scripts.continuity_spike_contracts import (
    SCHEMA_VERSION,
    CandidateProbeResult,
    SpikeCandidate,
    SpikeManifest,
)

REPORT_SCHEMA = "continuity-spike-report-v1"

REQUIRED_SOURCE_VIDEO_SCENARIOS = frozenset(
    {
        "single_subject",
        "co_primary_subjects",
        "partial_body",
        "headless",
        "close_contact",
        "occlusion",
        "fast_motion",
        "subtle_motion",
        "moderate_motion",
        "large_motion",
        "face_visible",
        "face_occluded",
        "face_not_visible",
        "low_light",
        "media_edge",
    }
)

THRESHOLDS = {
    "candidate_p50_ms_max": 10_000.0,
    "candidate_p95_ms_max": 30_000.0,
    "source_video_p95_ms_max": 1_200_000.0,
    "peak_vram_mib_max": 14_336.0,
    "auto_pass_rate_min": 0.40,
    "review_required_rate_max": 0.60,
}

MIN_REAL_SOURCE_VIDEO_CANDIDATES = 50
MIN_REAL_HISTORICAL_GIFS = 200

INSUFFICIENT_REAL_DATA = "INSUFFICIENT_REAL_DATA"
NO_GO = "NO_GO"
READY_FOR_HUMAN_REVIEW = "READY_FOR_HUMAN_GO_NO_GO_REVIEW"

_AMPLITUDE_CLASSES = ("unobservable", "subtle", "moderate", "large")
_NOISE_REFERENCES = ("static_context", "background_residual", "combined", "unavailable")
_IDENTITY_STATUSES = ("NOT_APPLICABLE", "CONSISTENT", "CONFLICT", "UNAVAILABLE")
_FACE_PRESENCES = ("NOT_DETECTED", "DETECTED_UNUSABLE", "USABLE")
_VLM_STATUSES = ("COMPLETED", "ABSTAIN", "SKIPPED", "ERROR")
_ADVISORY_ALERTS = frozenset({"TEMPORAL_ALERT", "ABSTAIN", "UNAVAILABLE"})
_FULL_INVENTORY_ITEMS = 16_894
_POSITION_BUCKETS = ("0_20", "20_40", "40_60", "60_80", "80_100")


def nearest_rank_percentile(values: Sequence[float], percentile: float) -> float:
    """Return the nearest-rank percentile, always an observed value."""

    if not values:
        raise ValueError("cannot take a percentile of an empty sequence")
    if not 0.0 < percentile <= 1.0:
        raise ValueError("percentile must be within (0, 1]")
    ordered = sorted(values)
    rank = math.ceil(percentile * len(ordered))
    return ordered[rank - 1]


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _pct(values: list[float], percentile: float) -> float | None:
    return nearest_rank_percentile(values, percentile) if values else None


def _count_by(
    pairs: Sequence[tuple[SpikeCandidate, CandidateProbeResult]],
    attribute: str,
    order: Sequence[str],
) -> dict[str, int]:
    counts = {name: 0 for name in order}
    for _candidate, result in pairs:
        value = getattr(result, attribute)
        counts[value] = counts.get(value, 0) + 1
    return counts


def _pairs(
    manifest: SpikeManifest,
    results: Sequence[CandidateProbeResult],
) -> tuple[list[tuple[SpikeCandidate, CandidateProbeResult]], bool]:
    by_id: dict[str, CandidateProbeResult] = {}
    for result in results:
        by_id.setdefault(result.candidate_id, result)
    manifest_ids = {candidate.candidate_id for candidate in manifest.candidates}
    mismatch = (
        len(results) != len(manifest.candidates)
        or set(by_id) != manifest_ids
    )
    paired = [
        (candidate, by_id[candidate.candidate_id])
        for candidate in manifest.candidates
        if candidate.candidate_id in by_id
    ]
    return paired, mismatch


def _peak_vram_mib(runtime_snapshots: Sequence[Mapping[str, object]]) -> float | None:
    peak: float | None = None
    for snapshot in runtime_snapshots:
        gpu_entries = snapshot.get("gpu")
        if not isinstance(gpu_entries, list):
            continue
        for entry in gpu_entries:
            if not isinstance(entry, Mapping):
                continue
            used = _finite(entry.get("memory_used_mib"))
            if used is not None and (peak is None or used > peak):
                peak = used
    return peak


def _source_batch_wall_clocks(
    runtime_snapshots: Sequence[Mapping[str, object]],
) -> list[float]:
    wall_clocks: list[float] = []
    for snapshot in runtime_snapshots:
        if snapshot.get("kind") != "source_batch_timing":
            continue
        if snapshot.get("media_kind") != "source_video":
            continue
        value = _finite(snapshot.get("wall_clock_ms"))
        if value is not None and value >= 0:
            wall_clocks.append(value)
    return wall_clocks


def _population_section(
    manifest: SpikeManifest,
    pairs: Sequence[tuple[SpikeCandidate, CandidateProbeResult]],
    mismatch: bool,
) -> dict[str, object]:
    source = [(c, r) for c, r in pairs if c.media_kind == "source_video"]
    human_labels = {"ACCEPTABLE": 0, "INVALID": 0, "UNLABELED": 0}
    feedback_labels: dict[str, int] = {}
    real = 0
    synthetic = 0
    for candidate, _result in pairs:
        human_labels[candidate.human_label] = (
            human_labels.get(candidate.human_label, 0) + 1
        )
        feedback_labels[candidate.feedback_label] = (
            feedback_labels.get(candidate.feedback_label, 0) + 1
        )
        if candidate.data_origin == "real":
            real += 1
        else:
            synthetic += 1
    dispositions: dict[str, int] = {}
    for _candidate, result in pairs:
        dispositions[result.disposition] = (
            dispositions.get(result.disposition, 0) + 1
        )
    error_count = sum(1 for _c, r in source if r.disposition == "ERROR")
    return {
        "candidate_count": len(manifest.candidates),
        "result_count": len(pairs),
        "source_video_count": len(source),
        "historical_gif_count": len(pairs) - len(source),
        "real_count": real,
        "synthetic_count": synthetic,
        "human_label_counts": human_labels,
        "feedback_label_counts": feedback_labels,
        "disposition_counts": dispositions,
        "error_rate": (error_count / len(source)) if source else None,
        "candidate_result_mismatch": mismatch,
    }


def _scenario_section(
    pairs: Sequence[tuple[SpikeCandidate, CandidateProbeResult]],
) -> tuple[dict[str, object], list[str]]:
    source = [c for c, _r in pairs if c.media_kind == "source_video"]
    tag_counts: dict[str, int] = {}
    for candidate in source:
        for tag in candidate.scenario_tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    covered = frozenset(tag_counts)
    required_missing = sorted(REQUIRED_SOURCE_VIDEO_SCENARIOS - covered)
    section: dict[str, object] = {
        "covered": sorted(covered),
        "required_missing": required_missing,
        "tag_counts": dict(sorted(tag_counts.items())),
    }
    return section, required_missing


def _independence_section(
    pairs: Sequence[tuple[SpikeCandidate, CandidateProbeResult]],
) -> dict[str, object]:
    group_sets: list[list[str]] = []
    invalid: list[str] = []
    valid = 0
    for candidate, result in pairs:
        if result.disposition != "HARD_REJECT":
            continue
        groups = sorted(set(result.confirming_independence_groups))
        group_sets.append([str(group) for group in groups])
        if "scene_boundary" in groups or len(groups) >= 2:
            valid += 1
        else:
            invalid.append(candidate.candidate_id)
    return {
        "hard_reject_group_sets": group_sets,
        "invalid_hard_rejects": invalid,
        "valid_hard_reject_count": valid,
    }


def _amplitude_section(
    source_pairs: Sequence[tuple[SpikeCandidate, CandidateProbeResult]],
) -> dict[str, object]:
    class_counts = _count_by(source_pairs, "amplitude_class", _AMPLITUDE_CLASSES)
    reference_counts = _count_by(
        source_pairs, "amplitude_noise_reference", _NOISE_REFERENCES
    )
    peaks = [
        value
        for value in (
            _finite(result.normalized_amplitude_peak) for _c, result in source_pairs
        )
        if value is not None
    ]
    floors = [
        value
        for value in (
            _finite(result.normalized_amplitude_noise_floor)
            for _c, result in source_pairs
        )
        if value is not None
    ]
    coverages = [
        value
        for value in (
            _finite(result.background_coverage) for _c, result in source_pairs
        )
        if value is not None
    ]
    inliers = [
        value
        for value in (
            _finite(result.affine_inlier_ratio) for _c, result in source_pairs
        )
        if value is not None
    ]
    total = len(source_pairs)
    low_confidence = sum(
        1
        for _c, result in source_pairs
        if result.camera_compensation_status == "LOW_CONFIDENCE"
    )
    phase_incomplete = sum(
        1 for _c, result in source_pairs if result.amplitude_phase_complete is False
    )
    review = sum(
        1
        for _c, result in source_pairs
        if result.amplitude_status in ("REVIEW", "UNAVAILABLE")
    )
    return {
        "class_counts": class_counts,
        "normalized_peak_p50": _pct(peaks, 0.50),
        "normalized_peak_p95": _pct(peaks, 0.95),
        "normalized_noise_floor_p50": _pct(floors, 0.50),
        "normalized_noise_floor_p95": _pct(floors, 0.95),
        "noise_reference_counts": reference_counts,
        "background_coverage_p50": _pct(coverages, 0.50),
        "background_coverage_p95": _pct(coverages, 0.95),
        "affine_inlier_p50": _pct(inliers, 0.50),
        "affine_inlier_p95": _pct(inliers, 0.95),
        "low_confidence_compensation_rate": (low_confidence / total)
        if total
        else None,
        "phase_incomplete_rate": (phase_incomplete / total) if total else None,
        "amplitude_review_rate": (review / total) if total else None,
    }


def _face_identity_section(
    source_pairs: Sequence[tuple[SpikeCandidate, CandidateProbeResult]],
) -> dict[str, object]:
    presence = _count_by(source_pairs, "face_presence_status", _FACE_PRESENCES)
    body_counts = _count_by(source_pairs, "body_identity_status", _IDENTITY_STATUSES)
    face_counts = _count_by(source_pairs, "face_identity_status", _IDENTITY_STATUSES)
    total = len(source_pairs)
    usable = sum(
        1 for _c, result in source_pairs if result.face_presence_status == "USABLE"
    )
    marginal = [
        candidate.candidate_id
        for candidate, result in source_pairs
        if candidate.human_label == "INVALID"
        and result.body_identity_status != "CONFLICT"
        and result.face_identity_status == "CONFLICT"
        and not (
            set(result.confirming_independence_groups) - {"appearance_identity"}
        )
    ]
    return {
        "presence_counts": presence,
        "usable_rate": (usable / total) if total else None,
        "body_identity_counts": body_counts,
        "face_identity_counts": face_counts,
        "marginal_true_catch_count": len(marginal),
        "marginal_true_catch_ids": marginal,
    }


def _facial_advisory_section(
    source_pairs: Sequence[tuple[SpikeCandidate, CandidateProbeResult]],
) -> dict[str, object]:
    decode_buckets = {"missing": 0, "le_320": 0, "gt_320": 0}
    for _candidate, result in source_pairs:
        width = result.face_decode_width_px
        if width is None:
            decode_buckets["missing"] += 1
        elif width <= 320:
            decode_buckets["le_320"] += 1
        else:
            decode_buckets["gt_320"] += 1
    costs = [
        value
        for value in (
            _finite(result.face_evidence_wall_clock_ms) for _c, result in source_pairs
        )
        if value is not None
    ]
    alerts = [
        candidate.candidate_id
        for candidate, result in source_pairs
        if result.facial_landmark_status in _ADVISORY_ALERTS
        or result.facial_expression_status in _ADVISORY_ALERTS
    ]
    return {
        "decode_width_buckets": decode_buckets,
        "component_cost_p50_ms": _pct(costs, 0.50),
        "component_cost_p95_ms": _pct(costs, 0.95),
        "landmark_status_counts": _count_by(
            source_pairs,
            "facial_landmark_status",
            ("NOT_APPLICABLE", "AVAILABLE", "TEMPORAL_ALERT", "ABSTAIN", "UNAVAILABLE"),
        ),
        "expression_status_counts": _count_by(
            source_pairs,
            "facial_expression_status",
            ("NOT_APPLICABLE", "AVAILABLE", "TEMPORAL_ALERT", "ABSTAIN", "UNAVAILABLE"),
        ),
        "marginal_alert_count": len(alerts),
        "marginal_alert_ids": alerts,
    }


def _historical_bucket(
    entries: Sequence[tuple[bool, bool]],
) -> dict[str, object]:
    return {
        "total": len(entries),
        "available": sum(1 for _in_bucket, available in entries if available),
    }


def _historical_curve_section(
    historical_pairs: Sequence[tuple[SpikeCandidate, CandidateProbeResult]],
) -> tuple[dict[str, object], dict[str, int]]:
    available = sum(
        1 for _c, result in historical_pairs if result.curve_feature_status == "AVAILABLE"
    )
    unavailable = sum(
        1
        for _c, result in historical_pairs
        if result.curve_feature_status == "UNAVAILABLE"
    )
    not_run = sum(
        1 for _c, result in historical_pairs if result.curve_feature_status == "NOT_RUN"
    )
    missing_reasons: dict[str, int] = {}
    for _candidate, result in historical_pairs:
        if result.curve_feature_status == "AVAILABLE":
            continue
        reason = result.curve_feature_missing_reason or "unspecified"
        missing_reasons[reason] = missing_reasons.get(reason, 0) + 1

    resolution_buckets: dict[str, list[tuple[bool, bool]]] = {
        "resolution_le_256": [],
        "resolution_257_480": [],
        "resolution_gt_480": [],
    }
    duration_buckets: dict[str, list[tuple[bool, bool]]] = {
        "duration_lt_2s": [],
        "duration_2_5s": [],
        "duration_gt_5s": [],
    }
    subject_buckets: dict[str, list[tuple[bool, bool]]] = {
        "subject_area_lt_0.25": [],
        "subject_area_0.25_0.60": [],
        "subject_area_gt_0.60": [],
    }
    for candidate, result in historical_pairs:
        is_available = result.curve_feature_status == "AVAILABLE"
        width = result.source_media_width_px
        height = result.source_media_height_px
        if width is not None and height is not None:
            longest = float(max(width, height))
            if longest <= 256:
                resolution_buckets["resolution_le_256"].append((True, is_available))
            elif longest <= 480:
                resolution_buckets["resolution_257_480"].append((True, is_available))
            else:
                resolution_buckets["resolution_gt_480"].append((True, is_available))
        duration = candidate.duration_s
        if duration < 2.0:
            duration_buckets["duration_lt_2s"].append((True, is_available))
        elif duration <= 5.0:
            duration_buckets["duration_2_5s"].append((True, is_available))
        else:
            duration_buckets["duration_gt_5s"].append((True, is_available))
        ratio = result.primary_subject_area_ratio
        if ratio is not None:
            if ratio < 0.25:
                subject_buckets["subject_area_lt_0.25"].append((True, is_available))
            elif ratio <= 0.60:
                subject_buckets["subject_area_0.25_0.60"].append(
                    (True, is_available)
                )
            else:
                subject_buckets["subject_area_gt_0.60"].append((True, is_available))

    effective = {"favorite": 0, "like": 0, "dislike": 0}
    for candidate, result in historical_pairs:
        if (
            result.curve_feature_status == "AVAILABLE"
            and candidate.feedback_label in effective
        ):
            effective[candidate.feedback_label] += 1
    costs = [
        value
        for value in (
            _finite(result.curve_feature_wall_clock_ms)
            for _c, result in historical_pairs
        )
        if value is not None
    ]
    mean_cost = sum(costs) / len(costs) if costs else None
    bucket_totals = {
        name: len(entries)
        for name, entries in (
            *resolution_buckets.items(),
            *duration_buckets.items(),
            *subject_buckets.items(),
        )
    }
    section = {
        "total": len(historical_pairs),
        "available": available,
        "unavailable": unavailable,
        "not_run": not_run,
        "missing_reasons": missing_reasons,
        "resolution_buckets": {
            name: _historical_bucket(entries)
            for name, entries in resolution_buckets.items()
        },
        "duration_buckets": {
            name: _historical_bucket(entries)
            for name, entries in duration_buckets.items()
        },
        "subject_area_buckets": {
            name: _historical_bucket(entries)
            for name, entries in subject_buckets.items()
        },
        "effective_training_rows": effective,
        "cost_p50_ms": _pct(costs, 0.50),
        "cost_p95_ms": _pct(costs, 0.95),
        "cost_mean_ms": mean_cost,
        "projected_full_inventory_gpu_hours": (
            mean_cost * _FULL_INVENTORY_ITEMS / 3_600_000
        )
        if mean_cost is not None
        else None,
    }
    return section, bucket_totals


def _performance_section(
    source_pairs: Sequence[tuple[SpikeCandidate, CandidateProbeResult]],
    runtime_snapshots: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    candidate_times = [
        value
        for value in (_finite(result.wall_clock_ms) for _c, result in source_pairs)
        if value is not None
    ]
    component_sums = []
    for _candidate, result in source_pairs:
        component_sum = 0.0
        for cost in (result.face_evidence_wall_clock_ms, result.curve_feature_wall_clock_ms):
            value = _finite(cost)
            if value is not None:
                component_sum += value
        component_sums.append(component_sum)
    batch_times = _source_batch_wall_clocks(runtime_snapshots)
    return {
        "candidate_p50_ms": _pct(candidate_times, 0.50),
        "candidate_p95_ms": _pct(candidate_times, 0.95),
        "source_video_p95_ms": _pct(batch_times, 0.95),
        "component_sum_p50_ms": _pct(component_sums, 0.50),
        "component_sum_p95_ms": _pct(component_sums, 0.95),
    }


def _position_quintiles(
    source_pairs: Sequence[tuple[SpikeCandidate, CandidateProbeResult]],
) -> list[dict[str, object]]:
    sections: list[dict[str, object]] = []
    for index, bucket in enumerate(_POSITION_BUCKETS):
        lower = index / 5
        upper = (index + 1) / 5
        in_bucket = []
        for candidate, result in source_pairs:
            if candidate.media_duration_s <= 0:
                continue
            midpoint = (candidate.window_start_s + candidate.window_end_s) / 2
            fraction = midpoint / candidate.media_duration_s
            if int(fraction * 5) == index:
                in_bucket.append((candidate, result))
        dispositions: dict[str, int] = {}
        budget_exhausted = 0
        for _candidate, result in in_bucket:
            dispositions[result.disposition] = (
                dispositions.get(result.disposition, 0) + 1
            )
            if "continuity_budget_exhausted" in result.reason_codes:
                budget_exhausted += 1
        sections.append(
            {
                "bucket": bucket,
                "candidate_count": len(in_bucket),
                "disposition_counts": dispositions,
                "budget_exhausted_count": budget_exhausted,
            }
        )
    return sections


def _vlm_section(
    source_pairs: Sequence[tuple[SpikeCandidate, CandidateProbeResult]],
) -> dict[str, object]:
    counts = _count_by(source_pairs, "vlm_status", _VLM_STATUSES)
    total = len(source_pairs)
    abstain = counts.get("ABSTAIN", 0)
    return {
        "status_counts": counts,
        "abstain_rate": (abstain / total) if total else None,
    }


def _threshold_checks(
    performance: Mapping[str, object],
    peak_vram_mib: float | None,
    source_pairs: Sequence[tuple[SpikeCandidate, CandidateProbeResult]],
) -> dict[str, dict[str, object]]:
    acceptable = [
        (c, r)
        for c, r in source_pairs
        if c.media_kind == "source_video" and c.human_label == "ACCEPTABLE"
    ]
    labeled = [
        (c, r)
        for c, r in source_pairs
        if c.media_kind == "source_video" and c.human_label != "UNLABELED"
    ]
    auto_pass = sum(1 for _c, r in acceptable if r.disposition == "AUTO_PASS")
    review_required = sum(
        1 for _c, r in labeled if r.disposition == "REVIEW_REQUIRED"
    )
    auto_rate: float | None = (
        auto_pass / len(acceptable) if acceptable else None
    )
    review_rate: float | None = (
        review_required / len(labeled) if labeled else None
    )

    def upper(name: str, value: float | None) -> dict[str, object]:
        limit = THRESHOLDS[name]
        return {
            "value": value,
            "threshold": limit,
            "passed": value is not None and value <= limit,
        }

    def lower(name: str, value: float | None) -> dict[str, object]:
        limit = THRESHOLDS[name]
        return {
            "value": value,
            "threshold": limit,
            "passed": value is not None and value >= limit,
        }

    return {
        "candidate_p50_ms_max": upper(
            "candidate_p50_ms_max",
            _finite(performance.get("candidate_p50_ms")),
        ),
        "candidate_p95_ms_max": upper(
            "candidate_p95_ms_max",
            _finite(performance.get("candidate_p95_ms")),
        ),
        "source_video_p95_ms_max": upper(
            "source_video_p95_ms_max",
            _finite(performance.get("source_video_p95_ms")),
        ),
        "peak_vram_mib_max": upper("peak_vram_mib_max", peak_vram_mib),
        "auto_pass_rate_min": lower("auto_pass_rate_min", auto_rate),
        "review_required_rate_max": upper(
            "review_required_rate_max", review_rate
        ),
    }


def build_spike_report(
    manifest: SpikeManifest,
    results: Sequence[CandidateProbeResult],
    runtime_snapshots: Sequence[Mapping[str, object]],
    *,
    backend_synthetic: bool,
) -> dict[str, object]:
    """Aggregate validated probe results into the feasibility report."""

    if manifest.schema_version != SCHEMA_VERSION:
        raise ValueError(
            f"expected manifest schema {SCHEMA_VERSION!r}, "
            f"got {manifest.schema_version!r}"
        )
    pairs, mismatch = _pairs(manifest, results)
    source_pairs = [
        (c, r) for c, r in pairs if c.media_kind == "source_video"
    ]
    historical_pairs = [
        (c, r) for c, r in pairs if c.media_kind == "historical_gif"
    ]

    population = _population_section(manifest, pairs, mismatch)
    scenario, required_missing = _scenario_section(pairs)
    performance = _performance_section(source_pairs, runtime_snapshots)
    peak_vram = _peak_vram_mib(runtime_snapshots)
    checks = _threshold_checks(performance, peak_vram, pairs)
    curve_section, bucket_totals = _historical_curve_section(historical_pairs)

    insufficient_reasons: list[str] = []
    if backend_synthetic:
        insufficient_reasons.append("synthetic_backend")
    if population["synthetic_count"]:
        insufficient_reasons.append("synthetic_candidates")
    real_source = [
        c for c, _r in pairs if c.media_kind == "source_video" and c.data_origin == "real"
    ]
    real_historical = [
        c
        for c, _r in pairs
        if c.media_kind == "historical_gif" and c.data_origin == "real"
    ]
    if len(real_source) < MIN_REAL_SOURCE_VIDEO_CANDIDATES:
        insufficient_reasons.append("insufficient_source_video_candidates")
    if len(real_historical) < MIN_REAL_HISTORICAL_GIFS:
        insufficient_reasons.append("insufficient_historical_gifs")
    if any(
        c.media_kind == "source_video" and c.human_label == "UNLABELED"
        for c, _r in pairs
    ):
        insufficient_reasons.append("unlabeled_source_video_candidates")
    if any(
        c.media_kind == "historical_gif" and c.feedback_label == "unknown"
        for c, _r in pairs
    ):
        insufficient_reasons.append("unknown_historical_feedback")
    for tag in required_missing:
        insufficient_reasons.append(f"missing_scenario_bucket:{tag}")
    for bucket_name, total in bucket_totals.items():
        if total == 0:
            insufficient_reasons.append(f"empty_historical_bucket:{bucket_name}")
    for label in ("favorite", "like", "dislike"):
        if not any(
            c.media_kind == "historical_gif" and c.feedback_label == label
            for c, _r in pairs
        ):
            insufficient_reasons.append(f"missing_feedback_label:{label}")
    if mismatch:
        insufficient_reasons.append("candidate_result_mismatch")

    if insufficient_reasons:
        state = INSUFFICIENT_REAL_DATA
        reasons = insufficient_reasons
    else:
        failed = [
            f"threshold_failed:{name}"
            for name, check in checks.items()
            if not check["passed"]
        ]
        if failed:
            state = NO_GO
            reasons = failed
        else:
            state = READY_FOR_HUMAN_REVIEW
            reasons = []

    return {
        "schema": REPORT_SCHEMA,
        "population": population,
        "scenario_coverage": scenario,
        "evidence_independence": _independence_section(pairs),
        "motion_amplitude": _amplitude_section(source_pairs),
        "face_identity": _face_identity_section(source_pairs),
        "facial_advisory": _facial_advisory_section(source_pairs),
        "historical_curve_features": curve_section,
        "performance": performance,
        "position_quintiles": _position_quintiles(source_pairs),
        "vlm": _vlm_section(source_pairs),
        "threshold_checks": checks,
        "go_no_go": {"state": state, "reasons": reasons},
    }

"""Immutable CLI runner for the continuity feasibility spike (milestone 0A).

The runner never mutates source media, claims a unique run directory, writes
artifacts atomically without ever overwriting, and converts backend contract
violations into canonical ERROR results. ``--dry-run`` validates the manifest
and prints JSON without creating directories, loading a backend, or capturing
telemetry. A run driven by the fake backend or synthetic media is always
statistically ineligible by construction.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from scripts.continuity_spike_backends import load_backend
from scripts.continuity_spike_contracts import (
    SCHEMA_VERSION,
    CandidateProbeResult,
    ContinuitySpikeBackend,
    ContinuitySpikeError,
    ResultValidationError,
    SourceHashMismatchError,
    SourceUnavailableError,
    SpikeCandidate,
    SpikeManifest,
    load_manifest,
    make_error_result,
    validate_probe_result,
    verify_source_identity,
)
from scripts.continuity_spike_metrics import build_spike_report
from scripts.continuity_spike_runtime import (
    DEFAULT_OLLAMA_BASE_URL,
    TelemetryMode,
    capture_runtime_snapshot,
    http_get_with_timeout,
)


@dataclass(frozen=True)
class CliRunResult:
    dry_run: bool
    run_dir: Path | None
    report_path: Path | None
    source_hashes: tuple[str, ...]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="continuity_spike",
        description=(
            "Read-only continuity feasibility spike runner (milestone 0A); "
            "never qualifies production enforce."
        ),
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--backend", default="fake")
    parser.add_argument(
        "--telemetry",
        choices=["off", "ollama", "gpu", "all"],
        default="off",
    )
    parser.add_argument("--ollama-base-url", default=DEFAULT_OLLAMA_BASE_URL)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print JSON without any filesystem or service effect",
    )
    return parser


def _claim_run_dir(parent: Path) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    while True:
        candidate = parent / f"run-{uuid.uuid4()}"
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return candidate


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    if path.exists():
        raise RuntimeError(f"refusing to overwrite existing artifact: {path}")
    text = json.dumps(payload, sort_keys=True, allow_nan=False, indent=2)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=path.parent,
        prefix=f"{path.name}.",
        suffix=".tmp",
    )
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise RuntimeError(f"refusing to overwrite existing artifact: {path}")
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def _candidate_snapshot(candidate: SpikeCandidate) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "candidate_id": candidate.candidate_id,
        "media_kind": candidate.media_kind,
        "source_path": str(candidate.source_path.resolve()),
        "source_sha256": candidate.source_sha256,
        "media_duration_s": candidate.media_duration_s,
        "confirmed_shot_start_s": candidate.confirmed_shot_start_s,
        "context_start_s": candidate.context_start_s,
        "window_start_s": candidate.window_start_s,
        "window_end_s": candidate.window_end_s,
        "context_end_s": candidate.context_end_s,
        "confirmed_shot_end_s": candidate.confirmed_shot_end_s,
        "scenario_tags": list(candidate.scenario_tags),
        "data_origin": candidate.data_origin,
        "human_label": candidate.human_label,
        "feedback_label": candidate.feedback_label,
        "duration_s": candidate.duration_s,
    }
    return snapshot


def _dry_run_summary(
    manifest: SpikeManifest,
    source_hashes: Sequence[str],
) -> dict[str, object]:
    human_labels: dict[str, int] = {}
    feedback_labels: dict[str, int] = {}
    scenario_counts: dict[str, int] = {}
    duration_buckets = {"lt_2s": 0, "2_5s": 0, "gt_5s": 0}
    truncation = {
        "start_shot_boundary": 0,
        "end_shot_boundary": 0,
        "start_media_edge": 0,
        "end_media_edge": 0,
    }
    source_video = 0
    historical_gif = 0
    real = 0
    synthetic = 0
    for candidate in manifest.candidates:
        if candidate.media_kind == "source_video":
            source_video += 1
        else:
            historical_gif += 1
        if candidate.data_origin == "real":
            real += 1
        else:
            synthetic += 1
        human_labels[candidate.human_label] = (
            human_labels.get(candidate.human_label, 0) + 1
        )
        feedback_labels[candidate.feedback_label] = (
            feedback_labels.get(candidate.feedback_label, 0) + 1
        )
        for tag in candidate.scenario_tags:
            scenario_counts[tag] = scenario_counts.get(tag, 0) + 1
        duration = candidate.duration_s
        if duration < 2.0:
            duration_buckets["lt_2s"] += 1
        elif duration <= 5.0:
            duration_buckets["2_5s"] += 1
        else:
            duration_buckets["gt_5s"] += 1
        if candidate.media_kind == "source_video":
            if (
                candidate.confirmed_shot_start_s > 0
                and candidate.context_start_s == candidate.confirmed_shot_start_s
            ):
                truncation["start_shot_boundary"] += 1
            if (
                candidate.confirmed_shot_start_s == 0.0
                and candidate.window_start_s > 0.0
            ):
                truncation["start_media_edge"] += 1
            if (
                candidate.confirmed_shot_end_s < candidate.media_duration_s
                and candidate.context_end_s == candidate.confirmed_shot_end_s
            ):
                truncation["end_shot_boundary"] += 1
            if (
                candidate.confirmed_shot_end_s == candidate.media_duration_s
                and candidate.window_end_s < candidate.confirmed_shot_end_s
            ):
                truncation["end_media_edge"] += 1

    return {
        "valid": True,
        "schema_version": SCHEMA_VERSION,
        "source_video_count": source_video,
        "historical_gif_count": historical_gif,
        "real_count": real,
        "synthetic_count": synthetic,
        "human_label_counts": dict(sorted(human_labels.items())),
        "feedback_label_counts": dict(sorted(feedback_labels.items())),
        "scenario_counts": dict(sorted(scenario_counts.items())),
        "shot_context_truncation": truncation,
        "duration_buckets": duration_buckets,
        "source_hashes": list(source_hashes),
        "resolution_strata": "NOT_MEASURED",
        "subject_size_strata": "NOT_MEASURED",
    }


def _group_by_source(
    candidates: Sequence[SpikeCandidate],
) -> list[tuple[tuple[str, str, str], list[SpikeCandidate]]]:
    groups: dict[tuple[str, str, str], list[SpikeCandidate]] = {}
    for candidate in candidates:
        key = (
            str(candidate.source_path),
            candidate.source_sha256,
            candidate.media_kind,
        )
        groups.setdefault(key, []).append(candidate)
    return list(groups.items())


def _apply_hard_policy(
    candidate: SpikeCandidate,
    result: CandidateProbeResult,
) -> CandidateProbeResult:
    """Apply only hard disposition consequences; advisory fields stay as-is."""

    if candidate.media_kind != "source_video":
        return result
    if result.disposition != "AUTO_PASS":
        return result
    needs_review = (
        result.amplitude_status in ("REVIEW", "UNAVAILABLE")
        or result.body_identity_status == "CONFLICT"
        or result.face_identity_status == "CONFLICT"
    )
    if not needs_review:
        return result
    return dataclasses.replace(result, disposition="REVIEW_REQUIRED")


def _validate_or_error(
    candidate: SpikeCandidate,
    result: CandidateProbeResult,
    backend_id: str,
) -> CandidateProbeResult:
    try:
        validate_probe_result(candidate, result)
    except ResultValidationError:
        return make_error_result(
            candidate,
            backend_id=backend_id,
            reason_code="backend_contract_invalid",
            wall_clock_ms=result.wall_clock_ms,
        )
    return result


def _error_results_for_group(
    group: Sequence[SpikeCandidate],
    backend_id: str,
    reason_code: str,
    wall_clock_ms: float,
) -> list[CandidateProbeResult]:
    per_candidate = wall_clock_ms / len(group) if group else wall_clock_ms
    return [
        make_error_result(
            candidate,
            backend_id=backend_id,
            reason_code=reason_code,
            wall_clock_ms=max(per_candidate, 1e-9),
        )
        for candidate in group
    ]


def _evaluate_source_group(
    group: Sequence[SpikeCandidate],
    backend: ContinuitySpikeBackend,
    runtime_entries: list[dict[str, object]],
) -> list[CandidateProbeResult]:
    group_results: list[CandidateProbeResult] | None = None
    try:
        verify_source_identity(group[0])
    except SourceUnavailableError:
        return _error_results_for_group(
            group, backend.backend_id, "source_unavailable", 0.0
        )
    except SourceHashMismatchError:
        return _error_results_for_group(
            group, backend.backend_id, "source_hash_mismatch", 0.0
        )

    start_ns = time.perf_counter_ns()
    failure_code: str | None = None
    raw_results: tuple[CandidateProbeResult, ...] | None = None
    try:
        raw_results = backend.evaluate_source(tuple(group))
    except Exception:
        failure_code = "backend_error"
    elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
    runtime_entries.append(
        {
            "kind": "source_batch_timing",
            "source_key": group[0].source_sha256,
            "media_kind": group[0].media_kind,
            "candidate_count": len(group),
            "wall_clock_ms": elapsed_ms,
        }
    )

    if failure_code is not None:
        return _error_results_for_group(
            group, backend.backend_id, failure_code, elapsed_ms
        )
    assert raw_results is not None
    if [result.candidate_id for result in raw_results] != [
        candidate.candidate_id for candidate in group
    ]:
        return _error_results_for_group(
            group, backend.backend_id, "backend_contract_invalid", elapsed_ms
        )
    group_results = [
        _validate_or_error(
            candidate,
            _apply_hard_policy(candidate, result),
            backend.backend_id,
        )
        for candidate, result in zip(group, raw_results)
    ]

    try:
        verify_source_identity(group[0])
    except SourceHashMismatchError:
        return _error_results_for_group(
            group, backend.backend_id, "source_hash_mismatch", elapsed_ms
        )
    except SourceUnavailableError:
        return _error_results_for_group(
            group, backend.backend_id, "source_unavailable", elapsed_ms
        )
    assert group_results is not None
    return group_results


def run(argv: Sequence[str] | None = None) -> CliRunResult:
    parsed = _build_parser().parse_args(argv)
    invocation_argv = list(sys.argv[1:]) if argv is None else list(argv)

    manifest = load_manifest(parsed.manifest)

    source_hashes: list[str] = []
    seen_paths: set[Path] = set()
    for candidate in manifest.candidates:
        if candidate.source_path in seen_paths:
            continue
        seen_paths.add(candidate.source_path)
        source_hashes.append(verify_source_identity(candidate))

    if parsed.dry_run:
        print(
            json.dumps(
                _dry_run_summary(manifest, source_hashes),
                sort_keys=True,
                allow_nan=False,
            )
        )
        return CliRunResult(
            dry_run=True,
            run_dir=None,
            report_path=None,
            source_hashes=tuple(source_hashes),
        )

    backend = load_backend(parsed.backend)
    output_dir = parsed.output_dir.resolve()
    run_dir = _claim_run_dir(output_dir)
    results_dir = run_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    _atomic_json(
        run_dir / "manifest.snapshot.json",
        {
            "kind": "continuity-spike-manifest-snapshot-v1",
            "schema_version": SCHEMA_VERSION,
            "candidates": [
                _candidate_snapshot(candidate)
                for candidate in manifest.candidates
            ],
            "backend": {
                "id": backend.backend_id,
                "synthetic": backend.synthetic,
            },
            "python_version": sys.version,
            "platform": platform.platform(),
            "argv": invocation_argv,
        },
    )

    runtime_entries: list[dict[str, object]] = []
    all_results: list[CandidateProbeResult] = []
    telemetry_mode: TelemetryMode = parsed.telemetry

    for _key, group in _group_by_source(manifest.candidates):
        capture_runtime_snapshot(
            phase="before_source",
            source_key=group[0].source_sha256,
            telemetry_mode=telemetry_mode,
            ollama_base_url=parsed.ollama_base_url,
            get=http_get_with_timeout,
            run=subprocess.run,
        )
        group_results = _evaluate_source_group(group, backend, runtime_entries)
        capture_runtime_snapshot(
            phase="after_source",
            source_key=group[0].source_sha256,
            telemetry_mode=telemetry_mode,
            ollama_base_url=parsed.ollama_base_url,
            get=http_get_with_timeout,
            run=subprocess.run,
        )
        for candidate, result in zip(group, group_results):
            _atomic_json(
                results_dir / f"{candidate.candidate_id}.json",
                dict(dataclasses.asdict(result)),
            )
        all_results.extend(group_results)

    _atomic_json(
        run_dir / "runtime.json",
        {
            "kind": "continuity-spike-runtime-v1",
            "entries": runtime_entries,
        },
    )
    report = build_spike_report(
        manifest,
        all_results,
        runtime_entries,
        backend_synthetic=backend.synthetic,
    )
    report_path = run_dir / "report.json"
    _atomic_json(report_path, report)

    go_no_go = report["go_no_go"]
    assert isinstance(go_no_go, dict)
    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "go_no_go_state": go_no_go["state"],
            },
            sort_keys=True,
            allow_nan=False,
        )
    )
    return CliRunResult(
        dry_run=False,
        run_dir=run_dir,
        report_path=report_path,
        source_hashes=tuple(source_hashes),
    )


def main() -> int:
    try:
        run()
    except ContinuitySpikeError as exc:
        print(f"continuity spike failed: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"continuity spike failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

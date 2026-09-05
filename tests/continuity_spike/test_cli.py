"""End-to-end CLI tests over generated videos; no live services are touched."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

import scripts.continuity_spike as spike_cli
from scripts.continuity_spike import run as cli_run
from scripts.continuity_spike_backends import DeterministicFakeBackend

from tests.action_media_fixtures import write_start_move_settle_video

SCHEMA_VERSION = "continuity-spike-input-v1"
MEDIA_DURATION_S = 8.125  # 65 frames at the fixture's 8 fps


class StubBackend:
    """Deterministic stub that delegates to the fake and applies overrides."""

    backend_id: str = "stub-v1"
    synthetic: bool = True

    def __init__(self, **overrides: object) -> None:
        self._overrides = overrides

    def evaluate_source(self, candidates: tuple) -> tuple:
        base = DeterministicFakeBackend().evaluate_source(candidates)
        if not self._overrides:
            return base
        return tuple(
            dataclasses.replace(result, **self._overrides)  # type: ignore[arg-type]
            for result in base
        )


class RecordingBackend:
    backend_id: str = "recording-v1"
    synthetic: bool = True

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def evaluate_source(self, candidates: tuple) -> tuple:
        self.calls.append([candidate.candidate_id for candidate in candidates])
        return DeterministicFakeBackend().evaluate_source(candidates)


class MutatingBackend:
    backend_id: str = "mutating-v1"
    synthetic: bool = True

    def evaluate_source(self, candidates: tuple) -> tuple:
        candidates[0].source_path.write_bytes(b"mutated-during-batch")
        return DeterministicFakeBackend().evaluate_source(candidates)


class FlakyBackend:
    backend_id: str = "flaky-v1"
    synthetic: bool = True

    def evaluate_source(self, candidates: tuple) -> tuple:
        if candidates[0].candidate_id.startswith("boom"):
            raise RuntimeError("synthetic backend failure")
        return DeterministicFakeBackend().evaluate_source(candidates)


def _write_source(tmp_path: Path, index: int = 0) -> Path:
    source = tmp_path / f"source-{index}.mp4"
    write_start_move_settle_video(source)
    return source


def write_synthetic_manifest(
    tmp_path: Path,
    *,
    source_index: int = 0,
    candidate_count: int = 1,
) -> tuple[Path, Path, Path]:
    source = _write_source(tmp_path, source_index)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    candidates = []
    for offset in range(candidate_count):
        candidates.append(
            {
                "candidate_id": f"synthetic-{source_index:03d}-{offset:02d}",
                "media_kind": "source_video",
                "source_path": str(source.resolve()),
                "source_sha256": digest,
                "media_duration_s": MEDIA_DURATION_S,
                "confirmed_shot_start_s": 0.0,
                "context_start_s": 0.0,
                "window_start_s": 2.0,
                "window_end_s": 5.0,
                "context_end_s": 6.0,
                "confirmed_shot_end_s": 8.0,
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
        )
    manifest_path = tmp_path / f"manifest-{source_index}.json"
    manifest_path.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "candidates": candidates}),
        encoding="utf-8",
    )
    return manifest_path, tmp_path / "output", source


def test_dry_run_validates_without_side_effects(tmp_path, monkeypatch, capsys):
    manifest_path, output_dir, source = write_synthetic_manifest(tmp_path)
    monkeypatch.setattr(
        spike_cli, "load_backend", lambda spec: pytest.fail("backend loaded")
    )
    monkeypatch.setattr(
        spike_cli,
        "capture_runtime_snapshot",
        lambda **kwargs: pytest.fail("telemetry called"),
    )

    result = cli_run(
        [
            "--manifest",
            str(manifest_path),
            "--output-dir",
            str(output_dir),
            "--backend",
            "fake",
            "--dry-run",
        ]
    )

    assert result.dry_run is True
    assert result.run_dir is None
    assert result.report_path is None
    assert not output_dir.exists()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == result.source_hashes[0]
    summary = json.loads(capsys.readouterr().out)
    assert summary["valid"] is True
    assert summary["schema_version"] == SCHEMA_VERSION
    assert summary["source_video_count"] == 1
    assert summary["historical_gif_count"] == 0
    assert summary["synthetic_count"] == 1
    assert summary["resolution_strata"] == "NOT_MEASURED"
    assert summary["subject_size_strata"] == "NOT_MEASURED"


def test_dry_run_reports_truncation_and_duration_buckets(tmp_path, capsys):
    manifest_path, _output_dir, _source = write_synthetic_manifest(tmp_path)

    cli_run(
        [
            "--manifest",
            str(manifest_path),
            "--output-dir",
            str(tmp_path / "output"),
            "--dry-run",
        ]
    )

    summary = json.loads(capsys.readouterr().out)
    assert summary["duration_buckets"]["2_5s"] == 1
    assert summary["shot_context_truncation"]["start_media_edge"] == 1


def test_fake_run_is_immutable_and_statistically_ineligible(tmp_path):
    manifest_path, output_dir, source = write_synthetic_manifest(tmp_path)
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    result = cli_run(
        [
            "--manifest",
            str(manifest_path),
            "--output-dir",
            str(output_dir),
            "--backend",
            "fake",
            "--telemetry",
            "off",
        ]
    )

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert result.dry_run is False
    assert result.run_dir is not None
    assert result.run_dir.parent == output_dir.resolve()
    assert result.report_path == result.run_dir / "report.json"
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["go_no_go"]["state"] == "INSUFFICIENT_REAL_DATA"
    assert "synthetic_backend" in report["go_no_go"]["reasons"]
    assert (result.run_dir / "manifest.snapshot.json").is_file()
    assert (result.run_dir / "runtime.json").is_file()
    assert (result.run_dir / "results" / "synthetic-000-00.json").is_file()


def test_two_runs_claim_different_directories(tmp_path):
    manifest_path, output_dir, _source = write_synthetic_manifest(tmp_path)
    argv = [
        "--manifest",
        str(manifest_path),
        "--output-dir",
        str(output_dir),
        "--telemetry",
        "off",
    ]

    first = cli_run(argv)
    second = cli_run(argv)

    assert first.run_dir != second.run_dir
    assert first.run_dir.is_dir() and second.run_dir.is_dir()


@pytest.mark.parametrize(
    "argv_extra",
    [
        ["--telemetry", "sometimes"],
        ["--bogus-flag"],
    ],
)
def test_unknown_cli_values_fail_before_writes(tmp_path, argv_extra):
    manifest_path, output_dir, _source = write_synthetic_manifest(tmp_path)

    with pytest.raises(SystemExit):
        cli_run(
            [
                "--manifest",
                str(manifest_path),
                "--output-dir",
                str(output_dir),
                *argv_extra,
            ]
        )

    assert not output_dir.exists()


def test_candidates_grouped_by_source_for_one_backend_call(tmp_path, monkeypatch):
    manifest_a, output_dir, _source_a = write_synthetic_manifest(
        tmp_path, source_index=0, candidate_count=2
    )
    manifest_b, _output_b, _source_b = write_synthetic_manifest(
        tmp_path, source_index=1
    )
    combined = tmp_path / "manifest-combined.json"
    combined_payload = {
        "schema_version": SCHEMA_VERSION,
        "candidates": [
            *json.loads(manifest_a.read_text(encoding="utf-8"))["candidates"],
            *json.loads(manifest_b.read_text(encoding="utf-8"))["candidates"],
        ],
    }
    combined.write_text(json.dumps(combined_payload), encoding="utf-8")

    recorder = RecordingBackend()
    monkeypatch.setattr(spike_cli, "load_backend", lambda spec: recorder)

    result = cli_run(
        [
            "--manifest",
            str(combined),
            "--output-dir",
            str(output_dir),
            "--telemetry",
            "off",
        ]
    )

    assert recorder.calls == [
        ["synthetic-000-00", "synthetic-000-01"],
        ["synthetic-001-00"],
    ]
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["population"]["candidate_count"] == 3


def test_source_mutation_during_batch_produces_hash_mismatch_errors(tmp_path, monkeypatch):
    manifest_path, output_dir, _source = write_synthetic_manifest(tmp_path)
    monkeypatch.setattr(spike_cli, "load_backend", lambda spec: MutatingBackend())

    result = cli_run(
        [
            "--manifest",
            str(manifest_path),
            "--output-dir",
            str(output_dir),
            "--telemetry",
            "off",
        ]
    )

    result_payload = json.loads(
        (result.run_dir / "results" / "synthetic-000-00.json").read_text(
            encoding="utf-8"
        )
    )
    assert result_payload["disposition"] == "ERROR"
    assert result_payload["reason_codes"] == ["source_hash_mismatch"]


def test_batch_exception_does_not_abort_later_sources(tmp_path, monkeypatch):
    manifest_a, output_dir, _source_a = write_synthetic_manifest(
        tmp_path, source_index=0
    )
    manifest_b, _output_b, _source_b = write_synthetic_manifest(
        tmp_path, source_index=1
    )
    renamed = {
        json.loads(manifest_b.read_text(encoding="utf-8"))["candidates"][0][
            "candidate_id"
        ]
    }
    combined = tmp_path / "manifest-combined.json"
    combined_payload = {
        "schema_version": SCHEMA_VERSION,
        "candidates": [
            *json.loads(manifest_a.read_text(encoding="utf-8"))["candidates"],
            *json.loads(manifest_b.read_text(encoding="utf-8"))["candidates"],
        ],
    }
    combined_payload["candidates"][0]["candidate_id"] = "boom-000-00"
    combined.write_text(json.dumps(combined_payload), encoding="utf-8")
    monkeypatch.setattr(spike_cli, "load_backend", lambda spec: FlakyBackend())

    result = cli_run(
        [
            "--manifest",
            str(combined),
            "--output-dir",
            str(output_dir),
            "--telemetry",
            "off",
        ]
    )

    errored = json.loads(
        (result.run_dir / "results" / "boom-000-00.json").read_text(encoding="utf-8")
    )
    assert errored["disposition"] == "ERROR"
    assert errored["reason_codes"] == ["backend_error"]
    ok_id = next(iter(renamed))
    ok = json.loads(
        (result.run_dir / "results" / f"{ok_id}.json").read_text(encoding="utf-8")
    )
    assert ok["disposition"] == "AUTO_PASS"


def test_amplitude_unavailable_normalizes_auto_pass_to_review(tmp_path, monkeypatch):
    manifest_path, output_dir, _source = write_synthetic_manifest(tmp_path)
    monkeypatch.setattr(
        spike_cli,
        "load_backend",
        lambda spec: StubBackend(
            amplitude_status="UNAVAILABLE",
            amplitude_class="unobservable",
            normalized_amplitude_peak=None,
            normalized_amplitude_noise_floor=None,
            amplitude_noise_reference="unavailable",
            background_coverage=None,
            affine_inlier_ratio=None,
            camera_compensation_status="NOT_APPLICABLE",
            amplitude_phase_complete=None,
        ),
    )

    result = cli_run(
        [
            "--manifest",
            str(manifest_path),
            "--output-dir",
            str(output_dir),
            "--telemetry",
            "off",
        ]
    )

    payload = json.loads(
        (result.run_dir / "results" / "synthetic-000-00.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["disposition"] == "REVIEW_REQUIRED"
    assert payload["amplitude_status"] == "UNAVAILABLE"


@pytest.mark.parametrize(
    "overrides",
    [
        {"body_identity_status": "CONFLICT"},
        {"face_identity_status": "CONFLICT"},
    ],
)
def test_identity_conflict_normalizes_auto_pass_to_review(
    tmp_path, monkeypatch, overrides
):
    manifest_path, output_dir, _source = write_synthetic_manifest(tmp_path)
    monkeypatch.setattr(
        spike_cli, "load_backend", lambda spec: StubBackend(**overrides)
    )

    result = cli_run(
        [
            "--manifest",
            str(manifest_path),
            "--output-dir",
            str(output_dir),
            "--telemetry",
            "off",
        ]
    )

    payload = json.loads(
        (result.run_dir / "results" / "synthetic-000-00.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["disposition"] == "REVIEW_REQUIRED"


def test_advisory_expression_abstain_keeps_auto_pass(tmp_path, monkeypatch):
    manifest_path, output_dir, _source = write_synthetic_manifest(tmp_path)
    monkeypatch.setattr(
        spike_cli,
        "load_backend",
        lambda spec: StubBackend(facial_expression_status="ABSTAIN"),
    )

    result = cli_run(
        [
            "--manifest",
            str(manifest_path),
            "--output-dir",
            str(output_dir),
            "--telemetry",
            "off",
        ]
    )

    payload = json.loads(
        (result.run_dir / "results" / "synthetic-000-00.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["disposition"] == "AUTO_PASS"
    assert payload["facial_expression_status"] == "ABSTAIN"


def test_same_group_hard_reject_becomes_backend_contract_invalid(
    tmp_path, monkeypatch
):
    manifest_path, output_dir, _source = write_synthetic_manifest(tmp_path)
    monkeypatch.setattr(
        spike_cli,
        "load_backend",
        lambda spec: StubBackend(
            disposition="HARD_REJECT",
            confirming_independence_groups=("pose_geometry",),
            reason_codes=("joint_motion_jump",),
        ),
    )

    result = cli_run(
        [
            "--manifest",
            str(manifest_path),
            "--output-dir",
            str(output_dir),
            "--telemetry",
            "off",
        ]
    )

    payload = json.loads(
        (result.run_dir / "results" / "synthetic-000-00.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["disposition"] == "ERROR"
    assert payload["reason_codes"] == ["backend_contract_invalid"]


def test_runtime_json_contains_measured_source_batch_timing(tmp_path):
    manifest_path, output_dir, _source = write_synthetic_manifest(tmp_path)

    result = cli_run(
        [
            "--manifest",
            str(manifest_path),
            "--output-dir",
            str(output_dir),
            "--telemetry",
            "off",
        ]
    )

    runtime = json.loads(
        (result.run_dir / "runtime.json").read_text(encoding="utf-8")
    )
    timings = [
        entry
        for entry in runtime["entries"]
        if entry["kind"] == "source_batch_timing"
    ]
    assert len(timings) == 1
    assert timings[0]["candidate_count"] == 1
    assert timings[0]["media_kind"] == "source_video"
    assert timings[0]["wall_clock_ms"] >= 0.0
    assert timings[0]["source_key"] == result.source_hashes[0]


def test_runtime_json_persists_telemetry_snapshots(tmp_path):
    manifest_path, output_dir, _source = write_synthetic_manifest(tmp_path)

    result = cli_run(
        [
            "--manifest",
            str(manifest_path),
            "--output-dir",
            str(output_dir),
            "--telemetry",
            "off",
        ]
    )

    runtime = json.loads(
        (result.run_dir / "runtime.json").read_text(encoding="utf-8")
    )
    snapshots = [
        entry for entry in runtime["entries"] if entry["kind"] == "telemetry"
    ]
    assert [snapshot["phase"] for snapshot in snapshots] == [
        "before_source",
        "after_source",
    ]
    assert all(snapshot["requested_mode"] == "off" for snapshot in snapshots)
    assert all(snapshot["gpu"] is None for snapshot in snapshots)


def test_artifacts_contain_no_nan_or_infinity(tmp_path):
    manifest_path, output_dir, _source = write_synthetic_manifest(tmp_path)

    result = cli_run(
        [
            "--manifest",
            str(manifest_path),
            "--output-dir",
            str(output_dir),
            "--telemetry",
            "off",
        ]
    )

    artifacts = sorted(result.run_dir.rglob("*.json"))
    assert artifacts
    for artifact in artifacts:
        text = artifact.read_text(encoding="utf-8")
        assert "NaN" not in text
        assert "Infinity" not in text

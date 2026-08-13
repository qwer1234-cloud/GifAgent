from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
import yaml

from app.quality_moe.models import EvidenceStatus
from app.quality_moe.sampling import SampledClip


def _video(path: Path, *, seconds: float = 2.0, fps: int = 8) -> Path:
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (96, 54)
    )
    assert writer.isOpened()
    for index in range(max(1, round(seconds * fps))):
        frame = np.full((54, 96, 3), 50 + index, dtype=np.uint8)
        writer.write(frame)
    writer.release()
    return path


def _config(path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "vlm": {
                    "provider": "ollama",
                    "model": "llava:13b",
                    "base_url": "http://127.0.0.1:11434",
                    "manage_lifecycle": False,
                    "launch_mode": "none",
                },
                "quality_moe": {
                    "report_only": True,
                    "judge": {
                        "model_id": "llava:13b",
                        "base_url": "inherit_vlm",
                        "temperature": 0,
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fake_assessment(candidate_id: str = "cli-smoke") -> SimpleNamespace:
    return SimpleNamespace(
        to_dict=lambda: {
            "candidate_id": candidate_id,
            "recommended_decision": "KEEP_AS_IS",
            "effective_decision": "KEEP_AS_IS",
            "confidence": 0.9,
            "repair": None,
            "evidence": [],
            "provenance": {},
        }
    )


def test_script_entrypoint_imports_app_when_launched_by_path(tmp_path: Path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "evaluate_quality_moe.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--help"], cwd=tmp_path,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert completed.returncode == 0, completed.stderr
    assert "--skip-judge" in completed.stdout


def test_cli_sampling_keeps_requested_bounds_when_codec_seeks_past_end(monkeypatch: pytest.MonkeyPatch):
    from scripts import evaluate_quality_moe as cli

    calls: list[tuple[float, float]] = []

    def endpoint_sensitive_sampler(video_path, start, end, candidate_id):
        calls.append((start, end))
        if end == 12.0:
            return SampledClip(
                candidate_id, video_path, start, end,
                status=EvidenceStatus.UNAVAILABLE,
                diagnostics={"code": "decoded_timestamp_outside_interval"},
            )
        frames = tuple(np.full((20, 30, 3), 80, np.uint8) for _ in range(6))
        timestamps = tuple(np.linspace(start + 0.01, end, 6))
        return SampledClip(candidate_id, video_path, start, end, timestamps, frames)

    monkeypatch.setattr(cli, "sample_clip_frames", endpoint_sensitive_sampler)
    sampled = cli._sample_exact_interval(Path("movie.mp4"), 0.0, 12.0, "candidate")

    assert calls == [(0.0, 12.0), (0.0, 11.95)]
    assert sampled.start_ts == 0.0
    assert sampled.end_ts == 12.0
    assert max(sampled.timestamps) < sampled.end_ts


def test_cli_evaluates_only_explicit_file_and_preserves_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from scripts import evaluate_quality_moe as cli

    source = _video(tmp_path / "chosen.avi")
    (tmp_path / "unrelated-broken.mp4").write_bytes(b"not video")
    config = _config(tmp_path / "models.yaml")
    before = _sha256(source)
    observed: dict[str, object] = {}

    def fake_evaluate(candidate, **kwargs):
        observed.update(candidate)
        # Skip mode freezes an explicit non-network sentinel before evaluation.
        assert kwargs["config"].judge["base_url"] == "skipped"
        return _fake_assessment(candidate["candidate_id"])

    monkeypatch.setattr(cli, "evaluate_candidate", fake_evaluate)
    result = cli.run(
        [
            "--video", str(source), "--duration", "1",
            "--config", str(config), "--output-dir", str(tmp_path / "out"),
            "--skip-judge",
        ]
    )

    assert result.output_dir.parent == (tmp_path / "out").resolve()
    assert result.output_dir.name.startswith("run-")
    assert observed["video_path"] == str(source.resolve())
    assert _sha256(source) == before
    payload = json.loads(result.assessment_path.read_text(encoding="utf-8"))
    assert payload["source"]["sha256_before"] == before
    assert payload["source"]["sha256_after"] == before
    assert payload["judge_execution"]["status"] == "SKIPPED"
    assert payload["run"]["requested_output_dir"] == str((tmp_path / "out").resolve())
    assert payload["run"]["actual_output_dir"] == str(result.output_dir)
    assert Path(payload["artifacts"]["original_contact_sheet"]).is_file()
    assert payload["artifacts"]["best_contact_sheet"] is None


def test_cli_clamps_out_of_range_request_to_centered_interval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from scripts import evaluate_quality_moe as cli

    source = _video(tmp_path / "short.avi", seconds=4.0)
    config = _config(tmp_path / "models.yaml")
    monkeypatch.setattr(cli, "evaluate_candidate", lambda *_args, **_kwargs: _fake_assessment())

    result = cli.run(
        ["--video", str(source), "--start", "1800", "--duration", "1",
         "--config", str(config), "--output-dir", str(tmp_path / "out"), "--skip-judge"]
    )
    payload = json.loads(result.assessment_path.read_text(encoding="utf-8"))
    interval = payload["interval"]
    assert interval["requested"] == {"start": 1800.0, "duration": 1.0}
    assert interval["resolved"]["duration"] == pytest.approx(1.0, abs=0.05)
    assert interval["resolved"]["start"] == pytest.approx(
        (interval["media_duration"] - 1.0) / 2.0, abs=0.05
    )
    assert interval["clamped"] is True


def test_cli_validates_source_and_path_conflicts_before_creating_output(tmp_path: Path):
    from scripts import evaluate_quality_moe as cli

    config = _config(tmp_path / "models.yaml")
    missing_output = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="video must be an existing regular file"):
        cli.run([
            "--video", str(tmp_path / "missing.mp4"), "--config", str(config),
            "--output-dir", str(missing_output), "--skip-judge",
        ])
    assert not missing_output.exists()

    source = _video(tmp_path / "source.avi")
    with pytest.raises(ValueError, match="output directory conflicts with source video"):
        cli.run([
            "--video", str(source), "--config", str(config),
            "--output-dir", str(source), "--skip-judge",
        ])
    assert source.is_file()


def test_skip_judge_never_constructs_external_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from scripts import evaluate_quality_moe as cli

    source = _video(tmp_path / "source.avi")
    config = _config(tmp_path / "models.yaml")
    monkeypatch.setattr(cli, "OllamaQualityJudge", lambda *_args, **_kwargs: pytest.fail("judge constructed"))
    monkeypatch.setattr(cli, "evaluate_candidate", lambda *_args, **_kwargs: _fake_assessment())

    cli.run([
        "--video", str(source), "--duration", "1", "--config", str(config),
        "--output-dir", str(tmp_path / "out"), "--skip-judge",
    ])


def test_skip_judge_does_not_resolve_auto_endpoint_or_touch_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from scripts import evaluate_quality_moe as cli

    config = tmp_path / "auto.yaml"
    config.write_text(yaml.safe_dump({
        "vlm": {
            "provider": "ollama", "model": "llava:13b", "base_url": "auto",
            "manage_lifecycle": True, "launch_mode": "wsl",
        },
        "quality_moe": {
            "judge": {"model_id": "llava:13b", "base_url": "auto", "temperature": 0},
        },
    }), encoding="utf-8")
    monkeypatch.setattr(
        cli, "OllamaRuntimeManager",
        lambda: pytest.fail("skip-judge must not discover or manage Ollama"),
    )

    frozen, _snapshot = cli._load_and_freeze_config(config, skip_judge=True)

    assert frozen.judge["base_url"] == "skipped"


def test_default_config_is_repository_absolute_from_unrelated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from scripts import evaluate_quality_moe as cli

    monkeypatch.chdir(tmp_path)
    parsed = cli._parser().parse_args(["--video", "movie.mp4", "--output-dir", "out"])

    assert Path(parsed.config).is_absolute()
    assert Path(parsed.config) == cli.REPOSITORY_ROOT / "configs" / "models.yaml"
    frozen, _snapshot = cli._load_and_freeze_config(Path(parsed.config), skip_judge=True)
    assert frozen.judge["base_url"] == "skipped"


def test_assessment_json_is_atomically_published_and_existing_run_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from scripts import evaluate_quality_moe as cli

    source = _video(tmp_path / "source.avi")
    config = _config(tmp_path / "models.yaml")
    output = tmp_path / "out"
    monkeypatch.setattr(cli, "evaluate_candidate", lambda *_args, **_kwargs: _fake_assessment())
    first = cli.run([
        "--video", str(source), "--duration", "1", "--config", str(config),
        "--output-dir", str(output), "--skip-judge",
    ])
    first_bytes = first.assessment_path.read_bytes()
    second = cli.run([
        "--video", str(source), "--duration", "1", "--config", str(config),
        "--output-dir", str(output), "--skip-judge",
    ])

    assert first.assessment_path.read_bytes() == first_bytes
    assert first.output_dir.parent == output
    assert second.output_dir.parent == output
    assert second.output_dir != first.output_dir
    assert second.assessment_path.is_file()
    assert not list(output.rglob("*.tmp"))


def test_atomic_json_reuses_identical_content_and_rejects_inconsistent_existing_file(tmp_path: Path):
    from scripts import evaluate_quality_moe as cli

    target = tmp_path / "quality_assessment.json"
    cli._atomic_json(target, {"decision": "REVIEW"})
    original = target.read_bytes()
    cli._atomic_json(target, {"decision": "REVIEW"})
    with pytest.raises(FileExistsError, match="different content"):
        cli._atomic_json(target, {"decision": "REJECT"})

    assert target.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_concurrent_json_publishers_cannot_overwrite_each_other(tmp_path: Path):
    from scripts import evaluate_quality_moe as cli

    target = tmp_path / "quality_assessment.json"

    def publish(decision: str) -> str:
        try:
            cli._atomic_json(target, {"decision": decision})
            return "published"
        except FileExistsError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(publish, ("KEEP_AS_IS", "REJECT")))

    assert sorted(outcomes) == ["published", "rejected"]
    assert json.loads(target.read_text(encoding="utf-8"))["decision"] in {
        "KEEP_AS_IS", "REJECT",
    }
    assert not list(tmp_path.glob("*.tmp"))


def test_concurrent_run_directory_claims_are_isolated(tmp_path: Path):
    from scripts import evaluate_quality_moe as cli

    root = tmp_path / "runs"
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda _index: cli._claim_run_dir(root), range(2)))

    assert len(set(claimed)) == 2
    assert all(path.is_dir() and path.parent == root for path in claimed)

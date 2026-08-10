from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
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
        # The CLI must freeze the inherited judge URL before evaluation.
        assert kwargs["config"].judge["base_url"] == "http://127.0.0.1:11434"
        return _fake_assessment(candidate["candidate_id"])

    monkeypatch.setattr(cli, "evaluate_candidate", fake_evaluate)
    result = cli.run(
        [
            "--video", str(source), "--duration", "1",
            "--config", str(config), "--output-dir", str(tmp_path / "out"),
            "--skip-judge",
        ]
    )

    assert result.output_dir == (tmp_path / "out").resolve()
    assert observed["video_path"] == str(source.resolve())
    assert _sha256(source) == before
    payload = json.loads(result.assessment_path.read_text(encoding="utf-8"))
    assert payload["source"]["sha256_before"] == before
    assert payload["source"]["sha256_after"] == before
    assert payload["judge_execution"]["status"] == "SKIPPED"
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


def test_assessment_json_is_atomically_published_and_existing_run_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from scripts import evaluate_quality_moe as cli

    source = _video(tmp_path / "source.avi")
    config = _config(tmp_path / "models.yaml")
    output = tmp_path / "out"
    monkeypatch.setattr(cli, "evaluate_candidate", lambda *_args, **_kwargs: _fake_assessment())
    replacements: list[tuple[Path, Path]] = []
    real_replace = cli.os.replace

    def recording_replace(source_path, target_path):
        replacements.append((Path(source_path), Path(target_path)))
        return real_replace(source_path, target_path)

    monkeypatch.setattr(cli.os, "replace", recording_replace)
    first = cli.run([
        "--video", str(source), "--duration", "1", "--config", str(config),
        "--output-dir", str(output), "--skip-judge",
    ])
    first_bytes = first.assessment_path.read_bytes()
    second = cli.run([
        "--video", str(source), "--duration", "1", "--config", str(config),
        "--output-dir", str(output), "--skip-judge",
    ])

    assert any(target.name == "quality_assessment.json" for _, target in replacements)
    assert first.assessment_path.read_bytes() == first_bytes
    assert second.output_dir.parent == output
    assert second.output_dir != first.output_dir
    assert second.assessment_path.is_file()
    assert not list(output.rglob("*.tmp"))

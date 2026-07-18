from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.task_engine.adaptive_adapter import (
    AdaptivePipelineAdapter,
    run_adaptive_stage,
)
from app.task_engine.models import ArtifactRef, StageName
from app.task_engine.stages import StageAdapter, StageContext, StageResult


# =========================================================================
# Dataclass contract tests
# =========================================================================


class TestStageResultContract:
    def test_is_frozen(self):
        r = StageResult(output_key="k", artifacts=(), metrics={"a": 1})
        with pytest.raises(AttributeError):
            r.output_key = "other"

    def test_fields(self):
        ref = ArtifactRef(
            artifact_id="a1",
            job_id="j1",
            video_id="v1",
            stage_name="sample",
            clip_id=None,
            path="/tmp/x.json",
            sha256="0" * 64,
            size_bytes=100,
            provenance_json="{}",
        )
        r = StageResult(
            output_key="sample_frames",
            artifacts=(ref,),
            metrics={"frame_count": 42},
        )
        assert r.output_key == "sample_frames"
        assert r.artifacts == (ref,)
        assert r.metrics == {"frame_count": 42}


class TestStageContextContract:
    def test_is_frozen(self):
        ctx = StageContext(
            job_id="j1",
            video_id="v1",
            video_path=Path("/v.mp4"),
            clip_id=None,
            input_key="ik",
            work_dir=Path("/tmp/w"),
            config={"a": 1},
        )
        with pytest.raises(AttributeError):
            ctx.job_id = "other"

    def test_fields(self):
        ctx = StageContext(
            job_id="j1",
            video_id="v1",
            video_path=Path("/v.mp4"),
            clip_id="c1",
            input_key="ik",
            work_dir=Path("/tmp/w"),
            config={"a": 1},
        )
        assert ctx.job_id == "j1"
        assert ctx.video_id == "v1"
        assert ctx.video_path == Path("/v.mp4")
        assert ctx.clip_id == "c1"
        assert ctx.input_key == "ik"
        assert ctx.work_dir == Path("/tmp/w")
        assert ctx.config == {"a": 1}


class TestStageAdapterProtocol:
    def test_is_protocol(self):
        # StageAdapter is a Protocol -- we can't instantiate it directly,
        # but we can verify its structural shape.
        assert hasattr(StageAdapter, "run")

    def test_concrete_adapter_properties(self):
        adapter = AdaptivePipelineAdapter("vlm", version="2")
        assert adapter.name == "vlm"
        assert adapter.version == "2"

    def test_default_version(self):
        adapter = AdaptivePipelineAdapter("sample")
        assert adapter.version == "1"


# =========================================================================
# run_adaptive_stage tests  (with fake _runner)
# =========================================================================


def _make_fake_runner(tmp_path: Path, result_data: dict | None = None):
    """Return a fake ``_runner`` callable for ``run_adaptive_stage``.

    The fake parses the command line to find ``--task-result``, writes
    *result_data* (or a minimal default) there, and returns a
    ``subprocess.CompletedProcess``.
    """
    if result_data is None:
        result_data = {
            "stage": "sample",
            "output_key": "sample_frames",
            "artifacts": [],
            "metrics": {"frame_count": 5, "duration_s": 120.0},
        }

    captured_cmds: list[list[str]] = []

    def fake_runner(cmd, **kwargs):
        captured_cmds.append(cmd)
        # Find --task-result in cmd
        try:
            result_idx = cmd.index("--task-result") + 1
        except ValueError:
            raise AssertionError("--task-result not in cmd")
        result_path = cmd[result_idx]
        Path(result_path).parent.mkdir(parents=True, exist_ok=True)
        with open(result_path, "w") as f:
            json.dump(result_data, f)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    return fake_runner, captured_cmds


class TestRunAdaptiveStage:
    def test_returns_stage_result_with_fake_runner(self, tmp_path):
        video = tmp_path / "video.mp4"
        video.write_text("fake video content")
        work_dir = tmp_path / "work"
        config_snap = tmp_path / "config.json"
        config_snap.write_text("{}")

        fake_runner, captured = _make_fake_runner(tmp_path)

        result = run_adaptive_stage(
            "sample",
            video=video,
            work_dir=work_dir,
            config_snapshot=config_snap,
            _runner=fake_runner,
        )

        assert isinstance(result, StageResult)
        assert result.output_key == "sample_frames"
        assert result.metrics["frame_count"] == 5

        # Verify the fake was called with the right command
        assert len(captured) == 1
        cmd = captured[0]
        assert "--task-stage" in cmd
        assert cmd[cmd.index("--task-stage") + 1] == "sample"
        assert "--task-result" in cmd

    def test_produces_artifacts_from_script_output(self, tmp_path):
        video = tmp_path / "video.mp4"
        video.write_text("fake")
        work_dir = tmp_path / "work"
        config_snap = tmp_path / "config.json"
        config_snap.write_text("{}")

        artifact_path = tmp_path / "artifact.json"
        artifact_path.write_text('{"result": "ok"}', encoding="utf-8")

        result_data = {
            "stage": "vlm",
            "output_key": "vlm_scored",
            "artifacts": [
                {
                    "artifact_id": "scored_json",
                    "path": str(artifact_path),
                    "artifact_kind": "vlm_manifest",
                    "clip_id": None,
                }
            ],
            "metrics": {"scored_count": 10},
        }

        fake_runner, _ = _make_fake_runner(tmp_path, result_data)

        result = run_adaptive_stage(
            "vlm",
            video=video,
            work_dir=work_dir,
            config_snapshot=config_snap,
            job_id="job001",
            video_id="vid001",
            stage_id="stage-test-001",
            _runner=fake_runner,
        )

        assert len(result.artifacts) == 1
        ref = result.artifacts[0]
        # P0-1: artifact_id is now generated by make_artifact_id(), not
        # taken from the script output directly.  It must be a hex hash.
        assert len(ref.artifact_id) == 64
        assert ref.artifact_id != "scored_json"  # no longer uses script's ID
        assert ref.job_id == "job001"
        assert ref.video_id == "vid001"
        assert ref.stage_name == "vlm"
        # P0-1: stage_id must be non-empty from the adapter.
        assert ref.stage_id == "stage-test-001"
        # P0-1: artifact_kind must not be "generic" for new artifacts.
        assert ref.artifact_kind == "vlm_manifest"
        # sha256 and size should be computed from the actual file
        assert len(ref.sha256) == 64
        assert ref.size_bytes == artifact_path.stat().st_size

    def test_gif_clip_passes_clip_id(self, tmp_path):
        """The ``gif_clip`` stage must forward --clip-id to the script."""
        video = tmp_path / "video.mp4"
        video.write_text("fake")
        work_dir = tmp_path / "work"
        config_snap = tmp_path / "config.json"
        config_snap.write_text("{}")

        captured_cmds: list[list[str]] = []

        def recording_runner(cmd, **kwargs):
            captured_cmds.append(cmd)
            result_idx = cmd.index("--task-result") + 1
            result_path = cmd[result_idx]
            Path(result_path).parent.mkdir(parents=True, exist_ok=True)
            with open(result_path, "w") as f:
                json.dump(
                    {
                        "stage": "gif_clip",
                        "output_key": "gif_clip",
                        "artifacts": [],
                        "metrics": {},
                    },
                    f,
                )
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        result = run_adaptive_stage(
            "gif_clip",
            video=video,
            work_dir=work_dir,
            config_snapshot=config_snap,
            clip_id="clip_007",
            _runner=recording_runner,
        )

        assert result is not None
        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        assert "--clip-id" in cmd
        assert cmd[cmd.index("--clip-id") + 1] == "clip_007"

    def test_non_gif_stage_omits_clip_id(self, tmp_path):
        """Stages other than gif_clip should NOT pass --clip-id."""
        video = tmp_path / "video.mp4"
        video.write_text("fake")
        work_dir = tmp_path / "work"
        config_snap = tmp_path / "config.json"
        config_snap.write_text("{}")

        captured_cmds: list[list[str]] = []

        def recording_runner(cmd, **kwargs):
            captured_cmds.append(cmd)
            result_idx = cmd.index("--task-result") + 1
            result_path = cmd[result_idx]
            Path(result_path).parent.mkdir(parents=True, exist_ok=True)
            with open(result_path, "w") as f:
                json.dump(
                    {
                        "stage": "vlm",
                        "output_key": "vlm_scored",
                        "artifacts": [],
                        "metrics": {},
                    },
                    f,
                )
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        run_adaptive_stage(
            "vlm",
            video=video,
            work_dir=work_dir,
            config_snapshot=config_snap,
            _runner=recording_runner,
        )

        cmd = captured_cmds[0]
        assert "--clip-id" not in cmd

    def test_raises_when_result_file_missing(self, tmp_path):
        """If the fake runner doesn't write a result, the adapter must raise."""
        video = tmp_path / "video.mp4"
        video.write_text("fake")
        work_dir = tmp_path / "work"
        config_snap = tmp_path / "config.json"
        config_snap.write_text("{}")

        def broken_runner(cmd, **kwargs):
            # Does NOT write the result file
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        with pytest.raises(FileNotFoundError, match="result"):
            run_adaptive_stage(
                "sample",
                video=video,
                work_dir=work_dir,
                config_snapshot=config_snap,
                _runner=broken_runner,
            )

    def test_isolated_work_directory(self, tmp_path):
        """Each stage invocation must receive its own --task-work-dir."""
        video = tmp_path / "video.mp4"
        video.write_text("fake")
        config_snap = tmp_path / "config.json"
        config_snap.write_text("{}")

        captured_cmds: list[list[str]] = []

        def recording_runner(cmd, **kwargs):
            captured_cmds.append(cmd)
            result_idx = cmd.index("--task-result") + 1
            result_path = cmd[result_idx]
            Path(result_path).parent.mkdir(parents=True, exist_ok=True)
            with open(result_path, "w") as f:
                json.dump(
                    {
                        "stage": "sample",
                        "output_key": "sample_frames",
                        "artifacts": [],
                        "metrics": {},
                    },
                    f,
                )
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        work_a = tmp_path / "work_a"
        work_b = tmp_path / "work_b"

        run_adaptive_stage(
            "sample",
            video=video,
            work_dir=work_a,
            config_snapshot=config_snap,
            _runner=recording_runner,
        )
        run_adaptive_stage(
            "vlm",
            video=video,
            work_dir=work_b,
            config_snapshot=config_snap,
            _runner=recording_runner,
        )

        assert len(captured_cmds) == 2
        work_dirs = []
        for cmd in captured_cmds:
            idx = cmd.index("--task-work-dir") + 1
            work_dirs.append(cmd[idx])
        assert work_dirs[0] != work_dirs[1]


class TestAdapterOutcomeContract:
    """P1-2 (fifth-review §6): the adapter parses the subprocess result's
    ``outcome`` as a strict Literal and rejects unknown values instead of
    silently mapping them to ``succeeded``."""

    def _result_runner(self, tmp_path, outcome):
        video = tmp_path / "video.mp4"
        video.write_text("fake")
        result_data = {
            "stage": "sample",
            "output_key": "sample_frames",
            "outcome": outcome,
            "artifacts": [],
            "metrics": {},
        }
        fake_runner, _ = _make_fake_runner(tmp_path, result_data)
        return fake_runner, video

    def test_adapter_accepts_succeeded_outcome(self, tmp_path):
        fake_runner, video = self._result_runner(tmp_path, "succeeded")
        work_dir = tmp_path / "work"
        config_snap = tmp_path / "config.json"
        config_snap.write_text("{}")
        result = run_adaptive_stage(
            "sample", video=video, work_dir=work_dir,
            config_snapshot=config_snap, _runner=fake_runner,
        )
        assert result.outcome == "succeeded"

    def test_adapter_accepts_needs_attention_outcome(self, tmp_path):
        fake_runner, video = self._result_runner(tmp_path, "needs_attention")
        work_dir = tmp_path / "work"
        config_snap = tmp_path / "config.json"
        config_snap.write_text("{}")
        result = run_adaptive_stage(
            "sample", video=video, work_dir=work_dir,
            config_snapshot=config_snap, _runner=fake_runner,
        )
        assert result.outcome == "needs_attention"

    def test_adapter_rejects_unknown_outcome(self, tmp_path):
        fake_runner, video = self._result_runner(tmp_path, "needs-atention-typo")
        work_dir = tmp_path / "work"
        config_snap = tmp_path / "config.json"
        config_snap.write_text("{}")
        with pytest.raises(ValueError, match="Unknown stage outcome"):
            run_adaptive_stage(
                "sample", video=video, work_dir=work_dir,
                config_snapshot=config_snap, _runner=fake_runner,
            )

    def test_adapter_missing_outcome_defaults_succeeded(self, tmp_path):
        """Backward-compat: a subprocess result without an outcome key is
        treated as ``succeeded`` (legacy script behavior)."""
        video = tmp_path / "video.mp4"
        video.write_text("fake")
        work_dir = tmp_path / "work"
        config_snap = tmp_path / "config.json"
        config_snap.write_text("{}")
        result_data = {
            "stage": "sample", "output_key": "sample_frames",
            "artifacts": [], "metrics": {},
        }
        fake_runner, _ = _make_fake_runner(tmp_path, result_data)
        result = run_adaptive_stage(
            "sample", video=video, work_dir=work_dir,
            config_snapshot=config_snap, _runner=fake_runner,
        )
        assert result.outcome == "succeeded"


def _stage_setup_unused():
    pass


# =========================================================================
# AdaptivePipelineAdapter tests
# =========================================================================


class TestAdaptivePipelineAdapter:
    def test_run_delegates_to_run_adaptive_stage(self, tmp_path, monkeypatch):
        """Verify that ``adapter.run()`` calls ``run_adaptive_stage``."""
        video = tmp_path / "video.mp4"
        video.write_text("fake")

        context = StageContext(
            job_id="j1",
            video_id="v1",
            video_path=video,
            clip_id=None,
            input_key="ik",
            work_dir=tmp_path / "adapter_work",
            config={"adaptive": {"sample_interval": 10}},
        )

        # Capture real subprocess.run BEFORE monkeypatching
        _real_run = subprocess.run
        called_with: dict = {}

        def fake_runner(cmd, **kwargs):
            if "--task-result" not in cmd:
                return _real_run(cmd, **kwargs)
            called_with["cmd"] = cmd
            result_idx = cmd.index("--task-result") + 1
            result_path = cmd[result_idx]
            Path(result_path).parent.mkdir(parents=True, exist_ok=True)
            with open(result_path, "w") as f:
                json.dump(
                    {
                        "stage": "sample",
                        "output_key": "sample_frames",
                        "artifacts": [],
                        "metrics": {"frame_count": 3},
                    },
                    f,
                )
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        monkeypatch.setattr(
            "app.task_engine.adaptive_adapter.subprocess.run", fake_runner
        )

        adapter = AdaptivePipelineAdapter("sample")
        result = adapter.run(context)

        assert result.output_key == "sample_frames"
        assert result.metrics["frame_count"] == 3
        # Config snapshot should have been written (with _stage_id injected)
        snap = context.work_dir / "config_snapshot.json"
        assert snap.exists()
        with open(snap) as f:
            saved = json.load(f)
        assert saved.get("adaptive", {}).get("sample_interval") == 10
        assert "_stage_id" in saved  # P1-3: injected by adapter

    def test_adapter_writes_config_snapshot(self, tmp_path, monkeypatch):
        """The adapter must persist context.config before running the stage."""
        video = tmp_path / "video.mp4"
        video.write_text("fake")

        context = StageContext(
            job_id="j2",
            video_id="v2",
            video_path=video,
            clip_id=None,
            input_key="ik",
            work_dir=tmp_path / "snap_test",
            config={"vlm": {"model": "llava:13b", "temperature": 0.7}},
        )

        _real_run = subprocess.run

        def fake_runner(cmd, **kwargs):
            if "--task-result" not in cmd:
                return _real_run(cmd, **kwargs)
            result_path = cmd[cmd.index("--task-result") + 1]
            Path(result_path).parent.mkdir(parents=True, exist_ok=True)
            with open(result_path, "w") as f:
                json.dump(
                    {
                        "stage": "vlm",
                        "output_key": "vlm_scored",
                        "artifacts": [],
                        "metrics": {},
                    },
                    f,
                )
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        monkeypatch.setattr(
            "app.task_engine.adaptive_adapter.subprocess.run", fake_runner
        )

        adapter = AdaptivePipelineAdapter("vlm")
        adapter.run(context)

        snap = context.work_dir / "config_snapshot.json"
        assert snap.exists()
        with open(snap) as f:
            saved = json.load(f)
        assert saved.get("vlm", {}).get("model") == "llava:13b"
        assert saved.get("vlm", {}).get("temperature") == 0.7
        assert "_stage_id" in saved  # P1-3: injected by adapter

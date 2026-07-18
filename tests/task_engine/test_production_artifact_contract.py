"""Phase 0: Production artifact contract tests.

Verify that each stage only registers the artifact kinds in its whitelist
and that control/input/log/result files are NEVER registered as artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.task_engine.adaptive_adapter import run_adaptive_stage
from app.task_engine.artifacts import STAGE_ARTIFACT_KINDS


@pytest.fixture(autouse=True)
def _mock_provenance(monkeypatch: pytest.MonkeyPatch):
    """Avoid requiring git for provenance in contract tests."""
    from app.services.provenance import Provenance

    monkeypatch.setattr(
        "app.services.provenance.current_provenance",
        lambda config, stage_versions, prompts=None: Provenance(
            git_commit="test-commit",
            config_hash="test-cfg-hash",
            model_versions={},
            prompt_hashes={},
            stage_versions=dict(stage_versions),
        ),
    )


class TestDiscoverArtifactContract:
    """discover work_dir has config_snapshot.json, input_manifest.json,
    discover_manifest.json — only discover_manifest.json is registered."""

    def test_discover_only_registers_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        video = tmp_path / "test.mp4"
        video.write_text("fake-video-data")

        config_snap = work_dir / "config_snapshot.json"
        config_data = {"adaptive": {"sample_interval": 8}, "preference_memory": {"enabled": True}}
        config_snap.write_text(json.dumps(config_data))

        result_path = work_dir / "result_discover.json"

        discover_manifest_path = work_dir / "discover_manifest.json"
        discover_manifest = {
            "schema_version": 1,
            "stage": "discover",
            "video_path": str(video),
            "video_name": "test",
            "duration_s": 120.0,
            "output_key": "discover",
        }
        discover_manifest_path.write_text(json.dumps(discover_manifest))

        def fake_run(cmd, **kwargs):
            result_data = {
                "stage": "discover",
                "output_key": "discover",
                "artifacts": [
                    {"path": str(discover_manifest_path), "artifact_kind": "discover_manifest"},
                ],
                "metrics": {"duration_s": 120.0},
            }
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(json.dumps(result_data))
            import subprocess
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr("subprocess.run", fake_run)

        result = run_adaptive_stage(
            "discover",
            video=video,
            work_dir=work_dir,
            config_snapshot=config_snap,
            job_id="j1",
            video_id="v1",
            stage_id="s1",
        )

        kinds = {a.artifact_kind for a in result.artifacts}
        assert "discover_manifest" in kinds, "discover must produce discover_manifest"
        allowed = set(STAGE_ARTIFACT_KINDS.get("discover", ()))
        for art in result.artifacts:
            assert art.artifact_kind in allowed, (
                f"discover artifact_kind {art.artifact_kind!r} not in whitelist {allowed}"
            )
            # No control files
            assert "config_snapshot" not in art.artifact_kind
            assert "input_manifest" not in art.artifact_kind


class TestSampleArtifactContract:
    """sample only registers sample_manifest + sample_frames, not config/input/log."""

    def test_sample_only_registers_manifest_and_frames(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        video = tmp_path / "test.mp4"
        video.write_text("fake-video-data")

        config_snap = work_dir / "config_snapshot.json"
        config_snap.write_text(json.dumps({"adaptive": {"sample_interval": 8}}))

        sample_manifest_path = work_dir / "sample_manifest.json"
        frame1_path = work_dir / "frames" / "ts_000010.jpg"
        frame1_path.parent.mkdir(parents=True, exist_ok=True)
        frame1_path.write_text("fake-frame-1")
        frame2_path = work_dir / "frames" / "ts_000020.jpg"
        frame2_path.write_text("fake-frame-2")

        result_path = work_dir / "result_sample.json"

        def fake_run(cmd, **kwargs):
            sample_manifest = {
                "schema_version": 1,
                "stage": "sample",
                "frame_count": 2,
                "timestamps": [10, 20],
                "frame_paths": [str(frame1_path), str(frame2_path)],
                "output_key": "sample",
            }
            sample_manifest_path.write_text(json.dumps(sample_manifest))
            result_data = {
                "stage": "sample",
                "output_key": "sample",
                "artifacts": [
                    {"path": str(sample_manifest_path), "artifact_kind": "sample_manifest"},
                    {"path": str(frame1_path), "artifact_kind": "sample_frames"},
                    {"path": str(frame2_path), "artifact_kind": "sample_frames"},
                ],
                "metrics": {"frame_count": 2},
            }
            result_path.write_text(json.dumps(result_data))
            import subprocess
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr("subprocess.run", fake_run)

        result = run_adaptive_stage(
            "sample",
            video=video,
            work_dir=work_dir,
            config_snapshot=config_snap,
            job_id="j1",
            video_id="v1",
            stage_id="s2",
        )

        kinds = {a.artifact_kind for a in result.artifacts}
        assert "sample_manifest" in kinds
        assert "sample_frames" in kinds
        for art in result.artifacts:
            assert "config_snapshot" not in art.artifact_kind
            assert "input_manifest" not in art.artifact_kind
            assert "stage.log" not in str(art.path)
            assert not str(art.path).endswith(".log")


class TestGifClipArtifactContract:
    """gif_clip only registers one gif_file + one gif_clip_manifest."""

    def test_gif_clip_only_registers_gif_and_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        video = tmp_path / "test.mp4"
        video.write_text("fake-video-data")

        config_snap = work_dir / "config_snapshot.json"
        config_snap.write_text(json.dumps({"adaptive": {"gif_fps": 24, "gif_max_width": 720}}))

        gif_path = work_dir / "exports" / "test" / "test_clip-A.gif"
        gif_path.parent.mkdir(parents=True, exist_ok=True)
        gif_path.write_text("GIF89a-fake-gif-data")

        manifest_path = work_dir / "gif_clip_manifest.json"
        result_path = work_dir / "result_gif_clip.json"

        def fake_run(cmd, **kwargs):
            manifest_data = {
                "schema_version": 1,
                "stage": "gif_clip",
                "clip_id": "clip-A",
                "gif_path": str(gif_path),
                "gif_name": "test_clip-A.gif",
                "sha256": "abc123",
                "start_ts": 10.0,
                "end_ts": 15.0,
                "output_key": "gif_clip:clip-A",
            }
            manifest_path.write_text(json.dumps(manifest_data))
            result_data = {
                "stage": "gif_clip",
                "output_key": "gif_clip:clip-A",
                "artifacts": [
                    {"path": str(gif_path), "artifact_kind": "gif_file", "clip_id": "clip-A"},
                    {"path": str(manifest_path), "artifact_kind": "gif_clip_manifest", "clip_id": "clip-A"},
                ],
                "metrics": {},
            }
            result_path.write_text(json.dumps(result_data))
            import subprocess
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr("subprocess.run", fake_run)

        result = run_adaptive_stage(
            "gif_clip",
            video=video,
            work_dir=work_dir,
            config_snapshot=config_snap,
            job_id="j1",
            video_id="v1",
            stage_id="s7",
            clip_id="clip-A",
        )

        kinds = {a.artifact_kind for a in result.artifacts}
        assert "gif_file" in kinds
        assert "gif_clip_manifest" in kinds
        gif_count = sum(1 for a in result.artifacts if a.artifact_kind == "gif_file")
        manifest_count = sum(1 for a in result.artifacts if a.artifact_kind == "gif_clip_manifest")
        assert gif_count == 1, f"Expected 1 gif_file, got {gif_count}"
        assert manifest_count == 1, f"Expected 1 gif_clip_manifest, got {manifest_count}"


class TestMaterializeArtifactContract:
    """materialize only registers result, materialize_manifest, and optionally pbf_file."""

    def test_materialize_only_registers_result_and_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        video = tmp_path / "test.mp4"
        video.write_text("fake-video-data")

        config_snap = work_dir / "config_snapshot.json"
        config_snap.write_text(json.dumps({"adaptive": {"potplayer_pbf_enabled": True}}))

        result_json_path = work_dir / "result.json"
        manifest_path = work_dir / "materialize_manifest.json"
        pbf_path = work_dir / "test.pbf"
        result_out_path = work_dir / "result_materialize.json"

        def fake_run(cmd, **kwargs):
            result_json_path.write_text(json.dumps({"video_name": "test", "gif_count": 1}))
            manifest_path.write_text(json.dumps({
                "schema_version": 1, "stage": "materialize", "gif_count": 1, "output_key": "materialize",
            }))
            pbf_path.write_text("pbf-binary-data")
            result_data = {
                "stage": "materialize",
                "output_key": "materialize",
                "artifacts": [
                    {"path": str(result_json_path), "artifact_kind": "result"},
                    {"path": str(manifest_path), "artifact_kind": "materialize_manifest"},
                    {"path": str(pbf_path), "artifact_kind": "pbf_file"},
                ],
                "metrics": {"gif_count": 1},
            }
            result_out_path.write_text(json.dumps(result_data))
            import subprocess
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr("subprocess.run", fake_run)

        result = run_adaptive_stage(
            "materialize",
            video=video,
            work_dir=work_dir,
            config_snapshot=config_snap,
            job_id="j1",
            video_id="v1",
            stage_id="s8",
        )

        kinds = {a.artifact_kind for a in result.artifacts}
        assert "result" in kinds
        assert "materialize_manifest" in kinds
        allowed = set(STAGE_ARTIFACT_KINDS.get("materialize", ()))
        allowed_with_pbf = allowed | {"pbf_file"}
        for art in result.artifacts:
            assert art.artifact_kind in allowed_with_pbf, (
                f"materialize artifact_kind {art.artifact_kind!r} not whitelisted"
            )


class TestAdapterRejectsUnknownKind:
    """Adapter must fail when artifact_kind is missing, unknown, or not in stage whitelist."""

    def test_missing_artifact_kind_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        video = tmp_path / "test.mp4"
        video.write_text("fake-video-data")

        config_snap = work_dir / "config_snapshot.json"
        config_snap.write_text(json.dumps({"adaptive": {}}))

        manifest_path = work_dir / "discover_manifest.json"
        manifest_path.write_text(json.dumps({"schema_version": 1, "stage": "discover", "duration_s": 120}))

        result_path = work_dir / "result_discover.json"

        def fake_run(cmd, **kwargs):
            result_data = {
                "stage": "discover",
                "output_key": "discover",
                "artifacts": [
                    {"path": str(manifest_path)},  # No artifact_kind!
                ],
                "metrics": {},
            }
            result_path.write_text(json.dumps(result_data))
            import subprocess
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr("subprocess.run", fake_run)

        with pytest.raises(ValueError, match="no explicit artifact_kind"):
            run_adaptive_stage(
                "discover",
                video=video,
                work_dir=work_dir,
                config_snapshot=config_snap,
                job_id="j1",
                video_id="v1",
                stage_id="s1",
            )

    def test_unknown_artifact_kind_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        video = tmp_path / "test.mp4"
        video.write_text("fake-video-data")

        config_snap = work_dir / "config_snapshot.json"
        config_snap.write_text(json.dumps({"adaptive": {}}))

        manifest_path = work_dir / "discover_manifest.json"
        manifest_path.write_text(json.dumps({"schema_version": 1, "stage": "discover", "duration_s": 120}))

        result_path = work_dir / "result_discover.json"

        def fake_run(cmd, **kwargs):
            result_data = {
                "stage": "discover",
                "output_key": "discover",
                "artifacts": [
                    {"path": str(manifest_path), "artifact_kind": "not_a_real_kind"},
                ],
                "metrics": {},
            }
            result_path.write_text(json.dumps(result_data))
            import subprocess
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr("subprocess.run", fake_run)

        with pytest.raises(ValueError, match="cannot produce artifact_kind"):
            run_adaptive_stage(
                "discover",
                video=video,
                work_dir=work_dir,
                config_snapshot=config_snap,
                job_id="j1",
                video_id="v1",
                stage_id="s1",
            )

    def test_wrong_stage_artifact_kind_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        video = tmp_path / "test.mp4"
        video.write_text("fake-video-data")

        config_snap = work_dir / "config_snapshot.json"
        config_snap.write_text(json.dumps({"adaptive": {}}))

        manifest_path = work_dir / "discover_manifest.json"
        manifest_path.write_text(json.dumps({"schema_version": 1, "stage": "discover", "duration_s": 120}))

        result_path = work_dir / "result_discover.json"

        def fake_run(cmd, **kwargs):
            result_data = {
                "stage": "discover",
                "output_key": "discover",
                "artifacts": [
                    {"path": str(manifest_path), "artifact_kind": "gif_file"},
                ],
                "metrics": {},
            }
            result_path.write_text(json.dumps(result_data))
            import subprocess
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr("subprocess.run", fake_run)

        with pytest.raises(ValueError, match="cannot produce artifact_kind"):
            run_adaptive_stage(
                "discover",
                video=video,
                work_dir=work_dir,
                config_snapshot=config_snap,
                job_id="j1",
                video_id="v1",
                stage_id="s1",
            )

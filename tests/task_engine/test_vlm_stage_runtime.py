"""Fifth-review §4 (P0-2) RED tests: VLM stage runtime injection.

``_stage_vlm`` must:
1. read the VLM model name, base URL and provider from the frozen job
   config snapshot (not a hardcoded ``llava:13b`` or a module-level constant);
2. NOT call ``wsl ollama stop`` when the configured endpoint is external
   (cloud provider / deterministic stub) - only local-Ollama-with-model-
   switching may stop/start models;
3. reject a missing VLM model config cleanly instead of crashing with a
   ``NameError`` (the old code referenced an out-of-scope ``LLM_MODEL``).

These tests drive the stage directly via the ``vlm`` config dict and a
deterministic local HTTP stub so the user's real Ollama/WSL is never
touched.
"""

from __future__ import annotations

import base64
import hashlib
import httpx
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Deterministic local HTTP stub that mimics Ollama /api/generate for VLM
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _StubServer:
    """Minimal HTTP server emulating Ollama's /api/generate + /api/ps.

    Records every request so tests can prove NO wsl/ollama subprocess was
    involved and the configured base_url + model were used.
    """

    def __init__(self, response_payload: dict):
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.response_payload = response_payload
        self.requests: list[dict] = []
        from http.server import BaseHTTPRequestHandler, HTTPServer

        server = self

        class _Handler(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(body) if body else {}
                except Exception:
                    payload = {}
                server.requests.append({
                    "path": self.path, "model": payload.get("model", ""),
                })
                resp = json.dumps(server.response_payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            def do_POST(self):  # noqa: N802
                self._handle()

            def do_GET(self):  # noqa: N802
                self._handle()

            def log_message(self, *a):  # silence
                pass

        self._server = HTTPServer(("127.0.0.1", self.port), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )

    def start(self):
        self._thread.start()
        # Wait until the port is accepting connections.
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.2):
                    return
            except OSError:
                time.sleep(0.02)

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


_VLM_RESPONSE = {
    "response": json.dumps({
        "caption": "A dramatic frame with strong lighting.",
        "emotional_core": "awe", "gif_worthiness": 0.9,
        "aesthetic_notes": ["contrast"], "reason": "good",
    })
}


def _make_one_frame_video(tmp_path: Path) -> Path:
    """Create a tiny valid mp4 + return it (real ffmpeg)."""
    import subprocess
    p = tmp_path / "v.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=2:r=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", "2", str(p)],
        capture_output=True, text=True, timeout=30,
    )
    if not p.exists():
        pytest.skip("ffmpeg unavailable")
    return p


def _load_stage_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "tva_vlm_stage",
        os.path.join(
            os.path.dirname(__file__), "..", "..", "scripts",
            "test_video_adaptive.py",
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tva_vlm_stage"] = mod  # required for dataclass eval in 3.14
    spec.loader.exec_module(mod)
    return mod


class TestVlmStageRuntimeInjection:
    """§4.2 RED tests for the VLM stage's runtime config + lifecycle."""

    def test_vlm_stage_uses_frozen_job_model_config(self, tmp_path, monkeypatch):
        """The stage must use the model/base_url from the frozen job config,
        not a hardcoded ``llava:13b``."""
        mod = _load_stage_module()
        stub = _StubServer(_VLM_RESPONSE)
        stub.start()
        try:
            # Build a sample manifest + frame artifact the stage can consume.
            work_dir = tmp_path / "vlm_work"
            work_dir.mkdir(parents=True)
            frames_dir = work_dir / "frames"
            frames_dir.mkdir()
            frame_path = frames_dir / "ts_000010.jpg"
            frame_path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 200)

            sample_manifest = {
                "schema_version": 1, "stage": "sample",
                "frame_count": 1, "timestamps": [10],
                "frame_paths": [str(frame_path)],
                "frame_entries": [
                    {"artifact_id": "aid-1", "timestamp": 10, "path": str(frame_path)},
                ],
            }
            sm_path = work_dir / "sample_manifest.json"
            sm_path.write_text(json.dumps(sample_manifest))

            cfg = mod.extract_config({"adaptive": {}, "preference_memory": {}})
            # Frozen job config: explicit VLM model + base_url pointing at the stub.
            config_data = {
                "vlm": {
                    "provider": "ollama",
                    "model": "stub-vlm-model",
                    "base_url": stub.base_url,
                },
            }
            inputs = {
                "sample_manifest": [{
                    "artifact_id": "a", "path": str(sm_path), "clip_id": None,
                }],
                "sample_frames": [{
                    "artifact_id": "aid-1", "path": str(frame_path),
                    "clip_id": None, "sha256": hashlib.sha256(
                        frame_path.read_bytes()).hexdigest(),
                    "size_bytes": frame_path.stat().st_size,
                }],
            }

            # Block any real Ollama / WSL subprocess so the test proves the
            # stage never reached for them.
            def _no_subprocess(*a, **kw):
                raise AssertionError(
                    f"stage must not spawn subprocess: {a[0] if a else a}"
                )
            monkeypatch.setattr(mod.subprocess, "run", _no_subprocess)
            monkeypatch.setattr(mod, "is_local_llm", lambda: False)

            result = mod._stage_vlm(
                str(frames_dir), str(work_dir), cfg, inputs, config_data,
            )
            assert result["output_key"] == "vlm"

            # The stub received a request for the configured model name.
            models_seen = [r["model"] for r in stub.requests
                            if r["path"] == "/api/generate"]
            assert "stub-vlm-model" in models_seen, (
                f"stage must use frozen job model 'stub-vlm-model'; "
                f"got {models_seen}"
            )
        finally:
            stub.stop()

    def test_vlm_stage_does_not_require_wsl_for_external_endpoint(
        self, tmp_path, monkeypatch,
    ):
        """A non-local provider / external base_url must NOT trigger wsl/ollama
        model switching subprocesses."""
        mod = _load_stage_module()
        stub = _StubServer(_VLM_RESPONSE)
        stub.start()
        try:
            work_dir = tmp_path / "vlm_work"
            work_dir.mkdir(parents=True)
            frames_dir = work_dir / "frames"
            frames_dir.mkdir()
            frame_path = frames_dir / "ts_000010.jpg"
            frame_path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 200)
            sm = {
                "schema_version": 1, "stage": "sample",
                "frame_count": 1, "timestamps": [10],
                "frame_paths": [str(frame_path)],
                "frame_entries": [
                    {"artifact_id": "aid-1", "timestamp": 10, "path": str(frame_path)},
                ],
            }
            sm_path = work_dir / "sample_manifest.json"
            sm_path.write_text(json.dumps(sm))

            cfg = mod.extract_config({"adaptive": {}, "preference_memory": {}})
            config_data = {
                "vlm": {
                    "provider": "ollama",
                    "model": "cloud-vlm",
                    "base_url": stub.base_url,
                    "manage_lifecycle": False,
                    "launch_mode": "none",
                },
            }
            inputs = {
                "sample_manifest": [{"artifact_id": "a", "path": str(sm_path),
                                     "clip_id": None}],
                "sample_frames": [{"artifact_id": "aid-1", "path": str(frame_path),
                                   "clip_id": None,
                                   "sha256": hashlib.sha256(
                                       frame_path.read_bytes()).hexdigest(),
                                   "size_bytes": frame_path.stat().st_size}],
            }

            spawned = []
            monkeypatch.setattr(mod.subprocess, "run",
                                lambda *a, **kw: spawned.append(a[0]) or None)
            monkeypatch.setattr(mod, "is_local_llm", lambda: False)

            mod._stage_vlm(str(frames_dir), str(work_dir), cfg, inputs, config_data)

            wsl_calls = [c for c in spawned if "wsl" in str(c)]
            assert wsl_calls == [], (
                f"external endpoint must not spawn wsl subprocesses: {wsl_calls}"
            )
        finally:
            stub.stop()

    def test_vlm_stage_rejects_missing_model_config_cleanly(self, tmp_path, monkeypatch):
        """A config snapshot with no VLM model must raise a clean error,
        not crash with NameError or silently use a hardcoded model."""
        mod = _load_stage_module()
        work_dir = tmp_path / "vlm_work"
        work_dir.mkdir(parents=True)
        frames_dir = work_dir / "frames"
        frames_dir.mkdir()
        frame_path = frames_dir / "ts_000010.jpg"
        frame_path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 200)
        sm = {
            "schema_version": 1, "stage": "sample",
            "frame_count": 1, "timestamps": [10],
            "frame_paths": [str(frame_path)],
            "frame_entries": [
                {"artifact_id": "aid-1", "timestamp": 10, "path": str(frame_path)},
            ],
        }
        sm_path = work_dir / "sample_manifest.json"
        sm_path.write_text(json.dumps(sm))
        cfg = mod.extract_config({"adaptive": {}, "preference_memory": {}})
        inputs = {
            "sample_manifest": [{"artifact_id": "a", "path": str(sm_path),
                                 "clip_id": None}],
            "sample_frames": [{"artifact_id": "aid-1", "path": str(frame_path),
                               "clip_id": None,
                               "sha256": hashlib.sha256(
                                   frame_path.read_bytes()).hexdigest(),
                               "size_bytes": frame_path.stat().st_size}],
        }
        # config_data with NO vlm model info.
        with pytest.raises((ValueError, KeyError)):
            mod._stage_vlm(str(frames_dir), str(work_dir), cfg, inputs, {})


# ---------------------------------------------------------------------------
# Seventh-review Task 1: RED tests for invalid VLM scores + empty refine
# ---------------------------------------------------------------------------


class TestScoreVlmFrameRejectsInvalidWorthiness:
    """P0-1: ``_score_vlm_frame`` must NOT accept missing/invalid
    ``gif_worthiness`` and silently coerce it to 0.5."""

    def test_score_vlm_frame_rejects_missing_worthiness(self, tmp_path):
        """Stub returns ``{"response": "{}"}`` -- no gif_worthiness at all.
        Must return (None, error mentioning gif_worthiness), NOT a 0.5
        success payload."""
        mod = _load_stage_module()
        stub = _StubServer({"response": "{}"})
        stub.start()
        try:
            payload, error = mod._score_vlm_frame(
                base_url=stub.base_url,
                model="stub-vlm",
                image_bytes=b"\xff\xd8\xff\xe0" + b"\x00" * 20,
                prompt="score",
                options={},
                threshold=0.55,
                timestamp=1.0,
                frame_path=str(tmp_path / "frame.jpg"),
            )
            assert payload is None, (
                f"missing gif_worthiness must NOT produce a payload; "
                f"got {payload!r}"
            )
            assert error is not None
            assert "gif_worthiness" in error, (
                f"error must mention gif_worthiness; got {error!r}"
            )
        finally:
            stub.stop()

    @pytest.mark.parametrize(
        "value", [None, True, -0.1, 1.1, "nan", "high"],
    )
    def test_score_vlm_frame_rejects_invalid_worthiness(self, tmp_path, value):
        """Non-finite, boolean, out-of-range or non-numeric worthiness must
        be rejected (payload=None), never coerced to 0.5."""
        mod = _load_stage_module()
        stub = _StubServer({
            "response": json.dumps({
                "caption": "x", "emotional_core": "awe",
                "gif_worthiness": value,
                "aesthetic_notes": [], "reason": "x",
            }),
        })
        stub.start()
        try:
            payload, error = mod._score_vlm_frame(
                base_url=stub.base_url, model="stub-vlm",
                image_bytes=b"\xff\xd8\xff\xe0" + b"\x00" * 20,
                prompt="score", options={}, threshold=0.55,
                timestamp=1.0, frame_path=str(tmp_path / "frame.jpg"),
            )
            assert payload is None, (
                f"invalid worthiness {value!r} must NOT produce a payload"
            )
            assert error is not None and "gif_worthiness" in error
        finally:
            stub.stop()


class TestRefineEmptyHighScore:
    """P0-2: ``_stage_refine`` with an empty (no-high-score) vlm_manifest
    must succeed with explicit zero counters, not crash with an
    UnboundLocalError on the counter variables."""

    def test_refine_no_high_score_succeeds_with_zero_counters(
        self, tmp_path, monkeypatch,
    ):
        mod = _load_stage_module()
        # Construct a valid discover_manifest + empty vlm_manifest.
        work_dir = tmp_path / "refine_work"
        work_dir.mkdir(parents=True)
        frames_dir = work_dir / "frames"
        frames_dir.mkdir()

        discover_manifest = {
            "schema_version": 1, "stage": "discover", "duration_s": 10.0,
        }
        dm_path = work_dir / "discover_manifest.json"
        dm_path.write_text(json.dumps(discover_manifest))

        vlm_manifest = {
            "schema_version": 1, "stage": "vlm",
            "scored_count": 0, "frames": [],
            "attempted_count": 1, "response_count": 1,
            "parsed_count": 1, "failed_count": 0,
            "output_key": "vlm",
        }
        vm_path = work_dir / "vlm_manifest.json"
        vm_path.write_text(json.dumps(vlm_manifest))

        # Minimal video file (refine_ts must be empty so no extraction).
        video_path = tmp_path / "v.mp4"
        video_path.write_bytes(b"fake")

        cfg = mod.extract_config({"adaptive": {}, "preference_memory": {}})
        config_data = {
            "vlm": {
                "provider": "ollama", "model": "stub-vlm",
                "base_url": "http://127.0.0.1:1",
                "manage_lifecycle": False, "launch_mode": "none",
            },
        }
        inputs = {
            "vlm_manifest": [{"artifact_id": "a", "path": str(vm_path),
                              "clip_id": None}],
            "discover_manifest": [{"artifact_id": "b", "path": str(dm_path),
                                   "clip_id": None}],
        }

        # No high-score frames -> refine_ts must be empty -> no extraction.
        result = mod._stage_refine(
            str(video_path), str(frames_dir), str(work_dir),
            cfg, inputs, config_data,
        )
        assert result["output_key"] == "refine"

        manifest = json.loads(
            (work_dir / "refine_manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["refine_regions"] == 0, manifest
        assert manifest.get("refine_requested") == 0, manifest
        assert manifest.get("refine_extracted") == 0, manifest
        assert manifest.get("refine_attempted") == 0, manifest
        assert manifest.get("refine_parsed") == 0, manifest
        assert manifest.get("refine_failed") == 0, manifest
        assert manifest["frames"] == [], manifest


class TestStageVlmAndRefineRejectInvalidScores:
    """Task 2 Step 3: both ``_stage_vlm`` and ``_stage_refine`` must raise
    ``RuntimeError`` on invalid scores and NOT write a success manifest."""

    def _build_sample_inputs(self, tmp_path, work_dir, frame_path):
        """Build sample_manifest + sample_frames inputs for _stage_vlm."""
        frames_dir = work_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        sm = {
            "schema_version": 1, "stage": "sample",
            "frame_count": 1, "timestamps": [10],
            "frame_paths": [str(frame_path)],
            "frame_entries": [
                {"artifact_id": "aid-1", "timestamp": 10, "path": str(frame_path)},
            ],
        }
        sm_path = work_dir / "sample_manifest.json"
        sm_path.write_text(json.dumps(sm))
        inputs = {
            "sample_manifest": [{"artifact_id": "a", "path": str(sm_path),
                                  "clip_id": None}],
            "sample_frames": [{"artifact_id": "aid-1", "path": str(frame_path),
                               "clip_id": None,
                               "sha256": hashlib.sha256(
                                   frame_path.read_bytes()).hexdigest(),
                               "size_bytes": frame_path.stat().st_size}],
        }
        return inputs

    def test_stage_vlm_raises_on_invalid_score(self, tmp_path, monkeypatch):
        mod = _load_stage_module()
        stub = _StubServer({"response": json.dumps({
            "caption": "x", "emotional_core": "awe",
            "gif_worthiness": "high",  # invalid string
            "aesthetic_notes": [], "reason": "x",
        })})
        stub.start()
        try:
            work_dir = tmp_path / "vlm_work"
            frames_dir = work_dir / "frames"
            frames_dir.mkdir(parents=True)
            frame_path = frames_dir / "ts_000010.jpg"
            frame_path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 200)
            inputs = self._build_sample_inputs(tmp_path, work_dir, frame_path)
            cfg = mod.extract_config({"adaptive": {}, "preference_memory": {}})
            config_data = {
                "vlm": {"provider": "ollama", "model": "stub-vlm",
                        "base_url": stub.base_url,
                        "manage_lifecycle": False, "launch_mode": "none",
                        "retry_delay_s": 0.0},
            }
            monkeypatch.setattr(mod.subprocess, "run",
                                lambda *a, **kw: None)
            monkeypatch.setattr(mod, "is_local_llm", lambda: False)
            with pytest.raises(RuntimeError):
                mod._stage_vlm(str(frames_dir), str(work_dir), cfg, inputs,
                               config_data)
            # No success manifest written.
            assert not (work_dir / "vlm_manifest.json").exists()
        finally:
            stub.stop()

    def test_stage_refine_raises_on_invalid_score(self, tmp_path, monkeypatch):
        mod = _load_stage_module()
        stub = _StubServer({"response": json.dumps({
            "caption": "x", "emotional_core": "awe",
            "gif_worthiness": "bad",  # invalid string
            "aesthetic_notes": [], "reason": "x",
        })})
        stub.start()
        try:
            work_dir = tmp_path / "refine_work"
            work_dir.mkdir(parents=True)
            frames_dir = work_dir / "frames"
            frames_dir.mkdir(parents=True)

            # discover + vlm manifest with one high-score frame (forces refine).
            dm_path = work_dir / "discover_manifest.json"
            dm_path.write_text(json.dumps({
                "schema_version": 1, "stage": "discover", "duration_s": 10.0,
            }))
            vm_path = work_dir / "vlm_manifest.json"
            vm_path.write_text(json.dumps({
                "schema_version": 1, "stage": "vlm",
                "scored_count": 1,
                "frames": [{"timestamp": 5, "path": "/f.jpg",
                            "gif_worthiness": 0.9, "emotional_core": "awe",
                            "caption": "x"}],
                "attempted_count": 1, "response_count": 1,
                "parsed_count": 1, "failed_count": 0,
                "output_key": "vlm",
            }))
            # Real tiny video so ffmpeg extract returns a real frame file.
            video_path = tmp_path / "v.mp4"
            r = subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i",
                 "testsrc=d=2:r=10:s=64x64", "-c:v", "libx264",
                 "-pix_fmt", "yuv420p", "-t", "2", str(video_path)],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0 or not video_path.exists():
                pytest.skip("ffmpeg unavailable")
            cfg = mod.extract_config({"adaptive": {
                "refine_threshold": 0.6, "refine_radius": 1,
                "refine_interval": 1, "worthiness_threshold": 0.5,
            }, "preference_memory": {}})
            config_data = {
                "vlm": {"provider": "ollama", "model": "stub-vlm",
                        "base_url": stub.base_url,
                        "manage_lifecycle": False, "launch_mode": "none",
                        "retry_delay_s": 0.0},
            }
            inputs = {
                "vlm_manifest": [{"artifact_id": "a", "path": str(vm_path),
                                  "clip_id": None}],
                "discover_manifest": [{"artifact_id": "b", "path": str(dm_path),
                                       "clip_id": None}],
            }
            with pytest.raises(RuntimeError):
                mod._stage_refine(str(video_path), str(frames_dir),
                                  str(work_dir), cfg, inputs, config_data)
        finally:
            stub.stop()


class TestRefineExtractionFailure:
    """Task 3 Step 4: total extraction failure raises RuntimeError."""

    def test_refine_extraction_failure_raises(self, tmp_path, monkeypatch):
        mod = _load_stage_module()
        work_dir = tmp_path / "refine_work"
        work_dir.mkdir(parents=True)
        frames_dir = work_dir / "frames"
        frames_dir.mkdir(parents=True)

        dm_path = work_dir / "discover_manifest.json"
        dm_path.write_text(json.dumps({
            "schema_version": 1, "stage": "discover", "duration_s": 10.0,
        }))
        vm_path = work_dir / "vlm_manifest.json"
        vm_path.write_text(json.dumps({
            "schema_version": 1, "stage": "vlm",
            "scored_count": 1,
            "frames": [{"timestamp": 5, "path": "/f.jpg",
                        "gif_worthiness": 0.9, "emotional_core": "awe",
                        "caption": "x"}],
            "attempted_count": 1, "response_count": 1,
            "parsed_count": 1, "failed_count": 0,
            "output_key": "vlm",
        }))
        video_path = tmp_path / "v.mp4"
        video_path.write_bytes(b"fake")

        cfg = mod.extract_config({"adaptive": {
            "refine_threshold": 0.6, "refine_radius": 1,
            "refine_interval": 1, "worthiness_threshold": 0.5,
        }, "preference_memory": {}})
        config_data = {
            "vlm": {"provider": "ollama", "model": "stub-vlm",
                    "base_url": "http://127.0.0.1:1",
                    "manage_lifecycle": False, "launch_mode": "none"},
        }
        inputs = {
            "vlm_manifest": [{"artifact_id": "a", "path": str(vm_path),
                              "clip_id": None}],
            "discover_manifest": [{"artifact_id": "b", "path": str(dm_path),
                                   "clip_id": None}],
        }
        # Force ffmpeg to fail (returncode=1).
        def _failing_ffmpeg(*a, **kw):
            return subprocess.CompletedProcess(a[0], 1, b"", b"err")
        monkeypatch.setattr(mod.subprocess, "run", _failing_ffmpeg)
        with pytest.raises(RuntimeError, match="Refine extraction failed"):
            mod._stage_refine(str(video_path), str(frames_dir),
                              str(work_dir), cfg, inputs, config_data)


# ---------------------------------------------------------------------------
# Eighth-review Task 1: empty synthesize must register manifest artifact
# ---------------------------------------------------------------------------


def test_synthesize_empty_frames_returns_manifest_artifact(tmp_path):
    """Empty refine manifest (0 scored frames) -> synthesize must produce a
    synthesize_manifest artifact, not just write the file silently.
    Root cause of the zero-clip E2E failure: the empty-frames branch
    returned no _artifacts, so the worker never registered the manifest."""
    mod = _load_stage_module()
    work_dir = tmp_path / "synthesize"
    work_dir.mkdir()
    refine_path = tmp_path / "refine_manifest.json"
    refine_path.write_text(json.dumps({
        "schema_version": 1,
        "stage": "refine",
        "scored_count": 0,
        "frames": [],
    }), encoding="utf-8")
    result = mod._stage_synthesize(
        str(work_dir),
        mod.extract_config({"adaptive": {}, "preference_memory": {}}),
        {"refine_manifest": [{"artifact_id": "refine-1", "path": str(refine_path)}]},
    )
    assert result["clip_count"] == 0
    assert len(result["_artifacts"]) == 1, (
        f"empty synthesize must return 1 artifact, got {result.get('_artifacts')}"
    )
    assert result["_artifacts"][0]["artifact_kind"] == "synthesize_manifest"
    assert Path(result["_artifacts"][0]["path"]).exists()


# Task 2 Step 5: unit-level retry count protection
def test_score_vlm_frame_retries_exactly_three_times_on_invalid_payload(tmp_path):
    mod = _load_stage_module()
    stub = _StubServer({"response": "{}"})
    stub.start()
    try:
        payload, error = mod._score_vlm_frame(
            base_url=stub.base_url, model="stub-vlm",
            image_bytes=b"\xff\xd8\xff\xe0" + b"\x00" * 20,
            prompt="score", options={}, threshold=0.5,
            timestamp=0.0, frame_path=str(tmp_path / "frame.jpg"),
            retry_delay_s=0.0,
        )
        assert payload is None
        assert error is not None and "gif_worthiness" in error
        gen = [r for r in stub.requests if r["path"] == "/api/generate"]
        assert len(gen) == 3, f"must retry 3 times, got {len(gen)}"
    finally:
        stub.stop()


# ---------------------------------------------------------------------------
# Eighth-review Task 3: explicit lifecycle contract tests
# ---------------------------------------------------------------------------


class _StatusResponse:
    status_code = 200
    def raise_for_status(self): pass
    @staticmethod
    def json():
        return {"response": json.dumps({"gif_worthiness": 0.9,
                "caption": "x", "emotional_core": "awe",
                "aesthetic_notes": [], "reason": "x"})}


class _PsResponse(list):
    """Fake httpx response for /api/ps."""
    status_code = 200
    def raise_for_status(self): pass

    @staticmethod
    def json():
        return {"models": []}


class TestVlmLifecycle:
    def test_lifecycle_does_not_infer_mode_from_url(self):
        mod = _load_stage_module()
        runtime = mod._resolve_vlm_runtime({"vlm": {
            "provider": "ollama", "model": "m",
            "base_url": "http://127.0.0.1:11434",
        }})
        assert runtime.manage_lifecycle is False
        assert runtime.launch_mode == "none"

    @pytest.mark.parametrize("mmode", [(False, "wsl"), (True, "none")])
    def test_lifecycle_disabled_never_spawns_model_command(
        self, tmp_path, monkeypatch, mmode,
    ):
        manage, mode = mmode
        mod = _load_stage_module()
        stub = _StubServer({"response": json.dumps({
            "caption": "x", "emotional_core": "awe",
            "gif_worthiness": 0.9, "aesthetic_notes": [],
            "reason": "x",
        })})
        stub.start()
        try:
            work_dir = tmp_path / "vlm_work"
            frames_dir = work_dir / "frames"
            frames_dir.mkdir(parents=True)
            frame_path = frames_dir / "ts_000010.jpg"
            frame_path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 200)
            sm = {
                "schema_version": 1, "stage": "sample",
                "frame_count": 1, "timestamps": [10],
                "frame_paths": [str(frame_path)],
                "frame_entries": [
                    {"artifact_id": "aid-1", "timestamp": 10,
                     "path": str(frame_path)},
                ],
            }
            sm_path = work_dir / "sample_manifest.json"
            sm_path.write_text(json.dumps(sm))
            cfg = mod.extract_config({"adaptive": {}, "preference_memory": {}})
            inputs = {
                "sample_manifest": [{"artifact_id": "a", "path": str(sm_path),
                                     "clip_id": None}],
                "sample_frames": [{"artifact_id": "aid-1", "path": str(frame_path),
                                   "sha256": hashlib.sha256(
                                       frame_path.read_bytes()).hexdigest(),
                                   "size_bytes": frame_path.stat().st_size,
                                   "clip_id": None}],
            }
            config_data = {
                "vlm": {
                    "provider": "ollama", "model": "stub-vlm",
                    "base_url": stub.base_url,
                    "manage_lifecycle": manage,
                    "launch_mode": mode,
                    "retry_delay_s": 0.0,
                },
            }
            spawned = []
            monkeypatch.setattr(mod.subprocess, "run",
                                lambda *a, **kw: spawned.append(a[0]) or None)
            monkeypatch.setattr(mod, "is_local_llm", lambda: False)

            result = mod._stage_vlm(str(frames_dir), str(work_dir), cfg,
                                    inputs, config_data)
            assert result["output_key"] == "vlm"
            assert not spawned, (
                f"no model command must run when manage={manage}, "
                f"mode={mode!r}; got {spawned}"
            )
        finally:
            stub.stop()

    def test_lifecycle_native_uses_native_ollama_command(self, monkeypatch):
        mod = _load_stage_module()
        runtime = mod.VlmRuntimeConfig(
            provider="ollama", model="m", base_url="http://stub",
            manage_lifecycle=True, launch_mode="native", retry_delay_s=0,
        )
        calls = []
        monkeypatch.setattr(mod.subprocess, "run",
                            lambda cmd, **kw: calls.append(cmd)
                            or subprocess.CompletedProcess(cmd, 0))
        monkeypatch.setattr(mod.httpx, "get",
                            lambda *a, **kw: _PsResponse())
        monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
        assert mod.stop_model("m", runtime) is True
        assert calls == [["ollama", "stop", "m"]], calls

    def test_lifecycle_wsl_uses_wsl_command_only_when_explicit(self, monkeypatch):
        mod = _load_stage_module()
        runtime = mod.VlmRuntimeConfig(
            provider="ollama", model="m", base_url="http://stub",
            manage_lifecycle=True, launch_mode="wsl", retry_delay_s=0,
        )
        calls = []
        monkeypatch.setattr(mod.subprocess, "run",
                            lambda cmd, **kw: calls.append(cmd)
                            or subprocess.CompletedProcess(cmd, 0))
        monkeypatch.setattr(mod.httpx, "get",
                            lambda *a, **kw: _PsResponse())
        monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
        assert mod.stop_model("m", runtime) is True
        assert calls == [["wsl", "ollama", "stop", "m"]], calls

    def test_wait_model_uses_frozen_base_url(self, monkeypatch):
        mod = _load_stage_module()
        runtime = mod.VlmRuntimeConfig(
            provider="ollama", model="m", base_url="http://127.0.0.1:45678",
            manage_lifecycle=True, launch_mode="native", retry_delay_s=0,
        )
        urls = []
        monkeypatch.setattr(mod.httpx, "post",
                            lambda url, **kw: urls.append(url)
                            or _StatusResponse())
        assert mod.wait_model("m", runtime, timeout_s=1) is True
        assert urls == ["http://127.0.0.1:45678/api/generate"], urls

    def test_lifecycle_rejects_unknown_launch_mode(self):
        mod = _load_stage_module()
        with pytest.raises(ValueError, match="launch_mode"):
            mod._resolve_vlm_runtime({"vlm": {
                "provider": "ollama", "model": "m",
                "base_url": "http://stub", "launch_mode": "auto",
            }})

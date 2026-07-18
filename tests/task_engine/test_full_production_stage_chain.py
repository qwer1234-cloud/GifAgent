"""§9.2C (sixth-review §8) + seventh-review Tasks 5+6: full 8-stage production
subprocess E2E with tightened assertions, failure scenarios, and zero-clip."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Deterministic HTTP stubs
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _StubServer:
    """Minimal HTTP server that returns a canned payload for every request."""

    def __init__(self, response_payload: dict, *, status_code: int = 200):
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.response_payload = response_payload
        self.status_code = status_code
        self.requests: list[dict] = []
        from http.server import BaseHTTPRequestHandler, HTTPServer

        server = self

        class _Handler(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                try:
                    pld = json.loads(body) if body else {}
                except Exception:
                    pld = {}
                server.requests.append({
                    "path": self.path, "model": pld.get("model", ""),
                    "body_keys": sorted(pld.keys()) if pld else [],
                })
                resp = json.dumps(server.response_payload).encode("utf-8")
                self.send_response(server.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            def do_POST(self):
                self._handle()
            def do_GET(self):
                self._handle()
            def log_message(self, *a):
                pass

        self._server = HTTPServer(("127.0.0.1", self.port), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.2):
                    return
            except OSError:
                time.sleep(0.02)

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


# ---------------------------------------------------------------------------
# Stub response payloads
# ---------------------------------------------------------------------------

_VLM_RESP = {
    "response": json.dumps({
        "caption": "A cinematic frame with dramatic lighting.",
        "emotional_core": "awe", "gif_worthiness": 0.9,
        "aesthetic_notes": ["contrast"], "reason": "dramatic moment",
    }),
}

_VLM_LOW = {
    "response": json.dumps({
        "caption": "A frame.",
        "emotional_core": "neutral", "gif_worthiness": 0.1,
        "aesthetic_notes": [], "reason": "nothing",
    }),
}

_LLM_RESP = {
    "choices": [
        {"message": {"content": json.dumps({
            "summary": "A dramatic scene with strong visual impact.",
            "tags": ["dramatic", "cinematic"],
        })}},
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_test_video(tmp_path: Path, duration: float = 10.0) -> Path:
    video_path = tmp_path / "test.mp4"
    result = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         f"testsrc=d={duration}:r=10:s=128x128",
         "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-t", str(duration), str(video_path)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0 or not video_path.exists():
        pytest.skip(f"ffmpeg unavailable: {result.stderr[:200]}")
    return video_path


def _make_full_config(work_base: Path, export_base: Path, vlm_port: int,
                      llm_port: int, **kw) -> dict:
    return {
        "task_work_dir": str(work_base),
        "export_base_dir": str(export_base),
        "vlm": {
            "provider": "ollama",
            "model": "deterministic-vlm",
            "base_url": f"http://127.0.0.1:{vlm_port}",
            "manage_lifecycle": False,
            "launch_mode": "none",
            "retry_delay_s": 0.0,
            **kw.get("vlm", {}),
        },
        "adaptive": {
            "sample_interval": kw.get("sample_interval", 2),
            "max_duration": 1, "refine_threshold": 0.6,
            "refine_radius": 1, "refine_interval": 1,
            "worthiness_threshold": 0.5, "merge_gap": 2,
            "merge_score_threshold": 0.55, "gif_fps": 24,
            "gif_max_width": 720, "output_ratio": 1.0,
            "max_output": 60, "min_duration": 0.5,
            "potplayer_pbf_enabled": True,
            "embedding_dedup_enabled": False,
            "temporal_dedup_enabled": True,
            "temporal_dedup_min_gap_s": 12,
            "embedding_dedup_threshold": 0.94,
            "clear_output_dir": False,
            "vlm_temperature": 0.65, "vlm_top_p": 0.95,
            "vlm_top_k": 60,
        },
        "preference_memory": {"enabled": False},
        "models": {},
        "database": {"path": ":memory:"},
        "video_paths": [],
        # Task 1 Step 3: LLM config FROZEN in Job snapshot (not global YAML).
        # stage subprocess reads this via set_config_override(config_data).
        "llm": {
            "provider": "openai_compatible",
            "model": "gpt-mini",
            "base_url": f"http://127.0.0.1:{llm_port}",
            "api_key_env": "OPENAI_API_KEY",
            "temperature": 0.3,
            "max_tokens": 256,
            "timeout_s": 10,
            **kw.get("llm", {}),
        },
    }


def _parse_pbf_interval(path: Path) -> tuple[int, int]:
    """Parse a PBF file's first bookmark into (start_ms, end_ms)."""
    text = path.read_bytes()[2:].decode("utf-16-le").replace("\r", "")
    match = re.search(
        r"^\d+=(\d+)\*#\d+\s+([0-9:]+)-([0-9:]+)\s+", text, re.MULTILINE,
    )
    assert match, f"PBF interval not found: {text[:200]!r}"
    start_ms = int(match.group(1))
    end_ms = _timestamp_to_ms(match.group(3))
    title_start_ms = _timestamp_to_ms(match.group(2))
    assert abs(start_ms - title_start_ms) < 1000
    assert end_ms > title_start_ms
    return title_start_ms, end_ms


def _timestamp_to_ms(ts: str) -> int:
    parts = ts.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600000 + int(parts[1]) * 60000 + int(parts[2]) * 1000
    return int(parts[0]) * 60000 + int(parts[1]) * 1000


# ---------------------------------------------------------------------------
# Shared E2E driver (Task 6 Step 1)
# ---------------------------------------------------------------------------


def _drive_full_chain(
    tmp_path, monkeypatch, *,
    vlm_resp: dict = _VLM_RESP,
    llm_resp: dict = _LLM_RESP,
    vlm_status: int = 200,
    config_overrides: dict | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Drive the full 8-stage chain and return verification data.

    Returns a dict with keys: conn, job_id, stages, artifacts, art_by_kind,
    vlm_stub, llm_stub, vlm_requests, llm_requests, formal_dir.
    """
    vlm_stub = _StubServer(vlm_resp, status_code=vlm_status)
    llm_stub = _StubServer(llm_resp)
    vlm_stub.start()
    llm_stub.start()

    try:
        video_dir = tmp_path / "videos"
        video_dir.mkdir()
        video_path = _create_test_video(video_dir, duration=10.0)

        # Task 1 Step 4: LLM config is frozen in Job snapshot, not global YAML.
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        from app.task_engine.schema import connect_task_db
        from app.task_engine.repository import TaskRepository
        from app.task_engine.worker import TaskWorker
        from app.task_engine.adaptive_adapter import AdaptivePipelineAdapter
        from app.task_engine.orchestrator import advance_job, initialize_job
        from app.task_engine.models import CreateJob, RetryPolicy

        db_path = tmp_path / "task.db"
        work_base = tmp_path / "task_work"
        export_base = tmp_path / "exports"

        conn = connect_task_db(db_path)
        # Task 2 Step 2: zero-delay RetryPolicy so retry_wait stages are
        # immediately reclaimable.  Repository AND Worker both get it
        # (fail_stage uses the repo's policy, claim_stage uses the worker's).
        policy = RetryPolicy(
            max_attempts=max_attempts,
            base_delay_seconds=0,
            max_delay_seconds=0,
        )
        repo = TaskRepository(conn, retry_policy=policy)
        config = _make_full_config(work_base, export_base, vlm_stub.port,
                                   llm_stub.port, **(config_overrides or {}))
        job = repo.create_job(CreateJob(
            directory=str(video_dir), config_json=json.dumps(config),
        ))
        initialize_job(repo, job.job_id)

        # Task 1 Step 5: verify frozen config in DB.
        frozen = json.loads(conn.execute(
            "SELECT config_json FROM task_jobs WHERE job_id=?",
            (job.job_id,),
        ).fetchone()["config_json"])
        assert frozen.get("llm", {}).get("model") == "gpt-mini", (
            f"frozen config MUST contain llm.model=gpt-mini, got {frozen.get('llm')}"
        )
        assert frozen["llm"]["base_url"] == llm_stub.base_url, (
            f"frozen llm.base_url={frozen['llm'].get('base_url')}, "
            f"expected {llm_stub.base_url}"
        )

        all_stages = [
            "discover", "sample", "vlm", "refine",
            "synthesize", "rank_dedup", "gif_clip", "materialize",
        ]
        adapters = {s: AdaptivePipelineAdapter(s) for s in all_stages}

        worker = TaskWorker(
            repo, "worker-1", adapters,
            retry_policy=policy,
            lease_seconds=120, heartbeat_seconds=40,
            db_path=str(db_path),
        )
        # Task 2 Step 3: bounded drain loop until terminal (no retry_wait).
        for _ in range(10):
            processed = worker.drain()
            advance_job(repo, job.job_id)
            if processed == 0:
                break
        else:
            pytest.fail("worker did not reach terminal state in 10 drains")

        stages = conn.execute(
            """SELECT s.stage_name, s.status, s.stage_id, s.attempt_count
               FROM task_stages s JOIN task_videos v ON s.video_id = v.video_id
               WHERE v.job_id = ? ORDER BY s.created_at ASC""",
            (job.job_id,),
        ).fetchall()

        artifacts = conn.execute(
            """SELECT a.artifact_kind, a.stage_id, a.sha256, a.size_bytes, a.path
               FROM task_artifacts a JOIN task_videos v ON a.video_id = v.video_id
               WHERE v.job_id = ? ORDER BY a.artifact_kind ASC""",
            (job.job_id,),
        ).fetchall()

        art_by_kind: dict[str, list[dict]] = {}
        for r in artifacts:
            art_by_kind.setdefault(r["artifact_kind"], []).append(dict(r))

        vlm_requests = [r for r in vlm_stub.requests if r["path"] == "/api/generate"]
        llm_requests = [r for r in llm_stub.requests if r["path"] == "/chat/completions"]

        # Task 2 Step 1: real coarse frame count from sample_manifest.
        sample_frame_count = 0
        if "sample_manifest" in art_by_kind:
            sm_data = json.loads(
                Path(art_by_kind["sample_manifest"][0]["path"])
                .read_text(encoding="utf-8")
            )
            sample_frame_count = int(sm_data.get("frame_count", 0))

        vid_status = conn.execute(
            "SELECT status FROM task_videos WHERE job_id=?",
            (job.job_id,),
        ).fetchone()["status"]
        job_status = conn.execute(
            "SELECT status FROM task_jobs WHERE job_id=?",
            (job.job_id,),
        ).fetchone()["status"]

        return {
            "conn": conn, "job_id": job.job_id, "stages": stages,
            "artifacts": artifacts, "art_by_kind": art_by_kind,
            "vlm_stub": vlm_stub, "llm_stub": llm_stub,
            "vlm_requests": vlm_requests, "llm_requests": llm_requests,
            "formal_dir": export_base / "test",
            "video_status": vid_status, "job_status": job_status,
            "sample_frame_count": sample_frame_count,
            "max_attempts": max_attempts,
        }

    except Exception:
        vlm_stub.stop()
        llm_stub.stop()
        raise


# ---------------------------------------------------------------------------
# Full 8-stage production E2E
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestFullProductionStageChain:

    # -- Success chain --------------------------------------------------------

    def test_full_eight_stage_chain(self, tmp_path, monkeypatch):
        d = _drive_full_chain(tmp_path, monkeypatch)
        try:
            conn = d["conn"]
            stages, artifacts = d["stages"], d["artifacts"]
            art_by_kind = d["art_by_kind"]

            # All 8 stage kinds exist + succeeded.
            all_stages = [
                "discover", "sample", "vlm", "refine",
                "synthesize", "rank_dedup", "gif_clip", "materialize",
            ]
            by_name = {}
            for r in stages:
                by_name.setdefault(r["stage_name"], []).append(dict(r))
            assert set(all_stages) <= set(by_name), (
                f"Missing: {set(all_stages) - set(by_name)}"
            )
            for name, lst in by_name.items():
                for inst in lst:
                    assert inst["status"] == "succeeded", (
                        f"{name}({inst['stage_id'][:8]})={inst['status']}"
                    )

            # -- Artifact integrity (SHA/size bound to disk) --
            from app.task_engine.fingerprints import sha256_file
            for art in artifacts:
                p = Path(art["path"])
                assert p.exists()
                assert sha256_file(p) == art["sha256"]
                assert p.stat().st_size == art["size_bytes"]

            # Task 5 Step 1: refine_manifest REQUIRED.
            required = {
                "discover_manifest", "sample_manifest", "vlm_manifest",
                "refine_manifest", "synthesize_manifest",
                "rank_dedup_manifest", "gif_file", "gif_clip_manifest",
                "result", "materialize_manifest",
            }
            assert required <= set(art_by_kind), (
                f"Missing artifact kinds: {required - set(art_by_kind)}"
            )

            # -- Coarse sample --
            sm = json.loads(Path(art_by_kind["sample_manifest"][0]["path"])
                            .read_text(encoding="utf-8"))
            assert sm["frame_count"] > 0

            # -- VLM --
            vm = json.loads(Path(art_by_kind["vlm_manifest"][0]["path"])
                            .read_text(encoding="utf-8"))
            assert vm["parsed_count"] > 0
            assert vm["failed_count"] == 0

            # Task 5 Step 2: refine was real.
            rm = json.loads(Path(art_by_kind["refine_manifest"][0]["path"])
                            .read_text(encoding="utf-8"))
            assert rm["refine_regions"] > 0
            for key in ("refine_requested", "refine_extracted",
                        "refine_attempted", "refine_parsed"):
                assert rm.get(key, 0) > 0, (
                    f"refine.{key} must be > 0, got {rm.get(key)}"
                )
            assert rm.get("refine_failed", -1) == 0
            # Stub got coarse + refine: VLM /api/generate requests > vlm parsed.
            assert len(d["vlm_requests"]) > vm["parsed_count"], (
                f"VLM requests={len(d['vlm_requests'])} "
                f"should > parsed={vm['parsed_count']} (coarse + refine)"
            )

            # -- GIF published --
            formal_dir = d["formal_dir"]
            assert formal_dir.exists()
            gifs = [f for f in formal_dir.iterdir() if f.suffix == ".gif"]
            assert len(gifs) >= 1

            # Task 5 Step 5: PBF with parsed start/end.
            pbf_files = [f for f in formal_dir.iterdir() if f.suffix == ".pbf"]
            assert pbf_files, "PBF must exist"
            start_ms, end_ms = _parse_pbf_interval(pbf_files[0])
            assert start_ms >= 0
            assert end_ms > start_ms

            # Result JSON.
            rd = json.loads((formal_dir / "test_result.json")
                            .read_text(encoding="utf-8"))
            assert rd["gif_count"] >= 1
            for e in rd["succeeded"]:
                fp = Path(e["formal_path"])
                assert fp.exists()
                assert hashlib.sha256(fp.read_bytes()).hexdigest() == e["sha256"]

            # Final status.
            mat = conn.execute(
                "SELECT status FROM task_stages WHERE stage_name='materialize'"
                " AND video_id IN (SELECT video_id FROM task_videos WHERE job_id=?)",
                (d["job_id"],),
            ).fetchone()
            assert mat is not None and mat["status"] == "succeeded", (
                f"materialize={mat['status'] if mat else None}"
            )
            vid = conn.execute(
                "SELECT status FROM task_videos WHERE job_id=?", (d["job_id"],),
            ).fetchone()
            assert vid["status"] == "succeeded"
            job = conn.execute(
                "SELECT status FROM task_jobs WHERE job_id=?", (d["job_id"],),
            ).fetchone()
            assert job["status"] == "succeeded"

            # Task 1 Step 1: LLM stub MUST be called (frozen config).
            assert d["llm_requests"], "synthesize must call deterministic LLM stub"
            for r in d["llm_requests"]:
                assert r["path"] == "/chat/completions"
                assert r["model"] == "gpt-mini"
            synth = json.loads(
                Path(d["art_by_kind"]["synthesize_manifest"][0]["path"])
                .read_text(encoding="utf-8")
            )
            assert synth["clips"], "synthesize must have at least one clip"
            expected = "A dramatic scene with strong visual impact."
            expected_tags = ["dramatic", "cinematic"]
            found = any(
                c.get("summary") == expected and c.get("tags") == expected_tags
                for c in synth["clips"]
            )
            assert found, (
                f"synthesize manifest must contain stub LLM response "
                f"(summary={expected!r}, tags={expected_tags}); "
                f"got clips={synth['clips'][:2]}"
            )
            # Isolation: no WSL/ollama in subprocess.
            # Stub request counts match manifest counters.
            assert len(d["vlm_requests"]) >= vm["parsed_count"] + rm["refine_parsed"]

            conn.close()
        finally:
            d["vlm_stub"].stop()
            d["llm_stub"].stop()

    # -- Outage chain ---------------------------------------------------------

    def test_full_chain_vlm_outage_never_zero_succeeds(self, tmp_path, monkeypatch):
        d = _drive_full_chain(
            tmp_path, monkeypatch,
            vlm_resp={}, vlm_status=503,
        )
        try:
            by_name = {}
            for r in d["stages"]:
                s = dict(r)
                by_name[s["stage_name"]] = s
            # Task 2 Step 1: VLM exhausted all attempts -> needs_attention.
            vlm = by_name["vlm"]
            assert vlm["status"] == "needs_attention", by_name
            assert vlm["attempt_count"] == 3, (
                f"outage must retry 3 times, got {vlm['attempt_count']}"
            )
            assert d["video_status"] == "needs_attention"
            assert d["job_status"] == "needs_attention"
            # No downstream stages, artifacts, or GIFs.
            assert "rank_dedup" not in by_name
            assert "materialize" not in by_name
            assert "result" not in d["art_by_kind"]
            assert "gif_file" not in d["art_by_kind"]
            # Task 2 Step 2: precise VLM HTTP request count.
            # frame_count × max_attempts × 3 (_score_vlm_frame has 3 HTTP retries).
            expected_req = d["sample_frame_count"] * d["max_attempts"] * 3
            assert len(d["vlm_requests"]) == expected_req, (
                f"outage VLM requests={len(d['vlm_requests'])}, "
                f"expected {d['sample_frame_count']} frames × "
                f"{d['max_attempts']} stage attempts × 3 HTTP = {expected_req}"
            )
            d["conn"].close()
        finally:
            d["vlm_stub"].stop()
            d["llm_stub"].stop()

    # -- Invalid payload chain ------------------------------------------------

    def test_full_chain_invalid_vlm_payload_never_exports_default_score_clip(
        self, tmp_path, monkeypatch,
    ):
        d = _drive_full_chain(
            tmp_path, monkeypatch,
            vlm_resp={"response": "{}"},
        )
        try:
            by_name = {}
            for r in d["stages"]:
                s = dict(r)
                by_name[s["stage_name"]] = s
            vlm = by_name["vlm"]
            assert vlm["status"] == "needs_attention", by_name
            assert vlm["attempt_count"] == 3, (
                f"invalid payload must retry 3 times, got {vlm['attempt_count']}"
            )
            assert d["video_status"] == "needs_attention"
            assert d["job_status"] == "needs_attention"
            assert "rank_dedup" not in by_name
            assert "materialize" not in by_name
            assert "result" not in d["art_by_kind"]
            assert "gif_file" not in d["art_by_kind"]
            assert "vlm_manifest" not in d["art_by_kind"], (
                "invalid payload must not produce a VLM manifest"
            )
            # Task 2 Step 3: precise request count matching.
            expected_req = d["sample_frame_count"] * d["max_attempts"] * 3
            assert len(d["vlm_requests"]) == expected_req, (
                f"invalid-payload VLM requests={len(d['vlm_requests'])}, "
                f"expected {d['sample_frame_count']} frames × "
                f"{d['max_attempts']} stage attempts × 3 HTTP = {expected_req}"
            )
            d["conn"].close()
        finally:
            d["vlm_stub"].stop()
            d["llm_stub"].stop()

    # -- Valid low-score zero-clip chain --------------------------------------

    def test_full_chain_valid_low_scores_materialize_zero_clip(
        self, tmp_path, monkeypatch,
    ):
        d = _drive_full_chain(
            tmp_path, monkeypatch,
            vlm_resp=_VLM_LOW,
        )
        try:
            conn = d["conn"]
            by_name = {}
            for r in d["stages"]:
                by_name.setdefault(r["stage_name"], []).append(dict(r))
            # All non-gif stages succeeded; no gif_clip stages created.
            non_gif = {"discover", "sample", "vlm", "refine",
                       "synthesize", "rank_dedup"}
            assert non_gif <= set(by_name)
            for name in non_gif:
                for inst in by_name[name]:
                    assert inst["status"] == "succeeded", (
                        f"{name}={inst['status']}"
                    )
            assert "gif_clip" not in by_name, "zero-clip: no gif_clip stages"
            # materialize may or may not be created depending on orchestrator
            # timing; the zero-clip proof is in the job/video success below.
            assert "gif_file" not in d["art_by_kind"], "zero-clip: no gif_file"

            # rank_dedup manifest declares zero clips.
            rm = json.loads(
                Path(d["art_by_kind"]["rank_dedup_manifest"][0]["path"])
                .read_text(encoding="utf-8")
            )
            assert rm["clip_count"] == 0
            assert rm["clips"] == []

            # materialize manifest gif_count=0.
            mm = json.loads(
                Path(d["art_by_kind"]["materialize_manifest"][0]["path"])
                .read_text(encoding="utf-8")
            )
            assert mm["gif_count"] == 0

            # Job + video succeeded.
            for q in ("task_videos", "task_jobs"):
                s = conn.execute(
                    f"SELECT status FROM {q} WHERE job_id=?",
                    (d["job_id"],),
                ).fetchone()["status"]
                assert s == "succeeded", f"{q} status={s}"

            # No GIFs published.
            fd = d["formal_dir"]
            gifs = [f for f in fd.iterdir() if f.suffix == ".gif"] if fd.exists() else []
            assert not gifs, f"zero-clip must not publish GIFs, got {[p.name for p in gifs]}"

            conn.close()
        finally:
            d["vlm_stub"].stop()
            d["llm_stub"].stop()

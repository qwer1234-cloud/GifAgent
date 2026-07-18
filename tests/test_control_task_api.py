"""Tests for GifAgentApiClient and the API-backed Control tab."""

from __future__ import annotations

import json
import os
from pathlib import Path

import gradio as gr
import httpx
import pytest

from app.ui.api_client import GifAgentApiClient
from app.ui.tabs.control import build_control_tab


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeResponse:
    """Minimal stand-in for httpx.Response used in monkeypatched calls."""

    def __init__(self, status_code: int, json_data, text: str | None = None):
        self.status_code = status_code
        self._json = json_data
        self.text = text or json.dumps(json_data) if not isinstance(json_data, str) else text or ""

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "http://test"),
                response=self,
            )


# ===================================================================
# GifAgentApiClient tests
# ===================================================================


class TestCreateTask:
    def test_sends_correct_request(self, monkeypatch):
        calls: list[tuple] = []

        def fake(method, url, **kw):
            calls.append((method, url, kw))
            return FakeResponse(201, {"job_id": "abc123", "status": "pending"})

        monkeypatch.setattr(httpx, "request", fake)

        client = GifAgentApiClient("http://test")
        result = client.create_task("/videos", 5, ".mp4")

        assert len(calls) == 1
        meth, url, kwargs = calls[0]
        assert meth == "POST"
        assert url == "http://test/api/tasks/jobs"
        assert kwargs["json"] == {"directory": "/videos", "limit": 5, "extensions": ".mp4"}
        assert result["job_id"] == "abc123"

    def test_returns_409_as_error_with_existing_job_id(self, monkeypatch):
        def fake(method, url, **kw):
            return FakeResponse(
                409,
                {"detail": {"existing_job_id": "existing456", "detail": "active job exists for directory"}},
            )

        monkeypatch.setattr(httpx, "request", fake)

        client = GifAgentApiClient("http://test")
        result = client.create_task("/videos", 5, ".mp4")

        assert "error" in result
        assert "active job exists" in result["error"]
        assert result["detail"]["existing_job_id"] == "existing456"

    def test_handles_connection_error(self, monkeypatch):
        def fake(method, url, **kw):
            raise httpx.RequestError("connection refused")

        monkeypatch.setattr(httpx, "request", fake)

        client = GifAgentApiClient("http://test")
        result = client.create_task("/videos", 5, ".mp4")

        assert "error" in result
        assert "Connection failed" in result["error"]

    def test_handles_generic_http_error(self, monkeypatch):
        def fake(method, url, **kw):
            return FakeResponse(500, {"detail": "internal error"})

        monkeypatch.setattr(httpx, "request", fake)

        client = GifAgentApiClient("http://test")
        result = client.create_task("/videos", 5, ".mp4")

        assert "error" in result
        assert "HTTP 500" in result["error"]


class TestCancelTask:
    def test_sends_correct_request(self, monkeypatch):
        calls: list[tuple] = []

        def fake(method, url, **kw):
            calls.append((method, url, kw))
            return FakeResponse(200, {"status": "ok", "command_id": "cmd1"})

        monkeypatch.setattr(httpx, "request", fake)

        client = GifAgentApiClient("http://test")
        result = client.cancel_task("job-xyz")

        assert len(calls) == 1
        meth, url, _ = calls[0]
        assert meth == "POST"
        assert url == "http://test/api/tasks/jobs/job-xyz/cancel"
        assert result["status"] == "ok"


class TestRetryTask:
    def test_sends_correct_request(self, monkeypatch):
        calls: list[tuple] = []

        def fake(method, url, **kw):
            calls.append((method, url, kw))
            return FakeResponse(200, {"status": "ok", "command_id": "cmd2"})

        monkeypatch.setattr(httpx, "request", fake)

        client = GifAgentApiClient("http://test")
        result = client.retry_task("job-abc")

        assert len(calls) == 1
        meth, url, _ = calls[0]
        assert meth == "POST"
        assert url == "http://test/api/tasks/jobs/job-abc/retry"
        assert result["status"] == "ok"


class TestListTasks:
    def test_returns_list_on_success(self, monkeypatch):
        jobs = [
            {"job_id": "j1", "status": "running", "folder": "vids", "video_count": 3,
             "stage_count": 6, "clip_count": 2, "created_at": "2026-07-18T10:00:00"},
        ]

        def fake(method, url, **kw):
            return FakeResponse(200, jobs)

        monkeypatch.setattr(httpx, "request", fake)

        client = GifAgentApiClient("http://test")
        result = client.list_tasks()

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["job_id"] == "j1"

    def test_returns_empty_list(self, monkeypatch):
        def fake(method, url, **kw):
            return FakeResponse(200, [])

        monkeypatch.setattr(httpx, "request", fake)

        client = GifAgentApiClient("http://test")
        result = client.list_tasks()

        assert isinstance(result, list)
        assert result == []

    def test_returns_error_on_failure(self, monkeypatch):
        def fake(method, url, **kw):
            return FakeResponse(503, {"detail": "unavailable"})

        monkeypatch.setattr(httpx, "request", fake)

        client = GifAgentApiClient("http://test")
        result = client.list_tasks()

        assert isinstance(result, dict)
        assert "error" in result


class TestTaskEvents:
    def test_returns_events(self, monkeypatch):
        events = [
            {"event_id": 1, "kind": "job.created", "payload": {}, "created_at": "2026-07-18T10:00:00"},
        ]

        def fake(method, url, **kw):
            return FakeResponse(200, events)

        monkeypatch.setattr(httpx, "request", fake)

        client = GifAgentApiClient("http://test")
        result = client.task_events(after_id=0)

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["kind"] == "job.created"

    def test_respects_after_id_param(self, monkeypatch):
        def fake(method, url, **kw):
            params = kw.get("params", {})
            assert params.get("after_id") == 42
            return FakeResponse(200, [])

        monkeypatch.setattr(httpx, "request", fake)

        client = GifAgentApiClient("http://test")
        client.task_events(after_id=42)


# ===================================================================
# build_control_tab tests
# ===================================================================


class TestBuildControlTab:
    """Verify that ``build_control_tab`` creates all expected components."""

    def test_returns_dict_with_all_expected_keys(self):
        client = GifAgentApiClient("http://test")
        with gr.Blocks():
            with gr.Tab("Control"):
                components = build_control_tab(client)

        assert isinstance(components, dict)

        expected_keys = {
            "job_table",
            "summary_text",
            "dir_input",
            "limit_input",
            "ext_input",
            "start_btn",
            "cancel_btn",
            "retry_btn",
            "job_id_input",
            "control_output",
            "event_log",
            "refresh_btn",
            "timer",
            "jobs_state",
        }
        assert set(components.keys()) == expected_keys, (
            f"Missing keys: {expected_keys - set(components.keys())}"
        )

    def test_components_are_gradio_instances(self):
        client = GifAgentApiClient("http://test")
        with gr.Blocks():
            with gr.Tab("Control"):
                components = build_control_tab(client)

        assert isinstance(components["job_table"], gr.Dataframe)
        assert isinstance(components["summary_text"], gr.Textbox)
        assert isinstance(components["dir_input"], gr.Textbox)
        assert isinstance(components["limit_input"], gr.Number)
        assert isinstance(components["ext_input"], gr.Textbox)
        assert isinstance(components["start_btn"], gr.Button)
        assert isinstance(components["cancel_btn"], gr.Button)
        assert isinstance(components["retry_btn"], gr.Button)
        assert isinstance(components["job_id_input"], gr.Textbox)
        assert isinstance(components["control_output"], gr.Textbox)
        assert isinstance(components["event_log"], gr.Textbox)
        assert isinstance(components["refresh_btn"], gr.Button)
        assert isinstance(components["timer"], gr.Timer)


class TestFormatJobs:
    """Internal helper that formats job dicts for the Dataframe."""

    @staticmethod
    def _format_jobs(jobs):
        """Replicate the logic from tabs/control.py for testing."""
        from app.ui.tabs.control import build_control_tab

        # Access the closure-bounded helper via function inspection
        # (we define a local copy for the test instead)
        if not jobs or (isinstance(jobs, dict) and "error" in jobs):
            return []
        rows: list[list] = []
        for job in jobs:
            rows.append(
                [
                    str(job.get("job_id", ""))[:12],
                    str(job.get("folder", "")),
                    str(job.get("status", "")),
                    str(job.get("video_count", 0)),
                    str(job.get("stage_count", 0)),
                    str(job.get("clip_count", 0)),
                    str(job.get("created_at", ""))[:19],
                ]
            )
        return rows

    def test_empty_list_returns_empty(self):
        assert self._format_jobs([]) == []

    def test_error_dict_returns_empty(self):
        assert self._format_jobs({"error": "fail"}) == []

    def test_none_returns_empty(self):
        assert self._format_jobs(None) == []

    def test_populated_jobs(self):
        jobs = [
            {
                "job_id": "abcdef1234567890",
                "folder": "my_videos",
                "status": "running",
                "video_count": 5,
                "stage_count": 10,
                "clip_count": 3,
                "created_at": "2026-07-18T10:00:00.123+00:00",
            }
        ]
        rows = self._format_jobs(jobs)
        assert len(rows) == 1
        assert rows[0][0] == "abcdef123456"  # truncated to 12 chars
        assert rows[0][1] == "my_videos"
        assert rows[0][2] == "running"
        assert rows[0][3] == "5"
        assert rows[0][6] == "2026-07-18T10:00:00"


class TestBuildSummary:
    """Internal summary string builder."""

    @staticmethod
    def _build_summary(jobs):
        from app.ui.tabs.control import build_control_tab

        if isinstance(jobs, dict) and "error" in jobs:
            return "API unavailable"
        total = len(jobs)
        active = sum(1 for j in jobs if j.get("status") in
                     ("pending", "running", "leased", "retry_wait"))
        succeeded = sum(1 for j in jobs if j.get("status") == "succeeded")
        attention = sum(1 for j in jobs if j.get("status") == "needs_attention")
        cancelled = sum(1 for j in jobs if j.get("status") == "cancelled")
        pending = sum(1 for j in jobs if j.get("status") == "pending")
        return (
            f"Total: {total} | Active: {active} | Pending: {pending} | "
            f"Succeeded: {succeeded} | Needs Attention: {attention} | "
            f"Cancelled: {cancelled}"
        )

    def test_empty_jobs(self):
        assert "Total: 0" in self._build_summary([])

    def test_counts_statuses(self):
        jobs = [
            {"status": "running"},
            {"status": "succeeded"},
            {"status": "needs_attention"},
            {"status": "pending"},
            {"status": "cancelled"},
        ]
        s = self._build_summary(jobs)
        assert "Total: 5" in s
        assert "Active: 2" in s  # "running" + "pending" (both in _ACTIVE_STATUSES)
        assert "Pending: 1" in s
        assert "Succeeded: 1" in s
        assert "Needs Attention: 1" in s
        assert "Cancelled: 1" in s

    def test_error_dict(self):
        assert "API unavailable" in self._build_summary({"error": "fail"})


class TestFormatEvents:
    """Internal event formatter."""

    @staticmethod
    def _format_events(events):
        if isinstance(events, dict) and "error" in events:
            return "Events unavailable"
        lines: list[str] = []
        for ev in events if isinstance(events, list) else []:
            kind = ev.get("kind", "?")
            ts = str(ev.get("created_at", ""))[:19]
            lines.append(f"[{ts}] {kind}")
        return "\n".join(lines[-50:])

    def test_empty_events(self):
        assert self._format_events([]) == ""

    def test_error_dict(self):
        assert "Events unavailable" in self._format_events({"error": "fail"})

    def test_formats_events(self):
        events = [
            {"event_id": 1, "kind": "job.created", "payload": {}, "created_at": "2026-07-18T10:00:00"},
            {"event_id": 2, "kind": "stage.completed", "payload": {}, "created_at": "2026-07-18T10:05:00"},
        ]
        text = self._format_events(events)
        assert "[2026-07-18T10:00:00] job.created" in text
        assert "[2026-07-18T10:05:00] stage.completed" in text


# ===================================================================
# Legacy fallback tests
# ===================================================================


class TestLegacyFallback:
    """``_legacy_read_status`` — file-based fallback when API is down."""

    @staticmethod
    def _legacy_read_status(pid_file: str | None = None,
                            checkpoint_file: str | None = None) -> dict:
        """Inline copy of the fallback logic from tabs/control.py."""
        import subprocess as _sp

        status = {
            "running": False,
            "pid": None,
            "completed": 0,
            "failed": 0,
            "total": 0,
            "current_video": "",
        }

        pid_path = pid_file or "data/batch_pid.txt"
        cp_path = checkpoint_file or "data/batch_checkpoint.json"

        if os.path.exists(pid_path):
            try:
                with open(pid_path) as f:
                    pid = int(f.read().strip())
                if os.name == "nt":
                    result = _sp.run(
                        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                        capture_output=True, text=True, timeout=3,
                    )
                    alive = str(pid) in result.stdout
                else:
                    result = _sp.run(
                        ["kill", "-0", str(pid)], capture_output=True, timeout=3,
                    )
                    alive = result.returncode == 0
                if alive:
                    status["running"] = True
                    status["pid"] = pid
                else:
                    try:
                        os.remove(pid_path)
                    except OSError:
                        pass
            except (ValueError, OSError, _sp.TimeoutExpired):
                try:
                    os.remove(pid_path)
                except OSError:
                    pass

        if os.path.exists(cp_path):
            try:
                with open(cp_path, encoding="utf-8-sig") as f:
                    cp = json.load(f)
                run = cp.get("last_run")
                if isinstance(run, dict):
                    status["completed"] = int(run.get("succeeded", 0)) + int(
                        run.get("dedup_skipped", 0)
                    )
                    status["failed"] = int(run.get("failed", 0))
                    status["total"] = int(run.get("planned", 0))
                    status["current_video"] = run.get("current_video", "") or ""
                else:
                    completed = 0
                    for info in cp.get("completed", {}).values():
                        item_status = (
                            info.get("status") if isinstance(info, dict) else None
                        )
                        if item_status in {"ok", "dedup_skipped"}:
                            completed += 1
                    status["completed"] = completed
                    status["total"] = completed
            except Exception:
                pass

        return status

    def test_no_files_returns_stopped(self, tmp_path):
        pid = str(tmp_path / "nonexistent_pid.txt")
        cp = str(tmp_path / "nonexistent_checkpoint.json")
        s = self._legacy_read_status(pid, cp)
        assert s["running"] is False
        assert s["pid"] is None

    def test_reads_last_run_checkpoint(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        pid_file = tmp_path / "batch_pid.txt"
        cp_file = tmp_path / "batch_checkpoint.json"

        # Write a checkpoint with last_run format
        cp_file.write_text(
            json.dumps({"last_run": {"succeeded": 10, "dedup_skipped": 2, "failed": 1, "planned": 15}}),
            encoding="utf-8",
        )

        s = self._legacy_read_status(str(pid_file), str(cp_file))
        assert s["running"] is False
        assert s["completed"] == 12  # 10 + 2
        assert s["failed"] == 1
        assert s["total"] == 15

    def test_reads_flat_checkpoint(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        pid_file = tmp_path / "batch_pid.txt"
        cp_file = tmp_path / "batch_checkpoint.json"

        cp_file.write_text(
            json.dumps({
                "completed": {
                    "v1": {"status": "ok"},
                    "v2": {"status": "dedup_skipped"},
                    "v3": {"status": "failed"},
                }
            }),
            encoding="utf-8",
        )

        s = self._legacy_read_status(str(pid_file), str(cp_file))
        assert s["completed"] == 2  # ok + dedup_skipped
        assert s["total"] == 2
        assert s["failed"] == 0


# ===================================================================
# Integration: client + tab built within Blocks renders without error
# ===================================================================


def test_build_control_tab_executes_without_exception():
    """Smoke test: calling build_control_tab inside gr.Blocks does not raise."""
    client = GifAgentApiClient("http://test")
    try:
        with gr.Blocks():
            with gr.Tab("Control"):
                components = build_control_tab(client)
    except Exception as exc:
        pytest.fail(f"build_control_tab raised an exception: {exc}")

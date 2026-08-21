"""Deterministic tests for the WSL/Ollama embedding runtime manager.

All subprocess and HTTP boundaries are mocked; no real WSL or Ollama is
launched from these tests.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import httpx
import pytest

from app.services import ollama_runtime


class FakeProc:
    """Minimal subprocess.Popen stand-in for keeper lifecycle tests."""

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.terminated = False
        self.killed = False
        self._returncode = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = 0

    def kill(self):
        self.killed = True
        self._returncode = 0

    def wait(self, timeout=None):
        return self._returncode


def _version_response():
    return SimpleNamespace(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: {"version": "0.30.5"},
    )


def _embed_response(dim=768, count=1):
    return SimpleNamespace(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: {"embeddings": [[0.0] * dim for _ in range(count)]},
    )


def _config(**overrides):
    values = dict(
        base_url="auto",
        manage_lifecycle=True,
        launch_mode="wsl",
        wsl_distro="Ubuntu-20.04",
        startup_timeout_s=5.0,
        request_timeout_s=60.0,
        retry_attempts=3,
        retry_backoff_s=0.0,
        keep_alive="30m",
        embedding_model="nomic-embed-text:latest",
        embedding_dim=768,
    )
    values.update(overrides)
    return ollama_runtime.EmbeddingRuntimeConfig(**values)


def _install_success_mocks(monkeypatch, *, procs=None, runs=None, gets=None, posts=None):
    procs = [] if procs is None else procs
    runs = [] if runs is None else runs
    gets = [] if gets is None else gets
    posts = [] if posts is None else posts

    def popen(cmd, **kwargs):
        proc = FakeProc(cmd, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(ollama_runtime.subprocess, "Popen", popen)

    def run(cmd, **kwargs):
        runs.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout="172.27.227.98  fe80::1\n",
            stderr="",
        )

    monkeypatch.setattr(ollama_runtime.subprocess, "run", run)
    monkeypatch.setattr(
        ollama_runtime.httpx, "get", lambda url, **kw: gets.append(url) or _version_response()
    )
    monkeypatch.setattr(
        ollama_runtime.httpx,
        "post",
        lambda url, **kw: posts.append((url, kw)) or _embed_response(),
    )
    return procs, runs, gets, posts


def test_env_override_wins_is_normalized_and_suppresses_wsl_launch(monkeypatch):
    monkeypatch.setenv("GIFAGENT_OLLAMA_BASE", " http://HOST:11434/ ")
    config = _config(base_url="http://explicit:11434")
    manager = ollama_runtime.OllamaRuntimeManager()

    calls = []
    monkeypatch.setattr(
        ollama_runtime.subprocess,
        "Popen",
        lambda *a, **k: calls.append(("Popen", a, k)) or FakeProc(*a, **k),
    )
    monkeypatch.setattr(
        ollama_runtime.subprocess,
        "run",
        lambda *a, **k: calls.append(("run", a, k))
        or SimpleNamespace(returncode=0, stdout="172.27.227.98\n", stderr=""),
    )
    monkeypatch.setattr(
        ollama_runtime.httpx, "get", lambda url, **kw: _version_response()
    )
    monkeypatch.setattr(
        ollama_runtime.httpx, "post", lambda url, **kw: _embed_response()
    )

    assert manager.resolve_base_url(config) == "http://HOST:11434"
    state = manager.ensure_ready(config)

    assert state.base_url == "http://HOST:11434"
    assert calls == []
    manager.shutdown()


def test_automatic_wsl_starts_keeper_discovers_polls_and_prewarms_once(monkeypatch):
    monkeypatch.delenv("GIFAGENT_OLLAMA_BASE", raising=False)
    manager = ollama_runtime.OllamaRuntimeManager()
    procs, runs, gets, posts = _install_success_mocks(monkeypatch)

    state = manager.ensure_ready(_config())

    assert state.base_url == "http://172.27.227.98:11434"
    assert len(procs) == 1
    proc = procs[0]
    assert proc.cmd == [
        "wsl.exe",
        "-d",
        "Ubuntu-20.04",
        "--exec",
        "sleep",
        "infinity",
    ]
    assert proc.kwargs["stdout"] == subprocess.DEVNULL
    assert proc.kwargs["stderr"] == subprocess.DEVNULL
    assert (
        proc.kwargs["creationflags"]
        == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    assert runs == [["wsl.exe", "-d", "Ubuntu-20.04", "--", "hostname", "-I"]]
    assert gets == ["http://172.27.227.98:11434/api/version"]
    assert len(posts) == 1
    post_url, post_kwargs = posts[0]
    assert post_url == "http://172.27.227.98:11434/api/embed"
    assert post_kwargs["json"] == {
        "model": "nomic-embed-text:latest",
        "input": ["ping"],
        "keep_alive": "30m",
    }

    # Repeated readiness calls reuse the successful state and keeper.
    again = manager.ensure_ready(_config())
    assert again is state
    assert len(procs) == 1
    assert len(runs) == 1
    assert len(gets) == 1
    assert len(posts) == 1
    manager.shutdown()


def test_shutdown_is_idempotent_and_only_terminates_owned_keeper(monkeypatch):
    monkeypatch.delenv("GIFAGENT_OLLAMA_BASE", raising=False)
    manager = ollama_runtime.OllamaRuntimeManager()
    procs, runs, gets, posts = _install_success_mocks(monkeypatch)
    external = FakeProc(["external"])

    state = manager.ensure_ready(_config())
    assert state.keeper is procs[0]

    assert manager.shutdown() is True
    assert procs[0].terminated is True
    assert external.terminated is False
    # Second shutdown is a no-op.
    assert manager.shutdown() is False
    assert procs[0].terminated is True

    # A later readiness call starts a fresh owned keeper.
    state2 = manager.ensure_ready(_config())
    assert len(procs) == 2
    assert state2.keeper is procs[1]
    manager.shutdown()
    assert procs[1].terminated is True
    assert external.terminated is False


def test_transport_invalidation_rediscovers_changed_wsl_ip(monkeypatch):
    monkeypatch.delenv("GIFAGENT_OLLAMA_BASE", raising=False)
    manager = ollama_runtime.OllamaRuntimeManager()
    procs, runs, gets, posts = _install_success_mocks(monkeypatch)

    first = manager.ensure_ready(_config())
    assert first.base_url == "http://172.27.227.98:11434"

    manager.invalidate()

    # Simulate the WSL VM getting a new address after reboot/restart.
    def run(cmd, **kwargs):
        runs.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout="172.27.228.10\n",
            stderr="",
        )

    monkeypatch.setattr(ollama_runtime.subprocess, "run", run)
    second = manager.ensure_ready(_config())

    assert second.base_url == "http://172.27.228.10:11434"
    assert second is not first
    assert len(procs) == 1  # the owned keeper is reused
    assert len(runs) == 2
    assert len(gets) == 2
    assert len(posts) == 2
    manager.shutdown()


def test_startup_timeout_raises_structured_retryable_error(monkeypatch):
    monkeypatch.delenv("GIFAGENT_OLLAMA_BASE", raising=False)
    manager = ollama_runtime.OllamaRuntimeManager()
    procs, runs, gets, posts = _install_success_mocks(monkeypatch)
    monkeypatch.setattr(ollama_runtime.time, "sleep", lambda _s: None)

    def flaky_get(url, **kwargs):
        raise httpx.ConnectError(
            "no route to host",
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(ollama_runtime.httpx, "get", flaky_get)

    with pytest.raises(ollama_runtime.EmbeddingRuntimeError) as excinfo:
        manager.ensure_ready(_config(startup_timeout_s=1.0))

    err = excinfo.value
    assert err.phase == "startup"
    assert err.retryable is True
    assert err.attempts >= 1
    assert err.base_url == "http://172.27.227.98:11434"
    assert "no route to host" in str(err.cause)
    manager.shutdown()


def test_prewarm_wrong_dimension_is_not_retried(monkeypatch):
    monkeypatch.delenv("GIFAGENT_OLLAMA_BASE", raising=False)
    manager = ollama_runtime.OllamaRuntimeManager()
    procs, runs, gets, posts = _install_success_mocks(monkeypatch)

    def wrong_dim_post(url, **kwargs):
        posts.append((url, kwargs))
        return SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {"embeddings": [[0.0] * 4]},
        )

    monkeypatch.setattr(ollama_runtime.httpx, "post", wrong_dim_post)

    with pytest.raises(ollama_runtime.EmbeddingRuntimeError) as excinfo:
        manager.ensure_ready(_config())

    err = excinfo.value
    assert err.phase == "prewarm"
    assert err.retryable is False
    assert err.base_url == "http://172.27.227.98:11434"
    assert len(posts) == 1
    manager.shutdown()


def test_automatic_mode_never_invokes_wsl_on_non_windows(monkeypatch):
    monkeypatch.delenv("GIFAGENT_OLLAMA_BASE", raising=False)
    monkeypatch.setattr(ollama_runtime.os, "name", "posix")
    manager = ollama_runtime.OllamaRuntimeManager()
    calls = []
    monkeypatch.setattr(
        ollama_runtime.subprocess,
        "Popen",
        lambda *a, **k: calls.append(("Popen", a, k)) or FakeProc(*a, **k),
    )
    monkeypatch.setattr(
        ollama_runtime.subprocess,
        "run",
        lambda *a, **k: calls.append(("run", a, k))
        or SimpleNamespace(returncode=0, stdout="172.27.227.98\n", stderr=""),
    )

    assert manager.resolve_base_url(_config()) == "http://127.0.0.1:11434"
    assert calls == []


def test_keeper_launch_failure_raises_structured_launch_error(monkeypatch):
    monkeypatch.delenv("GIFAGENT_OLLAMA_BASE", raising=False)
    manager = ollama_runtime.OllamaRuntimeManager()

    def failing_popen(cmd, **kwargs):
        raise FileNotFoundError("wsl.exe not found")

    monkeypatch.setattr(ollama_runtime.subprocess, "Popen", failing_popen)

    def unexpected_run(cmd, **kwargs):
        raise AssertionError("discovery must not run after keeper launch failure")

    monkeypatch.setattr(ollama_runtime.subprocess, "run", unexpected_run)

    with pytest.raises(ollama_runtime.EmbeddingRuntimeError) as excinfo:
        manager.ensure_ready(_config())

    err = excinfo.value
    assert err.phase == "launch"
    assert err.retryable is True
    assert err.attempts == 1
    assert err.base_url is None
    assert "wsl.exe not found" in str(err)
    manager.shutdown()


def test_version_poll_permanent_4xx_fails_immediately_without_retry(monkeypatch):
    monkeypatch.delenv("GIFAGENT_OLLAMA_BASE", raising=False)
    manager = ollama_runtime.OllamaRuntimeManager()
    procs, runs, gets, posts = _install_success_mocks(monkeypatch)

    gets.clear()
    sleeps = []
    monkeypatch.setattr(ollama_runtime.time, "sleep", lambda _s: sleeps.append(_s))

    def bad_version(url, **kwargs):
        gets.append(url)
        return SimpleNamespace(
            status_code=400,
            raise_for_status=lambda: None,
            json=lambda: {},
        )

    monkeypatch.setattr(ollama_runtime.httpx, "get", bad_version)

    with pytest.raises(ollama_runtime.EmbeddingRuntimeError) as excinfo:
        manager.ensure_ready(_config(startup_timeout_s=120.0))

    err = excinfo.value
    assert err.phase == "startup"
    assert err.retryable is False
    assert err.attempts == 1
    assert err.base_url == "http://172.27.227.98:11434"
    assert "HTTP 400" in str(err)
    assert len(gets) == 1
    assert sleeps == []
    manager.shutdown()


def test_version_poll_503_retries_until_startup_timeout(monkeypatch):
    monkeypatch.delenv("GIFAGENT_OLLAMA_BASE", raising=False)
    manager = ollama_runtime.OllamaRuntimeManager()
    procs, runs, gets, posts = _install_success_mocks(monkeypatch)

    gets.clear()
    sleeps = []
    monkeypatch.setattr(ollama_runtime.time, "sleep", lambda _s: sleeps.append(_s))

    def busy_version(url, **kwargs):
        gets.append(url)
        return SimpleNamespace(
            status_code=503,
            raise_for_status=lambda: None,
            json=lambda: {},
        )

    monkeypatch.setattr(ollama_runtime.httpx, "get", busy_version)

    with pytest.raises(ollama_runtime.EmbeddingRuntimeError) as excinfo:
        manager.ensure_ready(_config(startup_timeout_s=1.0))

    err = excinfo.value
    assert err.phase == "startup"
    assert err.retryable is True
    assert err.attempts >= 2
    assert err.base_url == "http://172.27.227.98:11434"
    assert len(gets) >= 2
    assert sleeps
    manager.shutdown()


def test_prewarm_non_object_json_is_not_retried(monkeypatch):
    monkeypatch.delenv("GIFAGENT_OLLAMA_BASE", raising=False)
    manager = ollama_runtime.OllamaRuntimeManager()
    procs, runs, gets, posts = _install_success_mocks(monkeypatch)

    def bad_post(url, **kwargs):
        posts.append((url, kwargs))
        return SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: ["not", "an", "object"],
        )

    monkeypatch.setattr(ollama_runtime.httpx, "post", bad_post)

    with pytest.raises(ollama_runtime.EmbeddingRuntimeError) as excinfo:
        manager.ensure_ready(_config())

    err = excinfo.value
    assert err.phase == "prewarm"
    assert err.retryable is False
    assert err.attempts == 1
    assert "JSON object" in str(err)
    assert len(posts) == 1
    manager.shutdown()


def test_ephemeral_wsl_endpoint_detection(monkeypatch):
    monkeypatch.setattr(ollama_runtime.os, "name", "nt")
    assert ollama_runtime.is_ephemeral_wsl_endpoint(
        "http://172.27.227.98:11434"
    )
    assert ollama_runtime.is_ephemeral_wsl_endpoint("172.31.0.1:11434")
    assert not ollama_runtime.is_ephemeral_wsl_endpoint(
        "http://172.15.0.1:11434"
    )
    assert not ollama_runtime.is_ephemeral_wsl_endpoint(
        "http://172.32.0.1:11434"
    )
    assert not ollama_runtime.is_ephemeral_wsl_endpoint("http://10.0.0.5:11434")
    assert not ollama_runtime.is_ephemeral_wsl_endpoint(
        "http://127.0.0.1:11434"
    )
    assert not ollama_runtime.is_ephemeral_wsl_endpoint("http://gpu-box:11434")
    assert not ollama_runtime.is_ephemeral_wsl_endpoint(
        "http://172.27.227.98:8000"
    )
    assert not ollama_runtime.is_ephemeral_wsl_endpoint("auto")
    monkeypatch.setattr(ollama_runtime.os, "name", "posix")
    assert not ollama_runtime.is_ephemeral_wsl_endpoint(
        "http://172.27.227.98:11434"
    )


def test_stale_wsl_nat_url_is_rediscovered_on_windows(monkeypatch):
    monkeypatch.delenv("GIFAGENT_OLLAMA_BASE", raising=False)
    monkeypatch.setattr(ollama_runtime.os, "name", "nt")
    manager = ollama_runtime.OllamaRuntimeManager()
    procs, runs, gets, posts = _install_success_mocks(monkeypatch)

    url = manager.resolve_base_url(
        _config(base_url="http://172.16.9.9:11434", manage_lifecycle=False)
    )

    assert url == "http://172.27.227.98:11434"
    assert runs == [["wsl.exe", "-d", "Ubuntu-20.04", "--", "hostname", "-I"]]
    assert procs == []
    manager.shutdown()


def test_explicit_hostname_endpoint_is_not_rediscovered(monkeypatch):
    monkeypatch.delenv("GIFAGENT_OLLAMA_BASE", raising=False)
    monkeypatch.setattr(ollama_runtime.os, "name", "nt")
    manager = ollama_runtime.OllamaRuntimeManager()
    calls = []
    monkeypatch.setattr(
        ollama_runtime.subprocess,
        "run",
        lambda *a, **k: calls.append(("run", a, k))
        or SimpleNamespace(returncode=0, stdout="1.2.3.4\n", stderr=""),
    )

    assert (
        manager.resolve_base_url(_config(base_url="http://gpu-box:11434"))
        == "http://gpu-box:11434"
    )
    assert calls == []


def test_automatic_mode_discovers_wsl_without_lifecycle(monkeypatch):
    monkeypatch.delenv("GIFAGENT_OLLAMA_BASE", raising=False)
    monkeypatch.setattr(ollama_runtime.os, "name", "nt")
    manager = ollama_runtime.OllamaRuntimeManager()
    procs, runs, gets, posts = _install_success_mocks(monkeypatch)

    url = manager.resolve_base_url(_config(manage_lifecycle=False))

    assert url == "http://172.27.227.98:11434"
    assert procs == []
    assert runs == [["wsl.exe", "-d", "Ubuntu-20.04", "--", "hostname", "-I"]]
    manager.shutdown()


def test_stale_wsl_url_ensure_ready_uses_auto_lifecycle(monkeypatch):
    monkeypatch.delenv("GIFAGENT_OLLAMA_BASE", raising=False)
    monkeypatch.setattr(ollama_runtime.os, "name", "nt")
    manager = ollama_runtime.OllamaRuntimeManager()
    procs, runs, gets, posts = _install_success_mocks(monkeypatch)

    state = manager.ensure_ready(_config(base_url="http://172.16.1.1:11434"))

    assert state.source == "auto"
    assert state.base_url == "http://172.27.227.98:11434"
    assert len(procs) == 1
    manager.shutdown()

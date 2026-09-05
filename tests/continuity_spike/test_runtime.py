"""Tests for read-only Ollama and GPU telemetry parsers.

No test may contact a real Ollama server or a real GPU: HTTP GET callables
and subprocess runners are injected, and the module source itself is scanned
for forbidden lifecycle command fragments.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.continuity_spike_runtime import (
    TelemetryError,
    capture_runtime_snapshot,
    http_get_with_timeout,
    query_nvidia_smi,
    query_ollama_ps,
)

import scripts.continuity_spike_runtime as runtime_module

_RUNTIME_MODULE = (
    Path(__file__).parents[2] / "scripts" / "continuity_spike_runtime.py"
)


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> object:
        return self._payload


def test_query_ollama_ps_reports_actual_resident_models():
    models = query_ollama_ps(
        "http://127.0.0.1:11434",
        get=lambda url, timeout: FakeResponse(
            {"models": [{"name": "qwen3-vl:8b", "size_vram": 9_000_000_000}]}
        ),
    )
    assert models == [{"name": "qwen3-vl:8b", "size_vram_bytes": 9_000_000_000}]


def test_query_ollama_ps_uses_get_api_ps_with_timeout():
    calls: list[tuple[str, float]] = []

    def fake_get(url: str, timeout: float) -> FakeResponse:
        calls.append((url, timeout))
        return FakeResponse({"models": []})

    query_ollama_ps("http://127.0.0.1:11434/", get=fake_get)

    assert calls == [("http://127.0.0.1:11434/api/ps", 5.0)]


def test_query_ollama_ps_unavailable_http_raises_structured_error():
    def failing_get(url: str, timeout: float) -> FakeResponse:
        raise ConnectionError("refused")

    with pytest.raises(TelemetryError) as excinfo:
        query_ollama_ps("http://127.0.0.1:11434", get=failing_get)
    assert excinfo.value.code == "ollama_unavailable"


@pytest.mark.parametrize(
    "payload",
    [
        "not-a-dict",
        {"unexpected": []},
        {"models": "not-a-list"},
        {"models": ["not-a-dict"]},
    ],
)
def test_query_ollama_ps_malformed_payload_raises_structured_error(payload):
    with pytest.raises(TelemetryError) as excinfo:
        query_ollama_ps(
            "http://127.0.0.1:11434", get=lambda url, timeout: FakeResponse(payload)
        )
    assert excinfo.value.code == "ollama_malformed"


def test_query_nvidia_smi_is_read_only():
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(command)
        assert kwargs  # capture_output/text/timeout are passed through
        return SimpleNamespace(returncode=0, stdout="0, NVIDIA GPU, 1024, 16384\n", stderr="")

    result = query_nvidia_smi(run=fake_run)

    assert calls == [
        [
            "nvidia-smi",
            "--query-gpu=index,name,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ]
    ]
    assert result == [
        {
            "index": 0,
            "name": "NVIDIA GPU",
            "memory_used_mib": 1024.0,
            "memory_total_mib": 16384.0,
        }
    ]


def test_query_nvidia_smi_absent_binary_raises_structured_error():
    def missing_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        raise FileNotFoundError("nvidia-smi not found")

    with pytest.raises(TelemetryError) as excinfo:
        query_nvidia_smi(run=missing_run)
    assert excinfo.value.code == "nvidia_smi_unavailable"


def test_query_nvidia_smi_non_zero_exit_raises_structured_error():
    def failing_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="", stderr="driver error")

    with pytest.raises(TelemetryError) as excinfo:
        query_nvidia_smi(run=failing_run)
    assert excinfo.value.code == "nvidia_smi_unavailable"
    assert "driver error" in excinfo.value.message


@pytest.mark.parametrize(
    "stdout",
    [
        "0, NVIDIA GPU, 1024\n",
        "0, NVIDIA GPU, not-a-number, 16384\n",
    ],
)
def test_query_nvidia_smi_malformed_csv_raises_structured_error(stdout):
    def malformed_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    with pytest.raises(TelemetryError) as excinfo:
        query_nvidia_smi(run=malformed_run)
    assert excinfo.value.code == "nvidia_smi_malformed"


def _ok_get(url: str, timeout: float) -> FakeResponse:
    return FakeResponse({"models": [{"name": "test-model", "size_vram": 1}]})


def _ok_run(command: list[str], **kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout="0, GPU, 512, 8192\n", stderr="")


def test_capture_snapshot_off_mode_queries_nothing():
    def forbidden_get(url: str, timeout: float) -> FakeResponse:
        raise AssertionError("ollama queried while telemetry off")

    def forbidden_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        raise AssertionError("nvidia-smi queried while telemetry off")

    snapshot = capture_runtime_snapshot(
        "before_source",
        "abc123",
        "off",
        get=forbidden_get,
        run=forbidden_run,
    )

    assert snapshot["kind"] == "telemetry"
    assert snapshot["requested_mode"] == "off"
    assert snapshot["ollama"] is None
    assert snapshot["gpu"] is None
    assert snapshot["errors"] == []
    assert snapshot["phase"] == "before_source"
    assert snapshot["source_key"] == "abc123"


def test_capture_snapshot_ollama_mode_skips_gpu():
    def forbidden_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        raise AssertionError("nvidia-smi queried in ollama mode")

    snapshot = capture_runtime_snapshot(
        "before_source",
        "abc123",
        "ollama",
        get=_ok_get,
        run=forbidden_run,
    )

    assert snapshot["ollama"] == [{"name": "test-model", "size_vram_bytes": 1}]
    assert snapshot["gpu"] is None
    assert snapshot["errors"] == []


def test_capture_snapshot_gpu_mode_skips_ollama():
    def forbidden_get(url: str, timeout: float) -> FakeResponse:
        raise AssertionError("ollama queried in gpu mode")

    snapshot = capture_runtime_snapshot(
        "after_source",
        "abc123",
        "gpu",
        get=forbidden_get,
        run=_ok_run,
    )

    assert snapshot["ollama"] is None
    assert snapshot["gpu"][0]["memory_used_mib"] == 512.0
    assert snapshot["errors"] == []


def test_capture_snapshot_all_mode_records_structured_telemetry_failures():
    def failing_get(url: str, timeout: float) -> FakeResponse:
        raise ConnectionError("refused")

    def failing_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        raise FileNotFoundError("nvidia-smi not found")

    snapshot = capture_runtime_snapshot(
        "before_source",
        "abc123",
        "all",
        get=failing_get,
        run=failing_run,
    )

    assert snapshot["ollama"] is None
    assert snapshot["gpu"] is None
    codes = {(error["source"], error["code"]) for error in snapshot["errors"]}
    assert codes == {("ollama", "ollama_unavailable"), ("nvidia_smi", "nvidia_smi_unavailable")}


def test_capture_snapshot_rejects_unknown_mode():
    with pytest.raises(ValueError):
        capture_runtime_snapshot(
            "phase", "key", "sometimes", get=_ok_get, run=_ok_run
        )


def test_runtime_module_contains_no_lifecycle_commands():
    source = _RUNTIME_MODULE.read_text(encoding="utf-8")
    for fragment in (
        "ollama stop",
        "ollama run",
        "ollama pull",
        "/api/generate",
        "/api/chat",
        "/api/embed",
    ):
        assert fragment not in source


def _resolve_to(address: str):
    def fake_getaddrinfo(host, port, type=None):
        return [(2, 1, 6, "", (address, port or 0))]

    return fake_getaddrinfo


def test_http_get_with_timeout_rejects_non_http_scheme():
    with pytest.raises(TelemetryError) as excinfo:
        http_get_with_timeout("file:///etc/passwd", 5.0)
    assert excinfo.value.code == "ollama_unavailable"


def test_http_get_with_timeout_blocks_metadata_and_public_addresses(monkeypatch):
    monkeypatch.setattr(
        runtime_module.socket, "getaddrinfo", _resolve_to("169.254.169.254")
    )

    def forbidden_get(*args: object, **kwargs: object) -> object:
        raise AssertionError("request reached the network")

    monkeypatch.setattr(runtime_module.httpx, "get", forbidden_get)

    with pytest.raises(TelemetryError):
        http_get_with_timeout("http://metadata.internal/api/ps", 5.0)

    monkeypatch.setattr(
        runtime_module.socket, "getaddrinfo", _resolve_to("8.8.8.8")
    )
    with pytest.raises(TelemetryError):
        http_get_with_timeout("http://example.com/api/ps", 5.0)


def test_http_get_with_timeout_allows_loopback_without_redirects(monkeypatch):
    monkeypatch.setattr(
        runtime_module.socket, "getaddrinfo", _resolve_to("127.0.0.1")
    )
    calls: list[dict[str, object]] = []

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse({"models": []})

    monkeypatch.setattr(runtime_module.httpx, "get", fake_get)

    response = http_get_with_timeout("http://127.0.0.1:11434/api/ps", 5.0)

    assert response.json() == {"models": []}
    assert calls == [
        {
            "url": "http://127.0.0.1:11434/api/ps",
            "timeout": 5.0,
            "follow_redirects": False,
        }
    ]

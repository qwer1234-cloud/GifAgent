"""Read-only Ollama and GPU telemetry for the continuity spike.

Telemetry is observational only. It queries Ollama's process list and
nvidia-smi memory counters and records structured failures; it never starts,
stops, loads, or unloads any model. All external calls are injected so tests
never touch live services, and telemetry failures never fail a source batch.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, Protocol

TelemetryMode = Literal["off", "ollama", "gpu", "all"]
TELEMETRY_MODES: tuple[str, ...] = ("off", "ollama", "gpu", "all")

DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"

_OLLAMA_TIMEOUT_S = 5.0
_SMI_TIMEOUT_S = 10.0
_SMI_COMMAND = [
    "nvidia-smi",
    "--query-gpu=index,name,memory.used,memory.total",
    "--format=csv,noheader,nounits",
]


class TelemetryError(RuntimeError):
    """A telemetry query failed; carries a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class SupportsJson(Protocol):
    def json(self) -> object: ...


class CompletedProbe(Protocol):
    returncode: int
    stdout: str
    stderr: str


HttpGet = Callable[[str, float], SupportsJson]
SubprocessRunner = Callable[..., CompletedProbe]


def query_ollama_ps(base_url: str, *, get: HttpGet) -> list[dict[str, object]]:
    """Issue a single read-only GET of the Ollama process list."""

    url = f"{base_url.rstrip('/')}/api/ps"
    try:
        response = get(url, _OLLAMA_TIMEOUT_S)
    except Exception as exc:
        raise TelemetryError("ollama_unavailable", str(exc)) from exc
    try:
        payload = response.json()
    except Exception as exc:
        raise TelemetryError("ollama_malformed", str(exc)) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise TelemetryError("ollama_malformed", "expected a models list")
    models: list[dict[str, object]] = []
    for entry in payload["models"]:
        if not isinstance(entry, dict):
            raise TelemetryError(
                "ollama_malformed", "model entries must be objects"
            )
        models.append(
            {
                "name": entry.get("name"),
                "size_vram_bytes": entry.get("size_vram"),
            }
        )
    return models


def query_nvidia_smi(*, run: SubprocessRunner) -> list[dict[str, object]]:
    """Issue a single read-only nvidia-smi memory query."""

    try:
        completed = run(
            _SMI_COMMAND,
            capture_output=True,
            text=True,
            timeout=_SMI_TIMEOUT_S,
        )
    except Exception as exc:
        raise TelemetryError("nvidia_smi_unavailable", str(exc)) from exc
    if completed.returncode != 0:
        raise TelemetryError(
            "nvidia_smi_unavailable",
            f"exit code {completed.returncode}: {completed.stderr.strip()}",
        )
    gpus: list[dict[str, object]] = []
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        columns = [column.strip() for column in stripped.split(",")]
        if len(columns) != 4:
            raise TelemetryError(
                "nvidia_smi_malformed", f"unexpected CSV row: {stripped!r}"
            )
        try:
            gpus.append(
                {
                    "index": int(columns[0]),
                    "name": columns[1],
                    "memory_used_mib": float(columns[2]),
                    "memory_total_mib": float(columns[3]),
                }
            )
        except ValueError as exc:
            raise TelemetryError(
                "nvidia_smi_malformed", f"unparseable CSV row: {stripped!r}"
            ) from exc
    return gpus


def capture_runtime_snapshot(
    phase: str,
    source_key: str,
    telemetry_mode: str,
    *,
    ollama_base_url: str = DEFAULT_OLLAMA_BASE_URL,
    get: HttpGet,
    run: SubprocessRunner,
) -> dict[str, object]:
    """Capture one observational telemetry snapshot; never raises on query failure."""

    if telemetry_mode not in TELEMETRY_MODES:
        raise ValueError(
            f"unknown telemetry mode {telemetry_mode!r}; "
            f"expected one of {list(TELEMETRY_MODES)}"
        )
    ollama: list[dict[str, object]] | None = None
    gpu: list[dict[str, object]] | None = None
    errors: list[dict[str, str]] = []

    if telemetry_mode in ("ollama", "all"):
        try:
            ollama = query_ollama_ps(ollama_base_url, get=get)
        except TelemetryError as exc:
            errors.append(
                {"source": "ollama", "code": exc.code, "message": exc.message}
            )
    if telemetry_mode in ("gpu", "all"):
        try:
            gpu = query_nvidia_smi(run=run)
        except TelemetryError as exc:
            errors.append(
                {"source": "nvidia_smi", "code": exc.code, "message": exc.message}
            )

    return {
        "kind": "telemetry",
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "monotonic_ns": time.perf_counter_ns(),
        "phase": phase,
        "source_key": source_key,
        "requested_mode": telemetry_mode,
        "ollama": ollama,
        "gpu": gpu,
        "errors": errors,
    }

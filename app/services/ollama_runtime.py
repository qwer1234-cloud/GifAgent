"""Runtime manager for reliable WSL/Ollama embedding endpoints.

Resolves the Ollama base URL at call time (never at import), keeps the
configured WSL distro resident with an owned hidden keeper process when
requested, discovers the current WSL address dynamically, waits for
``/api/version`` readiness, pre-warms the text embedding model, and
provides idempotent shutdown that only ever terminates a keeper started
by this process.
"""

from __future__ import annotations

import atexit
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.config import get

ENV_OLLAMA_BASE = "GIFAGENT_OLLAMA_BASE"
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_TEXT_MODEL = "nomic-embed-text:latest"
DEFAULT_EMBEDDING_DIM = 768
RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


class EmbeddingRuntimeError(RuntimeError, ValueError):
    """Structured Ollama/embedding failure.

    Subclasses both ``RuntimeError`` and ``ValueError`` so legacy callers
    that catch either keep working while new callers can read structured
    context (``phase``, ``attempts``, ``base_url``, ``retryable``).
    """

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        attempts: int,
        base_url: Optional[str],
        retryable: bool,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.attempts = attempts
        self.base_url = base_url
        self.retryable = retryable
        self.cause = cause

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "attempts": self.attempts,
            "base_url": self.base_url,
            "retryable": self.retryable,
            "error": str(self),
        }


@dataclass(frozen=True)
class EmbeddingRuntimeConfig:
    base_url: str = "auto"
    manage_lifecycle: bool = False
    launch_mode: str = "wsl"
    wsl_distro: str = "Ubuntu-20.04"
    startup_timeout_s: float = 120.0
    request_timeout_s: float = 60.0
    retry_attempts: int = 3
    retry_backoff_s: float = 2.0
    keep_alive: str = "30m"
    embedding_model: str = DEFAULT_TEXT_MODEL
    embedding_dim: int = DEFAULT_EMBEDDING_DIM


@dataclass
class RuntimeState:
    base_url: str
    keeper: Optional[subprocess.Popen] = None
    source: str = "auto"


def get_runtime_config() -> EmbeddingRuntimeConfig:
    """Read embedding runtime settings at call time, not import time."""
    return EmbeddingRuntimeConfig(
        base_url=str(get("embedding.base_url", "auto") or "auto"),
        manage_lifecycle=bool(get("embedding.manage_lifecycle", False)),
        launch_mode=str(get("embedding.launch_mode", "wsl") or "wsl").lower(),
        wsl_distro=str(
            get("embedding.wsl_distro", "Ubuntu-20.04") or "Ubuntu-20.04"
        ),
        startup_timeout_s=float(
            get("embedding.startup_timeout_s", 120.0) or 120.0
        ),
        request_timeout_s=float(
            get("embedding.request_timeout_s", 60.0) or 60.0
        ),
        retry_attempts=int(get("embedding.retry_attempts", 3) or 3),
        retry_backoff_s=float(
            get("embedding.retry_backoff_s", 2.0) or 2.0
        ),
        keep_alive=str(get("embedding.keep_alive", "") or ""),
        embedding_model=str(
            get("embedding.text_model", DEFAULT_TEXT_MODEL)
            or DEFAULT_TEXT_MODEL
        ),
        embedding_dim=int(
            get("embedding.embedding_dim", DEFAULT_EMBEDDING_DIM)
            or DEFAULT_EMBEDDING_DIM
        ),
    )


def normalize_base_url(url: str) -> str:
    """Normalize a base URL: trim, add scheme, strip trailing slash."""
    url = (url or "").strip()
    if not url:
        return DEFAULT_BASE_URL
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    return url.rstrip("/")


def is_ephemeral_wsl_endpoint(url: str) -> bool:
    """Return True for a Windows-side pin of WSL2 NAT Ollama.

    WSL2 Hyper-V typically assigns an address in ``172.16.0.0/12`` that
    changes every reboot.  Storing that address in ``models.yaml`` is the
    usual cause of ``timed out`` after restart.  Hostnames, localhost,
    and other private ranges are left alone so a real remote Ollama is
    not rewritten.
    """
    if os.name != "nt":
        return False
    text = (url or "").strip()
    if not text or text.lower() == "auto":
        return False
    try:
        parsed = httpx.URL(normalize_base_url(text))
    except Exception:
        return False
    host = parsed.host
    if not host:
        return False
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    if port != 11434:
        return False
    return _is_wsl_nat_ipv4(host)


def _is_wsl_nat_ipv4(host: str) -> bool:
    parts = host.split(".")
    if len(parts) != 4:
        return False
    try:
        octets = [int(part) for part in parts]
    except ValueError:
        return False
    if any(octet < 0 or octet > 255 for octet in octets):
        return False
    return octets[0] == 172 and 16 <= octets[1] <= 31


class OllamaRuntimeManager:
    """Owns WSL keeper lifecycle and caches only successful runtime state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: Optional[RuntimeState] = None
        self._keeper: Optional[subprocess.Popen] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def resolve_base_url(
        self, config: Optional[EmbeddingRuntimeConfig] = None
    ) -> str:
        """Resolve the base URL without starting any lifecycle process."""
        config = config or get_runtime_config()
        env_url = os.environ.get(ENV_OLLAMA_BASE)
        if env_url and env_url.strip():
            return normalize_base_url(env_url)
        if self._configured_url_is_explicit(config):
            return normalize_base_url(config.base_url)
        return self._resolve_automatic(config)

    def ensure_ready(
        self, config: Optional[EmbeddingRuntimeConfig] = None
    ) -> RuntimeState:
        """Return a ready runtime state, starting/discovering as needed.

        The environment override is honored first and never launches WSL.
        An explicit configured URL is honored next without lifecycle
        management, except a WSL2 NAT ``172.16.0.0/12:11434`` pin which
        is treated as ``auto``.  ``auto`` with ``launch_mode: wsl``
        discovers the live WSL address, and with ``manage_lifecycle``
        also starts an owned hidden keeper, polls readiness, and
        pre-warms the text model.
        """
        config = config or get_runtime_config()
        with self._lock:
            env_url = os.environ.get(ENV_OLLAMA_BASE)
            if env_url and env_url.strip():
                base_url = normalize_base_url(env_url)
                cached = self._cached(base_url=base_url, source="env")
                if cached is not None:
                    return cached
                return self._ensure_ready_at(
                    config, base_url, keeper=None, source="env"
                )
            if self._configured_url_is_explicit(config):
                base_url = normalize_base_url(config.base_url)
                cached = self._cached(base_url=base_url, source="explicit")
                if cached is not None:
                    return cached
                return self._ensure_ready_at(
                    config, base_url, keeper=None, source="explicit"
                )
            cached = self._cached(source="auto")
            if cached is not None:
                return cached
            keeper = None
            if self._wsl_managed(config):
                keeper = self._start_keeper_if_needed(config)
            base_url = self._resolve_automatic(config)
            return self._ensure_ready_at(
                config, base_url, keeper=keeper, source="auto"
            )

    def invalidate(self) -> None:
        """Drop cached URL/readiness so the next call rediscovers."""
        with self._lock:
            self._state = None

    def shutdown(self) -> bool:
        """Idempotently terminate only the keeper this manager started.

        Returns True when an owned keeper was terminated, False otherwise.
        """
        with self._lock:
            keeper = self._keeper
            self._keeper = None
            self._state = None
            if keeper is None:
                return False
            if keeper.poll() is None:
                try:
                    keeper.terminate()
                    keeper.wait(timeout=3.0)
                except Exception:
                    pass
                if keeper.poll() is None:
                    try:
                        keeper.kill()
                        keeper.wait(timeout=1.0)
                    except Exception:
                        pass
            return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _wsl_managed(self, config: EmbeddingRuntimeConfig) -> bool:
        return bool(config.manage_lifecycle) and self._wsl_discoverable(config)

    def _wsl_discoverable(self, config: EmbeddingRuntimeConfig) -> bool:
        return os.name == "nt" and str(config.launch_mode).lower() == "wsl"

    def _configured_url_is_explicit(
        self, config: EmbeddingRuntimeConfig
    ) -> bool:
        configured = (config.base_url or "").strip()
        if not configured or configured.lower() == "auto":
            return False
        return not is_ephemeral_wsl_endpoint(configured)

    def _resolve_automatic(
        self, config: EmbeddingRuntimeConfig
    ) -> str:
        if not self._wsl_discoverable(config):
            return DEFAULT_BASE_URL
        return self._discover_wsl_url(config)

    def _discover_wsl_url(
        self, config: EmbeddingRuntimeConfig
    ) -> str:
        cmd = [
            "wsl.exe",
            "-d",
            config.wsl_distro,
            "--",
            "hostname",
            "-I",
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except Exception as exc:
            raise EmbeddingRuntimeError(
                f"Failed to discover WSL address for {config.wsl_distro}: {exc}",
                phase="discover",
                attempts=1,
                base_url=None,
                retryable=True,
                cause=exc,
            ) from exc
        if proc.returncode != 0:
            raise EmbeddingRuntimeError(
                f"WSL address discovery failed (exit {proc.returncode}): "
                f"{proc.stderr.strip()}",
                phase="discover",
                attempts=1,
                base_url=None,
                retryable=True,
            )
        stdout_text = (proc.stdout or "").strip()
        first_ip = stdout_text.split()[0] if stdout_text else ""
        if not first_ip:
            raise EmbeddingRuntimeError(
                f"WSL address discovery returned no address for {config.wsl_distro}",
                phase="discover",
                attempts=1,
                base_url=None,
                retryable=True,
            )
        return f"http://{first_ip}:11434"

    def _start_keeper_if_needed(
        self, config: EmbeddingRuntimeConfig
    ) -> subprocess.Popen:
        keeper = self._keeper
        if keeper is not None and keeper.poll() is None:
            return keeper
        self._keeper = None
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                [
                    "wsl.exe",
                    "-d",
                    config.wsl_distro,
                    "--exec",
                    "sleep",
                    "infinity",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except Exception as exc:
            raise EmbeddingRuntimeError(
                f"Failed to start WSL keeper for {config.wsl_distro}: {exc}",
                phase="launch",
                attempts=1,
                base_url=None,
                retryable=True,
                cause=exc,
            ) from exc
        self._keeper = proc
        return proc

    def _cached(
        self,
        *,
        base_url: Optional[str] = None,
        source: Optional[str] = None,
    ) -> Optional[RuntimeState]:
        state = self._state
        if state is None:
            return None
        if base_url is not None and state.base_url != base_url:
            return None
        if source is not None and state.source != source:
            return None
        if state.keeper is not None and state.keeper.poll() is not None:
            self._state = None
            return None
        return state

    def _ensure_ready_at(
        self,
        config: EmbeddingRuntimeConfig,
        base_url: str,
        *,
        keeper: Optional[subprocess.Popen],
        source: str = "auto",
    ) -> RuntimeState:
        state = self._cached(base_url=base_url, source=source)
        if state is not None:
            return state

        if keeper is not None:
            self._keeper = keeper

        self._poll_version(config, base_url)
        self._prewarm(config, base_url)
        state = RuntimeState(base_url=base_url, keeper=keeper, source=source)
        self._state = state
        return state

    def _poll_version(
        self, config: EmbeddingRuntimeConfig, base_url: str
    ) -> None:
        deadline = time.monotonic() + max(0.0, float(config.startup_timeout_s))
        attempts = 0
        last_error: Optional[BaseException] = None
        while True:
            attempts += 1
            try:
                resp = httpx.get(f"{base_url}/api/version", timeout=2.0)
            except httpx.HTTPError as exc:
                last_error = exc
            except Exception as exc:
                last_error = exc
            else:
                if resp.status_code == 200:
                    return
                if resp.status_code not in RETRYABLE_STATUS_CODES:
                    raise EmbeddingRuntimeError(
                        f"Ollama /api/version returned HTTP {resp.status_code}",
                        phase="startup",
                        attempts=attempts,
                        base_url=base_url,
                        retryable=False,
                    )
                last_error = EmbeddingRuntimeError(
                    f"Ollama /api/version returned HTTP {resp.status_code}",
                    phase="startup",
                    attempts=attempts,
                    base_url=base_url,
                    retryable=True,
                )
            if time.monotonic() >= deadline:
                raise EmbeddingRuntimeError(
                    f"Ollama did not become ready at {base_url} within "
                    f"{config.startup_timeout_s}s",
                    phase="startup",
                    attempts=attempts,
                    base_url=base_url,
                    retryable=True,
                    cause=last_error,
                ) from last_error
            interval = min(1.0, max(0.05, float(config.startup_timeout_s) / 40.0))
            time.sleep(interval)

    def _prewarm(
        self, config: EmbeddingRuntimeConfig, base_url: str
    ) -> None:
        """Pre-warm the text model with one 768-dim embedding.

        Uses a direct low-level request so readiness is never re-entered.
        """
        payload: dict[str, Any] = {
            "model": config.embedding_model,
            "input": ["ping"],
        }
        if config.keep_alive:
            payload["keep_alive"] = config.keep_alive

        attempts = max(1, int(config.retry_attempts))
        last_error: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            try:
                resp = httpx.post(
                    f"{base_url}/api/embed",
                    json=payload,
                    timeout=httpx.Timeout(config.request_timeout_s, connect=5.0),
                )
                if resp.status_code in RETRYABLE_STATUS_CODES:
                    last_error = EmbeddingRuntimeError(
                        f"Ollama pre-warm returned HTTP {resp.status_code}",
                        phase="prewarm",
                        attempts=attempt,
                        base_url=base_url,
                        retryable=True,
                    )
                    self._sleep_backoff(config, attempt)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict):
                    raise ValueError(
                        "pre-warm response must be a JSON object, got "
                        f"{type(data).__name__}"
                    )
                embeddings = data.get("embeddings")
                if not isinstance(embeddings, list) or len(embeddings) != 1:
                    raise EmbeddingRuntimeError(
                        "pre-warm expected exactly 1 embedding, got "
                        f"{len(embeddings) if isinstance(embeddings, list) else type(embeddings).__name__}",
                        phase="prewarm",
                        attempts=attempt,
                        base_url=base_url,
                        retryable=False,
                    )
                vector = embeddings[0]
                if not isinstance(vector, list) or not vector:
                    raise EmbeddingRuntimeError(
                        "pre-warm returned an empty/non-list vector",
                        phase="prewarm",
                        attempts=attempt,
                        base_url=base_url,
                        retryable=False,
                    )
                if len(vector) != config.embedding_dim:
                    raise EmbeddingRuntimeError(
                        "pre-warm dimension mismatch: got "
                        f"{len(vector)}, expected {config.embedding_dim}",
                        phase="prewarm",
                        attempts=attempt,
                        base_url=base_url,
                        retryable=False,
                    )
                return
            except EmbeddingRuntimeError:
                raise
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in RETRYABLE_STATUS_CODES:
                    last_error = exc
                    self._sleep_backoff(config, attempt)
                    continue
                raise EmbeddingRuntimeError(
                    f"Ollama pre-warm failed with HTTP {status}",
                    phase="prewarm",
                    attempts=attempt,
                    base_url=base_url,
                    retryable=False,
                    cause=exc,
                ) from exc
            except httpx.TransportError as exc:
                last_error = exc
                self._sleep_backoff(config, attempt)
            except (ValueError, TypeError) as exc:
                raise EmbeddingRuntimeError(
                    f"Ollama pre-warm response was invalid: {exc}",
                    phase="prewarm",
                    attempts=attempt,
                    base_url=base_url,
                    retryable=False,
                    cause=exc,
                ) from exc

        raise EmbeddingRuntimeError(
            f"Ollama pre-warm failed after {attempts} attempts: {last_error}",
            phase="prewarm",
            attempts=attempts,
            base_url=base_url,
            retryable=True,
            cause=last_error,
        ) from last_error

    @staticmethod
    def _sleep_backoff(config: EmbeddingRuntimeConfig, attempt: int) -> None:
        time.sleep(float(config.retry_backoff_s) * (2 ** (attempt - 1)))


_default_runtime = OllamaRuntimeManager()


def ensure_runtime_ready(
    config: Optional[EmbeddingRuntimeConfig] = None,
) -> RuntimeState:
    """Ensure the default runtime is ready (module-level convenience)."""
    return _default_runtime.ensure_ready(config)


def resolve_base_url(
    config: Optional[EmbeddingRuntimeConfig] = None,
) -> str:
    """Resolve the runtime base URL (module-level convenience).

    When *config* is omitted, embedding settings from ``models.yaml`` are
    used.  Callers that already have a VLM/embedding snapshot should pass
    it so ``auto`` discovery uses that launch_mode/distro, not a stale
    global default.
    """
    return _default_runtime.resolve_base_url(config)


def invalidate_runtime() -> None:
    """Invalidate the default runtime's cached endpoint/readiness."""
    _default_runtime.invalidate()


def shutdown_runtime() -> bool:
    """Idempotently shut down the default runtime's owned keeper."""
    return _default_runtime.shutdown()


def _atexit_shutdown() -> None:
    try:
        shutdown_runtime()
    except Exception:
        pass


atexit.register(_atexit_shutdown)

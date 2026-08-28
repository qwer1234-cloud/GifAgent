"""Explicit VLM runtime configuration and Ollama lifecycle helpers."""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, replace

import httpx

from app.pipeline.timing import _timed

# P1-4: Ollama base URL is env-overridable so the full 8-stage production
# E2E can point the VLM at a local deterministic stub instead of the user's
# real Ollama/WSL instance (fourth-review §9.2C).
OLLAMA_BASE = os.environ.get("GIFAGENT_OLLAMA_BASE", "http://127.0.0.1:11434")


# ---------------------------------------------------------------------------
# P0 (sixth-review §4): shared VLM client - single scoring entry point for
# both _stage_vlm and _stage_refine.  Ensures provider validation, failure
# counting, and parse-error handling never drift between the two stages.
# ---------------------------------------------------------------------------


def _validate_vlm_provider(config_data: dict | None) -> dict:
    """Validate the VLM provider from the frozen job config.

    Stage Split production currently only supports ``provider=ollama``.
    Any other provider (``openai``, ``openai_compatible``, or unknown) is
    rejected with a clear error BEFORE any HTTP request is sent.

    On success returns the parsed ``vlm`` config dict so callers don't
    re-read it from the raw config_data.
    """
    if config_data is None:
        # Legacy direct mode - default Ollama.
        return {"provider": "ollama", "model": "llava:13b",
                "base_url": OLLAMA_BASE}
    vlm_cfg = config_data.get("vlm") or {}
    provider = (vlm_cfg.get("provider") or "ollama").lower()
    if provider != "ollama":
        raise ValueError(
            f"Unsupported vlm.provider={provider!r}.  Stage Split "
            f"production currently only supports 'ollama'.  Found in "
            f"job config's 'vlm' section: {vlm_cfg!r}"
        )
    model = vlm_cfg.get("model")
    base_url = vlm_cfg.get("base_url")
    errors = []
    if not model:
        errors.append("'vlm.model' is missing")
    if not base_url:
        errors.append("'vlm.base_url' is missing")
    if errors:
        raise ValueError(
            f"VLM config validation failed: {'; '.join(errors)}. "
            f"Config: {vlm_cfg!r}"
        )
    return vlm_cfg


# ---------------------------------------------------------------------------
# Task 4 (seventh-review): explicit VLM runtime configuration.
# Lifecycle decisions are NEVER inferred from URL; manage_lifecycle and
# launch_mode must be explicitly set in the job config or defaults apply.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VlmRuntimeConfig:
    provider: str          # only "ollama" currently
    model: str
    base_url: str
    manage_lifecycle: bool
    launch_mode: str       # "none" | "native" | "wsl"
    retry_delay_s: float   # mapped from config key "retry_delay_s"
    # Whether to unload other resident models (nomic-embed-text) before
    # loading the VLM.  True reproduces legacy behavior; large-VRAM setups
    # can set this False to keep embeddings warm across stages.
    free_vram_before_load: bool = True


def _expand_vlm_base_url(
    configured: str,
    *,
    launch_mode: str,
    wsl_distro: str,
    manage_lifecycle: bool,
) -> str:
    """Turn frozen ``auto`` / ephemeral WSL NAT URLs into a live endpoint.

    Job snapshots keep ``vlm.base_url: auto`` on purpose (WSL ``172.x``
    changes across reboots).  Stage subprocesses must expand that token
    before any HTTP call, otherwise ``wait_model`` posts to
    ``auto/api/generate`` and every VLM attempt dies as
    ``VLM not responding``.
    """
    from app.services.ollama_runtime import (
        EmbeddingRuntimeConfig,
        OllamaRuntimeManager,
        is_ephemeral_wsl_endpoint,
        normalize_base_url,
    )

    text = (configured or "").strip()
    if text and text.lower() != "auto" and not is_ephemeral_wsl_endpoint(text):
        return normalize_base_url(text)
    discover_mode = launch_mode if launch_mode in ("native", "wsl") else "wsl"
    return normalize_base_url(
        OllamaRuntimeManager().resolve_base_url(
            EmbeddingRuntimeConfig(
                base_url="auto",
                manage_lifecycle=manage_lifecycle,
                launch_mode=discover_mode,
                wsl_distro=wsl_distro or "Ubuntu-20.04",
            )
        )
    )


def _resolve_vlm_runtime(config_data: dict | None) -> VlmRuntimeConfig:
    """Parse the frozen job config into an immutable VLM runtime spec.

    New behaviour (seventh-review Task 4 Steps 2-4):
    * ``manage_lifecycle`` defaults to False, NOT inferred from URL.
    * ``launch_mode`` defaults to ``"none"``, NOT inferred from URL.
    * ``launch_mode`` must be one of ``none``, ``native``, ``wsl``.
    * Unknown launch_mode raises ``ValueError`` immediately.
    * ``retry_delay_s`` maps to ``vlm.retry_delay_s`` (default 2.0).
    * Backward-compat: when config_data is None (legacy direct mode),
      the old defaults (provider=ollama, manage_lifecycle=True,
      launch_mode=wsl, base_url=127.0.0.1:11434) are used.
    """
    if config_data is None:
        return VlmRuntimeConfig(
            provider="ollama", model="llava:13b",
            base_url=OLLAMA_BASE, manage_lifecycle=True,
            launch_mode="wsl", retry_delay_s=2.0,
        )

    vlm_cfg = config_data.get("vlm") or {}
    provider = (vlm_cfg.get("provider") or "ollama").lower()
    if provider != "ollama":
        raise ValueError(
            f"Unsupported vlm.provider={provider!r}; only 'ollama' allowed"
        )
    model = vlm_cfg.get("model")
    base_url = vlm_cfg.get("base_url")
    if not model:
        raise ValueError("VLM config missing 'vlm.model'")
    if not base_url:
        raise ValueError("VLM config missing 'vlm.base_url'")

    manage_lifecycle = bool(vlm_cfg.get("manage_lifecycle", False))
    launch_mode = str(vlm_cfg.get("launch_mode", "none")).lower()
    if launch_mode not in ("none", "native", "wsl"):
        raise ValueError(
            f"Unknown vlm.launch_mode={launch_mode!r}; "
            f"must be 'none', 'native', or 'wsl'"
        )
    retry_delay = float(vlm_cfg.get("retry_delay_s", 2.0))
    free_vram_before_load = bool(vlm_cfg.get("free_vram_before_load", True))

    return VlmRuntimeConfig(
        provider=provider, model=model, base_url=base_url,
        manage_lifecycle=manage_lifecycle, launch_mode=launch_mode,
        retry_delay_s=retry_delay,
        free_vram_before_load=free_vram_before_load,
    )


def _materialize_vlm_runtime(
    runtime: VlmRuntimeConfig,
    config_data: dict | None = None,
) -> VlmRuntimeConfig:
    """Expand ``auto`` / stale WSL NAT URLs without rewriting the snapshot."""
    vlm_cfg = (config_data or {}).get("vlm") or {}
    expanded = _expand_vlm_base_url(
        runtime.base_url,
        launch_mode=runtime.launch_mode,
        wsl_distro=str(vlm_cfg.get("wsl_distro") or "Ubuntu-20.04"),
        manage_lifecycle=runtime.manage_lifecycle,
    )
    if expanded == runtime.base_url:
        return runtime
    return replace(runtime, base_url=expanded)


def _ollama_command(runtime: VlmRuntimeConfig, *args: str) -> list[str]:
    """Build the platform command for a VLM lifecycle action."""
    if runtime.launch_mode == "native":
        return ["ollama", *args]
    if runtime.launch_mode == "wsl":
        return ["wsl", "ollama", *args]
    raise ValueError(
        f"launch_mode={runtime.launch_mode!r} cannot execute ollama commands"
    )


@_timed("model_wait")
def stop_model(name: str, runtime: VlmRuntimeConfig | None = None) -> bool:
    """Stop an Ollama model and wait until it's fully unloaded from GPU.

    Accepts an optional ``VlmRuntimeConfig`` so the lifecycle command
    (native ollama vs. wsl ollama) and base_url are drawn from the frozen
    config, not from module-level globals.  When ``runtime`` is ``None``
    the old defaults (wsl, OLLAMA_BASE) are used for backward compat.
    """
    if runtime is None:
        runtime = VlmRuntimeConfig(
            provider="ollama", model="", base_url=OLLAMA_BASE,
            manage_lifecycle=True, launch_mode="wsl", retry_delay_s=2.0,
        )

    def _confirmed_unloaded() -> bool:
        """``True`` only when ``/api/ps`` positively rules the model out.

        A transport failure or an ambiguous response must NOT be treated
        as "unloaded" -- that would skip the real stop command below.
        """
        try:
            r = httpx.get(f"{runtime.base_url}/api/ps", timeout=5)
            loaded = {m.get("name", "") for m in r.json().get("models", [])}
            return not any(name.split(":")[0] in m for m in loaded)
        except Exception:
            return False

    # Task 7: skip the stop command and every sleep entirely when the model
    # was already not resident -- there is nothing to stop and nothing to
    # wait for.
    if _confirmed_unloaded():
        return True

    for attempt in range(3):
        subprocess.run(
            _ollama_command(runtime, "stop", name),
            capture_output=True, timeout=30,
        )
        time.sleep(5)
        if _confirmed_unloaded():
            return True
        time.sleep(10)
    return False


@_timed("model_wait")
def wait_model(name: str, runtime: VlmRuntimeConfig | None = None,
               timeout_s: int = 120) -> bool:
    """Wait for an Ollama model to be ready, loading it if needed.

    Accepts an optional ``VlmRuntimeConfig`` so the base URL is drawn from
    the frozen config.  When ``runtime`` is ``None`` the old default
    (OLLAMA_BASE) is used for backward compat.
    """
    if runtime is None:
        runtime = VlmRuntimeConfig(
            provider="ollama", model="", base_url=OLLAMA_BASE,
            manage_lifecycle=True, launch_mode="wsl", retry_delay_s=2.0,
        )
    deadline = time.time() + timeout_s
    load_triggered = False
    while time.time() < deadline:
        try:
            r = httpx.post(
                f"{runtime.base_url}/api/generate",
                json={
                    "model": name, "prompt": "ping", "stream": False,
                    # An already-loaded model would otherwise generate a
                    # full reply to "ping" just to prove it's alive.
                    "options": {"num_predict": 1},
                },
                timeout=30,
            )
            if r.status_code == 200:
                return True
            if r.status_code == 503:
                time.sleep(10)
                continue
        except Exception:
            pass
        if not load_triggered:
            try:
                httpx.post(
                    f"{runtime.base_url}/api/generate",
                    json={
                        "model": name,
                        "prompt": "ping",
                        "stream": False,
                        "options": {"num_predict": 1},
                    },
                    timeout=5,
                )
            except Exception:
                pass
            load_triggered = True
        time.sleep(5)
    return False


def _is_stable_http_url(url: str) -> bool:
    """Return True for a non-sentinel, non-ephemeral HTTP(S) endpoint."""
    text = (url or "").strip()
    lowered = text.lower()
    if lowered in {"", "auto", "inherit_vlm"}:
        return False
    if not lowered.startswith(("http://", "https://")):
        return False
    from app.services.ollama_runtime import is_ephemeral_wsl_endpoint

    return not is_ephemeral_wsl_endpoint(text)


def _attach_live_vlm_base_url(cfg: dict, config_data: dict | None) -> str | None:
    """Remember the live VLM URL on *cfg* without rewriting the snapshot hash."""
    existing = str(cfg.get("_live_vlm_base_url") or "").strip()
    if existing.startswith(("http://", "https://")):
        return existing
    if not config_data:
        return None
    try:
        live = _materialize_vlm_runtime(
            _resolve_vlm_runtime(config_data), config_data
        ).base_url
    except ValueError:
        return None
    live = str(live or "").strip()
    if not live.startswith(("http://", "https://")):
        return None
    cfg["_live_vlm_base_url"] = live
    return live


def _resolve_vlm_config(config_data: dict | None) -> tuple[str, str]:
    """Read VLM model name and base URL from the frozen job config snapshot.

    Falls back to the module-level defaults (``llava:13b`` and
    ``OLLAMA_BASE``) when *config_data* is ``None`` (legacy direct mode).
    When a ``config_data`` dict IS provided (stage mode) but lacks a
    ``vlm`` section or has empty model/base_url, raises ``ValueError`` so
    a misconfigured stage subprocess fails fast instead of silently hitting
    the wrong endpoint.

    Returns ``(model, base_url)``.
    """
    if config_data is None:
        return "llava:13b", OLLAMA_BASE
    vlm_cfg = config_data.get("vlm") or {}
    model = vlm_cfg.get("model")
    base_url = vlm_cfg.get("base_url")
    if not model:
        raise ValueError(
            "VLM model not configured: config snapshot has no "
            "'vlm.model' set; stage subprocess must carry a frozen "
            "job config with a 'vlm' section"
        )
    if not base_url:
        base_url = OLLAMA_BASE
    return model, base_url


def _should_manage_vlm_lifecycle(config_data: dict | None, launch_mode: str | None = None) -> bool:
    """Return ``True`` only when the VLM runtime must be stopped/started.

    P1-3 (sixth-review §7): lifecycle management is EXPLICITLY configured.
    The function NEVER infers ``launch_mode`` from the base URL.

    ``manage_lifecycle: false`` or ``launch_mode: none`` disables all
    lifecycle (no WSL subprocess, no sleep).  ``launch_mode: native``
    runs the native ``ollama`` binary; ``launch_mode: wsl`` runs the WSL
    version.  When the config omits these fields, the old URL-based
    heuristic is used for backward compatibility (fifth-review logic).
    """
    if config_data is None:
        return True
    vlm_cfg = config_data.get("vlm") or {}
    ml = vlm_cfg.get("manage_lifecycle")
    if ml is not None:
        return bool(ml)
    lm = launch_mode or vlm_cfg.get("launch_mode", "").lower()
    if lm == "none":
        return False
    if lm in ("native", "wsl"):
        return True
    provider = (vlm_cfg.get("provider") or "ollama").lower()
    if provider and provider != "ollama":
        return False
    base_url = (vlm_cfg.get("base_url") or OLLAMA_BASE).rstrip("/")
    if base_url == "http://127.0.0.1:11434":
        return True
    return False

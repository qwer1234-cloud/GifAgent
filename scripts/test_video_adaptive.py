#!/usr/bin/env python3
"""
Two-pass adaptive GIF extraction:
  Pass 1: coarse sample every N seconds -> VLM scores
  Pass 2: around high-score regions, re-sample at finer intervals
  Adjacent high-score frames are merged into longer clips.
  Top-50 ranked by gif_worthiness.
"""
from __future__ import annotations

import atexit
import sys
import os
import subprocess
from dataclasses import dataclass
import json
import re
import base64
import hashlib
import time
import argparse
import math
from pathlib import Path
import httpx
from PIL import Image

# Windows console defaults to GBK -- reconfigure to handle Unicode
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, ".")
from app.db import init_db, get_connection
from app.config import load_config, get
from app.services.embedding import compute_text_embedding
from app.services.clip_dedup import temporal_dedup_clips
from app.services.batch_logging import format_gif_export_line, run_gif_export_attempt
from app.services.export_cleanup import (
    ExportDirectoryBusyError,
    ExportDirectoryLock,
    cleanup_adaptive_export_dir,
)
from app.services.export_ranking import rank_clips_for_export
from app.services.gif_naming import build_gif_filename
from app.services.indexer import get_index
from app.services.json_guard import parse_json_response
from app.services.llm_client import generate_llm_text, is_local_llm, llm_model_name, wait_for_llm
from app.services.potplayer_bookmarks import PotPlayerBookmark, write_pbf_file
from app.services.quality import validate_frame_analysis, normalize_emotional_core

# ---- Helpers (kept at module level, no side effects) ---------------


def parse_vlm_response(raw_text: str) -> dict:
    """Parse VLM response through quality gate, return cleaned dict."""
    result = parse_json_response(raw_text)
    if not result.ok:
        return {"_parse_error": True, "_raw": raw_text[:500]}
    cleaned, errors = validate_frame_analysis(result.data)
    if errors:
        cleaned["_quality_errors"] = errors
    return cleaned


SCORE_PROMPT = (
    "Evaluate this film frame for GIF potential. Use the full 0.0-1.0 scale.\n"
    "Output ONLY valid JSON with real, specific content. No template text.\n\n"
    '{"caption":"describe actual visible subjects, lighting, and composition",'
    '"emotional_core":"one lowercase word","gif_worthiness":0.5,'
    '"aesthetic_notes":["2-3 concrete visual observations"],'
    '"reason":"why this specific moment works as a GIF (or why not)"}\n\n'
    "gif_worthiness scale:\n"
    "  0.0-0.2: BAD - static, dark, blurry, nothing happening. Skip.\n"
    "  0.3-0.5: AVERAGE - some emotion, decent composition.\n"
    "  0.6-0.8: GOOD - clear emotion/action, cinematic framing.\n"
    "  0.9-1.0: EXCELLENT - iconic moment, beautiful lighting, peak drama.\n\n"
    "CRITICAL: emotional_core = EXACTLY ONE lowercase word from: "
    "tension|melancholy|awe|joy|sadness|catharsis|serenity|excitement|dread|nostalgia|"
    "admiration|intimacy|vulnerability|longing|desire|other\n"
    "NEVER output 'what you see', '2-3 observations', or pipe-delimited emotions."
)


def safe_worth(value):
    """Parse gif_worthiness robustly -- VLM sometimes returns text labels instead of numbers."""
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, str):
        lowered = value.strip().lower()
        try:
            return max(0.0, min(1.0, float(lowered)))
        except ValueError:
            pass
        if "excellent" in lowered:
            return 0.9
        if "good" in lowered:
            return 0.7
        if "average" in lowered:
            return 0.4
        if "bad" in lowered:
            return 0.15
    return 0.5  # fallback


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


def _score_vlm_frame(
    base_url: str,
    model: str,
    image_bytes: bytes,
    prompt: str,
    options: dict,
    threshold: float,
    timestamp: float,
    frame_path: str,
    retry_delay_s: float = 2.0,
) -> tuple[dict | None, str | None]:
    """Score one frame via the Ollama-compatible VLM endpoint.

    Returns ``(payload_or_None, error_or_None)``.

    * HTTP / transport / JSON / parse / invalid-score errors all retry up
      to 3 times (seventh-review Task 2 Step 2: parse error used to return
      after 1 attempt while claiming "after N attempts"; now it saves the
      error and continues the loop).
    * ``parse_vlm_response`` returning ``_parse_error`` is treated as a
      retryable failure, NOT a success.
    * A successful HTTP+JSON response whose ``gif_worthiness`` is missing,
      boolean, non-numeric, non-finite, or outside ``[0.0, 1.0]`` is a
      FAILURE -- the caller NEVER gets a default 0.5 score
      (seventh-review Task 2 Step 1: removed ``safe_worth(0.5)`` fallback).
    * Quality-gate errors in non-score fields are informational, not fatal.
    """
    import math

    img_b64 = base64.b64encode(image_bytes).decode("utf-8")
    last_error: str | None = None

    for attempt in range(3):
        try:
            resp = httpx.post(
                f"{base_url}/api/generate",
                json={
                    "model": model, "prompt": prompt,
                    "images": [img_b64], "stream": False,
                    "options": options,
                },
                timeout=120,
            )
            resp.raise_for_status()
            response_json = resp.json()
            raw = response_json.get("response", "")
            # Read + validate the raw gif_worthiness BEFORE parse_vlm_response
            # (validate_frame_analysis coerces bool->float, drops unconvertible
            # strings to None, and even raises on float("high") - all of which
            # would hide invalid values from the strict check).
            raw_worthiness: object = None
            parse_error_msg: str | None = None
            try:
                raw_parsed = json.loads(raw) if raw else {}
                if isinstance(raw_parsed, dict):
                    raw_worthiness = raw_parsed.get("gif_worthiness")
                else:
                    parse_error_msg = "response JSON is not an object"
            except (json.JSONDecodeError, TypeError) as je:
                parse_error_msg = f"parse_error: {je}"

            if parse_error_msg is not None:
                last_error = (
                    f"{parse_error_msg} after {attempt + 1} attempt(s)"
                )
                if attempt < 2:
                    time.sleep(retry_delay_s)
                    continue
                return None, last_error

            # Strict worthiness validation (seventh-review Task 2 Step 1).
            worth = raw_worthiness
            if (
                isinstance(worth, bool)
                or not isinstance(worth, (int, float))
                or not math.isfinite(float(worth))
                or not 0.0 <= float(worth) <= 1.0
            ):
                last_error = (
                    f"invalid gif_worthiness: expected finite number in "
                    f"[0, 1], got {worth!r}"
                )
                if attempt < 2:
                    time.sleep(retry_delay_s)
                    continue
                return None, last_error

            # Worth is valid - now run the quality-gate parser for caption
            # and other non-critical fields.  parse_vlm_response will not
            # raise because worth is already a valid finite number.
            parsed = parse_vlm_response(raw)
            if parsed.get("_parse_error"):
                last_error = (
                    f"parse_error after {attempt + 1} attempt(s): "
                    f"{parsed.get('_raw', '')[:120]}"
                )
                if attempt < 2:
                    time.sleep(retry_delay_s)
                    continue
                return None, last_error

            parsed["gif_worthiness"] = float(worth)
            parsed["timestamp"] = timestamp
            parsed["path"] = frame_path
            return parsed, None

        except Exception as e:
            last_error = str(e)
            if attempt == 2:
                return None, last_error
            time.sleep(retry_delay_s)

    return None, last_error or "exhausted 3 retries"


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

    return VlmRuntimeConfig(
        provider=provider, model=model, base_url=base_url,
        manage_lifecycle=manage_lifecycle, launch_mode=launch_mode,
        retry_delay_s=retry_delay,
    )


def _ollama_command(runtime: VlmRuntimeConfig, *args: str) -> list[str]:
    """Build the platform command for a VLM lifecycle action."""
    if runtime.launch_mode == "native":
        return ["ollama", *args]
    if runtime.launch_mode == "wsl":
        return ["wsl", "ollama", *args]
    raise ValueError(
        f"launch_mode={runtime.launch_mode!r} cannot execute ollama commands"
    )


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
    for attempt in range(3):
        subprocess.run(
            _ollama_command(runtime, "stop", name),
            capture_output=True, timeout=30,
        )
        time.sleep(5)
        try:
            r = httpx.get(f"{runtime.base_url}/api/ps", timeout=5)
            loaded = {m.get("name", "") for m in r.json().get("models", [])}
            still_loaded = any(name.split(":")[0] in m for m in loaded)
            if not still_loaded:
                return True
        except Exception:
            pass
        time.sleep(10)
    return False


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
                json={"model": name, "prompt": "ping", "stream": False},
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


# ---- Constants referenced by module-level helpers -----------------

# P1-4: Ollama base URL is env-overridable so the full 8-stage production
# E2E can point the VLM at a local deterministic stub instead of the user's
# real Ollama/WSL instance (fourth-review §9.2C).
OLLAMA_BASE = os.environ.get("GIFAGENT_OLLAMA_BASE", "http://127.0.0.1:11434")

# ---- Config extraction (shared by direct and stage mode) ----------


def extract_config(config_data: dict) -> dict:
    """Extract flat pipeline config from the full config dict."""
    adaptive = config_data.get("adaptive", {}) or {}
    pref_mem = config_data.get("preference_memory", {}) or {}
    return {
        "sample_interval": int(adaptive.get("sample_interval", 10)),
        "refine_interval": int(adaptive.get("refine_interval", 10)),
        "refine_radius": int(adaptive.get("refine_radius", 20)),
        "refine_threshold": float(adaptive.get("refine_threshold", 0.5)),
        "max_duration": float(adaptive.get("max_duration", 10)),
        "min_duration": 1.5,
        "worthiness_threshold": float(adaptive.get("worthiness_threshold", 0.2)),
        "merge_gap": int(adaptive.get("merge_gap", 12)),
        "merge_score_threshold": float(
            adaptive.get("merge_score_threshold", 0.55)
        ),
        "embed_sim_threshold": float(
            adaptive.get("embedding_dedup_threshold", 0.94)
        ),
        "embed_dedup_enabled": bool(
            adaptive.get("embedding_dedup_enabled", True)
        ),
        "temporal_dedup_enabled": bool(
            adaptive.get("temporal_dedup_enabled", True)
        ),
        "temporal_dedup_min_gap_s": float(
            adaptive.get("temporal_dedup_min_gap_s", 12)
        ),
        "output_ratio": float(adaptive.get("output_ratio", 1.0)),
        "max_output": int(adaptive.get("max_output", 0)),
        "gif_fps": int(adaptive.get("gif_fps", 24)),
        "gif_max_width": int(adaptive.get("gif_max_width", 720)),
        "clear_output_dir": bool(adaptive.get("clear_output_dir", True)),
        "potplayer_pbf_enabled": bool(
            adaptive.get("potplayer_pbf_enabled", True)
        ),
        "preference_memory_enabled": bool(pref_mem.get("enabled", False)),
        "base_score_weight": float(pref_mem.get("base_score_weight", 0.50)),
        "preference_score_weight": float(
            pref_mem.get("preference_score_weight", 0.50)
        ),
        "vlm_temperature": float(adaptive.get("vlm_temperature", 0.65)),
        "vlm_top_p": float(adaptive.get("vlm_top_p", 0.95)),
        "vlm_top_k": int(adaptive.get("vlm_top_k", 60)),
    }


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


# ---- Core pipeline (shared by both modes) -------------------------


def run_pipeline(video_path: str, frames_dir: str, export_dir: str, cfg: dict) -> dict:
    """Run phases 1-4 of the adaptive extraction pipeline.

    Returns the full ``output`` dict with all scores, clips, and paths.
    """

    # -- Unpack config -------------------------------------------------
    SAMPLE_INTERVAL = cfg["sample_interval"]
    REFINE_INTERVAL = cfg["refine_interval"]
    REFINE_RADIUS = cfg["refine_radius"]
    REFINE_THRESHOLD = cfg["refine_threshold"]
    MAX_DURATION = cfg["max_duration"]
    MIN_DURATION = cfg["min_duration"]
    WORTHINESS_THRESHOLD = cfg["worthiness_threshold"]
    MERGE_GAP = cfg["merge_gap"]
    MERGE_SCORE_THRESHOLD = cfg["merge_score_threshold"]
    EMBED_SIM_THRESHOLD = cfg["embed_sim_threshold"]
    EMBED_DEDUP_ENABLED = cfg["embed_dedup_enabled"]
    TEMPORAL_DEDUP_ENABLED = cfg["temporal_dedup_enabled"]
    TEMPORAL_DEDUP_MIN_GAP_S = cfg["temporal_dedup_min_gap_s"]
    OUTPUT_RATIO = cfg["output_ratio"]
    MAX_OUTPUT = cfg["max_output"]
    GIF_FPS = cfg["gif_fps"]
    GIF_MAX_WIDTH = cfg["gif_max_width"]
    CLEAR_OUTPUT_DIR = cfg["clear_output_dir"]
    POTPLAYER_PBF_ENABLED = cfg["potplayer_pbf_enabled"]
    PREFERENCE_MEMORY_ENABLED = cfg["preference_memory_enabled"]
    BASE_SCORE_WEIGHT = cfg["base_score_weight"]
    PREFERENCE_SCORE_WEIGHT = cfg["preference_score_weight"]

    VLM_OPTIONS = {
        "temperature": cfg["vlm_temperature"],
        "top_p": cfg["vlm_top_p"],
        "top_k": cfg["vlm_top_k"],
        "num_think": 0,
    }

    VLM_MODEL = "llava:13b"
    LLM_MODEL = llm_model_name()

    video_name = os.path.splitext(os.path.basename(video_path))[0]
    os.makedirs(frames_dir, exist_ok=True)
    os.makedirs(export_dir, exist_ok=True)

    # ---- Phase 1: Probe video + sample frames --------------------------
    print("\n[1/4] Probing video + extracting samples...")

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            video_path,
        ],
        capture_output=True,
        text=True,
    )
    total_duration = float(probe.stdout.strip())
    print(f"  Duration: {total_duration:.0f}s ({total_duration/60:.0f} min)")

    timestamps = list(
        range(SAMPLE_INTERVAL, int(total_duration) - int(MAX_DURATION), SAMPLE_INTERVAL)
    )
    print(f"  Sampling {len(timestamps)} timestamps")

    sample_frames = []
    for i, ts in enumerate(timestamps):
        out_path = f"{frames_dir}/ts_{ts:06d}.jpg"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                str(ts),
                "-i",
                video_path,
                "-vf",
                "scale=640:-1",
                "-vframes",
                "1",
                out_path,
            ],
            capture_output=True,
            timeout=15,
        )
        if os.path.exists(out_path) and os.path.getsize(out_path) > 500:
            try:
                img = Image.open(out_path).convert("L")
                brightness = sum(img.getdata()) / max(1, img.width * img.height)
                img.close()
                if brightness > 25:
                    sample_frames.append({"path": out_path, "timestamp": ts})
            except Exception:
                pass
        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{len(timestamps)}] extracted, {len(sample_frames)} kept")

    print(f"  Frames after dark filter: {len(sample_frames)}")

    # ---- Phase 2: VLM scoring ------------------------------------------
    print(f"\n[2/4] VLM scoring ({len(sample_frames)} frames)...")

    if is_local_llm():
        stop_model(LLM_MODEL.split("/")[-1].split(":")[0])
    stop_model("nomic-embed-text")
    time.sleep(5)
    if not wait_model(VLM_MODEL):
        print("ERROR: VLM not responding")
        sys.exit(1)

    scored = []
    for fi, sf in enumerate(sample_frames):
        with open(sf["path"], "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")

        for attempt in range(3):
            try:
                resp = httpx.post(
                    f"{OLLAMA_BASE}/api/generate",
                    json={
                        "model": VLM_MODEL,
                        "prompt": SCORE_PROMPT,
                        "images": [img_b64],
                        "stream": False,
                        "options": VLM_OPTIONS,
                    },
                    timeout=120,
                )
                resp.raise_for_status()
                raw = resp.json().get("response", "")
                parsed = parse_vlm_response(raw)

                worth = safe_worth(parsed.get("gif_worthiness", 0.5))
                parsed["gif_worthiness"] = worth
                parsed["timestamp"] = sf["timestamp"]
                parsed["path"] = sf["path"]

                if worth >= WORTHINESS_THRESHOLD:
                    scored.append(parsed)

                if (fi + 1) % 30 == 0:
                    avg = sum(s["gif_worthiness"] for s in scored) / max(1, len(scored))
                    print(
                        f"  [{fi+1}/{len(sample_frames)}] scored={len(scored)} kept, "
                        f"avg_worth={avg:.2f}"
                    )
                break
            except Exception as e:
                if attempt == 2:
                    print(f"  [{fi+1}] FAILED: {e}")
                time.sleep(2)

    print(f"  Scored: {len(scored)} frames kept (threshold={WORTHINESS_THRESHOLD})")

    bins = {"0.0-0.3": 0, "0.3-0.5": 0, "0.5-0.7": 0, "0.7-0.9": 0, "0.9-1.0": 0}
    for s in scored:
        w = s["gif_worthiness"]
        if w < 0.3:
            bins["0.0-0.3"] += 1
        elif w < 0.5:
            bins["0.3-0.5"] += 1
        elif w < 0.7:
            bins["0.5-0.7"] += 1
        elif w < 0.9:
            bins["0.7-0.9"] += 1
        else:
            bins["0.9-1.0"] += 1
    print(f"  Worthiness distribution: {bins}")

    # ---- Phase 2.5: Boundary refinement ---------------------------------
    print(f"\n[2.5/4] Boundary refinement around high-score regions...")

    high_ts = {r["timestamp"] for r in scored if r["gif_worthiness"] >= REFINE_THRESHOLD}
    refine_ts = set()

    for ts in high_ts:
        for offset in range(
            -REFINE_RADIUS, REFINE_RADIUS + REFINE_INTERVAL, REFINE_INTERVAL
        ):
            new_ts = ts + offset
            if (
                0 <= new_ts <= total_duration - 1
                and new_ts not in {r["timestamp"] for r in scored}
            ):
                refine_ts.add(new_ts)

    existing_ts = {r["timestamp"] for r in scored}
    refine_ts -= existing_ts

    print(
        f"  High-score regions: {len(high_ts)}, new frames to sample: {len(refine_ts)}"
    )

    if refine_ts:
        refine_frames = []
        for ts in sorted(refine_ts):
            out_path = f"{frames_dir}/ts_{ts:06d}.jpg"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    str(ts),
                    "-i",
                    video_path,
                    "-vf",
                    "scale=640:-1",
                    "-vframes",
                    "1",
                    out_path,
                ],
                capture_output=True,
                timeout=15,
            )
            if os.path.exists(out_path) and os.path.getsize(out_path) > 500:
                try:
                    img = Image.open(out_path).convert("L")
                    brightness = sum(img.getdata()) / max(1, img.width * img.height)
                    img.close()
                    if brightness > 25:
                        refine_frames.append({"path": out_path, "timestamp": ts})
                except Exception:
                    pass

        print(f"  Refinement frames after filter: {len(refine_frames)}")

        for fi, rf in enumerate(refine_frames):
            with open(rf["path"], "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("utf-8")

            for attempt in range(3):
                try:
                    resp = httpx.post(
                        f"{OLLAMA_BASE}/api/generate",
                        json={
                            "model": VLM_MODEL,
                            "prompt": SCORE_PROMPT,
                            "images": [img_b64],
                            "stream": False,
                            "options": VLM_OPTIONS,
                        },
                        timeout=120,
                    )
                    resp.raise_for_status()
                    raw = resp.json().get("response", "")
                    parsed = parse_vlm_response(raw)

                    worth = safe_worth(parsed.get("gif_worthiness", 0.5))
                    parsed["gif_worthiness"] = worth
                    parsed["timestamp"] = rf["timestamp"]
                    parsed["path"] = rf["path"]

                    if worth >= WORTHINESS_THRESHOLD:
                        scored.append(parsed)
                    break
                except Exception as e:
                    if attempt == 2:
                        print(f"  refine [{fi+1}] FAILED: {e}")
                    time.sleep(2)

            if (fi + 1) % 50 == 0:
                print(
                    f"  refine [{fi+1}/{len(refine_frames)}] done, "
                    f"scored={len(scored)}"
                )

        print(f"  After refinement: {len(scored)} total scored frames")

    # ---- Phase 2.6: Merge adjacent frames into clip groups --------------
    print(f"\n[2.6/4] Merging adjacent frames into clips...")

    scored.sort(key=lambda x: x["timestamp"])

    clips = []
    current_group = [scored[0]]

    for r in scored[1:]:
        gap = r["timestamp"] - current_group[-1]["timestamp"]
        both_good = (
            r["gif_worthiness"] >= MERGE_SCORE_THRESHOLD
            and current_group[-1]["gif_worthiness"] >= MERGE_SCORE_THRESHOLD
        )
        if gap <= MERGE_GAP and both_good:
            current_group.append(r)
        else:
            best = max(current_group, key=lambda x: x["gif_worthiness"])
            clips.append(
                {
                    "start_ts": current_group[0]["timestamp"],
                    "end_ts": current_group[-1]["timestamp"],
                    "best_frame": best,
                    "frame_count": len(current_group),
                    "gif_worthiness": best["gif_worthiness"],
                    "emotional_core": best.get("emotional_core", "?"),
                }
            )
            current_group = [r]

    if current_group:
        best = max(current_group, key=lambda x: x["gif_worthiness"])
        clips.append(
            {
                "start_ts": current_group[0]["timestamp"],
                "end_ts": current_group[-1]["timestamp"],
                "best_frame": best,
                "frame_count": len(current_group),
                "gif_worthiness": best["gif_worthiness"],
                "emotional_core": best.get("emotional_core", "?"),
            }
        )

    print(f"  Merged into {len(clips)} clips (merge_gap={MERGE_GAP}s)")
    multi_frame = sum(1 for c in clips if c["frame_count"] > 1)
    print(f"  Multi-frame clips (crossing boundaries): {multi_frame}")
    single_frame = sum(1 for c in clips if c["frame_count"] == 1)
    print(f"  Single-frame clips: {single_frame}")

    # ---- Phase 2.7: Embedding dedup -------------------------------------

    if EMBED_DEDUP_ENABLED and len(clips) > 1:
        print(f"\n[2.7/4] Embedding dedup (threshold={EMBED_SIM_THRESHOLD})...")
        import numpy as np

        clip_embeddings = []
        for clip in clips:
            bf = clip.get("best_frame", {})
            text = " ".join(
                filter(
                    None,
                    [
                        bf.get("caption", ""),
                        bf.get("emotional_core", ""),
                        bf.get("scene_type", ""),
                    ],
                )
            )
            if not text:
                clip_embeddings.append(None)
                continue
            try:
                emb = compute_text_embedding(text)
                clip_embeddings.append(np.array(emb, dtype=np.float32))
            except Exception:
                clip_embeddings.append(None)

        order = sorted(
            range(len(clips)),
            key=lambda i: clips[i]["gif_worthiness"],
            reverse=True,
        )
        kept_indices = []
        kept_embs = []
        duplicate_groups = []

        for idx in order:
            emb = clip_embeddings[idx]
            if emb is None:
                kept_indices.append(idx)
                kept_embs.append(None)
                continue
            is_dup = False
            for ki, ke_emb in zip(kept_indices, kept_embs):
                if ke_emb is None:
                    continue
                norm = np.linalg.norm(emb) * np.linalg.norm(ke_emb) + 1e-8
                sim = float(np.dot(emb, ke_emb) / norm)
                if sim >= EMBED_SIM_THRESHOLD:
                    is_dup = True
                    for dg in duplicate_groups:
                        if dg["keeper"] == ki:
                            dg["duplicates"].append(idx)
                            dg["max_sim"] = max(dg["max_sim"], sim)
                            break
                    else:
                        duplicate_groups.append(
                            {"keeper": ki, "duplicates": [idx], "max_sim": sim}
                        )
                    break
            if not is_dup:
                kept_indices.append(idx)
                kept_embs.append(emb)

        deduped_clips = [clips[i] for i in kept_indices]
        clusters = [
            {
                "center_emb": ki,
                "members": [ki]
                + sum(
                    [
                        dg["duplicates"]
                        for dg in duplicate_groups
                        if dg["keeper"] == ki
                    ],
                    [],
                ),
            }
            for ki in kept_indices
        ]
        dedup_input_clips = len(clips)
        print(
            f"  Dedup: {dedup_input_clips} -> {len(deduped_clips)} clips "
            f"({len(duplicate_groups)} duplicate groups, "
            f"{sum(len(dg['duplicates']) for dg in duplicate_groups)} clips removed)"
        )
    else:
        deduped_clips = clips
        clusters = [{"center_emb": None, "members": [i]} for i in range(len(clips))]
        duplicate_groups = []
        dedup_input_clips = len(clips)
        print(f"\n[2.7/4] Dedup disabled -- {len(deduped_clips)} clips passed through")

    embedding_deduped_clips = len(deduped_clips)
    if TEMPORAL_DEDUP_ENABLED and len(deduped_clips) > 1:
        before_temporal = len(deduped_clips)
        deduped_clips = temporal_dedup_clips(
            deduped_clips, min_gap_s=TEMPORAL_DEDUP_MIN_GAP_S
        )
        print(
            f"  Temporal dedup: {before_temporal} -> {len(deduped_clips)} clips "
            f"(min_gap={TEMPORAL_DEDUP_MIN_GAP_S:.1f}s)"
        )
    elif not TEMPORAL_DEDUP_ENABLED:
        print(
            f"  Temporal dedup disabled -- {len(deduped_clips)} clips passed through"
        )

    # ---- Phase 3: RAG + LLM synthesis -----------------------------------
    print(f"\n[3/4] RAG + LLM synthesis...")

    stop_model("llava")
    time.sleep(10)
    if not wait_for_llm(timeout_s=180):
        print("WARNING: LLM not responding -- skipping synthesis, proceeding to export")
        synthesis = {"_parse_error": True}

    idx = get_index()
    for r in scored:
        caption = r.get("caption", "")
        if caption and idx.count > 0:
            try:
                emb = compute_text_embedding(caption)
                similar = idx.search(emb, top_k=3)
                r["rag_similar"] = [
                    {
                        "mid": s["media_id"],
                        "score": s["score"],
                        "emo": s.get("emotional_core", ""),
                        "tags": s.get("tags", [])[:3],
                    }
                    for s in similar
                ]
            except Exception:
                r["rag_similar"] = []
        else:
            r["rag_similar"] = []

    top_for_synth = sorted(
        deduped_clips, key=lambda x: x["gif_worthiness"], reverse=True
    )[:20]
    analyses = "\n\n".join(
        f"Frame {i+1} (t={c['best_frame']['timestamp']}s, worth={c['gif_worthiness']:.2f}): "
        f"caption={c['best_frame'].get('caption','')}, "
        f"emotion={c['best_frame'].get('emotional_core','')}"
        for i, c in enumerate(top_for_synth)
    )

    synth_prompt = (
        "Synthesize scene analyses from a film. Output ONLY JSON:\n"
        '{"summary":"one sentence about visual style","emotional_core":"one dominant emotion",'
        '"aesthetic_notes":["2-4 qualities"],"tags":["3-5 keywords"],'
        '"scene_type":"close-up|dialogue|action|transition|reaction|establishing|montage|other"}\n\n'
        "Scene analyses:\n" + analyses
    )

    synthesis = {"_parse_error": True}
    llm_available = wait_for_llm(timeout_s=180)
    if llm_available:
        for attempt in range(3):
            try:
                raw = generate_llm_text(synth_prompt, temperature=0.3, timeout=180)
                result = parse_json_response(raw)
                if result.ok:
                    synthesis = result.data
                    print(f"  summary: {synthesis.get('summary','?')}")
                    print(f"  emotional_core: {synthesis.get('emotional_core','?')}")
                    print(f"  tags: {synthesis.get('tags',[])}")
                    break
                else:
                    synthesis = {"_parse_error": True, "_raw": raw[:500]}
                    print(f"  Attempt {attempt+1}: JSON parse failed")
            except Exception as e:
                print(f"  Attempt {attempt+1}: {e}")
                time.sleep(5)
    else:
        print(
            "  Skipping LLM synthesis -- model unavailable, proceeding to GIF export"
        )

    # ---- Phase 3.5: 9-grid sample thumbnail -----------------------------
    print(f"\n[3.5/4] Generating 9-grid sample thumbnail...")
    import imagehash
    from PIL import Image as PILImage

    GRID_SIZE = 3
    GRID_CELL_W = 480
    GRID_CELL_H = 270
    GRID_DEDUP_THRESHOLD = 10

    sample_dir = os.path.join(export_dir, "Sample")
    os.makedirs(sample_dir, exist_ok=True)

    ranked_frames = sorted(
        scored, key=lambda x: x.get("gif_worthiness", 0), reverse=True
    )

    selected = []
    selected_hashes = []
    for frame in ranked_frames:
        if len(selected) >= GRID_SIZE * GRID_SIZE:
            break
        fp = frame.get("path", "")
        if not fp or not os.path.exists(fp):
            continue
        try:
            with PILImage.open(fp) as img:
                ph = imagehash.phash(img)
        except Exception:
            continue
        if any(ph - sh <= GRID_DEDUP_THRESHOLD for sh in selected_hashes):
            continue
        selected.append(frame)
        selected_hashes.append(ph)

    print(f"  Selected {len(selected)} diverse frames (from {len(ranked_frames)} scored)")

    if selected:
        grid = PILImage.new(
            "RGB",
            (GRID_CELL_W * GRID_SIZE, GRID_CELL_H * GRID_SIZE),
            (0, 0, 0),
        )
        for i, frame in enumerate(selected):
            fp = frame["path"]
            ts = frame.get("timestamp", 0)
            worth = frame.get("gif_worthiness", 0)
            sample_path = os.path.join(
                sample_dir,
                f"{video_name}_sample_{i+1:02d}_{ts:.0f}s_w{worth:.2f}.jpg",
            )
            try:
                with PILImage.open(fp) as img:
                    img.save(sample_path, "JPEG", quality=90)
            except Exception as e:
                print(f"  Warning: could not save sample {i+1}: {e}")
            row, col = divmod(i, GRID_SIZE)
            try:
                with PILImage.open(fp) as img:
                    cell = img.resize(
                        (GRID_CELL_W, GRID_CELL_H), PILImage.LANCZOS
                    )
                    grid.paste(cell, (col * GRID_CELL_W, row * GRID_CELL_H))
            except Exception:
                pass

        grid_path = os.path.join(sample_dir, f"{video_name}_grid.jpg")
        grid.save(grid_path, "JPEG", quality=90)
        print(f"  Grid: {grid_path} ({len(selected)} frames)")
        print(f"  Individual samples: {sample_dir}/{video_name}_sample_*.jpg")

    # ---- Phase 4: Export adaptive-duration GIFs -------------------------
    output_count = int(len(deduped_clips) * OUTPUT_RATIO)
    if MAX_OUTPUT > 0:
        output_count = min(output_count, MAX_OUTPUT)
    output_count = max(1, output_count)

    print(
        f"\n[4/4] Exporting {output_count}/{len(deduped_clips)} GIFs (4K) "
        f"({OUTPUT_RATIO*100:.0f}% ratio, cap={MAX_OUTPUT})..."
    )

    if PREFERENCE_MEMORY_ENABLED:
        from app.services.reranker import PreferenceReranker, blend_export_scores

        reranker_conn = get_connection()
        reranker = PreferenceReranker(reranker_conn)

        def score_clip_with_preference(clip):
            caption = clip["best_frame"].get("caption", "")
            if not caption:
                return None
            vec = compute_text_embedding(caption)
            if vec is None:
                return None
            emo = clip["best_frame"].get("emotional_core", "")
            scenario_keys = [f"emotion:{emo}"] if (emo and emo != "?") else []
            breakdown = reranker.score(
                candidate_vector=vec,
                base_rag_similarity=clip["gif_worthiness"],
                scenario_keys=scenario_keys,
                profile_version=None,
                enabled=True,
            )
            profile_score = breakdown.get("profile_score")
            if profile_score is None:
                return None
            return {
                "final_score": blend_export_scores(
                    clip["gif_worthiness"],
                    profile_score,
                    BASE_SCORE_WEIGHT,
                    PREFERENCE_SCORE_WEIGHT,
                ),
                "profile_score": profile_score,
                "score_profile_version": breakdown.get("preference_profile_version"),
            }

        try:
            all_ranked_clips = rank_clips_for_export(
                deduped_clips, score_clip_with_preference
            )
        finally:
            reranker_conn.close()
        print(
            "Preference Memory: ranked all candidates with "
            f"base={BASE_SCORE_WEIGHT:.2f}, preference={PREFERENCE_SCORE_WEIGHT:.2f}"
        )
    else:
        all_ranked_clips = rank_clips_for_export(deduped_clips, lambda _clip: None)

    ranked_clips = all_ranked_clips[:output_count]

    exported_bookmarks = []
    gif_export_results = []
    potplayer_pbf_path = None

    for i, clip in enumerate(ranked_clips):
        worth = clip["gif_worthiness"]
        r = clip["best_frame"]

        if clip["frame_count"] > 1:
            duration = min(clip["end_ts"] - clip["start_ts"] + 3.0, MAX_DURATION + 2.0)
        else:
            duration = MIN_DURATION + (MAX_DURATION - MIN_DURATION) * worth

        ts = r["timestamp"]
        start = max(0, ts - duration * 0.4)
        start = min(start, total_duration - duration)

        start_ts = int(start)
        end_ts = int(start + duration)

        out_gif = os.path.join(
            export_dir,
            build_gif_filename(video_name, i + 1, start, start + duration),
        )
        palette = f"{export_dir}/pal_{i+1:03d}.png"

        fps = GIF_FPS

        attempt = run_gif_export_attempt(
            palette_command=[
                "ffmpeg",
                "-y",
                "-ss",
                str(start),
                "-t",
                str(duration),
                "-i",
                video_path,
                "-vf",
                f"fps={fps},scale={GIF_MAX_WIDTH}:-1:flags=lanczos,palettegen",
                palette,
            ],
            gif_command=[
                "ffmpeg",
                "-y",
                "-ss",
                str(start),
                "-t",
                str(duration),
                "-i",
                video_path,
                "-i",
                palette,
                "-filter_complex",
                f"fps={fps},scale={GIF_MAX_WIDTH}:-1:flags=lanczos[x];[x][1:v]paletteuse",
                out_gif,
            ],
            palette_path=palette,
            output_path=out_gif,
        )
        gif_export_results.append(
            {
                "index": i + 1,
                "path": out_gif,
                "status": "OK" if attempt.success else "FAILED",
                "size_bytes": attempt.size_bytes,
                "error": attempt.error,
            }
        )

        if attempt.success:
            exported_bookmarks.append(
                PotPlayerBookmark(
                    start_s=start,
                    end_s=start + duration,
                    rank=i + 1,
                    score=worth,
                    merged=clip["frame_count"] > 1,
                    caption=r.get("caption")
                    or r.get("reason")
                    or r.get("emotional_core")
                    or "",
                )
            )
        print(
            format_gif_export_line(
                video_name=video_name,
                index=i + 1,
                total=len(ranked_clips),
                output_path=out_gif,
                status="OK" if attempt.success else "FAILED",
                worthiness=worth,
                duration_s=duration,
                timestamp_s=int(ts),
                merged=clip["frame_count"] > 1,
                frame_count=clip["frame_count"],
                size_bytes=attempt.size_bytes,
                emotional_core=r.get("emotional_core", "?"),
                error=attempt.error,
            ),
            flush=True,
        )

    gif_attempted = len(gif_export_results)
    gif_succeeded = sum(item["status"] == "OK" for item in gif_export_results)
    gif_failed = gif_attempted - gif_succeeded

    if POTPLAYER_PBF_ENABLED and exported_bookmarks:
        potplayer_pbf_path = write_pbf_file(
            os.path.join(export_dir, f"{video_name}.pbf"),
            exported_bookmarks,
        )
        print(f"  PotPlayer bookmarks: {potplayer_pbf_path}")

    # ---- Build output dict ------------------------------------------------
    output = {
        "video": video_path,
        "sample_interval": SAMPLE_INTERVAL,
        "total_samples": len(sample_frames),
        "scored_kept": len(scored),
        "worthiness_distribution": bins,
        "synthesis": synthesis,
        "merge_gap": MERGE_GAP,
        "refine_radius": REFINE_RADIUS,
        "refine_interval": REFINE_INTERVAL,
        "output_ratio": OUTPUT_RATIO,
        "max_output": MAX_OUTPUT,
        "embed_dedup_threshold": EMBED_SIM_THRESHOLD,
        "embed_dedup_enabled": EMBED_DEDUP_ENABLED,
        "temporal_dedup_enabled": TEMPORAL_DEDUP_ENABLED,
        "temporal_dedup_min_gap_s": TEMPORAL_DEDUP_MIN_GAP_S,
        "potplayer_pbf_enabled": POTPLAYER_PBF_ENABLED,
        "potplayer_pbf_path": potplayer_pbf_path,
        "dedup_input_clips": dedup_input_clips,
        "embedding_deduped_clips": embedding_deduped_clips,
        "deduped_clips": len(deduped_clips),
        "clusters_after_dedup": len(deduped_clips),
        "duplicate_groups": duplicate_groups,
        "planned_output_count": output_count,
        "output_count": gif_succeeded,
        "gif_attempted": gif_attempted,
        "gif_succeeded": gif_succeeded,
        "gif_failed": gif_failed,
        "gif_exports": gif_export_results,
        "preference_memory_enabled": PREFERENCE_MEMORY_ENABLED,
        "base_score_weight": BASE_SCORE_WEIGHT,
        "preference_score_weight": PREFERENCE_SCORE_WEIGHT,
        "multi_frame_clips": sum(1 for c in clips if c["frame_count"] > 1),
        "top_clips": [
            {
                "rank": i + 1,
                "timestamp": clip["best_frame"]["timestamp"],
                "start_ts": clip["start_ts"],
                "end_ts": clip["end_ts"],
                "gif_worthiness": clip["gif_worthiness"],
                "final_score": clip.get("final_score", clip["gif_worthiness"]),
                "profile_score": clip.get("profile_score"),
                "score_profile_version": clip.get("score_profile_version"),
                "duration": (
                    min(clip["end_ts"] - clip["start_ts"] + 3.0, MAX_DURATION + 2.0)
                    if clip["frame_count"] > 1
                    else MIN_DURATION
                    + (MAX_DURATION - MIN_DURATION) * clip["gif_worthiness"]
                ),
                "frame_count": clip["frame_count"],
                "merged": clip["frame_count"] > 1,
                "caption": clip["best_frame"].get("caption"),
                "emotional_core": clip["best_frame"].get("emotional_core"),
                "aesthetic_notes": clip["best_frame"].get("aesthetic_notes"),
                "reason": clip["best_frame"].get("reason"),
                "export_status": gif_export_results[i]["status"],
                "export_path": gif_export_results[i]["path"],
                "export_error": gif_export_results[i]["error"],
            }
            for i, clip in enumerate(ranked_clips)
        ],
    }

    if gif_failed:
        raise SystemExit(1)

    return output


# ---- Direct mode (original end-to-end behavior) -------------------


def run_direct_mode(video_path: str, export_dir: str | None = None) -> dict:
    """Run the full adaptive pipeline with lock, cleanup, and result persistence."""
    load_config()
    init_db()

    video_name = os.path.splitext(os.path.basename(video_path))[0]
    if export_dir:
        EXPORT_DIR = os.path.join(export_dir, video_name)
    else:
        EXPORT_DIR = "data/exports/adaptive_test"
    FRAMES_DIR = f"data/frames/adaptive_test/{video_name}"
    os.makedirs(FRAMES_DIR, exist_ok=True)
    os.makedirs(EXPORT_DIR, exist_ok=True)

    print(f"Video: {os.path.basename(video_path)}")
    print(f"Export: {EXPORT_DIR}")

    # Read config via shared extract_config (same logic as stage mode)
    cfg = extract_config(
        {
            "adaptive": get("adaptive", {}) or {},
            "preference_memory": get("preference_memory", {}) or {},
        }
    )

    print("=" * 60)
    print(
        f"Adaptive GIF Extraction -- "
        f"{cfg['sample_interval']}s intervals, "
        f"ratio={cfg['output_ratio']}, cap={cfg['max_output']}"
    )
    print("=" * 60)

    export_lock = ExportDirectoryLock(EXPORT_DIR)
    try:
        export_lock.acquire()
    except ExportDirectoryBusyError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(2)
    atexit.register(export_lock.release)

    if cfg["clear_output_dir"]:
        removed = cleanup_adaptive_export_dir(EXPORT_DIR, video_name=video_name)
        if removed:
            print(f"Cleaned previous export artifacts: {removed}")

    output = run_pipeline(video_path, FRAMES_DIR, EXPORT_DIR, cfg)

    # Save result
    with open("data/adaptive_test_result.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # Print final stats
    ranked_clips = output.get("top_clips", [])
    if ranked_clips:
        durations = []
        for clip_data in ranked_clips:
            durations.append(clip_data["duration"])
        emotions = {}
        for c_data in ranked_clips:
            e = c_data.get("emotional_core", "?")
            emotions[e] = emotions.get(e, 0) + 1
        merged_count = sum(1 for c in ranked_clips if c["merged"])

        print(f"\n{'='*60}")
        print(f"Two-pass adaptive extraction complete!")
        print(
            f"  Sampling: every {output.get('sample_interval')}s, "
            f"refine {output.get('refine_radius')}s radius @ {output.get('refine_interval')}s"
        )
        print(
            f"  Pass 1: {output.get('total_samples', 0)} coarse frames scored"
        )
        print(
            f"  Pass 2: {output.get('refine_interval', 0)} refinement frames "
            f"around high-score regions"
        )
        print(f"  Clips: {output.get('dedup_input_clips', 0)} total")
        print(
            f"  Dedup: {output.get('dedup_input_clips', 0)} -> "
            f"{output.get('embedding_deduped_clips', 0)} embedding -> "
            f"{output.get('deduped_clips', 0)} temporal"
        )
        print(
            f"  Output: {output.get('output_count', 0)} GIFs @ max "
            f"{cfg['gif_max_width']}px "
            f"(ratio={output.get('output_ratio')}, cap={output.get('max_output')})"
        )
        if durations:
            print(
                f"  Duration: {min(durations):.1f}s - {max(durations):.1f}s"
            )
            print(
                f"  Worthiness: {min(c['gif_worthiness'] for c in ranked_clips):.2f} - "
                f"{max(c['gif_worthiness'] for c in ranked_clips):.2f}"
            )
        print(f"  Emotions: {dict(sorted(emotions.items(), key=lambda x: -x[1]))}")
        print(f"  Export: {EXPORT_DIR}/")
        print(f"{'='*60}")

    return output


# ---- Stage mode ---------------------------------------------------


def run_stage_mode(
    *,
    stage: str,
    video_path: str,
    work_dir: str,
    result_path: str,
    config_path: str,
    input_manifest_path: str | None = None,
    clip_id: str | None = None,
) -> None:
    """Run the adaptive pipeline in stage mode.

    Each stage reads its input manifest from the work directory (written
    by a previous stage), does only its own work, and writes its output
    manifest.  No stage re-executes work done by previous stages.

    Stage mode differs from direct mode:

    * Config is read from *config_path* (a JSON snapshot) instead of
      calling ``load_config()``.
    * Upstream inputs are read from *input_manifest_path* (a JSON file
      mapping artifact kinds to artifact metadata including file paths),
      NOT from directory guessing via ``prior_stage_work_dirs``.
    * All temporary files live under *work_dir*.
    * Export cleanup is **disabled** so previously-registered exports
      from earlier stages are never touched.
    * A machine-readable result JSON is written to *result_path*
      atomically (tmp + rename).
    * stdout/stderr are redirected to a log file under *work_dir*.
    """
    # Load config from the snapshot provided by the worker
    with open(config_path, "r", encoding="utf-8") as f:
        config_data = json.load(f)

    # Phase 3: Normalize config to unified top-level format.
    # Handles both historical config_snapshot wrapper and new flat format.
    from app.quality_lab.config_builder import normalize_task_config
    config_data = normalize_task_config(config_data)

    # Override the global config module so that ``get()`` calls inside
    # helpers (via imported modules) see the correct values.
    from app.config import set_config_override

    set_config_override(config_data)

    init_db()

    video_name = os.path.splitext(os.path.basename(video_path))[0]
    FRAMES_DIR = os.path.join(work_dir, "frames")
    EXPORT_DIR = os.path.join(work_dir, "exports", video_name)
    os.makedirs(FRAMES_DIR, exist_ok=True)
    os.makedirs(EXPORT_DIR, exist_ok=True)

    cfg = extract_config(config_data)
    # Stage mode never cleans the export dir
    cfg["clear_output_dir"] = False

    # P0-2: Load input manifest from the path the adapter provides.
    # This replaces the old prior_stage_work_dirs directory guessing.
    input_manifest: dict = {}
    if input_manifest_path and os.path.exists(input_manifest_path):
        with open(input_manifest_path, "r", encoding="utf-8") as f:
            input_manifest = json.load(f)

    # Redirect prints to a log file inside the work dir
    log_path = os.path.join(work_dir, "stage.log")
    log_file = open(log_path, "w", encoding="utf-8")
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = log_file
    sys.stderr = log_file

    try:
        output = _run_stage(
            stage,
            video_path=video_path,
            frames_dir=FRAMES_DIR,
            export_dir=EXPORT_DIR,
            work_dir=work_dir,
            cfg=cfg,
            input_manifest=input_manifest,
            clip_id=clip_id,
            config_data=config_data,
        )
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        log_file.close()

    # Build artifact list from the stage handler's explicit output.
    # Phase 2: Each stage handler explicitly returns artifacts with
    # artifact_kind.  We no longer scan directories by extension.
    artifacts: list[dict] = list(output.get("_artifacts", []))

    # Extract scalar metrics from the pipeline output
    metrics: dict[str, int | float | str] = {}
    for k, v in output.items():
        if isinstance(v, (int, float, str)):
            metrics[k] = v

    result = {
        "stage": stage,
        "output_key": output.get("output_key", stage),
        # P0-2: propagate the explicit terminal outcome so the worker can
        # mark materialize needs_attention on unrecoverable publish conflicts.
        "outcome": output.get("outcome", "succeeded"),
        "artifacts": artifacts,
        "metrics": metrics,
    }

    # Write atomically
    tmp_path = result_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, result_path)


# ---------------------------------------------------------------------------
# Manifest I/O helpers
# ---------------------------------------------------------------------------

_MANIFEST_NAME: dict[str, str] = {
    "discover": "discover_manifest.json",
    "sample": "sample_manifest.json",
    "vlm": "vlm_manifest.json",
    "refine": "refine_manifest.json",
    "synthesize": "synthesize_manifest.json",
    "rank_dedup": "rank_dedup_manifest.json",
    "gif_clip": "gif_clip_manifest.json",
    "materialize": "materialize_manifest.json",
}

_PREV_STAGE: dict[str, str | None] = {
    "discover": None,
    "sample": "discover",
    "vlm": "sample",
    "refine": "vlm",
    "synthesize": "refine",
    "rank_dedup": "synthesize",
    "gif_clip": "rank_dedup",
    "materialize": None,
}


def _load_manifest(work_dir: str, stage_name: str, prior_work_dirs: dict[str, str] | None = None) -> dict:
    """Load the manifest written by *stage_name* from the work directory.

    If *prior_work_dirs* is provided, it maps stage_name to the directory
    where that stage's manifests are stored (used for cross-stage reads).
    """
    manifest_name = _MANIFEST_NAME.get(stage_name, f"{stage_name}_manifest.json")
    search_dir = work_dir
    if prior_work_dirs and stage_name in prior_work_dirs:
        search_dir = prior_work_dirs[stage_name]
    path = os.path.join(search_dir, manifest_name)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_manifest(work_dir: str, stage_name: str, data: dict) -> str:
    """Save *data* as the manifest for *stage_name*, return the path."""
    manifest_name = _MANIFEST_NAME.get(stage_name, f"{stage_name}_manifest.json")
    path = os.path.join(work_dir, manifest_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def _make_artifact(path: str, artifact_kind: str, clip_id: str | None = None) -> dict:
    """Build an explicit artifact descriptor for use by the adapter.

    Phase 2: Every artifact must carry an explicit artifact_kind.
    The adapter validates this against the stage's whitelist.
    """
    abs_path = os.path.abspath(path)
    result: dict = {
        "path": abs_path,
        "artifact_kind": artifact_kind,
    }
    if clip_id is not None:
        result["clip_id"] = clip_id
    if os.path.exists(abs_path):
        result["size_bytes"] = os.path.getsize(abs_path)
    return result


def _hash_artifact_id(artifact_kind: str, path: str, stage_id: str = "", clip_id: str | None = None) -> str:
    """Generate a stable artifact_id hash compatible with
    ``app.task_engine.artifacts.make_artifact_id``.

    P1-3: Used by stage handlers to embed artifact_ids in manifests
    for cross-referencing by downstream stages.
    """
    from app.task_engine.fingerprints import canonical_hash
    return canonical_hash({
        "stage_id": stage_id,
        "artifact_kind": artifact_kind,
        "clip_id": clip_id or "",
        "path": Path(path).as_posix(),
    })


def _read_upstream_manifest(inputs: dict, artifact_kind: str, stage: str) -> dict:
    """Read an upstream manifest from the input dict (P0-2 protocol).

    Looks up *artifact_kind* in *inputs*, reads the first artifact's file,
    and validates the manifest structure via ``validate_manifest_json``.

    P1-2: All validation errors raise ``ValueError`` (which the worker
    converts to a structured ``StageError``).  Errors include:
    missing fields, wrong stage, wrong clip_id, unsupported version,
    empty JSON, wrong encoding, manifest/GIF SHA mismatch.

    Raises ``ValueError`` if missing, invalid, or inconsistent.
    """
    from app.task_engine.artifacts import validate_manifest_json

    entries = inputs.get(artifact_kind, [])
    if not entries:
        raise ValueError(
            f"No {artifact_kind} entry in input manifest for stage {stage!r}"
        )
    ref = entries[0]
    path = ref.get("path", "")
    if not path or not os.path.exists(path):
        raise ValueError(f"Input artifact file not found: {path}")

    # P1-2: Read raw bytes and validate via shared validator.
    try:
        with open(path, "rb") as f:
            raw_bytes = f.read()
    except (UnicodeDecodeError, OSError) as exc:
        raise ValueError(
            f"Cannot read manifest file {path}: {exc}"
        ) from exc

    if not raw_bytes:
        raise ValueError(f"Empty manifest file: {path}")

    # Determine the expected producer stage from the artifact kind.
    _EXPECTED_PRODUCER: dict[str, str] = {
        "discover_manifest": "discover",
        "sample_manifest": "sample",
        "vlm_manifest": "vlm",
        "refine_manifest": "refine",
        "synthesize_manifest": "synthesize",
        "rank_dedup_manifest": "rank_dedup",
        "gif_clip_manifest": "gif_clip",
    }
    expected_producer = _EXPECTED_PRODUCER.get(artifact_kind)
    expected_clip_id = ref.get("clip_id") or None

    # P1-2: Call shared validator with expected stage, clip_id.
    data = validate_manifest_json(
        raw_bytes,
        artifact_kind=artifact_kind,
        expected_stage=expected_producer,
        expected_clip_id=expected_clip_id,
    )

    # P1-2: For gif_clip_manifest, additionally verify SHA
    # matches the gif_file artifact.
    if artifact_kind == "gif_clip_manifest":
        gif_entries = inputs.get("gif_file", [])
        matching_gif = None
        for ge in gif_entries:
            if ge.get("clip_id") == expected_clip_id:
                matching_gif = ge
                break
        if matching_gif:
            manifest_sha = data.get("sha256")
            if manifest_sha and manifest_sha != matching_gif.get("sha256"):
                raise ValueError(
                    f"gif_clip_manifest SHA-256 mismatch for clip "
                    f"{expected_clip_id!r}: manifest says {manifest_sha[:16]}..., "
                    f"gif_file says {str(matching_gif.get('sha256'))[:16]}..."
                )

    return data


def _load_input_manifest(work_dir: str, stage: str, prior_work_dirs: dict[str, str] | None = None) -> dict:
    """Load the manifest from the stage immediately preceding *stage*.

    Uses *prior_work_dirs* to locate manifests from previous stages'
    work directories.  Raises ValueError if the manifest is missing
    or invalid when the stage requires one.
    """
    prev = _PREV_STAGE.get(stage)
    if prev is None:
        return {}
    search_dir = work_dir
    if prior_work_dirs and prev in prior_work_dirs:
        search_dir = prior_work_dirs[prev]
    manifest_name = _MANIFEST_NAME.get(prev, f"{prev}_manifest.json")
    path = os.path.join(search_dir, manifest_name)
    if not os.path.exists(path):
        raise ValueError(
            f"Input manifest not found for stage {stage!r}: "
            f"expected {prev}_manifest.json in {search_dir}"
        )
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("stage") != prev:
        raise ValueError(
            f"Manifest at {path} has wrong stage: "
            f"expected {prev!r}, got {data.get('stage')!r}"
        )
    schema_version = data.get("schema_version")
    if schema_version is None:
        raise ValueError(f"Manifest at {path} has no schema_version")
    return data


# ---------------------------------------------------------------------------
# Stage dispatcher
# ---------------------------------------------------------------------------


def _run_stage(
    stage: str,
    *,
    video_path: str,
    frames_dir: str,
    export_dir: str,
    work_dir: str,
    cfg: dict,
    input_manifest: dict | None = None,
    clip_id: str | None = None,
    config_data: dict | None = None,
) -> dict:
    """Dispatch to the correct per-stage handler.

    Each handler reads its input from the upstream artifact paths
    provided in *input_manifest* (P0-2), NOT from directory guessing
    via ``prior_stage_work_dirs``.
    """
    inputs = input_manifest or {}
    if stage == "discover":
        return _stage_discover(video_path, work_dir, cfg)
    elif stage == "sample":
        return _stage_sample(video_path, frames_dir, work_dir, cfg, inputs, config_data)
    elif stage == "vlm":
        return _stage_vlm(frames_dir, work_dir, cfg, inputs, config_data)
    elif stage == "refine":
        return _stage_refine(video_path, frames_dir, work_dir, cfg, inputs, config_data)
    elif stage == "synthesize":
        return _stage_synthesize(work_dir, cfg, inputs)
    elif stage == "rank_dedup":
        return _stage_rank_dedup(export_dir, work_dir, cfg, inputs)
    elif stage == "gif_clip":
        return _stage_gif_clip(video_path, frames_dir, export_dir, work_dir, cfg, clip_id, inputs)
    elif stage == "materialize":
        return _stage_materialize(video_path, export_dir, work_dir, cfg, inputs, config_data)
    else:
        raise ValueError(f"Unknown stage: {stage}")


# ---------------------------------------------------------------------------
# Stage 1: discover — ffprobe + metadata only
# ---------------------------------------------------------------------------


def _stage_discover(video_path: str, work_dir: str, cfg: dict) -> dict:
    """Probe the video with ffprobe and write a video-metadata manifest.

    Does NOT sample frames, call VLM, call LLM, or export GIFs.
    """
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ],
        capture_output=True, text=True,
    )
    total_duration = float(probe.stdout.strip())
    print(f"  Duration: {total_duration:.0f}s ({total_duration / 60:.0f} min)")

    manifest = {
        "schema_version": 1,
        "stage": "discover",
        "video_path": os.path.abspath(video_path),
        "video_name": os.path.splitext(os.path.basename(video_path))[0],
        "duration_s": total_duration,
        "output_key": "discover",
    }
    manifest_path = _save_manifest(work_dir, "discover", manifest)

    return {
        "output_key": "discover",
        "duration_s": total_duration,
        "_artifacts": [_make_artifact(manifest_path, "discover_manifest")],
    }


# ---------------------------------------------------------------------------
# Stage 2: sample — coarse frame extraction + dark filter
# ---------------------------------------------------------------------------


def _stage_sample(video_path: str, frames_dir: str, work_dir: str, cfg: dict, inputs: dict, config_data: dict | None = None) -> dict:
    """Read the discover manifest, coarse-sample frames, write sample manifest.

    Does NOT call VLM or export GIFs.
    """
    discover = _read_upstream_manifest(inputs, "discover_manifest", "sample")
    total_duration = discover.get("duration_s", 0)
    SAMPLE_INTERVAL = cfg["sample_interval"]
    MAX_DURATION = cfg["max_duration"]

    # P1-3: Get stage_id from config for stable artifact_id computation.
    config_data = config_data or {}
    stage_id = config_data.get("_stage_id", "")

    timestamps = list(
        range(SAMPLE_INTERVAL, int(total_duration) - int(MAX_DURATION), SAMPLE_INTERVAL)
    )
    print(f"  Sampling {len(timestamps)} timestamps")

    sample_frames = []
    for i, ts in enumerate(timestamps):
        out_path = os.path.join(frames_dir, f"ts_{ts:06d}.jpg")
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(ts), "-i", video_path,
             "-vf", "scale=640:-1", "-vframes", "1", out_path],
            capture_output=True, timeout=15,
        )
        if os.path.exists(out_path) and os.path.getsize(out_path) > 500:
            try:
                img = Image.open(out_path).convert("L")
                brightness = sum(img.getdata()) / max(1, img.width * img.height)
                img.close()
                if brightness > 25:
                    sample_frames.append({"path": out_path, "timestamp": ts})
            except Exception:
                pass
        if (i + 1) % 50 == 0:
            print(f"  [{i + 1}/{len(timestamps)}] extracted, {len(sample_frames)} kept")

    print(f"  Frames after dark filter: {len(sample_frames)}")

    manifest = {
        "schema_version": 1,
        "stage": "sample",
        "frame_count": len(sample_frames),
        "timestamps": [f["timestamp"] for f in sample_frames],
        "frame_paths": [f["path"] for f in sample_frames],
        "sample_interval": SAMPLE_INTERVAL,
        "output_key": "sample",
        # P1-3: Store artifact_id + timestamp pairs for cross-referencing
        # by VLM stage via sample_frames resolver entries.
        "frame_entries": [
            {
                "artifact_id": _hash_artifact_id("sample_frames", f["path"], stage_id),
                "timestamp": f["timestamp"],
                "path": f["path"],
            }
            for f in sample_frames
        ],
    }
    manifest_path = _save_manifest(work_dir, "sample", manifest)

    # Build explicit artifact list: manifest + each frame
    artifacts = [_make_artifact(manifest_path, "sample_manifest")]
    for sf in sample_frames:
        artifacts.append(_make_artifact(sf["path"], "sample_frames"))

    return {
        "output_key": "sample",
        "frame_count": len(sample_frames),
        "_artifacts": artifacts,
    }


# ---------------------------------------------------------------------------
# Stage 3: vlm — VLM scoring of sampled frames only
# ---------------------------------------------------------------------------


def _stage_vlm(frames_dir: str, work_dir: str, cfg: dict, inputs: dict, config_data: dict | None = None) -> dict:
    """Read the sample manifest, score frames with VLM, write VLM manifest.

    P1-3: Cross-references sample_manifest frame_entries with sample_frames
    resolver entries by artifact_id.  Fails if a frame is missing, SHA
    mismatched, duplicate artifact_id, or manifest references unknown frame.

    Does NOT re-execute sampling.
    """
    sample_manifest = _read_upstream_manifest(inputs, "sample_manifest", "vlm")
    frame_entries = sample_manifest.get("frame_entries", [])

    # P1-3: Cross-reference sample_frames resolver entries by artifact_id.
    sample_frames_refs = inputs.get("sample_frames", [])
    frames_by_artifact_id: dict[str, dict] = {}
    duplicate_ids: set[str] = set()
    for ref in sample_frames_refs:
        aid = ref.get("artifact_id", "")
        if not aid:
            raise ValueError(
                "sample_frames entry has no artifact_id. "
                "Resolver must return artifact_id for each frame."
            )
        if aid in frames_by_artifact_id:
            duplicate_ids.add(aid)
        frames_by_artifact_id[aid] = ref

    if duplicate_ids:
        raise ValueError(
            f"Duplicate artifact_ids in sample_frames: {sorted(duplicate_ids)}"
        )

    # P1-3: Build validated frame list by cross-referencing manifest
    # frame_entries with sample_frames resolver entries.
    validated_frames = []
    for entry in frame_entries:
        aid = entry.get("artifact_id", "")
        ts = entry.get("timestamp", 0)
        path = entry.get("path", "")

        if not aid:
            raise ValueError(
                f"sample_manifest frame_entry missing artifact_id: {entry}"
            )

        resolver_ref = frames_by_artifact_id.get(aid)
        if resolver_ref is None:
            raise ValueError(
                f"sample_manifest references artifact_id {aid!r} (ts={ts}) "
                f"but no corresponding sample_frames entry found. "
                f"Known artifact_ids: {sorted(frames_by_artifact_id.keys())[:5]}..."
            )

        # Verify the frame file exists and paths match.
        resolver_path = resolver_ref.get("path", "")
        if path and resolver_path and os.path.abspath(path) != os.path.abspath(resolver_path):
            raise ValueError(
                f"Path mismatch for artifact_id {aid!r}: "
                f"manifest says {path}, resolver says {resolver_path}"
            )

        if not os.path.exists(resolver_path):
            raise FileNotFoundError(
                f"sample_frames file not found: {resolver_path} "
                f"(artifact_id={aid!r})"
            )

        # P1-3: Verify SHA-256 if available.
        expected_sha = resolver_ref.get("sha256", "")
        if expected_sha:
            from app.task_engine.fingerprints import sha256_file
            actual_sha = sha256_file(Path(resolver_path))
            if actual_sha != expected_sha:
                raise ValueError(
                    f"SHA-256 mismatch for sample_frame {aid!r}: "
                    f"expected {expected_sha[:16]}..., actual {actual_sha[:16]}..."
                )

        validated_frames.append({"path": resolver_path, "timestamp": ts})

    print(f"  P1-3: Cross-referenced {len(validated_frames)} frames "
          f"(manifest had {len(frame_entries)} entries)")

    # Fallback: if frame_entries is empty but frame_paths/timestamps exist (legacy).
    if not frame_entries:
        frame_paths = sample_manifest.get("frame_paths", [])
        timestamps = sample_manifest.get("timestamps", [])
        validated_frames = [
            {"path": p, "timestamp": t}
            for p, t in zip(frame_paths, timestamps)
            if os.path.exists(p)
        ]

    WORTHINESS_THRESHOLD = cfg["worthiness_threshold"]

    # Task 4 (seventh-review): resolve the entire VLM runtime from the frozen
    # config.  Provider validation, model, base_url, lifecycle and launch_mode
    # are all explicit; no URL inference.
    vlm_rt = _resolve_vlm_runtime(config_data)
    vlm_model = vlm_rt.model
    vlm_base_url = vlm_rt.base_url
    vlm_retry_delay = vlm_rt.retry_delay_s
    VLM_OPTIONS = {
        "temperature": cfg["vlm_temperature"],
        "top_p": cfg["vlm_top_p"],
        "top_k": cfg["vlm_top_k"],
        "num_think": 0,
    }

    # Task 4: explicit lifecycle.  manage_lifecycle=False or launch_mode=none
    # skips ALL model lifecycle (no WSL subprocess, no sleep).
    if vlm_rt.manage_lifecycle and vlm_rt.launch_mode != "none":
        if is_local_llm():
            stop_model(llm_model_name().split("/")[-1].split(":")[0], vlm_rt)
        stop_model("nomic-embed-text", vlm_rt)
        time.sleep(5)
        if not wait_model(vlm_model, vlm_rt):
            print("ERROR: VLM not responding")
            sys.exit(1)
    else:
        print(f"  [VLM runtime] model={vlm_model} base_url={vlm_base_url} "
              f"lifecycle=False launch={vlm_rt.launch_mode} (skipped)")

    print(f"\n  VLM scoring ({len(validated_frames)} frames)...")
    scored = []
    attempted_count = 0
    response_count = 0
    parsed_count = 0
    failed_count = 0

    for fi, vf in enumerate(validated_frames):
        fpath = vf["path"]
        ts = vf["timestamp"]
        with open(fpath, "rb") as f:
            img_data = f.read()

        attempted_count += 1
        payload, error = _score_vlm_frame(
            base_url=vlm_base_url, model=vlm_model,
            image_bytes=img_data, prompt=SCORE_PROMPT,
            options=VLM_OPTIONS, threshold=WORTHINESS_THRESHOLD,
            timestamp=ts, frame_path=fpath,
            retry_delay_s=vlm_retry_delay,
        )

        if payload is not None:
            response_count += 1
            parsed_count += 1
            worth = payload.get("gif_worthiness", 0.0)
            if worth >= WORTHINESS_THRESHOLD:
                scored.append(payload)
                print(f"  [{fi + 1}] score={worth:.2f} KEPT")
            else:
                print(f"  [{fi + 1}] score={worth:.2f} below threshold")
        else:
            failed_count += 1
            print(f"  [{fi + 1}] FAILED: {error}")

        if (fi + 1) % 30 == 0:
            avg = sum(s["gif_worthiness"] for s in scored) / max(1, len(scored))
            print(f"  [{fi + 1}/{len(validated_frames)}] scored={len(scored)} kept, avg_worth={avg:.2f}")

    # P0: ALL frames failed and there were frames to analyze -> stage MUST
    # fail (never produce a false zero-clip success from a service outage).
    if attempted_count > 0 and parsed_count == 0:
        raise RuntimeError(
            f"VLM stage failed: all {attempted_count} frames failed to "
            f"parse (0 parsed, {failed_count} failed).  This is a service "
            f"outage or configuration error, NOT a legitimate zero result."
        )

    print(f"  Scored: {len(scored)} frames kept (threshold={WORTHINESS_THRESHOLD})")
    kept_count = len(scored)

    manifest = {
        "schema_version": 1,
        "stage": "vlm",
        "scored_count": kept_count,
        "attempted_count": attempted_count,
        "response_count": response_count,
        "parsed_count": parsed_count,
        "failed_count": failed_count,
        "frames": [
            {
                "timestamp": s["timestamp"],
                "path": s["path"],
                "gif_worthiness": s["gif_worthiness"],
                "emotional_core": s.get("emotional_core", "?"),
                "caption": s.get("caption", ""),
            }
            for s in scored
        ],
        "output_key": "vlm",
    }
    manifest_path = _save_manifest(work_dir, "vlm", manifest)

    return {
        "output_key": "vlm",
        "scored_count": len(scored),
        "_artifacts": [_make_artifact(manifest_path, "vlm_manifest")],
    }


# ---------------------------------------------------------------------------
# Stage 4: refine — refinement sampling + VLM around high-score regions
# ---------------------------------------------------------------------------


def _stage_refine(video_path: str, frames_dir: str, work_dir: str, cfg: dict, inputs: dict, config_data: dict | None = None) -> dict:
    """Read VLM manifest, refine around high-score regions, write merged manifest.

    P0-2: reads VLM model + base URL from the frozen job config via
    ``_resolve_vlm_config``; no hardcoded model or module-level constant.
    """
    vlm_manifest = _read_upstream_manifest(inputs, "vlm_manifest", "refine")
    discover = _read_upstream_manifest(inputs, "discover_manifest", "refine")
    total_duration = discover.get("duration_s", 0)
    scored_frames = vlm_manifest.get("frames", [])

    REFINE_THRESHOLD = cfg["refine_threshold"]
    REFINE_RADIUS = cfg["refine_radius"]
    REFINE_INTERVAL = cfg["refine_interval"]
    WORTHINESS_THRESHOLD = cfg["worthiness_threshold"]

    # P0 (sixth-review §4): validate provider via shared helper, then use
    # the shared ``_score_vlm_frame`` for every scoring request so VLM and
    # refine share one endpoint, one error semantics, and one parse path.
    vlm_cfg = _validate_vlm_provider(config_data)
    vlm_model = vlm_cfg.get("model", "llava:13b")
    vlm_base_url = vlm_cfg.get("base_url", OLLAMA_BASE)
    vlm_retry_delay = float(vlm_cfg.get("retry_delay_s", 2.0))
    VLM_OPTIONS = {
        "temperature": cfg["vlm_temperature"],
        "top_p": cfg["vlm_top_p"],
        "top_k": cfg["vlm_top_k"],
        "num_think": 0,
    }

    high_ts = {r["timestamp"] for r in scored_frames if r["gif_worthiness"] >= REFINE_THRESHOLD}
    refine_ts = set()

    for ts in high_ts:
        for offset in range(-REFINE_RADIUS, REFINE_RADIUS + REFINE_INTERVAL, REFINE_INTERVAL):
            new_ts = ts + offset
            if 0 <= new_ts <= total_duration - 1 and new_ts not in {r["timestamp"] for r in scored_frames}:
                refine_ts.add(new_ts)

    existing_ts = {r["timestamp"] for r in scored_frames}
    refine_ts -= existing_ts

    print(f"  High-score regions: {len(high_ts)}, new frames to sample: {len(refine_ts)}")

    # Task 3 Step 1: initialize ALL counters before any conditional branch
    # so an empty refine_ts path still produces a valid manifest (no
    # UnboundLocalError on the counter variables).
    refine_requested = len(refine_ts)
    refine_extracted = 0
    refine_extraction_failed = 0
    refine_attempted = 0
    refine_responded = 0
    refine_parsed = 0
    refine_failed = 0

    refine_frames = []
    if refine_ts:
        for ts in sorted(refine_ts):
            out_path = os.path.join(frames_dir, f"ts_{ts:06d}.jpg")
            # Task 3 Step 2: check ffmpeg extraction result explicitly.
            completed = subprocess.run(
                ["ffmpeg", "-y", "-ss", str(ts), "-i", video_path,
                 "-vf", "scale=640:-1", "-vframes", "1", out_path],
                capture_output=True, timeout=15,
            )
            if completed.returncode != 0:
                refine_extraction_failed += 1
                print(f"  refine extract FAILED ts={ts}: "
                      f"ffmpeg exit={completed.returncode}")
                continue
            if not os.path.exists(out_path) or os.path.getsize(out_path) <= 500:
                refine_extraction_failed += 1
                print(f"  refine extract FAILED ts={ts}: "
                      f"missing or too-small output")
                continue
            try:
                img = Image.open(out_path).convert("L")
                brightness = sum(img.getdata()) / max(1, img.width * img.height)
                img.close()
            except Exception as exc:
                refine_extraction_failed += 1
                print(f"  refine extract FAILED ts={ts}: decode {exc}")
                continue
            if brightness <= 25:
                refine_extraction_failed += 1
                print(f"  refine extract FAILED ts={ts}: "
                      f"brightness={brightness:.1f} below 25")
                continue
            refine_frames.append({"path": out_path, "timestamp": ts})
            refine_extracted += 1

        print(f"  Refinement frames after filter: {len(refine_frames)}")

        # Task 3 Step 3: complete extraction failure is a hard error,
        # NOT a silent zero-attempt success.
        if refine_requested > 0 and refine_extracted == 0:
            raise RuntimeError(
                f"Refine extraction failed: requested={refine_requested}, "
                f"extraction_failed={refine_extraction_failed}"
            )

        for fi, rf in enumerate(refine_frames):
            with open(rf["path"], "rb") as f:
                img_data = f.read()
            refine_attempted += 1
            payload, error = _score_vlm_frame(
                base_url=vlm_base_url, model=vlm_model,
                image_bytes=img_data, prompt=SCORE_PROMPT,
                options=VLM_OPTIONS, threshold=WORTHINESS_THRESHOLD,
                timestamp=rf["timestamp"], frame_path=rf["path"],
                retry_delay_s=vlm_retry_delay,
            )
            if payload is not None:
                refine_responded += 1
                refine_parsed += 1
                worth = payload.get("gif_worthiness", 0.0)
                if worth >= WORTHINESS_THRESHOLD:
                    scored_frames.append(payload)
                    print(f"  refine[{fi + 1}] score={worth:.2f} KEPT")
                else:
                    print(f"  refine[{fi + 1}] score={worth:.2f} below threshold")
            else:
                refine_failed += 1
                print(f"  refine[{fi + 1}] FAILED: {error}")

            if (fi + 1) % 50 == 0:
                print(f"  refine [{fi + 1}/{len(refine_frames)}] done, scored={len(scored_frames)}")

        # Task 2 Step 3: all-score-failed refine is a hard error too
        # (consistent with _stage_vlm).  Partial failure keeps going.
        if refine_attempted > 0 and refine_parsed == 0:
            raise RuntimeError(
                f"Refine VLM stage failed: all {refine_attempted} refine "
                f"frames failed to parse (0 parsed, {refine_failed} failed)."
            )

    print(f"  After refinement: {len(scored_frames)} total scored frames")

    manifest = {
        "schema_version": 1,
        "stage": "refine",
        "scored_count": len(scored_frames),
        "refine_regions": len(high_ts),
        "refine_requested": refine_requested,
        "refine_extracted": refine_extracted,
        "refine_extraction_failed": refine_extraction_failed,
        "refine_attempted": refine_attempted,
        "refine_responded": refine_responded,
        "refine_parsed": refine_parsed,
        "refine_failed": refine_failed,
        "frames": scored_frames,
        "output_key": "refine",
    }
    manifest_path = _save_manifest(work_dir, "refine", manifest)

    return {
        "output_key": "refine",
        "scored_count": len(scored_frames),
        "refine_regions": len(high_ts),
        "_artifacts": [_make_artifact(manifest_path, "refine_manifest")],
    }


# ---------------------------------------------------------------------------
# Stage 5: synthesize — RAG/LLM synthesis + clip merging
# ---------------------------------------------------------------------------


def _stage_synthesize(work_dir: str, cfg: dict, inputs: dict) -> dict:
    """Read refine manifest, merge into clips, synthesize tags (LLM non-fatal)."""
    refine_manifest = _read_upstream_manifest(inputs, "refine_manifest", "synthesize")
    scored_frames = refine_manifest.get("frames", [])

    MERGE_GAP = cfg["merge_gap"]
    MERGE_SCORE_THRESHOLD = cfg["merge_score_threshold"]

    # Build clip objects from scored frames
    clips_data = []
    for sf in scored_frames:
        clips_data.append({
            "timestamp": sf["timestamp"],
            "path": sf["path"],
            "gif_worthiness": sf["gif_worthiness"],
            "emotional_core": sf.get("emotional_core", "?"),
            "caption": sf.get("caption", ""),
        })

    # Sort by timestamp
    clips_data.sort(key=lambda x: x["timestamp"])

    if not clips_data:
        manifest = {
            "schema_version": 1,
            "stage": "synthesize",
            "clip_count": 0,
            "clips": [],
            "output_key": "synthesize",
        }
        manifest_path = _save_manifest(work_dir, "synthesize", manifest)
        return {
            "output_key": "synthesize",
            "clip_count": 0,
            "_artifacts": [_make_artifact(manifest_path, "synthesize_manifest")],
        }

    # Merge adjacent frames into clips
    clips = []
    current_group = [clips_data[0]]

    for r in clips_data[1:]:
        gap = r["timestamp"] - current_group[-1]["timestamp"]
        both_good = (
            r["gif_worthiness"] >= MERGE_SCORE_THRESHOLD
            and current_group[-1]["gif_worthiness"] >= MERGE_SCORE_THRESHOLD
        )
        if gap <= MERGE_GAP and both_good:
            current_group.append(r)
        else:
            best = max(current_group, key=lambda x: x["gif_worthiness"])
            clips.append({
                "start_ts": current_group[0]["timestamp"],
                "end_ts": current_group[-1]["timestamp"],
                "best_frame_ts": best["timestamp"],
                "best_frame_path": best["path"],
                "frame_count": len(current_group),
                "gif_worthiness": best["gif_worthiness"],
                "emotional_core": best.get("emotional_core", "?"),
                "caption": best.get("caption", ""),
            })
            current_group = [r]

    if current_group:
        best = max(current_group, key=lambda x: x["gif_worthiness"])
        clips.append({
            "start_ts": current_group[0]["timestamp"],
            "end_ts": current_group[-1]["timestamp"],
            "best_frame_ts": best["timestamp"],
            "best_frame_path": best["path"],
            "frame_count": len(current_group),
            "gif_worthiness": best["gif_worthiness"],
            "emotional_core": best.get("emotional_core", "?"),
            "caption": best.get("caption", ""),
        })

    print(f"  Merged into {len(clips)} clips (merge_gap={MERGE_GAP}s)")

    # LLM synthesis (non-fatal)
    for clip in clips:
        clip["summary"] = ""
        clip["tags"] = []
    try:
        _synthesize_clips_with_llm(clips, cfg)
    except Exception as e:
        print(f"  LLM synthesis failed (non-fatal): {e}")

    manifest = {
        "schema_version": 1,
        "stage": "synthesize",
        "clip_count": len(clips),
        "clips": clips,
        "output_key": "synthesize",
    }
    manifest_path = _save_manifest(work_dir, "synthesize", manifest)

    return {
        "output_key": "synthesize",
        "clip_count": len(clips),
        "_artifacts": [_make_artifact(manifest_path, "synthesize_manifest")],
    }


def _synthesize_clips_with_llm(clips: list[dict], cfg: dict) -> None:
    """Attempt LLM synthesis for each clip. Non-fatal on failure."""
    try:
        from app.services.llm_client import generate_llm_text
        from app.services.json_guard import parse_json_response

        for clip in clips:
            try:
                caption = clip.get("caption", "")
                emotional = clip.get("emotional_core", "")
                prompt = (
                    f"Analyze this film clip and provide a concise summary and 2-4 descriptive tags. "
                    f"Caption: {caption}. Emotional tone: {emotional}. "
                    f'Output JSON: {{"summary":"...", "tags":["tag1","tag2"]}}'
                )
                result = generate_llm_text(prompt)
                parsed = parse_json_response(result)
                if parsed.ok and isinstance(parsed.data, dict):
                    clip["summary"] = parsed.data.get("summary", "")
                    clip["tags"] = parsed.data.get("tags", [])
            except Exception:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Stage 6: rank_dedup — embedding dedup, temporal dedup, ranking, clip_ids
# ---------------------------------------------------------------------------


def _stage_rank_dedup(export_dir: str, work_dir: str, cfg: dict, inputs: dict) -> dict:
    """Read synthesize manifest, apply dedup, assign stable clip_ids."""
    synth_manifest = _read_upstream_manifest(inputs, "synthesize_manifest", "rank_dedup")
    clips = synth_manifest.get("clips", [])

    EMBED_SIM_THRESHOLD = cfg["embed_sim_threshold"]
    EMBED_DEDUP_ENABLED = cfg["embed_dedup_enabled"]
    TEMPORAL_DEDUP_ENABLED = cfg["temporal_dedup_enabled"]
    TEMPORAL_DEDUP_MIN_GAP_S = cfg["temporal_dedup_min_gap_s"]
    OUTPUT_RATIO = cfg["output_ratio"]
    MAX_OUTPUT = cfg["max_output"]

    if not clips:
        manifest = {
            "schema_version": 1,
            "stage": "rank_dedup",
            "clip_count": 0,
            "clips": [],
            "output_key": "rank_dedup",
        }
        manifest_path = _save_manifest(work_dir, "rank_dedup", manifest)
        return {
            "output_key": "rank_dedup",
            "clip_count": 0,
            "_artifacts": [_make_artifact(manifest_path, "rank_dedup_manifest")],
        }

    import numpy as np
    import hashlib

    # Embedding dedup
    deduped_clips = list(clips)
    if EMBED_DEDUP_ENABLED and len(clips) > 1:
        clip_embeddings = []
        for clip in clips:
            text = " ".join(filter(None, [
                clip.get("caption", ""),
                clip.get("emotional_core", ""),
            ]))
            if not text:
                clip_embeddings.append(None)
                continue
            try:
                emb = compute_text_embedding(text)
                clip_embeddings.append(np.array(emb, dtype=np.float32))
            except Exception:
                clip_embeddings.append(None)

        order = sorted(
            range(len(clips)),
            key=lambda i: clips[i]["gif_worthiness"],
            reverse=True,
        )
        kept_indices = []
        kept_embs = []

        for idx in order:
            emb = clip_embeddings[idx]
            if emb is None:
                kept_indices.append(idx)
                kept_embs.append(None)
                continue
            is_dup = False
            for ki, ke_emb in zip(kept_indices, kept_embs):
                if ke_emb is None:
                    continue
                norm = np.linalg.norm(emb) * np.linalg.norm(ke_emb) + 1e-8
                sim = float(np.dot(emb, ke_emb) / norm)
                if sim >= EMBED_SIM_THRESHOLD:
                    is_dup = True
                    break
            if not is_dup:
                kept_indices.append(idx)
                kept_embs.append(emb)

        deduped_clips = [clips[i] for i in kept_indices]
        print(f"  Embedding dedup: {len(clips)} -> {len(deduped_clips)} clips")

    # Temporal dedup
    if TEMPORAL_DEDUP_ENABLED and len(deduped_clips) > 1:
        deduped_clips = temporal_dedup_clips(deduped_clips, min_gap_s=TEMPORAL_DEDUP_MIN_GAP_S)
        print(f"  Temporal dedup: {len(deduped_clips)} clips remain")

    # Limit output
    output_count = max(1, int(len(deduped_clips) * OUTPUT_RATIO))
    output_count = min(output_count, MAX_OUTPUT)
    deduped_clips = sorted(deduped_clips, key=lambda c: c["gif_worthiness"], reverse=True)
    deduped_clips = deduped_clips[:output_count]

    # Assign stable clip_ids based on video name + start_ts + end_ts
    video_name = synth_manifest.get("video_name", "unknown")
    for i, clip in enumerate(deduped_clips):
        raw = f"{video_name}:{clip['start_ts']}:{clip['end_ts']}:{i}"
        clip["clip_id"] = hashlib.sha256(raw.encode()).hexdigest()[:16]
        clip["rank"] = i + 1

    print(f"  Final: {len(deduped_clips)} deduped clips")

    manifest = {
        "schema_version": 1,
        "stage": "rank_dedup",
        "clip_count": len(deduped_clips),
        "clips": deduped_clips,
        "output_key": "rank_dedup",
    }
    manifest_path = _save_manifest(work_dir, "rank_dedup", manifest)

    return {
        "output_key": "rank_dedup",
        "clip_count": len(deduped_clips),
        "_artifacts": [_make_artifact(manifest_path, "rank_dedup_manifest")],
    }


# ---------------------------------------------------------------------------
# Stage 7: gif_clip — export a single GIF for one clip
# ---------------------------------------------------------------------------


def _stage_gif_clip(
    video_path: str,
    frames_dir: str,
    export_dir: str,
    work_dir: str,
    cfg: dict,
    clip_id: str | None = None,
    inputs: dict | None = None,
) -> dict:
    """Read rank_dedup manifest, export exactly ONE GIF for *clip_id*.

    Each gif_clip stage runs independently for a single clip.  Fails if
    *clip_id* is not found in the rank_dedup manifest.
    """
    rank_manifest = _read_upstream_manifest(inputs or {}, "rank_dedup_manifest", "gif_clip")
    clips = rank_manifest.get("clips", [])

    GIF_FPS = cfg["gif_fps"]
    GIF_MAX_WIDTH = cfg["gif_max_width"]
    MIN_DURATION = cfg["min_duration"]

    target_clip = None
    for c in clips:
        if c.get("clip_id") == clip_id:
            target_clip = c
            break

    if target_clip is None:
        raise ValueError(f"clip_id {clip_id} not found in rank_dedup manifest")

    start_ts = target_clip["start_ts"]
    end_ts = target_clip["end_ts"]
    duration = end_ts - start_ts
    if duration < MIN_DURATION:
        end_ts = start_ts + max(MIN_DURATION, 0.5)

    video_name = os.path.splitext(os.path.basename(video_path))[0]
    gif_name = build_gif_filename(video_name, target_clip.get("rank", 1), start_ts, end_ts)
    gif_path = os.path.join(export_dir, gif_name)

    print(f"  Exporting clip {clip_id}: {start_ts:.2f}s - {end_ts:.2f}s -> {gif_name}")

    palette_path = os.path.join(frames_dir, f"palette_{clip_id}.png")
    attempt = run_gif_export_attempt(
        palette_command=[
            "ffmpeg", "-y", "-ss", str(start_ts), "-t", str(end_ts - start_ts),
            "-i", video_path,
            "-vf", f"fps={GIF_FPS},scale={GIF_MAX_WIDTH}:-1:flags=lanczos,palettegen",
            palette_path,
        ],
        gif_command=[
                "ffmpeg", "-y", "-ss", str(start_ts), "-t", str(end_ts - start_ts),
                "-i", video_path, "-i", palette_path,
                "-lavfi", f"fps={GIF_FPS},scale={GIF_MAX_WIDTH}:-1:flags=lanczos[x];[x][1:v]paletteuse",
                gif_path,
            ],
        palette_path=palette_path,
        output_path=gif_path,
    )
    print(
        format_gif_export_line(
            video_name=video_name,
            index=int(target_clip.get("rank", 1)),
            total=len(clips),
            output_path=gif_path,
            status="OK" if attempt.success else "FAILED",
            worthiness=float(target_clip.get("gif_worthiness", 0.0)),
            duration_s=float(end_ts - start_ts),
            timestamp_s=float(start_ts),
            merged=int(target_clip.get("frame_count", 1)) > 1,
            frame_count=int(target_clip.get("frame_count", 1)),
            size_bytes=attempt.size_bytes,
            emotional_core=(target_clip.get("best_frame") or {}).get("emotional_core", "?"),
            error=attempt.error,
        ),
        flush=True,
    )

    if not attempt.success:
        raise RuntimeError(
            f"GIF export failed for clip {clip_id}: {gif_path}: {attempt.error}"
        )

    gif_sha256 = hashlib.sha256()
    with open(gif_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            gif_sha256.update(chunk)

    manifest = {
        "schema_version": 1,
        "stage": "gif_clip",
        "clip_id": clip_id,
        "gif_path": os.path.abspath(gif_path),
        "gif_name": gif_name,
        "sha256": gif_sha256.hexdigest(),
        "start_ts": start_ts,
        "end_ts": end_ts,
        "output_key": f"gif_clip:{clip_id}",
    }
    manifest_path = _save_manifest(work_dir, f"gif_clip_{clip_id}", manifest)

    return {
        "output_key": f"gif_clip:{clip_id}",
        "gif_path": os.path.abspath(gif_path),
        "sha256": gif_sha256.hexdigest(),
        "clip_id": clip_id,
        "_artifacts": [
            _make_artifact(gif_path, "gif_file", clip_id=clip_id),
            _make_artifact(manifest_path, "gif_clip_manifest", clip_id=clip_id),
        ],
    }


# ---------------------------------------------------------------------------
# Stage 8: materialize — aggregate successful GIFs, write PBF, result JSON
# ---------------------------------------------------------------------------


def _stage_materialize(
    video_path: str,
    export_dir: str,
    work_dir: str,
    cfg: dict,
    inputs: dict | None = None,
    config_data: dict | None = None,
) -> dict:
    """Aggregate successful GIFs, publish to formal export dir, write PBF and result JSON.

    P0-3 enhancements:
      - Checks destination before publishing (same SHA=idempotent, different
        SHA=conflict handling, non-existent=normal publish).
      - Temp files use unique per-stage names (not shared across jobs).
      - Temp files on same volume for atomic os.replace().
      - On failure, cleans up temp files but does NOT delete historical files.
      - result JSON, PBF, and materialize manifest generated AFTER all GIFs published.
      - Only references successfully published GIFs in result JSON/PBF.

    P0-2 enhancement: reads from versioned input envelope:
      {
        "schema_version": 1,
        "stage": "materialize",
        "artifacts": {"gif_file": [...], "gif_clip_manifest": [...]},
        "stage_statuses": [...]
      }
    """
    import shutil
    import uuid

    video_name = os.path.splitext(os.path.basename(video_path))[0]
    inputs = inputs or {}

    # P0-2: Read from versioned envelope if present.
    if "artifacts" in inputs:
        # P1-2: defend against an unknown envelope version.
        from app.task_engine.artifacts import validate_materialize_envelope
        validate_materialize_envelope(inputs)
        gif_entries = inputs["artifacts"].get("gif_file", [])
        gif_manifest_entries = inputs["artifacts"].get("gif_clip_manifest", [])
        terminal_statuses: list[dict] = inputs.get("stage_statuses", [])
    else:
        # Legacy flat format.
        gif_entries = inputs.get("gif_file", [])
        gif_manifest_entries = inputs.get("gif_clip_manifest", [])
        config_data = config_data or {}
        terminal_statuses = config_data.get("_gif_clip_terminal_statuses", [])

    # Build a lookup of clip_id -> gif_clip manifest data.
    clip_meta: dict[str, dict] = {}
    for entry in gif_manifest_entries:
        path = entry.get("path", "")
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    gm = json.load(f)
                cid = gm.get("clip_id", entry.get("clip_id", ""))
                if cid:
                    clip_meta[cid] = gm
            except (json.JSONDecodeError, OSError):
                pass

    # Validate each gif_file entry.
    successful_gifs = []
    failed_gifs = []
    for entry in gif_entries:
        gif_path = entry.get("path", "")
        cid = entry.get("clip_id", "")
        expected_sha = entry.get("sha256", "")
        expected_size = entry.get("size_bytes", 0)

        if not gif_path or not os.path.exists(gif_path):
            failed_gifs.append({"clip_id": cid, "reason": "file_missing", "path": gif_path})
            continue

        actual_size = os.path.getsize(gif_path)
        if expected_size and actual_size != expected_size:
            failed_gifs.append({"clip_id": cid, "reason": "size_mismatch",
                               "expected": expected_size, "actual": actual_size})
            continue

        if expected_sha:
            actual_sha = hashlib.sha256()
            with open(gif_path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    actual_sha.update(chunk)
            if actual_sha.hexdigest() != expected_sha:
                failed_gifs.append({"clip_id": cid, "reason": "sha256_mismatch"})
                continue

        meta = clip_meta.get(cid, {})
        successful_gifs.append({
            "path": gif_path,
            "clip_id": cid,
            "sha256": expected_sha or hashlib.sha256(open(gif_path, "rb").read()).hexdigest(),
            "gif_name": meta.get("gif_name", os.path.basename(gif_path)),
            "start_ts": meta.get("start_ts", 0),
            "end_ts": meta.get("end_ts", 0),
        })

    # ---- P0-3: Publish to formal export directory with overwrite protection ----
    export_base = (config_data or {}).get("export_base_dir") or "data/exports/adaptive_test"
    formal_export_dir = os.path.join(export_base, video_name)
    os.makedirs(formal_export_dir, exist_ok=True)

    # P0-3: Find the volume of the formal export directory for temp files.
    formal_volume = os.path.splitdrive(os.path.abspath(formal_export_dir))[0] or "/"

    succeeded_formal = []
    materialize_failures = []
    temp_files_created: list[str] = []  # for cleanup on failure

    def _sha_of(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def _safe_remove(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    # P0-2: per-stage temp-file tag (work_dir embeds the stage_id).
    stage_id_tag = hashlib.sha256(work_dir.encode()).hexdigest()[:12]

    def _publish_to(target_path: str, target_name: str, new_sha: str) -> str:
        """Publish ``src`` to ``target_path``.

        Returns ``"published"`` | ``"idempotent"`` | ``"conflict"`` |
        ``"failed"``.  ``"conflict"`` means the target exists with a
        different SHA and the caller retries with the stable conflict name.
        On ``published``/``"idempotent"`` the GIF is appended to
        ``succeeded_formal`` with its (possibly conflict) ``gif_name``.
        """
        if os.path.exists(target_path):
            try:
                existing_sha = _sha_of(target_path)
            except OSError as exc:
                materialize_failures.append({
                    "clip_id": gm.get("clip_id"),
                    "reason": f"cannot_read_existing: {exc}",
                })
                return "failed"
            if existing_sha == new_sha:
                print(f"  [idempotent] {target_name}: same SHA-256, reusing existing file")
                succeeded_formal.append({
                    **gm, "gif_name": target_name,
                    "formal_path": os.path.abspath(target_path),
                })
                return "idempotent"
            return "conflict"
        # Target absent -> copy to unique same-volume temp, verify, atomic rename.
        tmp_name = f".{target_name}.{stage_id_tag}.{uuid.uuid4().hex[:8]}.tmp"
        tmp_path = os.path.join(formal_export_dir, tmp_name)
        temp_files_created.append(tmp_path)
        try:
            shutil.copy2(src, tmp_path)
        except OSError as exc:
            materialize_failures.append({
                "clip_id": gm.get("clip_id"),
                "reason": f"copy_failed: {exc}",
            })
            return "failed"
        try:
            actual_sha = _sha_of(tmp_path)
        except OSError as exc:
            _safe_remove(tmp_path)
            materialize_failures.append({
                "clip_id": gm.get("clip_id"),
                "reason": f"read_failed: {exc}",
            })
            return "failed"
        if actual_sha != new_sha:
            _safe_remove(tmp_path)
            materialize_failures.append({
                "clip_id": gm.get("clip_id"),
                "reason": "materialize_sha256_mismatch",
            })
            return "failed"
        try:
            os.replace(tmp_path, target_path)
        except OSError as exc:
            _safe_remove(tmp_path)
            materialize_failures.append({
                "clip_id": gm.get("clip_id"),
                "reason": f"atomic_rename_failed: {exc}",
            })
            return "failed"
        succeeded_formal.append({
            **gm, "gif_name": target_name,
            "formal_path": os.path.abspath(target_path),
        })
        return "published"

    # P0-2: stable conflict naming (fourth-review §5.2 rules 1-5):
    #   1. target absent            -> publish to original name
    #   2. same name + same SHA     -> idempotent reuse original
    #   3. same name + diff SHA     -> publish to stable conflict name
    #      {base}.{clip_id-8}.{new-sha-12}{ext}
    #   4. conflict name + same SHA -> idempotent reuse conflict name
    #   5. conflict name + diff SHA -> unrecoverable -> needs_attention
    for gm in successful_gifs:
        src = gm["path"]
        gif_name = gm.get("gif_name", os.path.basename(src))
        clip_id_short = gm.get("clip_id", "")[:8] or "unknown"
        # The NEW content SHA (from the gif_file artifact). Compute from
        # src if the manifest did not carry one; record it for the result JSON.
        new_sha = gm.get("sha256", "") or _sha_of(src)
        gm["sha256"] = new_sha

        formal_path = os.path.join(formal_export_dir, gif_name)
        status = _publish_to(formal_path, gif_name, new_sha)
        if status == "conflict":
            base_name, ext = os.path.splitext(gif_name)
            conflict_name = f"{base_name}.{clip_id_short}.{new_sha[:12]}{ext}"
            formal_path_conflict = os.path.join(formal_export_dir, conflict_name)
            cstatus = _publish_to(formal_path_conflict, conflict_name, new_sha)
            if cstatus == "conflict":
                materialize_failures.append({
                    "clip_id": gm.get("clip_id"),
                    "reason": (
                        f"unrecoverable_conflict: both {gif_name} and stable "
                        f"conflict name {conflict_name} already exist with "
                        f"different SHA-256"
                    ),
                    "existing_sha256": _sha_of(formal_path_conflict),
                    "suggested_path": formal_path_conflict,
                })

    print(f"  Materializing {len(succeeded_formal)} succeeded (formal), "
          f"{len(failed_gifs)} verification-failed, "
          f"{len(materialize_failures)} publish-failed GIFs")

    # ---- Write PBF (references formal-export GIFs only) ------------
    if cfg.get("potplayer_pbf_enabled", True) and succeeded_formal:
        bookmarks = []
        for i, gm in enumerate(succeeded_formal):
            bookmarks.append(PotPlayerBookmark(
                start_s=float(gm.get("start_ts", 0)),
                end_s=float(gm.get("end_ts", 0)),
                rank=i + 1,
                score=1.0,
                merged=False,
                caption=f"#{gm.get('clip_id', '')[:8]}",
            ))
        pbf_path = os.path.join(formal_export_dir, f"{video_name}.pbf")
        write_pbf_file(str(pbf_path), bookmarks)
        print(f"  PotPlayer bookmarks: {pbf_path}")

    # ---- Write comprehensive result JSON ---------------------------
    # Build cancelled/failed lists from terminal statuses.
    cancelled_clips = [
        s for s in terminal_statuses
        if s.get("status") == "cancelled"
    ]
    attention_clips = [
        s for s in terminal_statuses
        if s.get("status") in ("failed", "needs_attention")
    ]
    # Combine verification failures with publish failures.
    all_failed = failed_gifs + materialize_failures
    # Add attention clips that may not be in all_failed already.
    attention_cids = {s.get("clip_id") for s in attention_clips}
    for cid in attention_cids:
        if not any(f.get("clip_id") == cid for f in all_failed):
            all_failed.append({"clip_id": cid, "reason": "stage_terminal"})

    # P0-3: Only reference successfully published GIFs in result JSON.
    result_json = {
        "video_name": video_name,
        "video_path": os.path.abspath(video_path),
        "formal_export_dir": os.path.abspath(formal_export_dir),
        "gif_count": len(succeeded_formal),
        "succeeded": [
            {
                "clip_id": gm.get("clip_id"),
                "formal_path": gm.get("formal_path"),
                "sha256": gm.get("sha256"),
                "start_ts": gm.get("start_ts"),
                "end_ts": gm.get("end_ts"),
                "gif_name": gm.get("gif_name"),
            }
            for gm in succeeded_formal
        ],
        "failed": all_failed,
        "cancelled": cancelled_clips,
        "gif_clip_terminal_statuses": terminal_statuses,
    }
    result_json_path = os.path.join(formal_export_dir, f"{video_name}_result.json")
    tmp_result = result_json_path + ".tmp"
    with open(tmp_result, "w", encoding="utf-8") as f:
        json.dump(result_json, f, ensure_ascii=False, indent=2)
    os.replace(tmp_result, result_json_path)

    # ---- P0-3: Clean up any remaining temp files ------------------
    for tmp_file in temp_files_created:
        try:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)
        except OSError:
            pass

    # ---- Write materialize manifest (in work_dir) ------------------
    manifest = {
        "schema_version": 1,
        "stage": "materialize",
        "gif_count": len(succeeded_formal),
        "failed_count": len(all_failed),
        "cancelled_count": len(cancelled_clips),
        "formal_export_dir": os.path.abspath(formal_export_dir),
        "output_key": "materialize",
    }
    manifest_path = _save_manifest(work_dir, "materialize", manifest)

    # ---- Build artifact list --------------------------------------
    artifacts = [
        _make_artifact(result_json_path, "result"),
        _make_artifact(manifest_path, "materialize_manifest"),
    ]
    # PBF is optional
    pbf_path_local = os.path.join(formal_export_dir, f"{video_name}.pbf")
    if os.path.exists(pbf_path_local):
        artifacts.append(_make_artifact(pbf_path_local, "pbf_file"))

    # P0-2: materialize enters needs_attention when a SUCCEEDED clip could
    # not be published (verification failure or unrecoverable conflict).
    # Upstream gif_clip failures (attention_clips) do NOT make materialize
    # needs_attention - those are reflected by the gif_clip stages and video
    # aggregation.  Published GIFs stay published (partial success).
    publish_failed = bool(failed_gifs) or bool(materialize_failures)

    return {
        "output_key": "materialize",
        "outcome": "needs_attention" if publish_failed else "succeeded",
        "gif_count": len(succeeded_formal),
        "failed_count": len(all_failed),
        "cancelled_count": len(cancelled_clips),
        "formal_export_dir": os.path.abspath(formal_export_dir),
        "_artifacts": artifacts,
    }


# ---- CLI entry point ----------------------------------------------


def parse_cli_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Adaptive GIF extraction from video")
    parser.add_argument("--video", default=None, help="Video file path")
    parser.add_argument(
        "--export-dir", default=None, help="Export directory for GIFs"
    )
    # Stage-mode arguments
    parser.add_argument(
        "--task-stage",
        default=None,
        choices=[
            "discover",
            "sample",
            "vlm",
            "refine",
            "synthesize",
            "rank_dedup",
            "gif_clip",
            "materialize",
        ],
        help="Run in stage mode for the given stage",
    )
    parser.add_argument(
        "--task-work-dir", default=None, help="Working directory (stage mode)"
    )
    parser.add_argument(
        "--task-result",
        default=None,
        help="Path to write stage result JSON (stage mode)",
    )
    parser.add_argument(
        "--task-config",
        default=None,
        help="Path to config snapshot JSON (stage mode)",
    )
    parser.add_argument(
        "--task-input-manifest",
        default=None,
        help="Path to input manifest JSON describing upstream artifacts (stage mode)",
    )
    parser.add_argument(
        "--clip-id", default=None, help="Clip ID for gif_clip stage"
    )
    return parser.parse_args(args)


def main() -> None:
    args = parse_cli_args()

    if args.task_stage:
        # Stage mode
        if not args.video:
            print("ERROR: --video is required in stage mode", file=sys.stderr)
            sys.exit(1)
        if not args.task_work_dir or not args.task_result or not args.task_config:
            print(
                "ERROR: --task-work-dir, --task-result, and --task-config "
                "are required in stage mode",
                file=sys.stderr,
            )
            sys.exit(1)
        run_stage_mode(
            stage=args.task_stage,
            video_path=args.video,
            work_dir=args.task_work_dir,
            result_path=args.task_result,
            config_path=args.task_config,
            input_manifest_path=args.task_input_manifest,
            clip_id=args.clip_id,
        )
    else:
        # Direct mode (original behavior)
        if not args.video:
            args.video = "C:/Users/sunhao/Desktop/ToWatch/JUR-639.mp4"
        run_direct_mode(args.video, args.export_dir)


if __name__ == "__main__":
    main()

"""VLM scoring: frame requests, keep gate, checkpoints, caption backfill."""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx

from app.pipeline.config import DEFAULT_MAX_REFINE_FRAMES
from app.pipeline.timing import current_timings
from app.pipeline.vlm_runtime import _expand_vlm_base_url
from app.services.export_ranking import normalize_vlm_unit_score, sex_act_score
from app.services.json_guard import parse_json_response
from app.services.quality import validate_frame_analysis
from app.services.score_calibration import apply_calibrated_worthiness, load_calibrator


def parse_vlm_response(raw_text: str) -> dict:
    """Parse VLM response through quality gate, return cleaned dict."""
    result = parse_json_response(raw_text)
    if not result.ok:
        return {"_parse_error": True, "_raw": raw_text[:500]}
    cleaned, errors = validate_frame_analysis(result.data)
    if errors:
        cleaned["_quality_errors"] = errors
    return cleaned


def _video_identity(video_path: str) -> dict[str, object]:
    stat = os.stat(video_path)
    return {
        "path": os.path.abspath(video_path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _scored_checkpoint_path(frames_dir: str) -> str:
    return os.path.join(frames_dir, "scored_checkpoint.json")


def _load_scored_checkpoint(
    frames_dir: str,
    video_path: str,
    *,
    vlm_model: str,
    score_prompt_mode: str,
) -> list[dict] | None:
    path = _scored_checkpoint_path(frames_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("video") != _video_identity(video_path):
        return None
    if data.get("vlm_model") != vlm_model or data.get("score_prompt_mode") != score_prompt_mode:
        return None
    scored = data.get("scored")
    if not isinstance(scored, list) or not scored:
        return None
    return scored


def _save_scored_checkpoint(
    frames_dir: str,
    video_path: str,
    scored: list[dict],
    *,
    vlm_model: str,
    score_prompt_mode: str,
) -> None:
    payload = {
        "video": _video_identity(video_path),
        "vlm_model": vlm_model,
        "score_prompt_mode": score_prompt_mode,
        "scored": [
            {
                "timestamp": item.get("timestamp"),
                "gif_worthiness": item.get("gif_worthiness"),
                "sex_act": item.get("sex_act"),
                "path": item.get("path"),
                "caption": item.get("caption"),
                "emotional_core": item.get("emotional_core"),
                "aesthetic_notes": item.get("aesthetic_notes"),
                "reason": item.get("reason"),
            }
            for item in scored
        ],
    }
    path = _scored_checkpoint_path(frames_dir)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(tmp_path, path)


def _response_eval_count(response_json: object) -> int:
    """Return the Ollama-reported generated token count, or 0 if absent."""
    if not isinstance(response_json, dict):
        return 0
    try:
        count = int(response_json.get("eval_count") or 0)
    except (TypeError, ValueError):
        return 0
    return count if count > 0 else 0


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
    keep_alive: str | None = None,
    schema: str = "full",
    calibrator=None,
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
      boolean, non-numeric, non-finite, or not a unit float / 0-100 integer
      is a FAILURE -- the caller NEVER gets a default 0.5 score
      (seventh-review Task 2 Step 1: removed ``safe_worth(0.5)`` fallback).
      Integers ``0–100`` are divided by 100 before thresholds see them.
    * Quality-gate errors in non-score fields are informational, not fatal.
    * ``keep_alive`` is omitted from the request entirely when ``None``, so
      callers that don't pass it keep the byte-identical legacy body
      (Task 7: lets Ollama hold the VLM resident between stages instead of
      evicting and reloading it on its own default timeout).
    """
    if not str(base_url or "").strip().lower().startswith(("http://", "https://")):
        base_url = _expand_vlm_base_url(
            base_url or "auto",
            launch_mode="wsl",
            wsl_distro="Ubuntu-20.04",
            manage_lifecycle=True,
        )

    img_b64 = base64.b64encode(image_bytes).decode("utf-8")
    last_error: str | None = None
    # Tokens are summed across retries because every attempt really was
    # generated; the latency sample is the wall time the caller waited,
    # retry backoff included.
    generated_tokens = 0
    started_at = time.perf_counter()

    try:
        request_body = {
            "model": model, "prompt": prompt,
            "images": [img_b64], "stream": False,
            "format": "json",
            "think": False,
            "options": options,
        }
        if keep_alive is not None:
            request_body["keep_alive"] = keep_alive

        for attempt in range(3):
            try:
                resp = httpx.post(
                    f"{base_url}/api/generate",
                    json=request_body,
                    timeout=120,
                )
                resp.raise_for_status()
                response_json = resp.json()
                generated_tokens += _response_eval_count(response_json)
                raw = response_json.get("response", "")
                # Read + validate the raw gif_worthiness BEFORE parse_vlm_response
                # (validate_frame_analysis coerces bool->float, drops unconvertible
                # strings to None, and even raises on float("high") - all of which
                # would hide invalid values from the strict check).
                # Use parse_json_response so LLaVA prose/markdown wrappers still
                # yield the JSON object; json.loads(raw) rejects those.
                raw_worthiness: object = None
                parse_error_msg: str | None = None
                parsed_result = parse_json_response(raw if isinstance(raw, str) else "")
                raw_parsed = parsed_result.data
                if isinstance(raw_parsed, list) and raw_parsed and isinstance(raw_parsed[0], dict):
                    raw_parsed = raw_parsed[0]
                if not parsed_result.ok or not isinstance(raw_parsed, dict):
                    parse_error_msg = (
                        f"parse_error: {parsed_result.error or 'response JSON is not an object'}"
                        f"; raw={str(raw)[:120]!r}"
                    )
                else:
                    raw_worthiness = raw_parsed.get("gif_worthiness")

                if parse_error_msg is not None:
                    last_error = (
                        f"{parse_error_msg} after {attempt + 1} attempt(s)"
                    )
                    if attempt < 2:
                        time.sleep(retry_delay_s)
                        continue
                    return None, last_error

                # Strict worthiness validation (seventh-review Task 2 Step 1).
                worth = normalize_vlm_unit_score(raw_worthiness)
                if worth is None:
                    last_error = (
                        f"invalid gif_worthiness: expected finite number in "
                        f"[0, 1] or integer 0-100, got {raw_worthiness!r}"
                    )
                    if attempt < 2:
                        time.sleep(retry_delay_s)
                        continue
                    return None, last_error

                if schema == "score":
                    # Two-tier coarse/refine: no caption to quality-gate.
                    # Worthiness validation and sex_act extraction stay strict.
                    parsed = {
                        "caption": "",
                        "emotional_core": "?",
                        "aesthetic_notes": [],
                        "why_i_like_it": "",
                        "reason": "",
                        "gif_worthiness": float(worth),
                        "sex_act": sex_act_score(raw_parsed),
                        "timestamp": timestamp,
                        "path": frame_path,
                    }
                    if calibrator is not None:
                        apply_calibrated_worthiness(parsed, calibrator)
                    return parsed, None

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
                parsed["sex_act"] = sex_act_score(raw_parsed)
                parsed["timestamp"] = timestamp
                parsed["path"] = frame_path
                if calibrator is not None:
                    apply_calibrated_worthiness(parsed, calibrator)
                return parsed, None

            except Exception as e:
                last_error = str(e)
                if attempt == 2:
                    return None, last_error
                time.sleep(retry_delay_s)

        return None, last_error or "exhausted 3 retries"
    finally:
        current_timings().observe_vlm(
            generated_tokens, (time.perf_counter() - started_at) * 1000.0
        )


def collect_refine_timestamps(
    high_timestamps,
    *,
    radius: int,
    interval: int,
    existing_timestamps,
    duration_s: float,
    max_frames: int = DEFAULT_MAX_REFINE_FRAMES,
) -> list[int]:
    """Build extra refine timestamps around peaks, then cap the set.

    ``max_frames <= 0`` disables the cap.  The cap keeps 35B VLM refine
    from expanding into thousands of extra calls on dense adult scores.
    """
    existing = {int(ts) for ts in existing_timestamps}
    last = max(0, int(duration_s) - 1)
    step = max(1, int(interval))
    rad = max(0, int(radius))
    refine_ts: set[int] = set()
    for ts in high_timestamps:
        center = int(ts)
        for offset in range(-rad, rad + step, step):
            new_ts = center + offset
            if 0 <= new_ts <= last and new_ts not in existing:
                refine_ts.add(new_ts)
    ordered = sorted(refine_ts)
    cap = int(max_frames)
    if cap <= 0 or len(ordered) <= cap:
        return ordered
    n = len(ordered)
    if cap == 1:
        return [ordered[n // 2]]
    return [ordered[(i * (n - 1)) // (cap - 1)] for i in range(cap)]


def frame_passes_keep_gate(
    frame: dict,
    *,
    worthiness_threshold: float,
    sex_act_threshold: float = 0.0,
) -> bool:
    """Return True when a scored frame may enter merge / refine / export.

    ``sex_act_threshold <= 0`` disables the sex-act floor so cinematic
    snapshots and tests without ``sex_act`` keep historical behavior.
    """
    if float(frame.get("gif_worthiness") or 0.0) < float(worthiness_threshold):
        return False
    if float(sex_act_threshold) <= 0.0:
        return True
    return sex_act_score(frame) >= float(sex_act_threshold)


def _resolve_score_calibrator(cfg: dict, model_id: str):
    """Load the frozen calibrator when the snapshot asks for it."""
    if not cfg.get("score_calibration_enabled"):
        return None
    path = str(cfg.get("score_calibration_path") or "").strip()
    if not path:
        return None
    return load_calibrator(
        path,
        model_id=str(model_id or ""),
        prompt_mode=str(cfg.get("score_prompt_mode") or "default"),
    )


_BACKFILL_FIELDS = ("caption", "emotional_core", "aesthetic_notes", "reason")


def backfill_clip_captions(
    clips: list[dict],
    *,
    score_frame,
    max_frames: int = 150,
    counters: dict | None = None,
) -> list[dict]:
    """Re-score each clip's ``best_frame`` with the full caption schema.

    Clips are visited in descending ``gif_worthiness`` order and capped by
    ``max_frames``.  ``score_frame`` is injected so tests can drive this
    without a live VLM.  Failures leave caption empty and never raise.
    """
    stats = counters if counters is not None else {}
    attempted = 0
    succeeded = 0
    failed = 0
    budget = max(0, int(max_frames))
    ranked = sorted(
        clips,
        key=lambda clip: float(clip.get("gif_worthiness") or 0.0),
        reverse=True,
    )
    for clip in ranked:
        best = clip.get("best_frame")
        if not isinstance(best, dict):
            continue
        if attempted >= budget:
            best.setdefault("caption", "")
            continue
        attempted += 1
        payload = None
        try:
            payload = score_frame(best)
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            failed += 1
            best["caption"] = ""
            best.setdefault("emotional_core", "?")
            best.setdefault("aesthetic_notes", [])
            best.setdefault("reason", "")
            continue
        succeeded += 1
        for field in _BACKFILL_FIELDS:
            if field in payload:
                best[field] = payload[field]
        clip["caption"] = best.get("caption", "")
        if payload.get("emotional_core"):
            clip["emotional_core"] = payload["emotional_core"]
    stats["caption_backfill_attempted"] = attempted
    stats["caption_backfill_succeeded"] = succeeded
    stats["caption_backfill_failed"] = failed
    return clips


@dataclass
class _ScoredItem:
    frame: dict
    payload: dict | None
    error: str | None


def _score_frames_concurrent(
    frames: list[dict],
    *,
    score_one,
    workers: int = 1,
    on_progress=None,
    timestamp_key: str = "timestamp",
) -> list[_ScoredItem]:
    """Score *frames* with bounded concurrency.

    ``workers=1`` issues calls in the given order (today's serial
    behavior).  Any ``workers>1`` uses a thread pool; one frame's
    exception becomes an error on that item and does not drop siblings.
    Returned items are always sorted by timestamp so manifests stay
    byte-reproducible.  ``on_progress(completed, total, item)`` fires on
    completion count, not submission order.
    """
    if not frames:
        return []
    worker_count = max(1, min(int(workers), len(frames)))
    completed = 0
    lock = threading.Lock()
    results: list[_ScoredItem | None] = [None] * len(frames)

    def _run(index: int, frame: dict) -> _ScoredItem:
        nonlocal completed
        try:
            payload, error = score_one(frame)
        except Exception as exc:
            payload, error = None, str(exc)
        item = _ScoredItem(frame=frame, payload=payload, error=error)
        with lock:
            completed += 1
            done = completed
            results[index] = item
        if on_progress is not None:
            on_progress(done, len(frames), item)
        return item

    if worker_count == 1:
        for index, frame in enumerate(frames):
            _run(index, frame)
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = [
                pool.submit(_run, index, frame)
                for index, frame in enumerate(frames)
            ]
            for future in futures:
                future.result()

    ordered = [item for item in results if item is not None]
    return sorted(ordered, key=lambda item: float(item.frame.get(timestamp_key) or 0))

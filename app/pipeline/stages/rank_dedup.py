"""Stage 6: rank_dedup -- guard, dedup, quality boundary, ranking."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.pipeline.export_gif import _apply_boundary_snaps, _temporal_media_duration
from app.pipeline.prompts import _scoring_vlm_options, _vlm_options, get_score_prompt
from app.pipeline.quality_bridge import (
    _evaluate_quality_pipeline_candidates,
    _quality_moe_summary,
)
from app.pipeline.ranking import (
    _assign_candidate_identities,
    _compute_clip_embeddings,
    _planned_output_count,
    _rank_pipeline_clips,
)
from app.pipeline.scoring import _resolve_score_calibrator, _score_vlm_frame
from app.pipeline.stage_io import (
    _hash_artifact_id,
    _make_artifact,
    _read_upstream_manifest,
    _save_manifest,
)
from app.pipeline.timing import current_timings
from app.pipeline.vlm_runtime import (
    OLLAMA_BASE,
    _attach_live_vlm_base_url,
    _materialize_vlm_runtime,
    _resolve_vlm_runtime,
    _validate_vlm_provider,
)
from app.services.action_boundary import ActionBoundaryConfig
from app.services.action_config import freeze_action_config
from app.services.action_pipeline import materialize_action_candidates
from app.services.clip_dedup import embedding_dedup_clips, temporal_dedup_clips
from app.services.frame_extract import extract_frames
from app.services.temporal_evidence import TemporalEvidenceCache


# ── Task 3: candidate capture integration (single shared integration point) ─


@dataclass
class RankCaptureContext:
    """Everything the shared rank stage needs to capture candidate evidence.

    ``source_sha256`` is resolved lazily during capture when the config did
    not pin it, so the context itself stays mutable.
    """

    enabled: bool
    mode: str
    run_id: str
    video_path: str
    source_sha256: str
    capture_version: str
    evidence_root: Path
    db_path: str | None
    runtime: object | None
    failure_policy: str


def _rank_capture_context(video_path: str, cfg: dict) -> RankCaptureContext:
    """Build the capture context from the frozen flat pipeline config.

    Disabled by default: when ``candidate_capture_enabled`` is false the
    capture path must not touch the database or the feature runtime at all.
    """
    enabled = bool(cfg.get("candidate_capture_enabled", False))
    mode = str(cfg.get("curve_preference_mode", "off") or "off").lower()
    if mode in ("", "disabled"):
        mode = "off"
    runtime = cfg.get("curve_source_runtime")
    if runtime is None and enabled:
        from app.services.curve_source_runtime import CurveSourceRuntime

        runtime = CurveSourceRuntime(owner=f"rank:{os.path.basename(video_path)}")
    return RankCaptureContext(
        enabled=enabled,
        mode=mode,
        run_id=f"rank-{hashlib.sha256(os.path.abspath(video_path).encode('utf-8')).hexdigest()[:16]}",
        video_path=video_path,
        source_sha256=str(cfg.get("source_video_sha256", "") or ""),
        capture_version=str(cfg.get("candidate_capture_version", "capture-v1")),
        evidence_root=Path(
            cfg.get("candidate_capture_evidence_root", "data/candidate_evidence")
        ),
        db_path=str(cfg.get("candidate_capture_db_path", "") or "") or None,
        runtime=runtime,
        failure_policy=str(cfg.get("candidate_capture_failure_policy", "record_and_retry")),
    )


def _open_capture_connection(context: RankCaptureContext):
    if context.db_path:
        import sqlite3

        conn = sqlite3.connect(context.db_path, timeout=30)
    else:
        from app.db import get_connection

        conn = get_connection()
    conn.row_factory = None
    return conn


def _clip_feature_windows(clips: list[dict]) -> list[tuple[dict, "CurveWindow"]]:
    from app.services.curve_source_features import CurveWindow

    windows: list[tuple[dict, CurveWindow]] = []
    for clip in clips:
        key = clip.get("preexport_key")
        start_us = clip.get("start_us")
        end_us = clip.get("end_us")
        if not key or not isinstance(start_us, int) or not isinstance(end_us, int):
            continue
        windows.append(
            (
                clip,
                CurveWindow(
                    preexport_key=key, start_us=start_us, end_us=end_us
                ),
            )
        )
    return windows


def _extract_features_for_clips(
    context: RankCaptureContext, clips: list[dict]
) -> dict[str, "CurveFeatureResult"]:
    """Extract source curve features for all post-dedup windows in one batch
    request per video; results are keyed strictly by preexport key."""
    from app.services.curve_source_features import CurveFeatureResult
    from app.services.curve_source_runtime import CurveSourceRuntime

    window_pairs = _clip_feature_windows(clips)
    if not window_pairs:
        return {}
    if not isinstance(context.runtime, CurveSourceRuntime):
        # A fake/test runtime only needs the evaluate_video protocol.
        if context.runtime is None or not hasattr(context.runtime, "evaluate_video"):
            return {}
    windows = tuple(window for _clip, window in window_pairs)
    try:
        results = context.runtime.evaluate_video(
            Path(context.video_path),
            context.source_sha256,
            windows,
            float(120.0),
        )
    except Exception:
        return {
            window.preexport_key: CurveFeatureResult(
                preexport_key=window.preexport_key,
                status="UNAVAILABLE",
                feature_version="curve-source-v1",
                extractor_sha256="",
                features=None,
                actual_timestamps=(),
                input_hash="",
                missing_reasons=("inference_runtime_unavailable",),
                wall_clock_ms=0.0,
            )
            for _clip, window in window_pairs
        }
    by_key: dict[str, CurveFeatureResult] = {}
    for result in results:
        by_key[result.preexport_key] = result
    return by_key


def capture_rank_candidates(
    context: RankCaptureContext,
    clips: list[dict],
    runtime_snapshot: dict | None = None,
) -> "CaptureBatchResult":
    """Persist every post-dedup scorable candidate after the Quality MoE gate.

    Called between the quality boundary and output truncation. Failures are
    recorded as versioned failure payloads and returned in
    ``failed_preexport_keys``; they never raise into the ranking path.
    """
    from app.services.candidate_capture import (
        CaptureBatchResult,
        SourceIdentityUnavailable,
        persist_capture,
    )
    from app.services.preference_schema import apply_preference_schema

    if not context.enabled or not clips:
        return CaptureBatchResult(captures=(), failed_preexport_keys=())

    captures: list = []
    failed_keys: list[str] = []
    conn = _open_capture_connection(context)
    try:
        apply_preference_schema(conn)
        evidence_root = Path(context.evidence_root)
        evidence_root.mkdir(parents=True, exist_ok=True)

        if not context.source_sha256:
            from app.services.candidate_capture import compute_source_sha256

            try:
                context.source_sha256 = compute_source_sha256(context.video_path)
            except Exception as exc:
                print(f"  Candidate capture: source identity unavailable ({exc})")
                return CaptureBatchResult(captures=(), failed_preexport_keys=())

        feature_results = _extract_features_for_clips(context, clips)

        for clip in clips:
            key = clip.get("preexport_key")
            if not key:
                continue
            feature = feature_results.get(key)
            payload = _capture_payload_for_clip(
                context, clip, feature, runtime_snapshot
            )
            try:
                persisted = persist_capture(conn, payload, evidence_root)
                captures.append(persisted)
                clip["analysis_record_id"] = persisted.record_id
                clip["capture_status"] = "CAPTURED"
                if feature is not None:
                    clip["source_feature_status"] = feature.status
                    if feature.status == "AVAILABLE":
                        clip["source_features"] = feature.features
            except Exception as exc:
                failed_keys.append(str(key))
                clip["capture_status"] = "FAILED"
                clip["capture_failed_reason"] = str(exc)[:200]
                _persist_capture_failure(conn, context, clip, payload, exc)
        conn.commit()
    finally:
        conn.close()
    return CaptureBatchResult(
        captures=tuple(captures), failed_preexport_keys=tuple(failed_keys)
    )


def _capture_payload_for_clip(
    context: RankCaptureContext,
    clip: dict,
    feature,
    runtime_snapshot: dict | None,
) -> dict:
    """Build the immutable capture payload for one post-dedup candidate."""
    baseline = clip.get("gif_worthiness")
    if baseline is None:
        baseline = clip.get("final_score", 0.0)
    payload = {
        "run_id": context.run_id,
        "preexport_key": clip["preexport_key"],
        "capture_version": context.capture_version,
        "source_video_sha256": context.source_sha256,
        "start_us": clip["start_us"],
        "end_us": clip["end_us"],
        "anchor_us": clip.get("anchor_us", (clip["start_us"] + clip["end_us"]) // 2),
        "anchor_source": clip.get("anchor_source", "window_midpoint"),
        "candidate_id": clip.get("candidate_id"),
        "clip_id": clip.get("clip_id"),
        "baseline_score": float(baseline),
        "capture_status": "CAPTURED",
    }
    if feature is not None:
        payload["source_feature"] = {
            "status": feature.status,
            "feature_version": feature.feature_version,
            "extractor_sha256": feature.extractor_sha256,
            "features": feature.features,
            "actual_timestamps": list(feature.actual_timestamps),
            "input_hash": feature.input_hash,
            "missing_reasons": list(feature.missing_reasons),
            "wall_clock_ms": feature.wall_clock_ms,
        }
        if feature.status != "AVAILABLE":
            payload["capture_status"] = "CAPTURED"
            payload["feature_unavailable_reasons"] = list(feature.missing_reasons)
    if runtime_snapshot:
        payload["runtime_snapshot"] = dict(runtime_snapshot)
    return payload


def _persist_capture_failure(
    conn, context: RankCaptureContext, clip: dict, payload: dict, exc: Exception
) -> None:
    """Record a versioned failure payload; never raises into the pipeline."""
    from app.services.candidate_capture import persist_capture

    try:
        failure_payload = dict(payload)
        failure_payload["capture_status"] = "FAILED"
        failure_payload["failure_reason"] = str(exc)[:500]
        persist_capture(conn, failure_payload, Path(context.evidence_root))
        conn.commit()
    except Exception:
        print(
            f"  Candidate capture: failure payload could not be recorded for "
            f"{clip.get('preexport_key', '?')}"
        )


def _append_rank_selection_events(
    context: RankCaptureContext,
    clips: list[dict],
    selected_ids: set[str],
    quality_rejected_ids: set[str],
) -> None:
    """Append scored/selected/not_selected/quality_rejected analysis events.

    ``not_selected`` is a routing fact, never a dislike; ``quality_rejected``
    is a quality gate fact, never an aesthetic negative sample. Best-effort:
    event failures never break the formal output.
    """
    from app.services.candidate_capture import append_candidate_analysis_event

    if not context.enabled or not clips:
        return
    conn = _open_capture_connection(context)
    try:
        from app.services.preference_schema import apply_preference_schema

        apply_preference_schema(conn)
        evidence_root = Path(context.evidence_root)
        for clip in clips:
            record_id = clip.get("analysis_record_id")
            if not record_id:
                continue
            clip_id = str(clip.get("clip_id") or clip.get("candidate_id") or "")
            scored_payload = {
                "run_id": context.run_id,
                "clip_id": clip_id,
                "preexport_key": clip.get("preexport_key"),
                "route": clip.get("curve_route", "BASELINE"),
                "baseline_score": clip.get("baseline_score"),
                "final_score": clip.get("final_score"),
                "source_feature_status": clip.get("source_feature_status"),
            }
            try:
                append_candidate_analysis_event(
                    conn, record_id, "scored", scored_payload, evidence_root
                )
                if clip_id in quality_rejected_ids:
                    kind = "quality_rejected"
                elif clip_id in selected_ids:
                    kind = "selected"
                else:
                    kind = "not_selected"
                selection_payload = {
                    "run_id": context.run_id,
                    "clip_id": clip_id,
                    "preexport_key": clip.get("preexport_key"),
                    "rank": clip.get("rank"),
                    "kind": kind,
                }
                append_candidate_analysis_event(
                    conn, record_id, kind, selection_payload, evidence_root
                )
            except Exception:
                print(
                    f"  Candidate capture: selection event failed for {clip_id}"
                )
        conn.commit()
    finally:
        conn.close()


def _freeze_stage_action_config(
    cfg: dict,
) -> tuple[dict[str, object], str]:
    """Canonicalize legacy flat stage config before writing manifest v2."""
    repaired = ActionBoundaryConfig.from_mapping(
        {
            **cfg,
            "action_min_duration_s": cfg.get("min_duration", 2.0),
            "action_max_duration_s": cfg.get("max_duration", 20.0),
        },
        strict=False,
    )
    normalized = {
        "min_duration": repaired.min_duration_s,
        "max_duration": repaired.max_duration_s,
        "action_guard_enabled": repaired.enabled,
        "action_vlm_verify_enabled": repaired.vlm_verify_enabled,
        "action_analysis_version": repaired.analysis_version,
        "action_analysis_window_s": repaired.analysis_window_s,
        "action_preferred_min_duration_s": (
            repaired.preferred_min_duration_s
        ),
        "action_preferred_max_duration_s": (
            repaired.preferred_max_duration_s
        ),
        "action_scan_fps": repaired.scan_fps,
        "action_boundary_confidence_threshold": (
            repaired.boundary_confidence_threshold
        ),
        "action_loop_adjust_s": repaired.loop_adjust_s,
        "action_vlm_min_worthiness": repaired.vlm_min_worthiness,
        "action_fallback_mode": repaired.fallback_mode,
    }
    return freeze_action_config(normalized)


_ARTIFACT_LINEAGE_FIELDS = (
    "artifact_id", "stage_id", "artifact_kind", "sha256", "size_bytes",
)


def _rank_source_artifact_lineage(inputs: dict) -> dict:
    entries = inputs.get("synthesize_manifest", [])
    if not entries or not isinstance(entries[0], dict):
        raise ValueError("rank_dedup requires immutable synthesize artifact lineage")
    ref = entries[0]
    lineage = {field: ref.get(field) for field in _ARTIFACT_LINEAGE_FIELDS}
    if (
        lineage["artifact_kind"] != "synthesize_manifest"
        or any(not isinstance(lineage[field], str) or not lineage[field]
               for field in ("artifact_id", "stage_id", "sha256"))
        or not isinstance(lineage["size_bytes"], int)
        or lineage["size_bytes"] < 0
    ):
        raise ValueError("rank_dedup synthesize artifact lineage is incomplete")
    return lineage


def _write_rank_candidate_ledger(
    work_dir: str,
    quality_moe: dict,
    *,
    source_artifact: dict,
    stage_id: str,
) -> tuple[str, dict]:
    ledger = {
        "schema_version": 1,
        "stage": "rank_input",
        "upstream_artifact": source_artifact,
        "assessed_candidates": quality_moe["assessed_candidates"],
        "assessed_candidates_digest": quality_moe[
            "assessed_candidates_digest"
        ],
    }
    ledger_path = os.path.abspath(
        _save_manifest(
            work_dir, "rank_candidate_ledger", ledger, include_timings=False
        )
    )
    with open(ledger_path, "rb") as ledger_file:
        raw = ledger_file.read()
    ledger_ref = {
        "artifact_id": _hash_artifact_id(
            "rank_candidate_ledger", ledger_path, stage_id=stage_id
        ),
        "stage_id": stage_id,
        "artifact_kind": "rank_candidate_ledger",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "upstream_artifact": source_artifact,
    }
    quality_moe["candidate_ledger"] = {"mode": "external", **ledger_ref}
    return ledger_path, ledger_ref


def _stage_rank_dedup(
    video_path: str,
    export_dir: str,
    work_dir: str,
    cfg: dict,
    inputs: dict,
    config_data: dict | None = None,
) -> dict:
    """Guard synthesized windows, then dedup and assign stable clip IDs.

    The transition decision belongs here, before any embedding/temporal
    deduplication or fan-out.  ``gif_clip`` only exports these clean windows.
    """
    synth_manifest = _read_upstream_manifest(inputs, "synthesize_manifest", "rank_dedup")
    source_artifact = _rank_source_artifact_lineage(inputs)
    rank_stage_id = str(
        (config_data or {}).get("_stage_id") or "standalone-rank-stage"
    )
    _attach_live_vlm_base_url(cfg, config_data)
    vlm_model = str(((config_data or {}).get("vlm") or {}).get("model") or "")
    score_calibrator = _resolve_score_calibrator(cfg, vlm_model)
    clips = synth_manifest.get("clips", [])
    scored_frames = synth_manifest.get("scored_frames", [])

    EMBED_SIM_THRESHOLD = cfg["embed_sim_threshold"]
    EMBED_DEDUP_ENABLED = cfg["embed_dedup_enabled"]
    TEMPORAL_DEDUP_ENABLED = cfg["temporal_dedup_enabled"]
    TEMPORAL_DEDUP_MIN_GAP_S = cfg["temporal_dedup_min_gap_s"]
    EMBED_DEDUP_MAX_GAP_S = float(cfg.get("embed_dedup_max_gap_s", 0))
    OUTPUT_RATIO = cfg["output_ratio"]
    MAX_OUTPUT = cfg["max_output"]

    normalized_action, canonical_action_hash = _freeze_stage_action_config(cfg)

    transition_guard = {
        key: 0
        for key in (
            "input",
            "split",
            "trim",
            "drop",
            "unverified",
            "hard_cut",
            "soft_transition",
            "motion",
        )
    }
    action_guard = {
        "action_config_hash": canonical_action_hash,
        "action_analysis_version": int(
            normalized_action["action_analysis_version"]
        ),
        "input": 0,
        "output": 0,
        "cv": 0,
        "extended": 0,
        "trimmed": 0,
        "split": 0,
        "ambient_motion": 0,
        "vlm_checked": 0,
        "vlm_succeeded": 0,
        "vlm_failed": 0,
        "fallback": 0,
        "low_loop_quality": 0,
        "cv_ms": 0.0,
        "vlm_ms": 0.0,
        "total_ms": 0.0,
        "fallback_reasons": {},
    }

    if not clips:
        quality_moe = _quality_moe_summary(
            cfg,
            [],
            input_count=0,
            effective_count=0,
            human_review_count=0,
        )
        ledger_path, _ledger_ref = _write_rank_candidate_ledger(
            work_dir,
            quality_moe,
            source_artifact=source_artifact,
            stage_id=rank_stage_id,
        )
        manifest = {
            "schema_version": 2,
            "stage": "rank_dedup",
            "clip_count": 0,
            "clips": [],
            "transition_guard": transition_guard,
            "action_guard": action_guard,
            "quality_moe": quality_moe,
            "output_key": "rank_dedup",
        }
        manifest_path = _save_manifest(work_dir, "rank_dedup", manifest)
        return {
            "output_key": "rank_dedup",
            "clip_count": 0,
            "_artifacts": [
                _make_artifact(manifest_path, "rank_dedup_manifest"),
                _make_artifact(ledger_path, "rank_candidate_ledger"),
            ],
        }

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", video_path,
        ],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        raise RuntimeError(f"ffprobe failed for rank/dedup: {probe.stderr.strip()}")
    try:
        total_duration = float(probe.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("ffprobe returned no usable video duration for rank/dedup") from exc

    # Use the same transition-first action materializer as direct mode.  One
    # evidence cache is shared by the entire staged call so overlapping
    # candidates reuse the same temporal decode.
    clean_clips: list[dict] = []
    evidence_cache = TemporalEvidenceCache()
    action_materializer_config = {
        **cfg,
        **normalized_action,
        "action_min_duration_s": normalized_action["min_duration"],
        "action_max_duration_s": normalized_action["max_duration"],
    }
    vlm_cfg: dict | None = None
    vlm_options = _vlm_options(cfg)

    for clip_index, clip in enumerate(clips):
        def resolve_vlm() -> dict:
            nonlocal vlm_cfg
            if vlm_cfg is None:
                raw = (config_data or {}).get("vlm") or {}
                if raw:
                    _validate_vlm_provider(config_data)
                    live = _materialize_vlm_runtime(
                        _resolve_vlm_runtime(config_data), config_data
                    )
                    vlm_cfg = {
                        **raw,
                        "base_url": live.base_url,
                        "model": live.model,
                        "retry_delay_s": live.retry_delay_s,
                    }
                    print(
                        f"  [rank_dedup VLM] model={live.model} "
                        f"base_url={live.base_url}",
                        flush=True,
                    )
                else:
                    vlm_cfg = {
                        "provider": "ollama",
                        "model": "llava:13b",
                        "base_url": OLLAMA_BASE,
                        "retry_delay_s": 2.0,
                    }
            return vlm_cfg

        def frame_scorer(timestamp_s: float, label: str) -> dict | None:
            provider = resolve_vlm()
            try:
                with current_timings().span("extract"):
                    extracted = extract_frames(
                        video_path, [timestamp_s], work_dir, workers=1,
                    )[0]
                if not extracted.ok or not os.path.exists(extracted.path):
                    return None
                frame_path = extracted.path
                with open(frame_path, "rb") as frame_file:
                    payload, error = _score_vlm_frame(
                        base_url=provider.get("base_url", OLLAMA_BASE),
                        model=provider.get("model", "llava:13b"),
                        image_bytes=frame_file.read(),
                        prompt=get_score_prompt(
                            cfg.get("score_prompt_mode", "default"),
                            schema="full",
                        ),
                        options=_scoring_vlm_options(cfg, "full"),
                        threshold=cfg["worthiness_threshold"],
                        timestamp=timestamp_s,
                        frame_path=frame_path,
                        retry_delay_s=float(
                            provider.get("retry_delay_s", 2.0)
                        ),
                        keep_alive=cfg.get("vlm_keep_alive"),
                        schema="full",
                        calibrator=score_calibrator,
                    )
                if payload is None:
                    print(
                        "  Action rescore dropped segment at "
                        f"{timestamp_s:.2f}s: {error}"
                    )
                return payload
            except Exception as exc:
                print(
                    "  Action rescore dropped segment at "
                    f"{timestamp_s:.2f}s: {exc}"
                )
                return None

        def sequence_generator(image_bytes: bytes, prompt: str) -> str:
            provider = resolve_vlm()
            response = httpx.post(
                f"{provider.get('base_url', OLLAMA_BASE)}/api/generate",
                json={
                    "model": provider.get("model", "llava:13b"),
                    "prompt": prompt,
                    "images": [
                        base64.b64encode(image_bytes).decode("utf-8")
                    ],
                    "stream": False,
                    "options": vlm_options,
                },
                timeout=120,
            )
            response.raise_for_status()
            raw_response = response.json().get("response", "")
            return raw_response if isinstance(raw_response, str) else ""

        materialized = materialize_action_candidates(
            video_path=video_path,
            clip=clip,
            scored_frames=scored_frames,
            total_duration_s=_temporal_media_duration(
                total_duration, float(cfg["transition_scan_fps"])
            ),
            config=action_materializer_config,
            evidence_cache=evidence_cache,
            frame_scorer=frame_scorer,
            sequence_generator=sequence_generator,
        )
        for candidate in materialized.clips:
            normalized_candidate = dict(candidate)
            if not bool(normalized_action["action_guard_enabled"]):
                normalized_candidate.setdefault(
                    "action_boundary_mode", "disabled"
                )
                normalized_candidate.setdefault(
                    "action_boundary_confidence", None
                )
                normalized_candidate.setdefault(
                    "action_vlm_verified", False
                )
                normalized_candidate.setdefault(
                    "action_analysis_version",
                    int(normalized_action["action_analysis_version"]),
                )
            clean_clips.append(normalized_candidate)
        for name in transition_guard:
            transition_guard[name] += int(
                materialized.transition_metrics.get(name, 0)
            )
        for name in (
            "input",
            "output",
            "cv",
            "extended",
            "trimmed",
            "split",
            "ambient_motion",
            "vlm_checked",
            "vlm_succeeded",
            "vlm_failed",
            "fallback",
            "low_loop_quality",
        ):
            action_guard[name] += int(
                materialized.action_metrics.get(name, 0)
            )
        for name in ("cv_ms", "vlm_ms", "total_ms"):
            elapsed = float(materialized.action_metrics.get(name, 0.0))
            if math.isfinite(elapsed):
                action_guard[name] += max(0.0, elapsed)
        reasons = materialized.action_metrics.get("fallback_reasons", {})
        if isinstance(reasons, dict):
            grouped_reasons = action_guard["fallback_reasons"]
            for reason, count in reasons.items():
                reason_key = str(reason)
                grouped_reasons[reason_key] = (
                    int(grouped_reasons.get(reason_key, 0)) + int(count)
                )

    print(
        f"  Guard: {transition_guard['input']} input -> {len(clean_clips)} clean candidates "
        f"(split={transition_guard['split']}, trim={transition_guard['trim']}, "
        f"drop={transition_guard['drop']}, unverified={transition_guard['unverified']}, "
        f"action_split={action_guard['split']}, fallback={action_guard['fallback']})"
    )
    snap_stats = _apply_boundary_snaps(
        clean_clips, video_path, cfg, evidence_cache,
    )

    # Task 3: freeze candidate identities right after boundary snaps so dedup
    # works on stable identities; the post-dedup call below re-verifies the
    # frozen preexport keys (idempotent with capture enabled).
    _assign_candidate_identities(clean_clips, video_path, cfg=cfg)

    # Embedding dedup
    dedup_input_count = len(clean_clips)
    embedding_deduped_count = len(clean_clips)
    duplicate_groups: list[dict] = []
    deduped_clips = list(clean_clips)
    if EMBED_DEDUP_ENABLED and len(clean_clips) > 1:
        clip_embeddings = _compute_clip_embeddings(clean_clips)
        deduped_clips, duplicate_groups = embedding_dedup_clips(
            clean_clips,
            clip_embeddings,
            threshold=EMBED_SIM_THRESHOLD,
            max_gap_s=EMBED_DEDUP_MAX_GAP_S,
        )
        embedding_deduped_count = len(deduped_clips)
        print(f"  Embedding dedup: {len(clean_clips)} -> {len(deduped_clips)} clips")

    # Temporal dedup
    if TEMPORAL_DEDUP_ENABLED and len(deduped_clips) > 1:
        deduped_clips = temporal_dedup_clips(deduped_clips, min_gap_s=TEMPORAL_DEDUP_MIN_GAP_S)
        print(f"  Temporal dedup: {len(deduped_clips)} clips remain")

    # Evaluate quality on the full post-dedup set, then truncate for export.
    # Direct mode uses the same order so report_only evidence and active
    # soft-reject share one candidate population across both pipelines.
    _assign_candidate_identities(deduped_clips, video_path, cfg=cfg)
    deduped_clips, quality_moe = _evaluate_quality_pipeline_candidates(
        deduped_clips,
        video_path=video_path,
        cfg=cfg,
        work_dir=work_dir,
    )
    ledger_path, _ledger_ref = _write_rank_candidate_ledger(
        work_dir,
        quality_moe,
        source_artifact=source_artifact,
        stage_id=rank_stage_id,
    )

    # Task 3: capture evidence for every post-dedup scorable candidate after
    # the quality boundary and BEFORE ranking/output truncation.
    capture_context = _rank_capture_context(video_path, cfg)
    capture_batch = capture_rank_candidates(capture_context, deduped_clips)
    if capture_context.enabled:
        print(
            f"  Candidate capture: {len(capture_batch.captures)} captured, "
            f"{len(capture_batch.failed_preexport_keys)} failed"
        )
        if capture_batch.failed_preexport_keys:
            print(
                "  Candidate capture failed keys: "
                + ", ".join(key[:12] for key in capture_batch.failed_preexport_keys)
            )
        if capture_context.mode == "active":
            # Active curve mode requires complete capture: candidates whose
            # evidence could not be persisted must not reach protected output.
            failed_set = set(capture_batch.failed_preexport_keys)
            deduped_clips = [
                clip
                for clip in deduped_clips
                if clip.get("capture_status") != "FAILED"
                and str(clip.get("preexport_key")) not in failed_set
            ]

    deduped_count = len(deduped_clips)
    multi_frame_count = sum(
        1 for c in clean_clips if int(c.get("frame_count", 1)) > 1
    )
    full_rank_input = list(deduped_clips)
    deduped_clips = _rank_pipeline_clips(deduped_clips, cfg)
    output_count = _planned_output_count(
        len(deduped_clips), OUTPUT_RATIO, MAX_OUTPUT
    )
    deduped_clips = deduped_clips[:output_count]
    for i, clip in enumerate(deduped_clips):
        clip["rank"] = i + 1

    # Task 3: append selection events after ranking and the output cap.
    selected_ids = {
        str(clip.get("clip_id") or clip.get("candidate_id") or "")
        for clip in deduped_clips
    }
    assessed_ids = {
        str(entry.get("candidate_id"))
        for entry in (quality_moe.get("assessed_candidates") or [])
        if isinstance(entry, dict) and entry.get("candidate_id")
    }
    returned_ids = {
        str(clip.get("candidate_id") or clip.get("clip_id") or "")
        for clip in full_rank_input
    }
    _append_rank_selection_events(
        capture_context,
        full_rank_input,
        selected_ids,
        assessed_ids - returned_ids,
    )

    print(f"  Final: {len(deduped_clips)} deduped clips")

    manifest = {
        "schema_version": 2,
        "stage": "rank_dedup",
        "clip_count": len(deduped_clips),
        "clips": deduped_clips,
        "transition_guard": transition_guard,
        "boundary_snap": snap_stats,
        "action_guard": action_guard,
        "quality_moe": quality_moe,
        "output_key": "rank_dedup",
    }
    if capture_context.enabled:
        manifest["candidate_capture"] = {
            "enabled": True,
            "mode": capture_context.mode,
            "captured": len(capture_batch.captures),
            "failed_preexport_keys": list(capture_batch.failed_preexport_keys),
        }
    manifest_path = _save_manifest(work_dir, "rank_dedup", manifest)

    return {
        "output_key": "rank_dedup",
        "clip_count": len(deduped_clips),
        "dedup_input_clips": dedup_input_count,
        "embedding_deduped_clips": embedding_deduped_count,
        "deduped_clips": deduped_count,
        "duplicate_groups": duplicate_groups,
        "multi_frame_clips": multi_frame_count,
        "planned_output_count": output_count,
        "_artifacts": [
            _make_artifact(manifest_path, "rank_dedup_manifest"),
            _make_artifact(ledger_path, "rank_candidate_ledger"),
        ],
    }

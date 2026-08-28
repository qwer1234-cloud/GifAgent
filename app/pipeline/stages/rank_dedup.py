"""Stage 6: rank_dedup -- guard, dedup, quality boundary, ranking."""
from __future__ import annotations

import base64
import hashlib
import math
import os
import subprocess

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
    _assign_candidate_identities(deduped_clips, video_path)
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

    deduped_count = len(deduped_clips)
    multi_frame_count = sum(
        1 for c in clean_clips if int(c.get("frame_count", 1)) > 1
    )
    deduped_clips = _rank_pipeline_clips(deduped_clips, cfg)
    output_count = _planned_output_count(
        len(deduped_clips), OUTPUT_RATIO, MAX_OUTPUT
    )
    deduped_clips = deduped_clips[:output_count]
    for i, clip in enumerate(deduped_clips):
        clip["rank"] = i + 1

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

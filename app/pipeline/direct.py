"""Direct (single-process) adaptive pipeline.

``run_pipeline`` orchestrates the same stage handlers the production
workers use (``app.pipeline.stages.*``), in-process, writing the same
manifests into a one-shot work directory.  Direct-only extras -- the scored
checkpoint, grid sample thumbnail, video-level LLM synthesis, and the
aggregated export result -- are layered on top of the stage outputs.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import time

from app.config import load_config
from app.db import init_db
from app.pipeline.config import _extract_direct_snapshot_config
from app.pipeline.quality_bridge import _QUALITY_LINEAGE_FIELDS, _resolve_quality_runtime_snapshot
from app.pipeline.scoring import (
    _load_scored_checkpoint,
    _save_scored_checkpoint,
    frame_passes_keep_gate,
)
from app.pipeline.stage_io import (
    _hash_artifact_id,
    _load_manifest,
    _save_manifest,
)
from app.pipeline.stages.discover import _stage_discover
from app.pipeline.stages.gif_clip import _stage_gif_clip
from app.pipeline.stages.rank_dedup import _stage_rank_dedup
from app.pipeline.stages.refine import _stage_refine
from app.pipeline.stages.sample import _stage_sample
from app.pipeline.stages.synthesize import _stage_synthesize
from app.pipeline.stages.vlm import _stage_vlm
from app.pipeline.timing import reset_timings
from app.pipeline.vlm_runtime import (
    OLLAMA_BASE,
    VlmRuntimeConfig,
    _materialize_vlm_runtime,
    _resolve_vlm_runtime,
    stop_model,
)
from app.services.export_cleanup import (
    ExportDirectoryBusyError,
    ExportDirectoryLock,
    cleanup_adaptive_export_dir,
)
from app.services.gif_naming import build_gif_filename
from app.services.grid_select import select_grid_frames
from app.services.json_guard import parse_json_response
from app.services.llm_client import (
    generate_llm_text,
    llm_model_name,
    wait_for_llm,
)
from app.services.potplayer_bookmarks import PotPlayerBookmark, write_pbf_file


def _stage_config_data_from_runtime(
    vlm_runtime: VlmRuntimeConfig | None,
) -> dict | None:
    """Project the materialized runtime into the frozen-snapshot shape the
    shared stage handlers read their VLM configuration from."""
    if vlm_runtime is None:
        return None
    return {
        "_stage_id": "direct",
        "vlm": {
            "provider": "ollama",
            "model": vlm_runtime.model,
            "base_url": vlm_runtime.base_url,
            "manage_lifecycle": vlm_runtime.manage_lifecycle,
            "launch_mode": vlm_runtime.launch_mode,
            "retry_delay_s": vlm_runtime.retry_delay_s,
            "free_vram_before_load": vlm_runtime.free_vram_before_load,
        },
    }


def _lineage_manifest_ref(
    manifest_path: str, artifact_kind: str, stage_id: str = "direct"
) -> dict:
    """Build the input-manifest entry the shared stages validate upstream
    manifests against.  Direct has no task_artifacts DB, so the lineage is
    computed from the manifest file this run just wrote."""
    with open(manifest_path, "rb") as handle:
        raw = handle.read()
    return {
        "path": manifest_path,
        "artifact_kind": artifact_kind,
        "stage_id": stage_id,
        "artifact_id": _hash_artifact_id(
            artifact_kind, manifest_path, stage_id=stage_id
        ),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }


def _should_stop_vlm_for_direct_llm(vlm_runtime: VlmRuntimeConfig | None) -> bool:
    """True only when Direct actually loaded a managed VLM that occupies VRAM."""
    return (
        vlm_runtime is not None
        and bool(vlm_runtime.manage_lifecycle)
        and str(vlm_runtime.launch_mode or "") != "none"
    )


def _release_vlm_for_llm(vlm_runtime: VlmRuntimeConfig | None) -> None:
    """Unload the Direct VLM before video-level LLM synthesis.

    Scoring already honors ``manage_lifecycle``.  The historical Direct
    path always called ``stop_model("llava")`` with a default WSL
    runtime; that stops the wrong model (or starts a WSL stop) even when
    lifecycle is frozen off.
    """
    if not _should_stop_vlm_for_direct_llm(vlm_runtime):
        return
    assert vlm_runtime is not None
    stop_model(vlm_runtime.model, vlm_runtime)


def _video_level_synthesis(ranked_clips: list[dict]) -> dict:
    """One video-level LLM pass over the rank-truncated clip set."""
    synthesis_clips = sorted(
        ranked_clips,
        key=lambda clip: clip.get("gif_worthiness") or 0.0,
        reverse=True,
    )[:20]
    analyses = "\n\n".join(
        f"Frame {i+1} (t={clip.get('best_frame_ts')}s, worth={clip.get('gif_worthiness', 0.0):.2f}): "
        f"caption={clip.get('caption','')}, "
        f"emotion={clip.get('emotional_core','')}"
        for i, clip in enumerate(synthesis_clips)
    )

    synth_prompt = (
        "Synthesize scene analyses from a film. Output ONLY JSON:\n"
        '{"summary":"one sentence about visual style","emotional_core":"one dominant emotion",'
        '"aesthetic_notes":["2-4 qualities"],"tags":["3-5 keywords"],'
        '"scene_type":"close-up|dialogue|action|transition|reaction|establishing|montage|other"}\n\n'
        "Scene analyses:\n" + analyses
    )

    if not wait_for_llm(timeout_s=180):
        print("WARNING: LLM not responding -- skipping synthesis, proceeding to export")
        return {"_parse_error": True}

    synthesis: dict = {"_parse_error": True}
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
            synthesis = {"_parse_error": True, "_raw": raw[:500]}
            print(f"  Attempt {attempt+1}: JSON parse failed")
        except Exception as e:
            print(f"  Attempt {attempt+1}: {e}")
            time.sleep(5)
    return synthesis


def run_pipeline(
    video_path: str,
    frames_dir: str,
    export_dir: str,
    cfg: dict,
    vlm_runtime: VlmRuntimeConfig | None = None,
) -> dict:
    """Run the adaptive extraction pipeline through the shared stage handlers.

    Returns the full ``output`` dict with all scores, clips, and paths.
    """
    SAMPLE_INTERVAL = cfg["sample_interval"]
    OUTPUT_RATIO = cfg["output_ratio"]
    MAX_OUTPUT = cfg["max_output"]
    WORTHINESS_THRESHOLD = cfg["worthiness_threshold"]
    SEX_ACT_THRESHOLD = float(cfg.get("sex_act_threshold", 0.0))
    MIN_BRIGHTNESS = float(cfg.get("min_brightness", 25))
    POTPLAYER_PBF_ENABLED = cfg["potplayer_pbf_enabled"]
    PREFERENCE_MEMORY_ENABLED = cfg["preference_memory_enabled"]
    BASE_SCORE_WEIGHT = cfg["base_score_weight"]
    PREFERENCE_SCORE_WEIGHT = cfg["preference_score_weight"]

    SCORE_PROMPT_MODE = cfg.get("score_prompt_mode", "default")
    VLM_MODEL = vlm_runtime.model if vlm_runtime else "llava:13b"
    VLM_BASE_URL = vlm_runtime.base_url if vlm_runtime else OLLAMA_BASE
    live = str(VLM_BASE_URL or "").strip()
    if live.startswith(("http://", "https://")):
        # Direct mode never rewrites the frozen inherit_vlm/auto snapshot;
        # Quality MoE still needs the live endpoint the VLM is already using.
        cfg["_live_vlm_base_url"] = live.rstrip("/")
    LLM_MODEL = llm_model_name()
    print(f"  VLM: {VLM_MODEL}  prompt={SCORE_PROMPT_MODE}")

    video_name = os.path.splitext(os.path.basename(video_path))[0]
    os.makedirs(frames_dir, exist_ok=True)
    os.makedirs(export_dir, exist_ok=True)

    # One-shot manifest work directory: recreated per run so a rerun never
    # reads a previous run's stage outputs.
    work_dir = os.path.abspath(frames_dir).rstrip("/\\") + "_stage_work"
    shutil.rmtree(work_dir, ignore_errors=True)
    os.makedirs(work_dir, exist_ok=True)
    stage_config_data = _stage_config_data_from_runtime(vlm_runtime)

    # ---- Stage 1: discover (ffprobe only) ------------------------------
    print("\n[1/4] Probing video + extracting samples...")
    discover_out = _stage_discover(video_path, work_dir, cfg)
    total_duration = float(discover_out["duration_s"])
    print(f"  Duration: {total_duration:.0f}s ({total_duration/60:.0f} min)")
    discover_ref = _lineage_manifest_ref(
        discover_out["_artifacts"][0]["path"], "discover_manifest"
    )

    # ---- Stages 2-4: sample -> vlm -> refine (checkpoint aware) --------
    resumed_scored = _load_scored_checkpoint(
        frames_dir,
        video_path,
        vlm_model=VLM_MODEL,
        score_prompt_mode=SCORE_PROMPT_MODE,
    )
    total_samples = 0
    dark_dropped = 0
    if resumed_scored:
        print(
            f"  Sampling skipped ({len(resumed_scored)} scored frames from checkpoint)"
        )
        print("\n[2/4] VLM scoring skipped (checkpoint)...")
        print("\n[2.5/4] Boundary refinement skipped (checkpoint)")
        scored = [
            item
            for item in resumed_scored
            if frame_passes_keep_gate(
                item,
                worthiness_threshold=WORTHINESS_THRESHOLD,
                sex_act_threshold=SEX_ACT_THRESHOLD,
            )
        ]
        print(
            f"  Resumed {len(resumed_scored)} checkpoint frames, "
            f"{len(scored)} kept at worthiness>={WORTHINESS_THRESHOLD} "
            f"sex_act>={SEX_ACT_THRESHOLD}"
        )
        refine_path = _save_manifest(
            work_dir,
            "refine",
            {
                "schema_version": 1,
                "stage": "refine",
                "scored_count": len(scored),
                "refine_regions": 0,
                "refine_requested": 0,
                "refine_extracted": 0,
                "refine_extraction_failed": 0,
                "refine_attempted": 0,
                "refine_responded": 0,
                "refine_parsed": 0,
                "refine_failed": 0,
                "frames": scored,
                "output_key": "refine",
            },
        )
    else:
        sample_out = _stage_sample(
            video_path, frames_dir, work_dir, cfg,
            {"discover_manifest": [discover_ref]}, stage_config_data,
        )
        total_samples = int(sample_out["frame_count"])
        dark_dropped = int(sample_out.get("dark_dropped", 0))
        sample_ref = _lineage_manifest_ref(
            sample_out["_artifacts"][0]["path"], "sample_manifest"
        )
        sample_manifest = _load_manifest(work_dir, "sample")

        print(f"\n[2/4] VLM scoring ({total_samples} frames)...")
        vlm_out = _stage_vlm(
            frames_dir, work_dir, cfg,
            {
                "sample_manifest": [sample_ref],
                "sample_frames": list(sample_manifest.get("frame_entries", [])),
            },
            stage_config_data,
        )
        vlm_full_frames = list(vlm_out.get("frames", []))
        vlm_ref = _lineage_manifest_ref(
            vlm_out["_artifacts"][0]["path"], "vlm_manifest"
        )

        print("\n[2.5/4] Boundary refinement around high-score regions...")
        refine_out = _stage_refine(
            video_path, frames_dir, work_dir, cfg,
            {"vlm_manifest": [vlm_ref], "discover_manifest": [discover_ref]},
            stage_config_data,
        )
        refine_path = refine_out["_artifacts"][0]["path"]
        scored = [dict(frame) for frame in refine_out["frames"]]
        # The vlm manifest carries only a subset of each scored frame;
        # restore the full schema (aesthetic_notes, reason, ...) so the
        # checkpoint keeps the identity it always had.
        full_by_ts = {
            float(frame.get("timestamp") or 0): frame for frame in vlm_full_frames
        }
        for frame in scored:
            full = full_by_ts.get(float(frame.get("timestamp") or 0))
            if full is not None:
                for key, value in full.items():
                    frame.setdefault(key, value)
        _save_scored_checkpoint(
            frames_dir,
            video_path,
            scored,
            vlm_model=VLM_MODEL,
            score_prompt_mode=SCORE_PROMPT_MODE,
        )
        print(f"  Wrote scored checkpoint ({len(scored)} frames)")
    refine_ref = _lineage_manifest_ref(refine_path, "refine_manifest")

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

    # ---- Stage 5: synthesize (merge only; video-level LLM is Direct-only)
    print("\n[2.6/4] Merging adjacent frames into clips...")
    synth_out = _stage_synthesize(
        work_dir, cfg, {"refine_manifest": [refine_ref]}, clip_llm=False,
    )
    synth_ref = _lineage_manifest_ref(
        synth_out["_artifacts"][0]["path"], "synthesize_manifest"
    )

    # ---- Stage 6: rank_dedup (guard, snap, dedup, quality, rank) --------
    print("\n[2.65/4] Guarding transitions and action completeness...")
    rank_out = _stage_rank_dedup(
        video_path, export_dir, work_dir, cfg,
        {"synthesize_manifest": [synth_ref]}, stage_config_data,
    )
    rank_path = rank_out["_artifacts"][0]["path"]
    rank_manifest = _load_manifest(work_dir, "rank_dedup")
    ranked_clips = list(rank_manifest.get("clips", []))
    quality_moe = rank_manifest.get("quality_moe", {})
    transition_guard = rank_manifest.get("transition_guard", {})
    snap_stats = rank_manifest.get(
        "boundary_snap", {"snapped": 0, "kept": 0, "unavailable": 0}
    )
    action_guard = rank_manifest.get("action_guard", {})
    dedup_input_clips = int(rank_out.get("dedup_input_clips", 0))
    embedding_deduped_clips = int(rank_out.get("embedding_deduped_clips", 0))
    deduped_count = int(rank_out.get("deduped_clips", len(ranked_clips)))
    duplicate_groups = rank_out.get("duplicate_groups", [])
    multi_frame_clips = int(rank_out.get("multi_frame_clips", 0))
    output_count = int(rank_out.get("planned_output_count", len(ranked_clips)))
    rank_ref = _lineage_manifest_ref(rank_path, "rank_dedup_manifest")
    rank_stage_id = (
        (stage_config_data or {}).get("_stage_id") or "standalone-rank-stage"
    )
    ledger_ref = _lineage_manifest_ref(
        rank_out["_artifacts"][1]["path"],
        "rank_candidate_ledger",
        stage_id=rank_stage_id,
    )

    frame_details: dict[float, dict] = {}
    for frame in scored:
        frame_details.setdefault(float(frame.get("timestamp") or 0), frame)

    def _best_frame_for(clip: dict) -> dict:
        best = frame_details.get(float(clip.get("best_frame_ts") or -1))
        if best is None:
            best = {
                "timestamp": clip.get("best_frame_ts"),
                "path": clip.get("best_frame_path", ""),
                "caption": clip.get("caption", ""),
                "emotional_core": clip.get("emotional_core", "?"),
                "aesthetic_notes": [],
                "reason": "",
            }
        return best

    # ---- Video-level LLM synthesis (direct-only) ------------------------
    print(f"\n[3/4] LLM synthesis...")
    _release_vlm_for_llm(vlm_runtime)
    synthesis = _video_level_synthesis(ranked_clips)

    # ---- 9-grid sample thumbnail (direct-only) --------------------------
    print(f"\n[3.5/4] Generating 9-grid sample thumbnail...")
    import imagehash
    from PIL import Image as PILImage

    GRID_SIZE = 3
    GRID_CELL_W = 480
    GRID_CELL_H = 270
    GRID_DEDUP_THRESHOLD = 10

    sample_dir = os.path.join(export_dir, "Sample")
    os.makedirs(sample_dir, exist_ok=True)

    def _grid_phash(frame):
        fp = frame.get("path", "")
        if not fp or not os.path.exists(fp):
            return None
        try:
            with PILImage.open(fp) as img:
                return imagehash.phash(img)
        except Exception:
            return None

    selected = select_grid_frames(
        scored,
        count=GRID_SIZE * GRID_SIZE,
        phash_fn=_grid_phash,
        phash_threshold=GRID_DEDUP_THRESHOLD,
    )
    grid_sample_timestamps = [frame.get("timestamp") for frame in selected]

    print(f"  Selected {len(selected)} time-spread frames (from {len(scored)} scored)")

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

    # ---- Stage 7 fan-out: one gif_clip run per ranked clip ---------------
    print(
        f"\n[4/4] Exporting {output_count}/{deduped_count} GIFs (4K) "
        f"({OUTPUT_RATIO*100:.0f}% ratio, cap={MAX_OUTPUT})..."
    )

    gif_input_refs = {
        "rank_dedup_manifest": [rank_ref],
        "rank_candidate_ledger": [ledger_ref],
        "synthesize_manifest": [synth_ref],
    }

    exported_bookmarks = []
    gif_export_results = []
    potplayer_pbf_path = None

    for i, clip in enumerate(ranked_clips):
        clip_id = str(clip.get("clip_id") or "")
        best = _best_frame_for(clip)
        common = {
            "index": i + 1,
            "transition_action": clip.get("transition_action"),
            "transition_risk": clip.get("transition_risk"),
            "motion_type": clip.get("motion_type"),
            "guard_reason": clip.get("guard_reason"),
            "guarded_export_window": bool(
                clip.get("guarded_export_window", False)
            ),
            "quality_assessment": clip.get("quality_assessment"),
            "action_boundary_mode": clip.get("action_boundary_mode"),
            "action_start_ts": clip.get("action_start_ts"),
            "action_peak_ts": clip.get("action_peak_ts"),
            "action_end_ts": clip.get("action_end_ts"),
            "action_completeness_score": clip.get(
                "action_completeness_score"
            ),
            "action_boundary_confidence": clip.get(
                "action_boundary_confidence"
            ),
            "loop_quality_score": clip.get("loop_quality_score"),
            "action_split_index": clip.get("action_split_index"),
            "action_split_count": clip.get("action_split_count"),
            "action_split_reason": clip.get("action_split_reason"),
            "action_vlm_verified": bool(
                clip.get("action_vlm_verified", False)
            ),
            "action_fallback_reason": clip.get(
                "action_fallback_reason"
            ),
            "action_analysis_version": clip.get(
                "action_analysis_version"
            ),
        }
        try:
            gif_out = _stage_gif_clip(
                video_path, frames_dir, export_dir, work_dir, cfg,
                clip_id=clip_id,
                inputs=gif_input_refs,
            )
        except Exception as exc:
            start = float(clip.get("start_ts") or 0)
            end = float(clip.get("end_ts") or 0)
            gif_export_results.append({
                **common,
                "path": os.path.join(
                    export_dir,
                    build_gif_filename(
                        video_name, clip.get("rank", i + 1), start, end
                    ),
                ),
                "status": "FAILED",
                "size_bytes": None,
                "error": str(exc),
                "start_ts": start,
                "end_ts": end,
            })
            continue
        gif_manifest = _load_manifest(work_dir, f"gif_clip_{clip_id}")
        gif_export_results.append({
            **common,
            "path": gif_out["gif_path"],
            "status": "OK",
            "size_bytes": gif_manifest.get("size_bytes"),
            "error": None,
            "start_ts": gif_manifest.get("start_ts"),
            "end_ts": gif_manifest.get("end_ts"),
            **{
                field: gif_manifest[field]
                for field in _QUALITY_LINEAGE_FIELDS
                if field in gif_manifest
            },
        })
        exported_bookmarks.append(
            PotPlayerBookmark(
                start_s=float(gif_manifest.get("start_ts") or 0),
                end_s=float(gif_manifest.get("end_ts") or 0),
                rank=i + 1,
                score=clip.get("gif_worthiness") or 0.0,
                merged=int(clip.get("frame_count", 1)) > 1,
                caption=best.get("caption")
                or best.get("reason")
                or best.get("emotional_core")
                or "",
            )
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
        "total_samples": total_samples,
        "scored_kept": len(scored),
        "worthiness_distribution": bins,
        "synthesis": synthesis,
        "merge_gap": cfg["merge_gap"],
        "merge_score_threshold": cfg["merge_score_threshold"],
        "max_merge_span_s": float(cfg.get("max_merge_span_s", 24)),
        "merge_peak_threshold": float(
            cfg.get("merge_peak_threshold", cfg.get("refine_threshold", 0.55))
        ),
        "refine_radius": cfg["refine_radius"],
        "refine_interval": cfg["refine_interval"],
        "output_ratio": OUTPUT_RATIO,
        "max_output": MAX_OUTPUT,
        "min_brightness": MIN_BRIGHTNESS,
        "dark_dropped": dark_dropped,
        "score_prompt_mode": SCORE_PROMPT_MODE,
        "embed_dedup_threshold": cfg["embed_sim_threshold"],
        "embed_dedup_enabled": cfg["embed_dedup_enabled"],
        "embed_dedup_max_gap_s": float(cfg.get("embed_dedup_max_gap_s", 0)),
        "temporal_dedup_enabled": cfg["temporal_dedup_enabled"],
        "temporal_dedup_min_gap_s": cfg["temporal_dedup_min_gap_s"],
        "grid_sample_timestamps": grid_sample_timestamps,
        "potplayer_pbf_enabled": POTPLAYER_PBF_ENABLED,
        "potplayer_pbf_path": potplayer_pbf_path,
        "dedup_input_clips": dedup_input_clips,
        "transition_guard": transition_guard,
        "boundary_snap": snap_stats,
        "action_guard": action_guard,
        "action_config_hash": cfg.get("action_config_hash"),
        "action_input_count": int(action_guard.get("input", 0)),
        "action_output_count": int(action_guard.get("output", 0)),
        "embedding_deduped_clips": embedding_deduped_clips,
        "deduped_clips": deduped_count,
        "clusters_after_dedup": deduped_count,
        "duplicate_groups": duplicate_groups,
        "planned_output_count": output_count,
        "output_count": gif_succeeded,
        "gif_attempted": gif_attempted,
        "gif_succeeded": gif_succeeded,
        "gif_failed": gif_failed,
        "gif_exports": gif_export_results,
        "quality_moe": quality_moe,
        "preference_memory_enabled": PREFERENCE_MEMORY_ENABLED,
        "base_score_weight": BASE_SCORE_WEIGHT,
        "preference_score_weight": PREFERENCE_SCORE_WEIGHT,
        "multi_frame_clips": multi_frame_clips,
        "top_clips": [
            {
                "rank": i + 1,
                "timestamp": best["timestamp"],
                "start_ts": gif_export_results[i]["start_ts"],
                "end_ts": gif_export_results[i]["end_ts"],
                "gif_worthiness": clip["gif_worthiness"],
                "sex_act": clip.get("sex_act", best.get("sex_act")),
                "final_score": clip.get("final_score", clip["gif_worthiness"]),
                "profile_score": clip.get("profile_score"),
                "score_profile_version": clip.get("score_profile_version"),
                "duration": (
                    gif_export_results[i]["end_ts"]
                    - gif_export_results[i]["start_ts"]
                ),
                "frame_count": clip["frame_count"],
                "merged": clip["frame_count"] > 1,
                "caption": best.get("caption"),
                "emotional_core": best.get("emotional_core"),
                "aesthetic_notes": best.get("aesthetic_notes"),
                "reason": best.get("reason"),
                "export_status": gif_export_results[i]["status"],
                "export_path": gif_export_results[i]["path"],
                "export_error": gif_export_results[i]["error"],
                "transition_action": clip.get("transition_action"),
                "transition_risk": clip.get("transition_risk"),
                "motion_type": clip.get("motion_type"),
                "guard_reason": clip.get("guard_reason"),
                "guarded_export_window": bool(
                    clip.get("guarded_export_window", False)
                ),
                "action_boundary_mode": clip.get("action_boundary_mode"),
                "action_start_ts": clip.get("action_start_ts"),
                "action_peak_ts": clip.get("action_peak_ts"),
                "action_end_ts": clip.get("action_end_ts"),
                "action_completeness_score": clip.get(
                    "action_completeness_score"
                ),
                "action_boundary_confidence": clip.get(
                    "action_boundary_confidence"
                ),
                "loop_quality_score": clip.get("loop_quality_score"),
                "action_split_index": clip.get("action_split_index"),
                "action_split_count": clip.get("action_split_count"),
                "action_split_reason": clip.get("action_split_reason"),
                "action_vlm_verified": bool(
                    clip.get("action_vlm_verified", False)
                ),
                "action_fallback_reason": clip.get(
                    "action_fallback_reason"
                ),
                "action_analysis_version": clip.get(
                    "action_analysis_version"
                ),
            }
            for i, (clip, best) in enumerate(
                (clip, _best_frame_for(clip)) for clip in ranked_clips
            )
        ],
    }

    if gif_failed:
        raise SystemExit(1)

    return output


def run_direct_mode(
    video_path: str,
    export_dir: str | None = None,
    *,
    config_path: str | None = None,
    frames_dir: str | None = None,
) -> dict:
    """Run the full adaptive pipeline with lock, cleanup, and result persistence."""
    config_data = _resolve_quality_runtime_snapshot(
        load_config(config_path or "configs/models.yaml")
    )
    init_db()

    video_name = os.path.splitext(os.path.basename(video_path))[0]
    if export_dir:
        EXPORT_DIR = os.path.join(export_dir, video_name)
    else:
        EXPORT_DIR = "data/exports/adaptive_test"
    FRAMES_DIR = frames_dir or f"data/frames/adaptive_test/{video_name}"
    os.makedirs(FRAMES_DIR, exist_ok=True)
    os.makedirs(EXPORT_DIR, exist_ok=True)

    print(f"Video: {os.path.basename(video_path)}")
    print(f"Export: {EXPORT_DIR}")

    # Read config via shared extract_config (same logic as stage mode)
    cfg = _extract_direct_snapshot_config(config_data)

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

    # Direct execution must honor the configured VLM endpoint/model, just as
    # staged jobs do.  The module default is only a legacy fallback.
    vlm_runtime = _materialize_vlm_runtime(
        _resolve_vlm_runtime(config_data), config_data
    )
    timings = reset_timings()
    with timings.span("stage"):
        output = run_pipeline(
            video_path, FRAMES_DIR, EXPORT_DIR, cfg, vlm_runtime=vlm_runtime
        )
    timing_payload = timings.to_dict()
    if timing_payload:
        output["timings"] = timing_payload

    # Save result
    os.makedirs("data", exist_ok=True)
    with open("data/adaptive_test_result.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    export_result_path = os.path.join(EXPORT_DIR, f"{video_name}_result.json")
    with open(export_result_path, "w", encoding="utf-8") as f:
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

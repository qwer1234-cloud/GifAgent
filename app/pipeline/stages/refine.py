"""Stage 4: refine -- refinement sampling + VLM around high-score regions."""
from __future__ import annotations

import os
import time

from PIL import Image

from app.pipeline.config import DEFAULT_MAX_REFINE_FRAMES
from app.pipeline.prompts import _scoring_schema, _scoring_vlm_options, get_score_prompt
from app.pipeline.scoring import (
    _ScoredItem,
    _resolve_score_calibrator,
    _score_frames_concurrent,
    _score_vlm_frame,
    backfill_clip_captions,
    collect_refine_timestamps,
    frame_passes_keep_gate,
)
from app.pipeline.stage_io import _make_artifact, _read_upstream_manifest, _save_manifest
from app.pipeline.timing import current_timings
from app.pipeline.vlm_runtime import (
    _materialize_vlm_runtime,
    _resolve_vlm_runtime,
    _validate_vlm_provider,
    wait_model,
)
from app.services.clip_merge import merge_scored_frames_into_clips
from app.services.frame_extract import extract_frames


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
    MAX_REFINE_FRAMES = int(cfg.get("max_refine_frames", DEFAULT_MAX_REFINE_FRAMES))
    WORTHINESS_THRESHOLD = cfg["worthiness_threshold"]
    SEX_ACT_THRESHOLD = float(cfg.get("sex_act_threshold", 0.0))
    MIN_BRIGHTNESS = float(cfg.get("min_brightness", 25))

    # P0 (sixth-review §4): validate provider via shared helper, then use
    # the shared ``_score_vlm_frame`` for every scoring request so VLM and
    # refine share one endpoint, one error semantics, and one parse path.
    vlm_cfg = _validate_vlm_provider(config_data)
    vlm_rt = _materialize_vlm_runtime(_resolve_vlm_runtime(config_data), config_data)
    vlm_model = vlm_rt.model
    vlm_base_url = vlm_rt.base_url
    vlm_retry_delay = vlm_rt.retry_delay_s
    coarse_schema = _scoring_schema(cfg)
    VLM_OPTIONS = _scoring_vlm_options(cfg, coarse_schema)
    score_calibrator = _resolve_score_calibrator(cfg, vlm_model)
    if vlm_rt.manage_lifecycle and vlm_rt.launch_mode != "none":
        print(
            f"  [refine] waiting for VLM {vlm_model} at {vlm_rt.base_url}",
            flush=True,
        )
        if not wait_model(vlm_model, vlm_rt, timeout_s=300):
            raise RuntimeError(
                f"VLM not responding at refine start: {vlm_model} "
                f"{vlm_rt.base_url}"
            )

    high_ts = {r["timestamp"] for r in scored_frames if r["gif_worthiness"] >= REFINE_THRESHOLD}
    existing_ts = {r["timestamp"] for r in scored_frames}
    refine_ts = collect_refine_timestamps(
        high_ts,
        radius=REFINE_RADIUS,
        interval=REFINE_INTERVAL,
        existing_timestamps=existing_ts,
        duration_s=total_duration,
        max_frames=MAX_REFINE_FRAMES,
    )

    print(
        f"  High-score regions: {len(high_ts)}, "
        f"new frames to sample: {len(refine_ts)} "
        f"(interval={REFINE_INTERVAL}s, radius={REFINE_RADIUS}s, "
        f"cap={MAX_REFINE_FRAMES})",
        flush=True,
    )

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
        # Task 3 Step 2 (preserved): check the ffmpeg extraction result
        # explicitly for each frame.
        with current_timings().span("extract"):
            extraction_results = extract_frames(
                video_path, sorted(refine_ts), frames_dir,
                workers=cfg.get("frame_extract_workers", 1),
            )
        for result in extraction_results:
            ts = int(result.timestamp_s)
            if not result.ok:
                refine_extraction_failed += 1
                print(f"  refine extract FAILED ts={ts}: "
                      f"ffmpeg exit={result.returncode}")
                continue
            if not os.path.exists(result.path) or os.path.getsize(result.path) <= 500:
                refine_extraction_failed += 1
                print(f"  refine extract FAILED ts={ts}: "
                      f"missing or too-small output")
                continue
            try:
                img = Image.open(result.path).convert("L")
                brightness = sum(img.getdata()) / max(1, img.width * img.height)
                img.close()
            except Exception as exc:
                refine_extraction_failed += 1
                print(f"  refine extract FAILED ts={ts}: decode {exc}")
                continue
            if brightness <= MIN_BRIGHTNESS and MIN_BRIGHTNESS > 0:
                refine_extraction_failed += 1
                print(f"  refine extract FAILED ts={ts}: "
                      f"brightness={brightness:.1f} below {MIN_BRIGHTNESS}")
                continue
            refine_frames.append({"path": result.path, "timestamp": ts})
            refine_extracted += 1

        print(f"  Refinement frames after filter: {len(refine_frames)}")

        # Task 3 Step 3: complete extraction failure is a hard error,
        # NOT a silent zero-attempt success.
        if refine_requested > 0 and refine_extracted == 0:
            raise RuntimeError(
                f"Refine extraction failed: requested={refine_requested}, "
                f"extraction_failed={refine_extraction_failed}"
            )

        def _score_one_refine(rf: dict) -> tuple[dict | None, str | None]:
            with open(rf["path"], "rb") as frame_file:
                img_data = frame_file.read()
            return _score_vlm_frame(
                base_url=vlm_base_url, model=vlm_model,
                image_bytes=img_data, prompt=get_score_prompt(
                    cfg.get("score_prompt_mode", "default"), schema=coarse_schema
                ),
                options=VLM_OPTIONS, threshold=WORTHINESS_THRESHOLD,
                timestamp=rf["timestamp"], frame_path=rf["path"],
                retry_delay_s=vlm_retry_delay,
                keep_alive=cfg.get("vlm_keep_alive"),
                schema=coarse_schema,
                calibrator=score_calibrator,
            )

        def _refine_stage_progress(done: int, total: int, item: _ScoredItem) -> None:
            if item.payload is None:
                print(f"  refine[{done}] FAILED: {item.error}", flush=True)
            else:
                worth = item.payload.get("gif_worthiness", 0.0)
                kept = frame_passes_keep_gate(
                    item.payload,
                    worthiness_threshold=WORTHINESS_THRESHOLD,
                    sex_act_threshold=SEX_ACT_THRESHOLD,
                )
                label = "KEPT" if kept else "below threshold"
                print(f"  refine[{done}] score={worth:.2f} {label}", flush=True)
            if done % 10 == 0 or done == total:
                print(
                    f"  refine [{done}/{total}] done, "
                    f"scored={len(scored_frames)}",
                    flush=True,
                )

        refine_results = _score_frames_concurrent(
            refine_frames,
            score_one=_score_one_refine,
            workers=int(cfg.get("vlm_score_workers", 1)),
            on_progress=_refine_stage_progress,
        )
        for item in refine_results:
            refine_attempted += 1
            if item.payload is not None:
                refine_responded += 1
                refine_parsed += 1
                if frame_passes_keep_gate(
                    item.payload,
                    worthiness_threshold=WORTHINESS_THRESHOLD,
                    sex_act_threshold=SEX_ACT_THRESHOLD,
                ):
                    scored_frames.append(item.payload)
            else:
                refine_failed += 1

        # Task 2 Step 3: all-score-failed refine is a hard error too
        # (consistent with _stage_vlm).  Partial failure keeps going.
        if refine_attempted > 0 and refine_parsed == 0:
            raise RuntimeError(
                f"Refine VLM stage failed: all {refine_attempted} refine "
                f"frames failed to parse (0 parsed, {refine_failed} failed)."
            )

    scored_frames.sort(key=lambda item: float(item.get("timestamp") or 0))
    print(f"  After refinement: {len(scored_frames)} total scored frames")

    backfill_stats = {
        "caption_backfill_attempted": 0,
        "caption_backfill_succeeded": 0,
        "caption_backfill_failed": 0,
    }
    if cfg.get("score_schema_mode") == "two_tier" and scored_frames:
        # Same frozen merge keys synthesize will use. Because
        # merge_scored_frames_into_clips is pure, the best_frame set is
        # identical to the one synthesize will derive from this manifest.
        provisional = merge_scored_frames_into_clips(
            scored_frames,
            merge_gap=cfg["merge_gap"],
            merge_score_threshold=cfg["merge_score_threshold"],
            max_merge_span_s=float(cfg.get("max_merge_span_s", 24)),
            peak_threshold=float(
                cfg.get("merge_peak_threshold", cfg.get("refine_threshold", 0.55))
            ),
        )

        def _backfill_one(frame: dict) -> dict | None:
            path = str(frame.get("path") or "")
            if not path or not os.path.exists(path):
                return None
            with open(path, "rb") as frame_file:
                payload, _error = _score_vlm_frame(
                    base_url=vlm_base_url,
                    model=vlm_model,
                    image_bytes=frame_file.read(),
                    prompt=get_score_prompt(
                        cfg.get("score_prompt_mode", "default"), schema="full"
                    ),
                    options=_scoring_vlm_options(cfg, "full"),
                    threshold=WORTHINESS_THRESHOLD,
                    timestamp=float(frame.get("timestamp") or 0),
                    frame_path=path,
                    retry_delay_s=vlm_retry_delay,
                    keep_alive=cfg.get("vlm_keep_alive"),
                    schema="full",
                    calibrator=score_calibrator,
                )
            return payload

        backfill_clip_captions(
            provisional,
            score_frame=_backfill_one,
            max_frames=int(cfg.get("caption_backfill_max_frames", 150)),
            counters=backfill_stats,
        )
        print(
            "  Caption backfill: "
            f"attempted={backfill_stats['caption_backfill_attempted']} "
            f"succeeded={backfill_stats['caption_backfill_succeeded']} "
            f"failed={backfill_stats['caption_backfill_failed']}"
        )

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
        **backfill_stats,
        "frames": scored_frames,
        "output_key": "refine",
    }
    manifest_path = _save_manifest(work_dir, "refine", manifest)

    return {
        "output_key": "refine",
        "scored_count": len(scored_frames),
        "refine_regions": len(high_ts),
        "frames": scored_frames,
        "_artifacts": [_make_artifact(manifest_path, "refine_manifest")],
    }

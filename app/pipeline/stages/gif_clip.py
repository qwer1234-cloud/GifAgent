"""Stage 7: gif_clip -- export a single GIF for one clip."""
from __future__ import annotations

import hashlib
import os
import subprocess

from app.pipeline.export_gif import (
    _ffmpeg_seconds,
    _palette_filters_for,
    _single_frame_cap,
)
from app.pipeline.quality_bridge import (
    _assert_quality_source_unchanged,
    _export_repair_recipe,
    _quality_config_from_pipeline_cfg,
    _quality_export_lineage,
)
from app.pipeline.stage_io import _make_artifact, _read_upstream_manifest, _save_manifest
from app.pipeline.timing import current_timings
from app.quality_moe.repair import build_ffmpeg_filter
from app.services.batch_logging import format_gif_export_line, run_gif_export_attempt
from app.services.gif_naming import build_gif_filename
from app.services.gif_windows import build_export_window


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
    MAX_DURATION = cfg["max_duration"]
    quality_config = _quality_config_from_pipeline_cfg(cfg)

    target_clip = None
    for c in clips:
        if c.get("clip_id") == clip_id:
            target_clip = c
            break

    if target_clip is None:
        raise ValueError(f"clip_id {clip_id} not found in rank_dedup manifest")

    assessment = target_clip.get("quality_assessment")
    _assert_quality_source_unchanged(video_path, assessment)

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ],
        capture_output=True,
        text=True,
    )
    total_duration = float(probe.stdout.strip())
    # Guarded segments are already exact safe intervals.  Re-centering them
    # here could expand a split segment across its confirmed boundary.  Older
    # rank/dedup manifests have no such marker, so retain the shared bounded
    # window fallback for their legacy uncapped merged spans.
    if target_clip.get("guarded_export_window"):
        start_ts = float(target_clip["start_ts"])
        end_ts = float(target_clip["end_ts"])
        duration = end_ts - start_ts
        if duration < 2.0 - 1e-6 or duration > 20.0 + 1e-6:
            raise ValueError(
                f"rank_dedup clip {clip_id} guarded action duration "
                f"{duration:.6f}s is outside [2.000000, 20.000000]"
            )
    else:
        window = build_export_window(
            target_clip,
            total_duration_s=total_duration,
            min_duration_s=MIN_DURATION,
            max_duration_s=MAX_DURATION,
            single_frame_max_duration_s=_single_frame_cap(cfg, MAX_DURATION),
        )
        start_ts = window.start_s
        end_ts = window.end_s
        duration = window.duration_s
    if start_ts < 0 or end_ts > total_duration + 1e-6:
        raise ValueError(f"rank_dedup clip {clip_id} has an out-of-video window")
    if (
        not target_clip.get("guarded_export_window")
        and (
            duration < MIN_DURATION - 1e-6
            or duration > MAX_DURATION + 1e-6
        )
    ):
        raise ValueError(
            f"rank_dedup clip {clip_id} duration {duration:.6f}s is outside "
            f"[{MIN_DURATION:.6f}, {MAX_DURATION:.6f}]"
        )

    video_name = os.path.splitext(os.path.basename(video_path))[0]
    gif_name = build_gif_filename(video_name, target_clip.get("rank", 1), start_ts, end_ts)
    gif_path = os.path.join(export_dir, gif_name)

    print(f"  Exporting clip {clip_id}: {start_ts:.2f}s - {end_ts:.2f}s -> {gif_name}")

    palette_path = os.path.join(frames_dir, f"palette_{clip_id}.png")
    ffmpeg_start = _ffmpeg_seconds(start_ts)
    ffmpeg_duration = _ffmpeg_seconds(end_ts - start_ts)
    repair_recipe = _export_repair_recipe(
        assessment,
        candidate_id=str(clip_id or ""),
        quality_config=quality_config,
    )
    ffmpeg_filter = build_ffmpeg_filter(
        repair_recipe, fps=GIF_FPS, max_width=GIF_MAX_WIDTH
    )
    palettegen, paletteuse = _palette_filters_for(cfg)
    quality_lineage = _quality_export_lineage(
        assessment,
        candidate_id=str(clip_id or ""),
        video_path=video_path,
        start_ts=start_ts,
        end_ts=end_ts,
        config_hash=str(cfg["quality_moe_config_hash"]),
        repair_applied=repair_recipe is not None,
    )
    with current_timings().span("gif_export"):
        attempt = run_gif_export_attempt(
            palette_command=[
                "ffmpeg", "-y", "-ss", ffmpeg_start, "-t", ffmpeg_duration,
                "-i", video_path,
                "-vf", f"{ffmpeg_filter},{palettegen}",
                palette_path,
            ],
            gif_command=[
                "ffmpeg", "-y", "-ss", ffmpeg_start, "-t", ffmpeg_duration,
                "-i", video_path, "-i", palette_path,
                "-lavfi", f"{ffmpeg_filter}[x];[x][1:v]{paletteuse}",
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
        "schema_version": (
            2 if rank_manifest.get("schema_version") == 2 else 1
        ),
        "stage": "gif_clip",
        "clip_id": clip_id,
        "gif_path": os.path.abspath(gif_path),
        "gif_name": gif_name,
        "sha256": gif_sha256.hexdigest(),
        "start_ts": start_ts,
        "end_ts": end_ts,
        "duration_s": duration,
        "size_bytes": int(attempt.size_bytes),
        "status": "succeeded",
        "output_key": f"gif_clip:{clip_id}",
        "quality_assessment": assessment,
        **quality_lineage,
    }
    if manifest["schema_version"] == 2:
        for field in (
            "action_boundary_mode",
            "action_start_ts",
            "action_peak_ts",
            "action_end_ts",
            "action_completeness_score",
            "action_boundary_confidence",
            "loop_quality_score",
            "action_split_reason",
            "action_split_index",
            "action_split_count",
            "action_vlm_verified",
            "action_fallback_reason",
            "action_analysis_version",
            "guarded_export_window",
            "transition_action",
            "transition_risk",
            "motion_type",
            "guard_reason",
        ):
            if field in target_clip:
                manifest[field] = target_clip[field]
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

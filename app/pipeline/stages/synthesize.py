"""Stage 5: synthesize -- LLM synthesis + clip merging."""
from __future__ import annotations

from app.pipeline.stage_io import _make_artifact, _read_upstream_manifest, _save_manifest
from app.services.clip_merge import merge_scored_frames_into_clips


def _stage_synthesize(
    work_dir: str,
    cfg: dict,
    inputs: dict,
    *,
    clip_llm: bool = True,
) -> dict:
    """Read refine manifest, merge into clips, optionally tag clips via LLM.

    Direct mode passes ``clip_llm=False`` so it only merges (matching the
    historical Direct path) and keeps a single video-level LLM call later.
    Staged jobs keep the default and tag each clip.
    """
    refine_manifest = _read_upstream_manifest(inputs, "refine_manifest", "synthesize")
    scored_frames = refine_manifest.get("frames", [])

    MERGE_GAP = cfg["merge_gap"]
    MERGE_SCORE_THRESHOLD = cfg["merge_score_threshold"]
    MAX_MERGE_SPAN_S = float(cfg.get("max_merge_span_s", 24))
    MERGE_PEAK_THRESHOLD = float(
        cfg.get("merge_peak_threshold", cfg.get("refine_threshold", 0.55))
    )

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

    if not clips_data:
        manifest = {
            "schema_version": 1,
            "stage": "synthesize",
            "clip_count": 0,
            "clips": [],
            # Rank/dedup needs the refined evidence to select a segment-local
            # best frame after a transition split.  Keep clips unchanged for
            # consumers of v1 manifests.
            "scored_frames_version": 1,
            "scored_frames": [],
            "output_key": "synthesize",
        }
        manifest_path = _save_manifest(work_dir, "synthesize", manifest)
        return {
            "output_key": "synthesize",
            "clip_count": 0,
            "_artifacts": [_make_artifact(manifest_path, "synthesize_manifest")],
        }

    # Region-aware merge (shared with direct mode)
    merged = merge_scored_frames_into_clips(
        clips_data,
        merge_gap=MERGE_GAP,
        merge_score_threshold=MERGE_SCORE_THRESHOLD,
        max_merge_span_s=MAX_MERGE_SPAN_S,
        peak_threshold=MERGE_PEAK_THRESHOLD,
    )
    clips = []
    for clip in merged:
        best = clip["best_frame"]
        clips.append({
            "start_ts": clip["start_ts"],
            "end_ts": clip["end_ts"],
            "best_frame_ts": best["timestamp"],
            "best_frame_path": best.get("path", ""),
            "frame_count": clip["frame_count"],
            "gif_worthiness": clip["gif_worthiness"],
            "emotional_core": clip.get("emotional_core", "?"),
            "caption": best.get("caption", ""),
        })

    print(
        f"  Merged into {len(clips)} clips "
        f"(merge_gap={MERGE_GAP}s, max_span={MAX_MERGE_SPAN_S:.0f}s, "
        f"peak>={MERGE_PEAK_THRESHOLD:.2f})"
    )

    for clip in clips:
        clip["summary"] = ""
        clip["tags"] = []
    if clip_llm:
        try:
            _synthesize_clips_with_llm(clips, cfg)
        except Exception as e:
            print(f"  LLM synthesis failed (non-fatal): {e}")

    manifest = {
        "schema_version": 1,
        "stage": "synthesize",
        "clip_count": len(clips),
        "clips": clips,
        "scored_frames_version": 1,
        "scored_frames": scored_frames,
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

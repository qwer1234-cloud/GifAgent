"""Stage 2: sample -- coarse frame extraction + dark filter."""
from __future__ import annotations

import os

from PIL import Image

from app.pipeline.stage_io import (
    _hash_artifact_id,
    _make_artifact,
    _read_upstream_manifest,
    _save_manifest,
)
from app.pipeline.timing import current_timings
from app.services.frame_extract import extract_frames


def _stage_sample(video_path: str, frames_dir: str, work_dir: str, cfg: dict, inputs: dict, config_data: dict | None = None) -> dict:
    """Read the discover manifest, coarse-sample frames, write sample manifest.

    Does NOT call VLM or export GIFs.
    """
    discover = _read_upstream_manifest(inputs, "discover_manifest", "sample")
    total_duration = discover.get("duration_s", 0)
    SAMPLE_INTERVAL = cfg["sample_interval"]
    MIN_BRIGHTNESS = float(cfg.get("min_brightness", 25))

    # P1-3: Get stage_id from config for stable artifact_id computation.
    config_data = config_data or {}
    stage_id = config_data.get("_stage_id", "")

    timestamps = list(range(SAMPLE_INTERVAL, int(total_duration), SAMPLE_INTERVAL))
    print(f"  Sampling {len(timestamps)} timestamps")

    sample_frames = []
    dark_dropped = 0
    # Canonical path: the frame path stored in frame_entries and hashed into
    # frame_entries[].artifact_id MUST use the same absolute representation
    # that _make_artifact() reports and that the adapter persists into
    # task_artifacts.  extract_frames() already returns absolute paths, so
    # this stays true without any extra os.path.abspath() here.
    with current_timings().span("extract"):
        extraction_results = extract_frames(
            video_path, timestamps, frames_dir,
            workers=cfg.get("frame_extract_workers", 1),
        )
    for i, result in enumerate(extraction_results):
        ts = int(result.timestamp_s)
        if os.path.exists(result.path) and os.path.getsize(result.path) > 500:
            try:
                img = Image.open(result.path).convert("L")
                brightness = sum(img.getdata()) / max(1, img.width * img.height)
                img.close()
                if MIN_BRIGHTNESS <= 0 or brightness > MIN_BRIGHTNESS:
                    sample_frames.append({"path": result.path, "timestamp": ts})
                else:
                    dark_dropped += 1
            except Exception:
                pass
        if (i + 1) % 50 == 0:
            print(f"  [{i + 1}/{len(timestamps)}] extracted, {len(sample_frames)} kept")

    print(
        f"  Frames after dark filter: {len(sample_frames)} "
        f"(min_brightness={MIN_BRIGHTNESS}, dropped={dark_dropped})"
    )

    manifest = {
        "schema_version": 1,
        "stage": "sample",
        "frame_count": len(sample_frames),
        "dark_dropped": dark_dropped,
        "min_brightness": MIN_BRIGHTNESS,
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
        "dark_dropped": dark_dropped,
        "_artifacts": artifacts,
    }


def _resolve_legacy_sample_frame_ref(
    aid: str,
    entry_path: str,
    sample_frames_refs: list[dict],
) -> dict | None:
    """Strictly resolve a legacy sample_frames ID derived from a relative path.

    Manifests written before the canonical-path fix hashed the raw
    ``frame_entries[].path`` (e.g. ``data/task_work/sample/.../ts_000016.jpg``)
    while ``task_artifacts`` persisted the same frame under its absolute
    path.  This fallback accepts ONLY when:

    * exactly one resolver entry has the same normalized absolute path as
      the manifest entry (ambiguous matches are rejected); and
    * the manifest's legacy ID equals the hash of the manifest's legacy
      path and the upstream sample stage id, proving the ID relationship.

    Returns ``None`` for unknown IDs, missing paths, ambiguous matches,
    missing upstream stage provenance, or any other case that cannot be
    proven.  Callers keep rejecting those cases as unknown artifact IDs.
    """
    if not entry_path:
        return None
    target_abs = os.path.normcase(os.path.abspath(entry_path))
    matches = []
    for ref in sample_frames_refs:
        ref_path = ref.get("path", "")
        if ref_path and os.path.normcase(os.path.abspath(ref_path)) == target_abs:
            matches.append(ref)
    if len(matches) != 1:
        return None
    ref = matches[0]
    expected_legacy_id = _hash_artifact_id(
        "sample_frames",
        entry_path,
        stage_id=ref.get("stage_id", ""),
        clip_id=ref.get("clip_id"),
    )
    if expected_legacy_id != aid:
        return None
    return ref

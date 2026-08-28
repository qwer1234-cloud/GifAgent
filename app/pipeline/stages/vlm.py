"""Stage 3: vlm -- VLM scoring of sampled frames only."""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from app.pipeline.prompts import _scoring_schema, _scoring_vlm_options, get_score_prompt
from app.pipeline.scoring import (
    _ScoredItem,
    _resolve_score_calibrator,
    _score_frames_concurrent,
    _score_vlm_frame,
    frame_passes_keep_gate,
)
from app.pipeline.stage_io import _make_artifact, _read_upstream_manifest, _save_manifest
from app.pipeline.vlm_runtime import (
    _materialize_vlm_runtime,
    _resolve_vlm_runtime,
    stop_model,
    wait_model,
)
from app.pipeline.stages.sample import _resolve_legacy_sample_frame_ref
from app.services.llm_client import is_local_llm, llm_model_name


def _stage_vlm(frames_dir: str, work_dir: str, cfg: dict, inputs: dict, config_data: dict | None = None) -> dict:
    """Read the sample manifest, score frames with VLM, write VLM manifest.

    P1-3: Cross-references sample_manifest frame_entries with sample_frames
    resolver entries by artifact_id.  Fails if a frame is missing, SHA
    mismatched, duplicate artifact_id, or manifest references unknown frame.
    Legacy manifests whose frame entry ID was hashed from a relative path are
    recovered only when the legacy ID provably matches the entry path and
    the upstream sample stage id (see _resolve_legacy_sample_frame_ref).

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
            # Legacy manifests hashed the raw (possibly relative) frame path
            # while task_artifacts persisted the absolute path.  Recover only
            # when the legacy ID provably belongs to this entry; anything
            # unproven stays an unknown-ID rejection.
            resolver_ref = _resolve_legacy_sample_frame_ref(
                aid, path, sample_frames_refs,
            )
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
    SEX_ACT_THRESHOLD = float(cfg.get("sex_act_threshold", 0.0))

    # Task 4 (seventh-review): resolve the entire VLM runtime from the frozen
    # config.  Provider validation, model, base_url, lifecycle and launch_mode
    # are all explicit; no URL inference.
    vlm_rt = _materialize_vlm_runtime(_resolve_vlm_runtime(config_data), config_data)
    vlm_model = vlm_rt.model
    vlm_base_url = vlm_rt.base_url
    vlm_retry_delay = vlm_rt.retry_delay_s
    coarse_schema = _scoring_schema(cfg)
    VLM_OPTIONS = _scoring_vlm_options(cfg, coarse_schema)
    score_calibrator = _resolve_score_calibrator(cfg, vlm_model)

    # Task 4: explicit lifecycle.  manage_lifecycle=False or launch_mode=none
    # skips ALL model lifecycle (no WSL subprocess, no sleep).
    print(
        f"  [VLM runtime] model={vlm_model} base_url={vlm_base_url} "
        f"lifecycle={vlm_rt.manage_lifecycle} launch={vlm_rt.launch_mode}",
        flush=True,
    )
    if vlm_rt.manage_lifecycle and vlm_rt.launch_mode != "none":
        if is_local_llm():
            stop_model(llm_model_name().split("/")[-1].split(":")[0], vlm_rt)
        if vlm_rt.free_vram_before_load:
            stop_model("nomic-embed-text", vlm_rt)
        time.sleep(5)
        if not wait_model(vlm_model, vlm_rt, timeout_s=300):
            print("ERROR: VLM not responding")
            sys.exit(1)

    print(f"\n  VLM scoring ({len(validated_frames)} frames)...")
    scored = []
    attempted_count = 0
    response_count = 0
    parsed_count = 0
    failed_count = 0
    progress_kept: list[dict] = []
    scored_lock = threading.Lock()

    def _score_one_validated(vf: dict) -> tuple[dict | None, str | None]:
        fpath = vf["path"]
        ts = vf["timestamp"]
        with open(fpath, "rb") as frame_file:
            img_data = frame_file.read()
        return _score_vlm_frame(
            base_url=vlm_base_url, model=vlm_model,
            image_bytes=img_data, prompt=get_score_prompt(
                cfg.get("score_prompt_mode", "default"), schema=coarse_schema
            ),
            options=VLM_OPTIONS, threshold=WORTHINESS_THRESHOLD,
            timestamp=ts, frame_path=fpath,
            retry_delay_s=vlm_retry_delay,
            keep_alive=cfg.get("vlm_keep_alive"),
            schema=coarse_schema,
            calibrator=score_calibrator,
        )

    def _vlm_progress(done: int, total: int, item: _ScoredItem) -> None:
        if item.payload is None:
            print(f"  [{done}] FAILED: {item.error}")
        else:
            worth = item.payload.get("gif_worthiness", 0.0)
            if frame_passes_keep_gate(
                item.payload,
                worthiness_threshold=WORTHINESS_THRESHOLD,
                sex_act_threshold=SEX_ACT_THRESHOLD,
            ):
                with scored_lock:
                    progress_kept.append(item.payload)
                print(f"  [{done}] score={worth:.2f} KEPT")
            else:
                print(f"  [{done}] score={worth:.2f} below threshold")
        if done % 30 == 0:
            with scored_lock:
                kept = list(progress_kept)
            avg = sum(s["gif_worthiness"] for s in kept) / max(1, len(kept))
            print(f"  [{done}/{total}] scored={len(kept)} kept, avg_worth={avg:.2f}")

    vlm_results = _score_frames_concurrent(
        validated_frames,
        score_one=_score_one_validated,
        workers=int(cfg.get("vlm_score_workers", 1)),
        on_progress=_vlm_progress,
    )
    for item in vlm_results:
        attempted_count += 1
        if item.payload is not None:
            response_count += 1
            parsed_count += 1
            if frame_passes_keep_gate(
                item.payload,
                worthiness_threshold=WORTHINESS_THRESHOLD,
                sex_act_threshold=SEX_ACT_THRESHOLD,
            ):
                scored.append(item.payload)
        else:
            failed_count += 1

    # P0: ALL frames failed and there were frames to analyze -> stage MUST
    # fail (never produce a false zero-clip success from a service outage).
    if attempted_count > 0 and parsed_count == 0:
        raise RuntimeError(
            f"VLM stage failed: all {attempted_count} frames failed to "
            f"parse (0 parsed, {failed_count} failed).  This is a service "
            f"outage or configuration error, NOT a legitimate zero result."
        )

    print(
        f"  Scored: {len(scored)} frames kept "
        f"(worthiness>={WORTHINESS_THRESHOLD}, sex_act>={SEX_ACT_THRESHOLD})"
    )
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
        "frames": scored,
        "_artifacts": [_make_artifact(manifest_path, "vlm_manifest")],
    }

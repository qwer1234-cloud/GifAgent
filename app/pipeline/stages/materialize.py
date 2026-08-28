"""Stage 8: materialize -- aggregate successful GIFs, write PBF, result JSON."""
from __future__ import annotations

import hashlib
import json
import os
import re

from app.pipeline.quality_bridge import _QUALITY_LINEAGE_FIELDS
from app.pipeline.stage_io import _make_artifact, _save_manifest
from app.services.potplayer_bookmarks import PotPlayerBookmark, write_pbf_file


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
    has_versioned_envelope = "artifacts" in inputs
    if has_versioned_envelope:
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
    from app.task_engine.artifacts import validate_manifest_json

    clip_meta: dict[str, dict] = {}
    if has_versioned_envelope:
        gif_clip_ids = []
        for entry in gif_entries:
            if not isinstance(entry, dict):
                raise ValueError("materialize gif_file entry must be an object")
            cid = entry.get("clip_id")
            if not isinstance(cid, str) or not cid:
                raise ValueError("materialize gif_file entry needs a clip_id")
            gif_clip_ids.append(cid)
        if len(set(gif_clip_ids)) != len(gif_clip_ids):
            raise ValueError("materialize envelope has duplicate gif_file clip_id")

        manifest_by_clip: dict[str, dict] = {}
        for entry in gif_manifest_entries:
            if not isinstance(entry, dict):
                raise ValueError(
                    "materialize gif_clip_manifest entry must be an object"
                )
            cid = entry.get("clip_id")
            if not isinstance(cid, str) or not cid:
                raise ValueError(
                    "materialize gif_clip_manifest entry needs a clip_id"
                )
            if cid in manifest_by_clip:
                raise ValueError(
                    f"materialize envelope has duplicate manifest for {cid!r}"
                )
            manifest_by_clip[cid] = entry
        if set(manifest_by_clip) != set(gif_clip_ids):
            missing = sorted(set(gif_clip_ids) - set(manifest_by_clip))
            extra = sorted(set(manifest_by_clip) - set(gif_clip_ids))
            raise ValueError(
                "materialize envelope gif/manifest clip_ids differ: "
                f"missing={missing}, extra={extra}"
            )

        for cid in gif_clip_ids:
            entry = manifest_by_clip[cid]
            path = entry.get("path")
            expected_sha = entry.get("sha256")
            expected_size = entry.get("size_bytes")
            if not isinstance(path, str) or not path:
                raise ValueError(
                    f"gif_clip_manifest for {cid!r} needs a path"
                )
            if (
                not isinstance(expected_sha, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None
            ):
                raise ValueError(
                    f"gif_clip_manifest for {cid!r} needs a lowercase SHA-256"
                )
            if (
                isinstance(expected_size, bool)
                or not isinstance(expected_size, int)
                or expected_size < 0
            ):
                raise ValueError(
                    f"gif_clip_manifest for {cid!r} needs a valid size_bytes"
                )
            try:
                with open(path, "rb") as manifest_file:
                    raw_manifest = manifest_file.read()
            except OSError as exc:
                raise ValueError(
                    f"cannot read gif_clip_manifest for {cid!r}: {exc}"
                ) from exc
            if len(raw_manifest) != expected_size:
                raise ValueError(
                    f"gif_clip_manifest size mismatch for {cid!r}"
                )
            if hashlib.sha256(raw_manifest).hexdigest() != expected_sha:
                raise ValueError(
                    f"gif_clip_manifest SHA-256 mismatch for {cid!r}"
                )
            clip_meta[cid] = validate_manifest_json(
                raw_manifest,
                "gif_clip_manifest",
                expected_stage="gif_clip",
                expected_clip_id=cid,
            )
    else:
        for entry in gif_manifest_entries:
            path = entry.get("path", "")
            if path and os.path.exists(path):
                try:
                    with open(path, "rb") as f:
                        gm = validate_manifest_json(
                            f.read(),
                            "gif_clip_manifest",
                            expected_stage="gif_clip",
                        )
                    cid = gm.get("clip_id", entry.get("clip_id", ""))
                    if cid:
                        clip_meta[cid] = gm
                except OSError:
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
            **meta,
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
                **{
                    field: gm[field]
                    for field in _QUALITY_LINEAGE_FIELDS
                    if field in gm
                },
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

"""Stage-mode plumbing: manifest I/O, run_stage_mode, stage dispatch."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from app.db import init_db
from app.pipeline.config import extract_config
from app.pipeline.timing import _attach_timings, reset_timings
from app.pipeline.vlm_runtime import _attach_live_vlm_base_url


class _TeeIO:
    """Write the same text to a log file and the original stream, flushing both."""

    def __init__(self, log_file, original):
        self._log = log_file
        self._original = original

    def write(self, data):
        self._log.write(data)
        self._log.flush()
        self._original.write(data)
        self._original.flush()

    def flush(self):
        self._log.flush()
        self._original.flush()

    def reconfigure(self, **kwargs):
        reconfigure = getattr(self._original, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(**kwargs)


def run_stage_mode(
    *,
    stage: str,
    video_path: str,
    work_dir: str,
    result_path: str,
    config_path: str,
    input_manifest_path: str | None = None,
    clip_id: str | None = None,
) -> None:
    """Run the adaptive pipeline in stage mode.

    Each stage reads its input manifest from the work directory (written
    by a previous stage), does only its own work, and writes its output
    manifest.  No stage re-executes work done by previous stages.

    Stage mode differs from direct mode:

    * Config is read from *config_path* (a JSON snapshot) instead of
      calling ``load_config()``.
    * Upstream inputs are read from *input_manifest_path* (a JSON file
      mapping artifact kinds to artifact metadata including file paths),
      NOT from directory guessing via ``prior_stage_work_dirs``.
    * All temporary files live under *work_dir*.
    * Export cleanup is **disabled** so previously-registered exports
      from earlier stages are never touched.
    * A machine-readable result JSON is written to *result_path*
      atomically (tmp + rename).
    * stdout/stderr are redirected to a log file under *work_dir*.
    """
    # Load config from the snapshot provided by the worker
    with open(config_path, "r", encoding="utf-8") as f:
        config_data = json.load(f)

    # Phase 3: Normalize config to unified top-level format.
    # Handles both historical config_snapshot wrapper and new flat format.
    from app.quality_lab.config_builder import normalize_task_config
    config_data = normalize_task_config(config_data)

    # Override the global config module so that ``get()`` calls inside
    # helpers (via imported modules) see the correct values.  Restore in
    # finally covering init_db / extract_config / log setup, not only the
    # stage body — otherwise a failure before the inner try leaks the job
    # snapshot into the rest of the process.
    from app.config import swap_config_override

    previous_config = swap_config_override(config_data)
    log_file = None
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    redirected = False
    timings = reset_timings()
    try:
        init_db()

        video_name = os.path.splitext(os.path.basename(video_path))[0]
        FRAMES_DIR = os.path.join(work_dir, "frames")
        EXPORT_DIR = os.path.join(work_dir, "exports", video_name)
        os.makedirs(FRAMES_DIR, exist_ok=True)
        os.makedirs(EXPORT_DIR, exist_ok=True)

        cfg = extract_config(config_data)
        # Stage mode never cleans the export dir
        cfg["clear_output_dir"] = False

        # P0-2: Load input manifest from the path the adapter provides.
        # This replaces the old prior_stage_work_dirs directory guessing.
        input_manifest: dict = {}
        if input_manifest_path and os.path.exists(input_manifest_path):
            with open(input_manifest_path, "r", encoding="utf-8") as f:
                input_manifest = json.load(f)

        # Redirect prints to a line-buffered log and keep the console copy.
        log_path = os.path.join(work_dir, "stage.log")
        log_file = open(log_path, "w", encoding="utf-8", buffering=1)
        sys.stdout = _TeeIO(log_file, old_stdout)
        sys.stderr = _TeeIO(log_file, old_stderr)
        redirected = True

        live = _attach_live_vlm_base_url(cfg, config_data)
        if live:
            print(f"  [stage runtime] vlm_base_url={live}", flush=True)
        with timings.span("stage"):
            output = _run_stage(
                stage,
                video_path=video_path,
                frames_dir=FRAMES_DIR,
                export_dir=EXPORT_DIR,
                work_dir=work_dir,
                cfg=cfg,
                input_manifest=input_manifest,
                clip_id=clip_id,
                config_data=config_data,
            )
    finally:
        if redirected:
            sys.stdout.flush()
            sys.stderr.flush()
            sys.stdout = old_stdout
            sys.stderr = old_stderr
        if log_file is not None:
            log_file.close()
        swap_config_override(previous_config)

    # Build artifact list from the stage handler's explicit output.
    # Phase 2: Each stage handler explicitly returns artifacts with
    # artifact_kind.  We no longer scan directories by extension.
    artifacts: list[dict] = list(output.get("_artifacts", []))

    # Extract scalar metrics from the pipeline output
    metrics: dict[str, int | float | str] = {}
    for k, v in output.items():
        if isinstance(v, (int, float, str)):
            metrics[k] = v

    result = {
        "stage": stage,
        "output_key": output.get("output_key", stage),
        # P0-2: propagate the explicit terminal outcome so the worker can
        # mark materialize needs_attention on unrecoverable publish conflicts.
        "outcome": output.get("outcome", "succeeded"),
        "artifacts": artifacts,
        "metrics": metrics,
    }
    timing_payload = timings.to_dict()
    if timing_payload:
        result["timings"] = timing_payload

    # Write atomically
    tmp_path = result_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, result_path)


_MANIFEST_NAME: dict[str, str] = {
    "discover": "discover_manifest.json",
    "sample": "sample_manifest.json",
    "vlm": "vlm_manifest.json",
    "refine": "refine_manifest.json",
    "synthesize": "synthesize_manifest.json",
    "rank_dedup": "rank_dedup_manifest.json",
    "gif_clip": "gif_clip_manifest.json",
    "materialize": "materialize_manifest.json",
}

_PREV_STAGE: dict[str, str | None] = {
    "discover": None,
    "sample": "discover",
    "vlm": "sample",
    "refine": "vlm",
    "synthesize": "refine",
    "rank_dedup": "synthesize",
    "gif_clip": "rank_dedup",
    "materialize": None,
}


def _load_manifest(work_dir: str, stage_name: str, prior_work_dirs: dict[str, str] | None = None) -> dict:
    """Load the manifest written by *stage_name* from the work directory.

    If *prior_work_dirs* is provided, it maps stage_name to the directory
    where that stage's manifests are stored (used for cross-stage reads).
    """
    manifest_name = _MANIFEST_NAME.get(stage_name, f"{stage_name}_manifest.json")
    search_dir = work_dir
    if prior_work_dirs and stage_name in prior_work_dirs:
        search_dir = prior_work_dirs[stage_name]
    path = os.path.join(search_dir, manifest_name)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_manifest(
    work_dir: str,
    stage_name: str,
    data: dict,
    *,
    include_timings: bool = True,
) -> str:
    """Save *data* as the manifest for *stage_name*, return the path.

    Stage manifests carry a ``timings`` block so throughput is auditable
    per run.  Strictly-shaped side artifacts such as the rank candidate
    ledger opt out.
    """
    if include_timings:
        _attach_timings(data)
    manifest_name = _MANIFEST_NAME.get(stage_name, f"{stage_name}_manifest.json")
    path = os.path.join(work_dir, manifest_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def _make_artifact(path: str, artifact_kind: str, clip_id: str | None = None) -> dict:
    """Build an explicit artifact descriptor for use by the adapter.

    Phase 2: Every artifact must carry an explicit artifact_kind.
    The adapter validates this against the stage's whitelist.
    """
    abs_path = os.path.abspath(path)
    result: dict = {
        "path": abs_path,
        "artifact_kind": artifact_kind,
    }
    if clip_id is not None:
        result["clip_id"] = clip_id
    if os.path.exists(abs_path):
        result["size_bytes"] = os.path.getsize(abs_path)
    return result


def _hash_artifact_id(artifact_kind: str, path: str, stage_id: str = "", clip_id: str | None = None) -> str:
    """Generate a stable artifact_id hash compatible with
    ``app.task_engine.artifacts.make_artifact_id``.

    P1-3: Used by stage handlers to embed artifact_ids in manifests
    for cross-referencing by downstream stages.
    """
    from app.task_engine.fingerprints import canonical_hash
    return canonical_hash({
        "stage_id": stage_id,
        "artifact_kind": artifact_kind,
        "clip_id": clip_id or "",
        "path": Path(path).as_posix(),
    })


def _read_upstream_manifest(inputs: dict, artifact_kind: str, stage: str) -> dict:
    """Read an upstream manifest from the input dict (P0-2 protocol).

    Looks up *artifact_kind* in *inputs*, reads the first artifact's file,
    and validates the manifest structure via ``validate_manifest_json``.

    P1-2: All validation errors raise ``ValueError`` (which the worker
    converts to a structured ``StageError``).  Errors include:
    missing fields, wrong stage, wrong clip_id, unsupported version,
    empty JSON, wrong encoding, manifest/GIF SHA mismatch.

    Raises ``ValueError`` if missing, invalid, or inconsistent.
    """
    from app.task_engine.artifacts import validate_manifest_json

    entries = inputs.get(artifact_kind, [])
    if not entries:
        raise ValueError(
            f"No {artifact_kind} entry in input manifest for stage {stage!r}"
        )
    ref = entries[0]
    path = ref.get("path", "")
    if not path or not os.path.exists(path):
        raise ValueError(f"Input artifact file not found: {path}")

    # P1-2: Read raw bytes and validate via shared validator.
    try:
        with open(path, "rb") as f:
            raw_bytes = f.read()
    except (UnicodeDecodeError, OSError) as exc:
        raise ValueError(
            f"Cannot read manifest file {path}: {exc}"
        ) from exc

    if not raw_bytes:
        raise ValueError(f"Empty manifest file: {path}")
    recorded_sha = ref.get("sha256")
    if isinstance(recorded_sha, str) and recorded_sha:
        actual_sha = hashlib.sha256(raw_bytes).hexdigest()
        if actual_sha != recorded_sha:
            raise ValueError(f"{artifact_kind} SHA-256 mismatch")
    recorded_size = ref.get("size_bytes")
    if (
        isinstance(recorded_size, int)
        and not isinstance(recorded_size, bool)
        and len(raw_bytes) != recorded_size
    ):
        raise ValueError(f"{artifact_kind} size mismatch")

    # Determine the expected producer stage from the artifact kind.
    _EXPECTED_PRODUCER: dict[str, str] = {
        "discover_manifest": "discover",
        "sample_manifest": "sample",
        "vlm_manifest": "vlm",
        "refine_manifest": "refine",
        "synthesize_manifest": "synthesize",
        "rank_dedup_manifest": "rank_dedup",
        "gif_clip_manifest": "gif_clip",
    }
    expected_producer = _EXPECTED_PRODUCER.get(artifact_kind)
    expected_clip_id = ref.get("clip_id") or None

    # P1-2: Call shared validator with expected stage, clip_id.
    lineage_kwargs = {}
    if artifact_kind == "rank_dedup_manifest":
        lineage_kwargs["require_external_quality_ledger"] = True
        ledger_entries = inputs.get("rank_candidate_ledger", [])
        upstream_entries = inputs.get("synthesize_manifest", [])
        if ledger_entries and upstream_entries:
            ledger_ref = ledger_entries[0]
            ledger_path = ledger_ref.get("path", "")
            if not ledger_path or not os.path.exists(ledger_path):
                raise ValueError(f"Input artifact file not found: {ledger_path}")
            with open(ledger_path, "rb") as ledger_file:
                ledger_bytes = ledger_file.read()
            lineage_kwargs.update({
                "candidate_ledger_bytes": ledger_bytes,
                "candidate_ledger_ref": ledger_ref,
                "upstream_artifact_ref": upstream_entries[0],
            })

    data = validate_manifest_json(
        raw_bytes,
        artifact_kind=artifact_kind,
        expected_stage=expected_producer,
        expected_clip_id=expected_clip_id,
        **lineage_kwargs,
    )

    # P1-2: For gif_clip_manifest, additionally verify SHA
    # matches the gif_file artifact.
    if artifact_kind == "gif_clip_manifest":
        gif_entries = inputs.get("gif_file", [])
        matching_gif = None
        for ge in gif_entries:
            if ge.get("clip_id") == expected_clip_id:
                matching_gif = ge
                break
        if matching_gif:
            manifest_sha = data.get("sha256")
            if manifest_sha and manifest_sha != matching_gif.get("sha256"):
                raise ValueError(
                    f"gif_clip_manifest SHA-256 mismatch for clip "
                    f"{expected_clip_id!r}: manifest says {manifest_sha[:16]}..., "
                    f"gif_file says {str(matching_gif.get('sha256'))[:16]}..."
                )

    return data


def _load_input_manifest(work_dir: str, stage: str, prior_work_dirs: dict[str, str] | None = None) -> dict:
    """Load the manifest from the stage immediately preceding *stage*.

    Uses *prior_work_dirs* to locate manifests from previous stages'
    work directories.  Raises ValueError if the manifest is missing
    or invalid when the stage requires one.
    """
    prev = _PREV_STAGE.get(stage)
    if prev is None:
        return {}
    search_dir = work_dir
    if prior_work_dirs and prev in prior_work_dirs:
        search_dir = prior_work_dirs[prev]
    manifest_name = _MANIFEST_NAME.get(prev, f"{prev}_manifest.json")
    path = os.path.join(search_dir, manifest_name)
    if not os.path.exists(path):
        raise ValueError(
            f"Input manifest not found for stage {stage!r}: "
            f"expected {prev}_manifest.json in {search_dir}"
        )
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("stage") != prev:
        raise ValueError(
            f"Manifest at {path} has wrong stage: "
            f"expected {prev!r}, got {data.get('stage')!r}"
        )
    schema_version = data.get("schema_version")
    if schema_version is None:
        raise ValueError(f"Manifest at {path} has no schema_version")
    return data


def _run_stage(
    stage: str,
    *,
    video_path: str,
    frames_dir: str,
    export_dir: str,
    work_dir: str,
    cfg: dict,
    input_manifest: dict | None = None,
    clip_id: str | None = None,
    config_data: dict | None = None,
) -> dict:
    """Dispatch to the correct per-stage handler.

    Each handler reads its input from the upstream artifact paths
    provided in *input_manifest* (P0-2), NOT from directory guessing
    via ``prior_stage_work_dirs``.

    Stage modules are imported here (not at module level) because they
    import the manifest helpers below; this keeps ``stage_io`` importable
    without the full stage closure.
    """
    inputs = input_manifest or {}
    if stage == "discover":
        from app.pipeline.stages.discover import _stage_discover

        return _stage_discover(video_path, work_dir, cfg)
    elif stage == "sample":
        from app.pipeline.stages.sample import _stage_sample

        return _stage_sample(video_path, frames_dir, work_dir, cfg, inputs, config_data)
    elif stage == "vlm":
        from app.pipeline.stages.vlm import _stage_vlm

        return _stage_vlm(frames_dir, work_dir, cfg, inputs, config_data)
    elif stage == "refine":
        from app.pipeline.stages.refine import _stage_refine

        return _stage_refine(video_path, frames_dir, work_dir, cfg, inputs, config_data)
    elif stage == "synthesize":
        from app.pipeline.stages.synthesize import _stage_synthesize

        return _stage_synthesize(work_dir, cfg, inputs)
    elif stage == "rank_dedup":
        from app.pipeline.stages.rank_dedup import _stage_rank_dedup

        return _stage_rank_dedup(
            video_path, export_dir, work_dir, cfg, inputs, config_data
        )
    elif stage == "gif_clip":
        from app.pipeline.stages.gif_clip import _stage_gif_clip

        return _stage_gif_clip(video_path, frames_dir, export_dir, work_dir, cfg, clip_id, inputs)
    elif stage == "materialize":
        from app.pipeline.stages.materialize import _stage_materialize

        return _stage_materialize(video_path, export_dir, work_dir, cfg, inputs, config_data)
    else:
        raise ValueError(f"Unknown stage: {stage}")

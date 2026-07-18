from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app.task_engine.artifacts import make_artifact_id, STAGE_ARTIFACT_KINDS
from app.task_engine.fingerprints import sha256_file
from app.task_engine.models import ArtifactRef, StageName
from app.task_engine.stages import StageAdapter, StageContext, StageResult

_ADAPTIVE_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "test_video_adaptive.py"
)


def run_adaptive_stage(
    stage_name: StageName,
    *,
    video: Path,
    work_dir: Path,
    config_snapshot: Path,
    input_manifest: Path | None = None,
    job_id: str = "",
    video_id: str = "",
    stage_id: str = "",
    clip_id: str | None = None,
    _runner=None,
) -> StageResult:
    """Invoke test_video_adaptive.py in stage mode and return a StageResult.

    Parameters
    ----------
    stage_name:
        Which pipeline stage to label the result for.
    video:
        Path to the source video file.
    work_dir:
        Isolated working directory for this stage invocation.
    config_snapshot:
        Path to a JSON file containing the full config dict that the
        stage-mode script should use (replaces load_config/get).
    input_manifest:
        Path to a JSON file describing upstream inputs (P0-2).
        Passed to the script via ``--task-input-manifest``.
    job_id, video_id, stage_id:
        Identifiers embedded in produced ArtifactRef records.
    clip_id:
        Required for the ``gif_clip`` stage; ignored otherwise.
    _runner:
        Test hook — callable with the same signature as ``subprocess.run``.
        Must be None in production (resolved to ``subprocess.run`` at
        call time to support monkeypatching).
    """
    if _runner is None:
        _runner = subprocess.run
    work_dir.mkdir(parents=True, exist_ok=True)
    result_file = work_dir / f"result_{stage_name}.json"

    cmd = [
        sys.executable,
        str(_ADAPTIVE_SCRIPT),
        "--task-stage",
        stage_name,
        "--video",
        str(video),
        "--task-work-dir",
        str(work_dir),
        "--task-result",
        str(result_file),
        "--task-config",
        str(config_snapshot),
    ]
    if input_manifest is not None and input_manifest.exists():
        cmd.extend(["--task-input-manifest", str(input_manifest)])
    if clip_id:
        cmd.extend(["--clip-id", clip_id])

    _runner(cmd, check=True, capture_output=True, timeout=3600)

    if not result_file.exists():
        raise FileNotFoundError(
            f"Stage-mode script did not produce result at {result_file}"
        )

    with open(result_file, "r", encoding="utf-8") as f:
        result_data = json.load(f)

    # Build provenance from the config snapshot (lazy import to avoid
    # circular dependency: provenance -> task_engine.fingerprints ->
    # task_engine.__init__ -> adaptive_adapter -> ... -> provenance).
    from app.services.provenance import current_provenance, provenance_to_json

    config: dict = {}
    if config_snapshot.exists():
        with open(config_snapshot, "r", encoding="utf-8") as f:
            config = json.load(f)
    prov = current_provenance(config, {stage_name: "1"})
    prov_json = provenance_to_json(prov)

    # Determine the expected artifact kinds for this stage.
    expected_kinds = set(STAGE_ARTIFACT_KINDS.get(stage_name, ()))

    # Build ArtifactRef objects from the paths the script reported.
    artifacts: list[ArtifactRef] = []
    for art_info in result_data.get("artifacts", []):
        art_path = Path(art_info["path"])
        if art_path.exists():
            actual_sha256 = sha256_file(art_path)
            actual_size = art_path.stat().st_size
        else:
            actual_sha256 = art_info.get("sha256", "")
            actual_size = art_info.get("size_bytes", 0)

        # Phase 2: Require explicit artifact_kind from the script.
        art_kind = art_info.get("artifact_kind", "")

        # Phase 2: Reject missing or unknown artifact_kind.
        if not art_kind or art_kind == "generic":
            raise ValueError(
                f"Stage {stage_name!r} artifact at {art_path} has no explicit "
                f"artifact_kind.  Stage-mode scripts must declare an "
                f"artifact_kind from: {sorted(expected_kinds)}"
            )

        # Phase 2: Reject artifact_kind not in this stage's whitelist.
        if art_kind not in expected_kinds:
            raise ValueError(
                f"Stage {stage_name!r} cannot produce artifact_kind={art_kind!r}. "
                f"Allowed kinds: {sorted(expected_kinds)}"
            )

        # Phase 2: Validate path is not a control file (config, input manifest, log).
        _fname = art_path.name.lower()
        if _fname in ("config_snapshot.json", "input_manifest.json", "stage.log"):
            raise ValueError(
                f"Stage {stage_name!r} artifact at {art_path} is a control file "
                f"({_fname}) and must not be registered as an artifact."
            )
        if _fname.startswith("result_") and _fname.endswith((".json", ".tmp")):
            raise ValueError(
                f"Stage {stage_name!r} artifact at {art_path} is a result file "
                f"and must not be registered as an artifact."
            )

        artifact_id = make_artifact_id(
            stage_id=stage_id,
            artifact_kind=art_kind,
            clip_id=art_info.get("clip_id", clip_id),
            normalized_path=str(art_path),
        )

        ref = ArtifactRef(
            artifact_id=artifact_id,
            job_id=job_id,
            video_id=video_id,
            stage_name=stage_name,
            clip_id=art_info.get("clip_id", clip_id),
            path=str(art_path),
            sha256=actual_sha256,
            size_bytes=actual_size,
            provenance_json=prov_json,
            stage_id=stage_id,
            artifact_kind=art_kind,
        )
        artifacts.append(ref)

    from app.task_engine.stages import normalize_outcome

    return StageResult(
        output_key=result_data.get("output_key", stage_name),
        artifacts=tuple(artifacts),
        metrics=result_data.get("metrics", {}),
        outcome=normalize_outcome(result_data.get("outcome", "succeeded")),
    )


class AdaptivePipelineAdapter:
    """Concrete StageAdapter that delegates to ``run_adaptive_stage``."""

    def __init__(self, stage_name: StageName, version: str = "1") -> None:
        self._name = stage_name
        self._version = version

    @property
    def name(self) -> StageName:
        return self._name

    @property
    def version(self) -> str:
        return self._version

    def run(self, context: StageContext) -> StageResult:
        # Persist the context config so the subprocess can read it.
        # P1-3: Inject stage_id and clip_id into the config so the
        # stage script can compute stable artifact_ids for manifests.
        config_for_script = dict(context.config)
        config_for_script["_stage_id"] = context.stage_id
        if context.clip_id:
            config_for_script["_clip_id"] = context.clip_id

        config_snap = context.work_dir / "config_snapshot.json"
        context.work_dir.mkdir(parents=True, exist_ok=True)
        with open(config_snap, "w", encoding="utf-8") as f:
            json.dump(config_for_script, f, ensure_ascii=False, default=str)

        # P0-2: Serialize stage inputs to input_manifest.json.
        # For the materialize stage, use the versioned envelope format
        # (includes artifacts + stage_statuses).  For all other stages,
        # use the flat kind->artifacts mapping.
        input_manifest_path: Path | None = None
        if context.inputs is not None:
            input_manifest_path = context.work_dir / "input_manifest.json"
            if self._name == "materialize":
                # Use the versioned envelope built by the worker.
                envelope = context.config.get("_materialize_envelope") or {}
                if not envelope:
                    # Fallback: build a minimal envelope from inputs.
                    input_data: dict[str, list[dict]] = {}
                    for input_kind, refs in context.inputs.items():
                        input_data[input_kind] = [
                            {
                                "artifact_id": r.artifact_id,
                                "stage_id": r.stage_id,
                                "artifact_kind": r.artifact_kind,
                                "clip_id": r.clip_id,
                                "path": r.path,
                                "sha256": r.sha256,
                                "size_bytes": r.size_bytes,
                            }
                            for r in refs
                        ]
                    envelope = {
                        "schema_version": 1,
                        "stage": "materialize",
                        "artifacts": input_data,
                        "stage_statuses": [],
                    }
                with open(input_manifest_path, "w", encoding="utf-8") as f:
                    json.dump(envelope, f, ensure_ascii=False, indent=2)
            else:
                input_data: dict[str, list[dict]] = {}
                for input_kind, refs in context.inputs.items():
                    input_data[input_kind] = [
                        {
                            "artifact_id": r.artifact_id,
                            "stage_id": r.stage_id,
                            "artifact_kind": r.artifact_kind,
                            "clip_id": r.clip_id,
                            "path": r.path,
                            "sha256": r.sha256,
                            "size_bytes": r.size_bytes,
                        }
                        for r in refs
                    ]
                with open(input_manifest_path, "w", encoding="utf-8") as f:
                    json.dump(input_data, f, ensure_ascii=False, indent=2)

        return run_adaptive_stage(
            self._name,
            video=context.video_path,
            work_dir=context.work_dir,
            config_snapshot=config_snap,
            input_manifest=input_manifest_path,
            job_id=context.job_id,
            video_id=context.video_id,
            stage_id=context.stage_id,
            clip_id=context.clip_id,
        )

from __future__ import annotations

import json
import sqlite3

from app.quality_lab.models import ExperimentConfig
from app.services.provenance import Provenance, provenance_to_json
from app.task_engine.fingerprints import canonical_hash, canonical_json


def snapshot_experiment_config(
    config: dict,
    provenance: Provenance,
) -> ExperimentConfig:
    """Create an immutable snapshot of an experiment configuration.

    Returns an ``ExperimentConfig`` whose ``config_id`` is the canonical
    hash of *config*, and whose ``config_json`` / ``provenance_json``
    fields hold canonical (sorted-key) serialisations.

    ``snapshot_experiment_config`` is a pure-data function -- it does **not**
    write to any database.  The caller is responsible for persisting the
    returned object.
    """
    config_id = canonical_hash(config)
    return ExperimentConfig(
        config_id=config_id,
        config_json=canonical_json(config),
        provenance_json=provenance_to_json(provenance),
    )


def provenance_for_candidate(
    conn: sqlite3.Connection,
    candidate_id: str,
) -> dict:
    """Resolve the full provenance trail for a materialised candidate GIF.

    The function traces from ``candidate_gifs`` through the task-engine
    tables (``task_jobs``, ``task_videos``, ``task_stages``,
    ``task_artifacts``) to produce a single dict containing every
    provenance field.

    The caller's *conn* **must** have both the preference schema
    (``candidate_gifs``) and the task-engine schema (``task_jobs``,
    ``task_videos``, ``task_stages``, ``task_artifacts``) accessible.
    In practice this means either using the task-state database and
    ``ATTACH``-ing the library database, or creating a combined in-memory
    database for testing.

    Returns
    -------
    dict
        Keys include ``candidate_id``, ``source_run_id``,
        ``source_run_candidate_id``, ``source_video_sha256``,
        ``source_fingerprint``, ``artifact_id``, ``git_commit``,
        ``config_hash``, ``model_versions``, ``prompt_hashes``,
        ``stage_versions``, ``task_job``, ``task_video``,
        ``task_stages``, ``task_artifacts``, and ``provenance_json``.

    Raises
    ------
    ValueError
        If *candidate_id* does not exist in ``candidate_gifs``.
    """
    row = conn.execute(
        "SELECT source_run_id, source_run_candidate_id, source_video_sha256, "
        "source_video_path, artifact_id, provenance_json, status, final_score "
        "FROM candidate_gifs WHERE candidate_id = ?",
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Candidate not found: {candidate_id}")

    candidate = dict(row)
    source_run_id = candidate["source_run_id"]

    # 1. Parse stored provenance (present for post-migration candidates).
    stored: dict | None = None
    pj = candidate.get("provenance_json")
    if pj:
        try:
            stored = json.loads(pj)
        except (json.JSONDecodeError, TypeError):
            stored = None

    # 2. Resolve the originating task job.
    job = conn.execute(
        "SELECT job_id, directory, config_json, status, created_at "
        "FROM task_jobs WHERE job_id = ?",
        (source_run_id,),
    ).fetchone()
    task_job = dict(job) if job else None

    # 3. Resolve the source video for that job.
    video = conn.execute(
        "SELECT video_id, path, fingerprint, status "
        "FROM task_videos WHERE job_id = ?",
        (source_run_id,),
    ).fetchone()
    task_video = dict(video) if video else None

    # 4. Resolve stages and artifacts for that video.
    task_stages: list[dict] = []
    task_artifacts: list[dict] = []
    if video:
        vid = video["video_id"]
        for s in conn.execute(
            "SELECT stage_id, stage_name, clip_id, status "
            "FROM task_stages WHERE video_id = ?",
            (vid,),
        ):
            task_stages.append(dict(s))
        for a in conn.execute(
            "SELECT artifact_id, stage_name, clip_id, path, sha256 "
            "FROM task_artifacts WHERE video_id = ?",
            (vid,),
        ):
            task_artifacts.append(dict(a))

    return {
        "candidate_id": candidate_id,
        "source_run_id": source_run_id,
        "source_run_candidate_id": candidate["source_run_candidate_id"],
        "source_video_sha256": candidate["source_video_sha256"],
        "source_video_path": candidate["source_video_path"],
        "source_fingerprint": task_video["fingerprint"] if task_video else None,
        "artifact_id": candidate.get("artifact_id"),
        "git_commit": stored.get("git_commit") if stored else None,
        "config_hash": stored.get("config_hash") if stored else None,
        "model_versions": stored.get("model_versions", {}) if stored else {},
        "prompt_hashes": stored.get("prompt_hashes", {}) if stored else {},
        "stage_versions": stored.get("stage_versions", {}) if stored else {},
        "task_job": task_job,
        "task_video": task_video,
        "task_stages": task_stages,
        "task_artifacts": task_artifacts,
        "provenance_json": candidate.get("provenance_json"),
    }

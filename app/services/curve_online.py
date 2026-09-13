"""Curve online execution: fixed version resolution, shadow/active routing, and candidate scoring.

Ensures that unqualified or shadow curve models never change production ranking outputs.
All three score routes (baseline / nominal / effective) flow through the one
shared production scorer ``score_preference_export_candidate`` -- there is no
second linear blend formula in this codebase.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.services.reranker import score_preference_export_candidate

CURVE_NOMINAL_WEIGHT = 0.10


def _canonical_config_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def curve_algorithm_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """The frozen curve-relevant algorithm parameters of one video run."""
    return {
        "base_score_weight": float(config.get("base_score_weight", 0.50)),
        "preference_score_weight": float(config.get("preference_score_weight", 0.50)),
        "curve_nominal_weight": CURVE_NOMINAL_WEIGHT,
        "curve_feature_version": str(
            config.get("curve_feature_version", "curve-source-v1")
        ),
    }


@dataclass(frozen=True)
class CurveRuntimeSnapshot:
    """Per-video frozen curve execution authority.

    Resolved once per video by :func:`load_curve_runtime_snapshot`; mid-video
    profile or deployment changes cannot split a batch.
    """

    mode: str
    deployment_revision: int | None
    model_version: str | None
    qualification_id: str | None
    profile_version: str | None
    algorithm_config_hash: str
    feature_version: str
    extractor_sha256: str
    confidence: float
    qualified: bool = False
    compatible: bool = False
    deployment_available: bool = True
    nominal_weight: float = CURVE_NOMINAL_WEIGHT
    base_score_weight: float = 0.50
    preference_score_weight: float = 0.50
    quality_pass: bool = True
    reranker: object | None = None
    inactive_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class CurveCandidateScores:
    route: str
    baseline_score: float
    nominal_score: float | None
    effective_score: float | None
    production_score: float
    inactive_reasons: tuple[str, ...]


def curve_route(
    mode: str,
    qualified: bool,
    confidence: float,
    compatible: bool,
    quality_pass: bool,
) -> str:
    """Determine the runtime curve execution route: BASELINE, SHADOW, or ACTIVE."""
    mode_clean = str(mode or "off").strip().lower()

    if mode_clean in ("off", "disabled"):
        return "BASELINE"

    if mode_clean == "shadow":
        return "SHADOW"

    if mode_clean == "active":
        if (
            qualified
            and isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and confidence > 0.0
            and math.isfinite(confidence)
            and compatible
            and quality_pass
        ):
            return "ACTIVE"
        return "BASELINE"

    return "BASELINE"


def _snap_get(snapshot: Any, key: str, default: Any = None) -> Any:
    if isinstance(snapshot, Mapping):
        return snapshot.get(key, default)
    return getattr(snapshot, key, default)


def _profile_rows_sha256(profile_rows: Any) -> str:
    """Content fingerprint over normalized profile rows (the exact
    representation the qualification snapshot freezes)."""
    return hashlib.sha256(
        json.dumps(profile_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _live_profile_rows(conn: Any, profile_version: str) -> list[Any]:
    """The live profile rows in the qualification snapshot's normalized
    representation, so qualification and live content compare byte-for-byte."""
    from app.services.preference_profile_snapshot import _profile_rows

    return _profile_rows(conn, profile_version=profile_version)


def load_curve_runtime_snapshot(
    conn: Any,
    config: Mapping[str, Any],
) -> CurveRuntimeSnapshot:
    """Resolve the curve execution authority once per video.

    Reads the current profile pointer and the active deployment exactly once;
    later mid-video changes cannot affect this batch. Without an active
    deployment an ``active`` request degrades to BASELINE with
    ``deployment_unavailable``.
    """
    curve_enabled = bool(config.get("curve_preference_enabled", False))
    mode = str(config.get("curve_preference_mode", "off") or "off").strip().lower()
    if not curve_enabled or mode in ("", "disabled"):
        mode = "off"

    algorithm_cfg = curve_algorithm_config(config)
    algorithm_config_hash = _canonical_config_hash(algorithm_cfg)
    base_score_weight = float(algorithm_cfg["base_score_weight"])
    preference_score_weight = float(algorithm_cfg["preference_score_weight"])

    profile_version: str | None = None
    deployment_revision: int | None = None
    model_version: str | None = None
    qualification_id: str | None = None
    model_status: str | None = None
    qualification_status: str | None = None
    feature_version = str(algorithm_cfg["curve_feature_version"])
    extractor_sha256 = ""
    confidence = 0.0
    deployment_config_hash = ""
    inactive: list[str] = []

    if conn is not None:
        row = conn.execute(
            "SELECT profile_version FROM preference_profile_current WHERE slot='current'"
        ).fetchone()
        if row is not None:
            profile_version = row[0]

        deployment = _active_deployment_row(conn)
        if deployment is not None:
            deployment_revision = int(deployment[0])
            deployment_row = deployment[1]
            model_version = deployment_row["model_version"]
            qualification_id = deployment_row["qualification_id"]
            deployment_config_hash = str(deployment_row["config_hash"] or "")
            deployment_profile_version = deployment_row.get("profile_version")
            deployment_profile_state_sha = deployment_row.get(
                "profile_snapshot_sha256"
            )

            model_row = conn.execute(
                """
                SELECT status, feature_version, extractor_sha256, curve_model_confidence
                FROM curve_preference_models WHERE model_version = ?
                """,
                (model_version,),
            ).fetchone()
            if model_row is not None:
                model_status = model_row[0]
                feature_version = str(model_row[1] or feature_version)
                extractor_sha256 = str(model_row[2] or "")
                model_confidence = model_row[3]
                if (
                    isinstance(model_confidence, (int, float))
                    and not isinstance(model_confidence, bool)
                    and math.isfinite(float(model_confidence))
                ):
                    confidence = float(model_confidence)
            qual_row = conn.execute(
                "SELECT status FROM curve_preference_qualification_runs "
                "WHERE qualification_id = ?",
                (qualification_id,),
            ).fetchone()
            if qual_row is not None:
                qualification_status = qual_row[0]

    deployment_available = deployment_revision is not None
    if mode == "active" and not deployment_available:
        inactive.append("deployment_unavailable")

    compatible = True
    if deployment_available:
        if model_status != "PUBLISHED":
            compatible = False
            inactive.append("model_not_published")
        if qualification_status != "QUALIFIED":
            compatible = False
            inactive.append("qualification_not_qualified")
        if not (0.0 < confidence <= 1.0):
            compatible = False
            inactive.append("confidence_out_of_range")
        if deployment_config_hash != algorithm_config_hash:
            compatible = False
            inactive.append("algorithm_config_mismatch")
        if not extractor_sha256:
            compatible = False
            inactive.append("extractor_sha256_missing")
        # Profile binding: ACTIVE only runs on the exact profile identity the
        # qualification froze. A missing identity, a newer published profile,
        # or a same-version content rebuild all fail closed to BASELINE until
        # re-qualification.
        if not deployment_profile_version or not deployment_profile_state_sha:
            # Profile identity = version AND content fingerprint; either
            # missing means the deployment is not verifiably bound.
            compatible = False
            inactive.append("profile_identity_missing")
        elif str(profile_version or "") != str(deployment_profile_version):
            compatible = False
            inactive.append("profile_version_mismatch")
        else:
            live_state = _profile_rows_sha256(
                _live_profile_rows(conn, str(profile_version))
            )
            if live_state != str(deployment_profile_state_sha):
                compatible = False
                inactive.append("profile_content_mismatch")

    qualified = bool(
        deployment_available
        and model_status == "PUBLISHED"
        and qualification_status == "QUALIFIED"
        and compatible
    )

    return CurveRuntimeSnapshot(
        mode=mode,
        deployment_revision=deployment_revision,
        model_version=model_version,
        qualification_id=qualification_id,
        profile_version=profile_version,
        algorithm_config_hash=algorithm_config_hash,
        feature_version=feature_version,
        extractor_sha256=extractor_sha256,
        confidence=confidence,
        qualified=qualified,
        compatible=compatible,
        deployment_available=deployment_available,
        nominal_weight=CURVE_NOMINAL_WEIGHT,
        base_score_weight=base_score_weight,
        preference_score_weight=preference_score_weight,
        quality_pass=bool(config.get("quality_pass", True)),
        reranker=config.get("reranker"),
        inactive_reasons=tuple(inactive),
    )


def _score_route(
    *,
    reranker: Any,
    candidate_vector: Any,
    gif_worthiness: float,
    scenario_keys: list[str],
    profile_version: str | None,
    base_score: float,
    base_score_weight: float,
    preference_score_weight: float,
    curve_probability: float | None,
    curve_model_version: str | None,
    curve_model_confidence: float | None,
) -> dict[str, Any]:
    return score_preference_export_candidate(
        reranker=reranker,
        candidate_vector=candidate_vector,
        gif_worthiness=gif_worthiness,
        scenario_keys=scenario_keys,
        profile_version=profile_version,
        base_export_score=float(base_score),
        base_score_weight=float(base_score_weight),
        preference_score_weight=float(preference_score_weight),
        curve_probability=curve_probability,
        curve_model_version=curve_model_version,
        curve_model_confidence=curve_model_confidence,
    )


def score_curve_candidate(
    conn: Any,
    snapshot: Any,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Score one candidate three ways under the frozen snapshot.

    Baseline is the true production score of the same candidate, profile, and
    weights with the curve component disabled; nominal re-scores with the
    curve component at confidence 1.0; effective uses the qualification
    confidence. All three routes call the shared production scorer, so every
    number lives on the production final-score scale.
    """
    mode = str(_snap_get(snapshot, "mode", "off"))
    qualified = bool(_snap_get(snapshot, "qualified", False))
    confidence = float(_snap_get(snapshot, "confidence", 0.0) or 0.0)
    compatible = bool(_snap_get(snapshot, "compatible", False))
    quality_pass = bool(_snap_get(snapshot, "quality_pass", True))
    deployment_available = bool(_snap_get(snapshot, "deployment_available", True))
    profile_version = _snap_get(snapshot, "profile_version")
    base_score_weight = float(_snap_get(snapshot, "base_score_weight", 0.50))
    preference_score_weight = float(
        _snap_get(snapshot, "preference_score_weight", 0.50)
    )
    model_version = _snap_get(snapshot, "model_version")
    inactive_reasons = list(_snap_get(snapshot, "inactive_reasons", ()) or ())

    route = curve_route(
        mode=mode,
        qualified=qualified,
        confidence=confidence,
        compatible=compatible,
        quality_pass=quality_pass,
    )
    if route == "ACTIVE" and not deployment_available:
        route = "BASELINE"
        if "deployment_unavailable" not in inactive_reasons:
            inactive_reasons.append("deployment_unavailable")

    base_score = candidate.get("base_score")
    if base_score is None:
        base_score = (
            candidate.get("final_score") or candidate.get("gif_worthiness") or 0.0
        )
    base_score = float(base_score)
    gif_worthiness = float(candidate.get("gif_worthiness") or 0.0)
    scenario_keys = list(candidate.get("scenario_keys") or [])
    candidate_vector = candidate.get("candidate_vector")
    curve_probability = candidate.get("curve_probability")

    reranker = _snap_get(snapshot, "reranker")
    if reranker is None and conn is not None:
        from app.services.reranker import PreferenceReranker

        reranker = PreferenceReranker(conn)

    common = {
        "reranker": reranker,
        "candidate_vector": candidate_vector,
        "gif_worthiness": gif_worthiness,
        "scenario_keys": scenario_keys,
        "profile_version": profile_version,
        "base_score": base_score,
        "base_score_weight": base_score_weight,
        "preference_score_weight": preference_score_weight,
    }

    baseline_result = _score_route(
        **common,
        curve_probability=None,
        curve_model_version=None,
        curve_model_confidence=None,
    )
    baseline_score = float(baseline_result["final_score"])

    if curve_probability is None:
        if "curve_evidence_unavailable" not in inactive_reasons:
            inactive_reasons.append("curve_evidence_unavailable")
        return {
            "route": route,
            "baseline_score": baseline_score,
            "nominal_score": None,
            "effective_score": None,
            "production_score": baseline_score,
            "final_score": baseline_score,
            "nominal_shadow_score": None,
            "effective_shadow_score": None,
            "profile_score": baseline_result.get("profile_score"),
            "score_profile_version": baseline_result.get("score_profile_version"),
            "inactive_reasons": inactive_reasons,
        }

    nominal_result = _score_route(
        **common,
        curve_probability=float(curve_probability),
        curve_model_version=model_version,
        curve_model_confidence=1.0,
    )
    effective_result = _score_route(
        **common,
        curve_probability=float(curve_probability),
        curve_model_version=model_version,
        curve_model_confidence=confidence,
    )
    nominal_score = float(nominal_result["final_score"])
    effective_score = float(effective_result["final_score"])

    production_score = effective_score if route == "ACTIVE" else baseline_score
    return {
        "route": route,
        "baseline_score": baseline_score,
        "nominal_score": nominal_score,
        "effective_score": effective_score,
        "production_score": production_score,
        "final_score": production_score,
        "nominal_shadow_score": nominal_score,
        "effective_shadow_score": effective_score,
        "profile_score": effective_result.get("profile_score"),
        "score_profile_version": effective_result.get("score_profile_version"),
        "inactive_reasons": inactive_reasons if route == "BASELINE" else [],
    }


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def validate_deployment(
    model_status: str,
    qualified: bool,
    config_matches: bool,
) -> list[str]:
    """Validate whether a curve preference model meets requirements for active deployment."""
    reasons: list[str] = []
    if model_status != "PUBLISHED":
        reasons.append("model_not_published")
    if not qualified:
        reasons.append("model_not_qualified")
    if not config_matches:
        reasons.append("config_mismatch")
    return reasons


@dataclass(frozen=True)
class DeploymentResult:
    status: str
    revision: int | None
    reasons: tuple[str, ...]


def _current_pointer_revision(conn: Any) -> int | None:
    row = conn.execute(
        "SELECT revision FROM curve_model_deployment_current WHERE slot='current'"
    ).fetchone()
    return int(row[0]) if row else None


def _active_deployment_row(conn: Any) -> tuple[int, dict[str, Any]] | None:
    """Resolve the active deployment: current pointer first, legacy is_active
    row as a one-way fallback for databases predating the pointer table.

    Returns ``(revision, row_dict)`` where row_dict always carries
    model_version / qualification_id / config_hash.
    """
    revision = _current_pointer_revision(conn)
    if revision is not None:
        row = conn.execute(
            "SELECT model_version, qualification_id, config_hash, extractor_sha256, "
            "is_active, qualification_bundle_path, qualification_bundle_sha256, "
            "profile_version, profile_snapshot_sha256 "
            "FROM curve_model_deployments WHERE revision = ?",
            (revision,),
        ).fetchone()
        if row is not None and int(row[4] or 0) == 1:
            return revision, {
                "model_version": row[0],
                "qualification_id": row[1],
                "config_hash": row[2],
                "profile_version": row[7],
                "profile_snapshot_sha256": row[8],
            }
        return None
    row = conn.execute(
        "SELECT revision, model_version, qualification_id, config_hash, "
        "is_active FROM curve_model_deployments WHERE is_active = 1 "
        "ORDER BY revision DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), {
        "model_version": row[1],
        "qualification_id": row[2],
        "config_hash": row[3],
        "profile_version": None,
        "profile_snapshot_sha256": None,
    }


def get_current_deployment_revision(conn: Any) -> int:
    """Return the current deployment revision (pointer, else MAX revision)."""
    pointer = _current_pointer_revision(conn)
    if pointer is not None:
        return pointer
    row = conn.execute("SELECT MAX(revision) FROM curve_model_deployments").fetchone()
    if row and row[0] is not None:
        return int(row[0])
    return 0


def _replay_qualification_scores(
    conn: Any,
    bundle: Any,
    run_dir: Path,
) -> list[str]:
    """Recompute baseline/nominal/effective from the frozen bundle artifacts
    and compare row-by-row with the qualification score files."""
    import numpy as np

    from app.services.curve_preference import load_curve_model, curve_probability
    from app.services.curve_preference_qualification import score_evidence_from_summary

    reasons: list[str] = []
    input_manifest = bundle.input_manifest
    model_version = str(input_manifest.get("model_version"))
    try:
        model = load_curve_model(
            conn, model_version, allowed_statuses=frozenset({"PUBLISHED"})
        )
    except ValueError:
        return ["model_not_published"]

    def jsonl(name: str) -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for line in (run_dir / name).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    profile_snapshot = json.loads(
        (run_dir / "profile_snapshot.json").read_text(encoding="utf-8")
    )
    profile_version = str(profile_snapshot.get("profile_version") or "")

    vectors = {
        str(row["candidate_id"]): row for row in jsonl("vectors.jsonl")
    }
    baselines = {
        str(row["candidate_id"]): float(row["baseline_score"])
        for row in jsonl("baseline_scores.jsonl")
    }
    nominals = {
        str(row["candidate_id"]): float(row["nominal_score"])
        for row in jsonl("nominal_scores.jsonl")
    }
    effectives = {
        str(row["candidate_id"]): float(row["effective_score"])
        for row in jsonl("effective_scores.jsonl")
    }

    base_weight = float(input_manifest["target_base_score_weight"])
    preference_weight = float(input_manifest["target_preference_score_weight"])
    confidence = float(bundle.metrics.get("effective_confidence") or 0.0)

    max_diff = 0.0
    compared = 0
    for record in input_manifest.get("records") or []:
        cid = str(record["candidate_id"])
        vector_row = vectors.get(cid)
        if vector_row is None:
            return ["candidate_identity_mismatch"]
        vector = np.asarray(
            vector_row.get("candidate_vector"), dtype=np.float32
        )
        summary = record.get("vlm_evidence") or {}
        evidence = score_evidence_from_summary(summary)
        if evidence is None:
            worthiness = float(record.get("base_export_score") or 0.0) * 100.0
        else:
            worthiness = float(evidence["gif_worthiness"])
        probability = curve_probability(model, dict(record.get("curve_features") or {}))
        scenario_keys = list(vector_row.get("scenario_keys") or [])

        def score(
            curve_probability_value: float | None,
            curve_confidence: float | None,
        ) -> float:
            result = score_preference_export_candidate(
                    reranker=_replay_reranker(run_dir),
                    candidate_vector=vector,
                    gif_worthiness=worthiness,
                    scenario_keys=scenario_keys,
                    profile_version=profile_version,
                    base_export_score=float(record["base_export_score"]),
                    base_score_weight=base_weight,
                    preference_score_weight=preference_weight,
                    curve_probability=curve_probability_value,
                    curve_model_version=model_version,
                    curve_model_confidence=curve_confidence,
                )
            return float(str(result["final_score"]))

        max_diff = max(max_diff, abs(score(None, None) - baselines[cid]))
        max_diff = max(max_diff, abs(score(probability, 1.0) - nominals[cid]))
        max_diff = max(max_diff, abs(score(probability, confidence) - effectives[cid]))
        compared += 1

    if compared == 0:
        return ["qualification_replay_empty"]
    if max_diff > 1e-9:
        reasons.append("qualification_replay_mismatch")
    return reasons


def _replay_reranker(run_dir: Path) -> Any:
    """A reranker bound to the bundle's frozen work.db (the profile as of
    qualification time), not the live library profile."""
    import sqlite3

    from app.services.reranker import PreferenceReranker

    work_db = run_dir / "work.db"
    conn = sqlite3.connect(f"file:{work_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return PreferenceReranker(conn)


def _bind_deployment_config(
    conn: Any,
    bundle: Any,
    runtime_config: Mapping[str, Any],
) -> list[str]:
    """Bind the runtime config to the qualification's frozen algorithm config.

    The algorithm config hash is derived from the qualification input
    manifest's real weights and model feature domain -- never from the
    profile snapshot hash and never a constant True.
    """
    reasons: list[str] = []
    input_manifest = bundle.input_manifest

    expected_cfg = curve_algorithm_config(
        {
            "base_score_weight": input_manifest["target_base_score_weight"],
            "preference_score_weight": input_manifest["target_preference_score_weight"],
            "curve_feature_version": input_manifest.get("model_feature_version"),
        }
    )
    expected_hash = _canonical_config_hash(expected_cfg)

    runtime_hash = runtime_config.get("algorithm_config_hash")
    if not runtime_hash:
        reasons.append("config_hash_missing")
    elif str(runtime_hash) != expected_hash:
        reasons.append("algorithm_config_mismatch")

    runtime_base = runtime_config.get("base_score_weight")
    runtime_pref = runtime_config.get("preference_score_weight")
    if runtime_base is None or abs(
        float(runtime_base) - float(input_manifest["target_base_score_weight"])
    ) > 1e-12:
        reasons.append("base_weight_mismatch")
    if runtime_pref is None or abs(
        float(runtime_pref) - float(input_manifest["target_preference_score_weight"])
    ) > 1e-12:
        reasons.append("preference_weight_mismatch")

    runtime_feature_version = runtime_config.get("feature_version")
    if (
        runtime_feature_version is not None
        and str(runtime_feature_version) != str(input_manifest.get("model_feature_version"))
    ):
        reasons.append("feature_version_mismatch")

    runtime_profile_version = runtime_config.get("profile_version")
    bundle_profile_version = input_manifest.get("bundle_profile_version")
    if (
        runtime_profile_version is not None
        and bundle_profile_version
        and str(runtime_profile_version) != str(bundle_profile_version)
    ):
        reasons.append("profile_version_mismatch")

    model_extractor = input_manifest.get("model_extractor_sha256")
    runtime_extractor = runtime_config.get("extractor_sha256")
    if not runtime_extractor or not model_extractor:
        reasons.append("extractor_sha256_missing")
    elif str(runtime_extractor) != str(model_extractor):
        reasons.append("extractor_sha256_mismatch")

    return reasons


def _enrich_bundle_identity(conn: Any, bundle: Any) -> str:
    """Fill the bundle manifest's model identity fields from the live model
    row (in place) and return the model version."""
    input_manifest = bundle.input_manifest
    model_version = str(input_manifest.get("model_version"))
    previous_factory = conn.row_factory
    conn.row_factory = None
    try:
        row = conn.execute(
            "SELECT feature_version, extractor_sha256 FROM curve_preference_models "
            "WHERE model_version = ?",
            (model_version,),
        ).fetchone()
    finally:
        conn.row_factory = previous_factory
    input_manifest.setdefault("model_feature_version", row[0] if row else None)
    input_manifest.setdefault("model_extractor_sha256", row[1] if row else None)
    return model_version


def activate_deployment(
    conn: Any,
    qualification_dir: Path,
    expected_revision: int,
    operator: str = "operator",
    runtime_config: Mapping[str, Any] | None = None,
) -> DeploymentResult:
    """Activate a PUBLISHED model deployment from a verified qualification bundle.

    The bundle is fully re-validated (model PUBLISHED, evidence integrity,
    score replay), the runtime config is bound to the qualification's frozen
    algorithm config, and the revision is appended under an atomic
    compare-and-swap on the current pointer. Refusals leave the database
    unchanged.
    """
    from app.services.curve_preference import (
        load_qualification_bundle,
        validate_qualification_evidence,
    )

    runtime_config = dict(runtime_config or {})
    run_dir = Path(qualification_dir)

    bundle, load_reasons = load_qualification_bundle(run_dir)
    if bundle is None:
        return DeploymentResult("REFUSED", None, tuple(load_reasons))

    model_version = _enrich_bundle_identity(conn, bundle)

    try:
        profile_snapshot = json.loads(
            (run_dir / "profile_snapshot.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        profile_snapshot = {}
    bundle_profile_version = str(profile_snapshot.get("profile_version") or "")
    bundle.input_manifest.setdefault("bundle_profile_version", bundle_profile_version)

    reasons: list[str] = []
    qualified_rows_sha = ""
    if not bundle_profile_version:
        reasons.append("profile_identity_missing")
    else:
        manifest_rows = (profile_snapshot.get("manifest") or {}).get("profile_rows")
        if not isinstance(manifest_rows, list) or not manifest_rows:
            reasons.append("profile_identity_missing")
        else:
            qualified_rows_sha = _profile_rows_sha256(manifest_rows)
        live_profile_row = conn.execute(
            "SELECT profile_version FROM preference_profile_current WHERE slot='current'"
        ).fetchone()
        live_profile_version = str(live_profile_row[0]) if live_profile_row else ""
        if live_profile_version != bundle_profile_version:
            # Deploying onto any profile other than the qualified one (or no
            # published profile at all) is refused: re-qualify first.
            reasons.append("profile_version_mismatch")
        elif qualified_rows_sha:
            live_rows_sha = _profile_rows_sha256(
                _live_profile_rows(conn, bundle_profile_version)
            )
            if live_rows_sha != qualified_rows_sha:
                # Same version but ANY content field (centroid, confidence,
                # tag weights) drifted from the qualification snapshot: the
                # live profile was never qualified. Refuse activation.
                reasons.append("profile_content_mismatch")
    evidence = validate_qualification_evidence(
        conn,
        model_version=model_version,
        bundle=bundle,
        run_dir=run_dir,
        allowed_model_statuses=frozenset({"PUBLISHED"}),
        model_status_error="model_not_published",
    )
    reasons.extend(evidence)

    reasons.extend(_bind_deployment_config(conn, bundle, runtime_config))

    if not reasons:
        reasons.extend(_replay_qualification_scores(conn, bundle, run_dir))

    if reasons:
        return DeploymentResult("REFUSED", None, tuple(dict.fromkeys(reasons)))

    bundle_sha = bundle.hashes.get("input_manifest_sha256") or ""
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = _current_pointer_revision(conn)
        current_rev = current if current is not None else 0
        if current_rev != int(expected_revision):
            conn.rollback()
            return DeploymentResult(
                "CONFLICT",
                None,
                (
                    f"revision_mismatch: expected {expected_revision}, "
                    f"current {current_rev}",
                ),
            )
        frozen_profile_state = qualified_rows_sha
        conn.execute("UPDATE curve_model_deployments SET is_active = 0")
        conn.execute(
            """
            INSERT INTO curve_model_deployments (
                model_version, qualification_id, config_hash, extractor_sha256,
                is_active, approved_by, approved_at, previous_revision,
                qualification_bundle_path, qualification_bundle_sha256,
                profile_version, profile_snapshot_sha256
            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                model_version,
                bundle.qualification_id,
                str(runtime_config.get("algorithm_config_hash") or ""),
                str(runtime_config.get("extractor_sha256") or ""),
                operator,
                _now_iso(),
                int(expected_revision),
                str(run_dir.resolve()),
                bundle_sha,
                bundle_profile_version,
                frozen_profile_state,
            ),
        )
        revision = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO curve_model_deployment_current "
            "(slot, revision, updated_by) VALUES ('current', ?, ?)",
            (int(revision), operator),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    return DeploymentResult("ACTIVATED", int(revision), ())


def rollback_deployment(
    conn: Any,
    target_revision: int,
    expected_revision: int,
    operator: str = "operator",
    runtime_config: Mapping[str, Any] | None = None,
) -> DeploymentResult:
    """Roll back to a previous revision (re-verified) or to baseline.

    A model rollback re-checks that the target model is still PUBLISHED, its
    qualification bundle is readable and unmodified, and the runtime config
    is compatible. An incompatible target only permits an explicit baseline
    rollback. Every rollback appends an audited revision.
    """
    from app.services.curve_preference import (
        load_qualification_bundle,
        validate_qualification_evidence,
    )

    runtime_config = dict(runtime_config or {})
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = _current_pointer_revision(conn)
        current_rev = current if current is not None else 0
        if current_rev != int(expected_revision):
            conn.rollback()
            return DeploymentResult(
                "CONFLICT",
                None,
                (
                    f"revision_mismatch: expected {expected_revision}, "
                    f"current {current_rev}",
                ),
            )

        if int(target_revision) <= 0:
            conn.execute("UPDATE curve_model_deployments SET is_active = 0")
            conn.execute(
                """
                INSERT INTO curve_model_deployments (
                    model_version, qualification_id, config_hash, extractor_sha256,
                    is_active, approved_by, approved_at, previous_revision
                ) VALUES ('none', 'none', 'none', 'none', 0, ?, ?, ?)
                """,
                (operator, _now_iso(), int(expected_revision)),
            )
            revision = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT OR REPLACE INTO curve_model_deployment_current "
                "(slot, revision, updated_by) VALUES ('current', ?, ?)",
                (int(revision), operator),
            )
            conn.commit()
            return DeploymentResult("ROLLED_BACK", int(revision), ())

        target = conn.execute(
            "SELECT model_version, qualification_id, qualification_bundle_path, "
            "profile_version, profile_snapshot_sha256 "
            "FROM curve_model_deployments WHERE revision = ?",
            (int(target_revision),),
        ).fetchone()
        if target is None:
            conn.rollback()
            return DeploymentResult(
                "REFUSED", None, ("target_revision_not_found",)
            )
        model_version, qualification_id, bundle_path = (
            target[0], target[1], target[2],
        )
        target_profile_version = target[3]
        target_profile_state_sha = target[4]
        conn.rollback()
    except BaseException:
        conn.rollback()
        raise

    reasons: list[str] = []
    if not bundle_path or not Path(bundle_path).is_dir():
        return DeploymentResult(
            "REFUSED", None, ("qualification_bundle_missing",)
        )
    bundle, load_reasons = load_qualification_bundle(Path(bundle_path))
    if bundle is None:
        return DeploymentResult("REFUSED", None, tuple(load_reasons))

    _enrich_bundle_identity(conn, bundle)
    input_manifest = bundle.input_manifest
    if str(input_manifest.get("model_version")) != str(model_version):
        reasons.append("qualification_model_mismatch")
    if str(input_manifest.get("qualification_id")) != str(qualification_id):
        reasons.append("qualification_model_mismatch")
    # The rollback target's frozen profile identity must still match the
    # qualification bundle's profile -- otherwise the binding is stale.
    bundle_profile_version = ""
    try:
        profile_snapshot = json.loads(
            (Path(bundle_path) / "profile_snapshot.json").read_text(encoding="utf-8")
        )
        bundle_profile_version = str(profile_snapshot.get("profile_version") or "")
    except (OSError, ValueError):
        reasons.append("profile_identity_missing")
    if not target_profile_version or not target_profile_state_sha:
        # A rollback target without a verifiable profile identity cannot be
        # restored: only an explicit baseline rollback remains.
        reasons.append("profile_identity_missing")
    elif bundle_profile_version != str(target_profile_version):
        reasons.append("profile_version_mismatch")
    else:
        live_state = _profile_rows_sha256(
            _live_profile_rows(conn, str(target_profile_version))
        )
        if live_state != str(target_profile_state_sha):
            reasons.append("profile_content_mismatch")
    if not reasons:
        reasons.extend(
            validate_qualification_evidence(
                conn,
                model_version=str(model_version),
                bundle=bundle,
                run_dir=Path(bundle_path),
                allowed_model_statuses=frozenset({"PUBLISHED"}),
                model_status_error="model_not_published",
            )
        )
    if not reasons:
        reasons.extend(_bind_deployment_config(conn, bundle, runtime_config))
    if reasons:
        return DeploymentResult("REFUSED", None, tuple(dict.fromkeys(reasons)))

    conn.execute("BEGIN IMMEDIATE")
    try:
        current = _current_pointer_revision(conn)
        current_rev = current if current is not None else 0
        if current_rev != int(expected_revision):
            conn.rollback()
            return DeploymentResult(
                "CONFLICT",
                None,
                (
                    f"revision_mismatch: expected {expected_revision}, "
                    f"current {current_rev}",
                ),
            )
        conn.execute("UPDATE curve_model_deployments SET is_active = 0")
        conn.execute(
            """
            INSERT INTO curve_model_deployments (
                model_version, qualification_id, config_hash, extractor_sha256,
                is_active, approved_by, approved_at, previous_revision,
                qualification_bundle_path, qualification_bundle_sha256,
                profile_version, profile_snapshot_sha256
            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                model_version,
                qualification_id,
                str(runtime_config.get("algorithm_config_hash") or ""),
                str(runtime_config.get("extractor_sha256") or ""),
                operator,
                _now_iso(),
                int(expected_revision),
                str(Path(bundle_path).resolve()),
                bundle.hashes.get("input_manifest_sha256") or "",
                target_profile_version,
                target_profile_state_sha,
            ),
        )
        revision = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO curve_model_deployment_current "
            "(slot, revision, updated_by) VALUES ('current', ?, ?)",
            (int(revision), operator),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return DeploymentResult("ROLLED_BACK", int(revision), ())

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

        deployment = conn.execute(
            """
            SELECT revision, model_version, qualification_id, config_hash
            FROM curve_model_deployments
            WHERE is_active = 1
            ORDER BY revision DESC LIMIT 1
            """
        ).fetchone()
        if deployment is not None:
            deployment_revision = int(deployment[0])
            model_version = deployment[1]
            qualification_id = deployment[2]
            deployment_config_hash = str(deployment[3] or "")

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


def get_current_deployment_revision(conn: Any) -> int:
    """Return the highest existing revision number in curve_model_deployments."""
    row = conn.execute("SELECT MAX(revision) FROM curve_model_deployments").fetchone()
    if row and row[0] is not None:
        return int(row[0])
    return 0


def activate_deployment(
    conn: Any,
    qualification_id: str,
    expected_revision: int = 0,
    operator: str = "operator",
) -> dict[str, Any]:
    """Activate a published curve preference model deployment via compare-and-swap."""
    curr_rev = get_current_deployment_revision(conn)
    if curr_rev != expected_revision:
        return {
            "status": "CONFLICT",
            "error": f"revision_mismatch: expected {expected_revision}, got {curr_rev}",
        }

    row = conn.execute(
        """
        SELECT q.model_version, m.status, q.status, q.profile_snapshot_sha256, m.extractor_sha256
        FROM curve_preference_qualification_runs q
        JOIN curve_preference_models m ON m.model_version = q.model_version
        WHERE q.qualification_id = ?
        """,
        (qualification_id,),
    ).fetchone()

    if not row:
        return {"status": "FAILED", "error": "qualification_or_model_not_found"}

    is_map = isinstance(row, dict) or hasattr(row, "keys")
    model_version = row["model_version"] if is_map else row[0]
    model_status = row["status"] if is_map else row[1]
    qual_status = row[2]
    config_hash = row["profile_snapshot_sha256"] if is_map else row[3]
    extractor_sha256 = row["extractor_sha256"] if is_map else row[4]

    reasons = validate_deployment(model_status, qual_status == "QUALIFIED", True)
    if reasons:
        return {"status": "FAILED", "error": f"validation_failed: {reasons}"}

    from datetime import datetime, timezone

    now_str = datetime.now(timezone.utc).isoformat()

    conn.execute("UPDATE curve_model_deployments SET is_active = 0")
    cursor = conn.execute(
        """
        INSERT INTO curve_model_deployments (
            model_version, qualification_id, config_hash, extractor_sha256,
            is_active, approved_by, approved_at, previous_revision
        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
        """,
        (model_version, qualification_id, config_hash, extractor_sha256, operator, now_str, expected_revision),
    )
    conn.commit()
    return {
        "status": "ACTIVATED",
        "revision": cursor.lastrowid,
        "model_version": model_version,
    }


def rollback_deployment(
    conn: Any,
    target_revision: int,
    expected_revision: int,
    operator: str = "operator",
) -> dict[str, Any]:
    """Roll back curve model deployment to a previous revision or baseline (target_revision=0)."""
    curr_rev = get_current_deployment_revision(conn)
    if curr_rev != expected_revision:
        return {
            "status": "CONFLICT",
            "error": f"revision_mismatch: expected {expected_revision}, got {curr_rev}",
        }

    from datetime import datetime, timezone

    now_str = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE curve_model_deployments SET is_active = 0")

    if target_revision <= 0:
        cursor = conn.execute(
            """
            INSERT INTO curve_model_deployments (
                model_version, qualification_id, config_hash, extractor_sha256,
                is_active, approved_by, approved_at, previous_revision
            ) VALUES ('none', 'none', 'none', 'none', 0, ?, ?, ?)
            """,
            (operator, now_str, expected_revision),
        )
        conn.commit()
        return {
            "status": "ROLLED_BACK",
            "revision": cursor.lastrowid,
            "target_revision": target_revision,
            "is_active": False,
        }

    target_row = conn.execute(
        """
        SELECT model_version, qualification_id, config_hash, extractor_sha256
        FROM curve_model_deployments
        WHERE revision = ?
        """,
        (target_revision,),
    ).fetchone()

    if not target_row:
        return {"status": "FAILED", "error": "target_revision_not_found"}

    is_map = isinstance(target_row, dict) or hasattr(target_row, "keys")
    model_version = target_row["model_version"] if is_map else target_row[0]
    qualification_id = target_row["qualification_id"] if is_map else target_row[1]
    config_hash = target_row["config_hash"] if is_map else target_row[2]
    extractor_sha256 = target_row["extractor_sha256"] if is_map else target_row[3]

    cursor = conn.execute(
        """
        INSERT INTO curve_model_deployments (
            model_version, qualification_id, config_hash, extractor_sha256,
            is_active, approved_by, approved_at, previous_revision
        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
        """,
        (model_version, qualification_id, config_hash, extractor_sha256, operator, now_str, expected_revision),
    )
    conn.commit()
    return {
        "status": "ROLLED_BACK",
        "revision": cursor.lastrowid,
        "target_revision": target_revision,
        "is_active": True,
    }

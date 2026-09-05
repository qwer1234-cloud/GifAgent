"""Centralized curve preference model loading, validation, and scoring.

Single source of truth for the ``curve_preference_models`` lifecycle:
schema ownership lives in ``preference_schema.apply_preference_schema``;
this module loads one exact model version with full integrity validation
and exposes the standardized logit plus a stable sigmoid probability.

Publication rules: the trainer may only persist ``DRAFT`` rows; a model
becomes ``PUBLISHED`` exclusively through the promotion service after a
verified ``QUALIFIED`` qualification run.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from app.services.preference_schema import apply_preference_schema


class EvidenceUnavailable(ValueError):
    """A candidate lacks usable evidence for curve scoring."""


@dataclass(frozen=True)
class CurvePreferenceModel:
    model_version: str
    feature_version: str
    extractor_sha256: str
    feature_names: tuple[str, ...]
    mean: np.ndarray
    std: np.ndarray
    coefficients: np.ndarray
    intercept: float
    training_manifest_sha256: str
    holdout_manifest_sha256: str
    training_source_set_sha256: str
    holdout_source_set_sha256: str
    training_manifest: dict[str, object]
    holdout_manifest: dict[str, object]


def canonical_json_sha256(payload: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical (sorted, compact) JSON serialization."""

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_named_mapping(
    raw: str,
    field: str,
    feature_names: tuple[str, ...],
) -> np.ndarray:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"model {field} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"model {field} must be a JSON object")
    keys = frozenset(parsed)
    if keys != frozenset(feature_names):
        raise ValueError(
            f"model {field} keys do not match feature names "
            f"({sorted(keys)} != {sorted(feature_names)})"
        )
    values = np.zeros(len(feature_names), dtype=np.float64)
    for index, name in enumerate(feature_names):
        value = parsed[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"model {field}[{name}] is not numeric")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"model {field}[{name}] is not finite")
        values[index] = number
    return values


def load_curve_model(
    conn: sqlite3.Connection,
    model_version: str,
    *,
    allowed_statuses: frozenset[str],
) -> CurvePreferenceModel:
    """Load and validate one exact model version; raise ValueError on mismatch."""

    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT model_version, feature_version, extractor_sha256, status,
                   coefficients_json, intercept, feature_mean_json,
                   feature_std_json, feature_names_json,
                   training_manifest_sha256, holdout_manifest_sha256,
                   training_source_set_sha256, holdout_source_set_sha256,
                   training_manifest_json, holdout_manifest_json
            FROM curve_preference_models
            WHERE model_version = :model_version
            """,
            {"model_version": model_version},
        ).fetchone()
    finally:
        conn.row_factory = previous_factory
    if row is None:
        raise ValueError(f"curve model {model_version!r} not found")
    if row["status"] not in allowed_statuses:
        raise ValueError(
            f"curve model {model_version!r} has status {row['status']!r}, "
            f"expected one of {sorted(allowed_statuses)}"
        )

    try:
        feature_names_raw = json.loads(row["feature_names_json"])
    except (TypeError, ValueError) as exc:
        raise ValueError("model feature_names_json is not valid JSON") from exc
    if (
        not isinstance(feature_names_raw, list)
        or not feature_names_raw
        or any(not isinstance(name, str) or not name for name in feature_names_raw)
        or len(set(feature_names_raw)) != len(feature_names_raw)
    ):
        raise ValueError("model feature_names_json must be unique non-empty strings")
    feature_names = tuple(feature_names_raw)

    mean = _parse_named_mapping(row["feature_mean_json"], "mean", feature_names)
    std = _parse_named_mapping(row["feature_std_json"], "std", feature_names)
    if not bool(np.all(std > 0)):
        raise ValueError("model std values must all be positive")
    coefficients = _parse_named_mapping(
        row["coefficients_json"], "coefficient", feature_names
    )
    intercept = row["intercept"]
    if isinstance(intercept, bool) or not isinstance(intercept, (int, float)):
        raise ValueError("model intercept is not numeric")
    intercept = float(intercept)
    if not math.isfinite(intercept):
        raise ValueError("model intercept is not finite")

    training_manifest = json.loads(row["training_manifest_json"])
    holdout_manifest = json.loads(row["holdout_manifest_json"])
    if not isinstance(training_manifest, dict) or not isinstance(holdout_manifest, dict):
        raise ValueError("model manifests must be JSON objects")

    training_sha = canonical_json_sha256(training_manifest)
    if training_sha != row["training_manifest_sha256"]:
        raise ValueError(
            "model training_manifest_sha256 mismatch: recomputed "
            f"{training_sha} != stored {row['training_manifest_sha256']}"
        )
    holdout_sha = canonical_json_sha256(holdout_manifest)
    if holdout_sha != row["holdout_manifest_sha256"]:
        raise ValueError(
            "model holdout_manifest_sha256 mismatch: recomputed "
            f"{holdout_sha} != stored {row['holdout_manifest_sha256']}"
        )

    for label, manifest in (
        ("training", training_manifest),
        ("holdout", holdout_manifest),
    ):
        if manifest.get("feature_version") != row["feature_version"]:
            raise ValueError(
                f"model {label} manifest feature_version "
                f"{manifest.get('feature_version')!r} != model row "
                f"{row['feature_version']!r}"
            )

    return CurvePreferenceModel(
        model_version=str(row["model_version"]),
        feature_version=str(row["feature_version"]),
        extractor_sha256=str(row["extractor_sha256"]),
        feature_names=feature_names,
        mean=mean,
        std=std,
        coefficients=coefficients,
        intercept=intercept,
        training_manifest_sha256=row["training_manifest_sha256"],
        holdout_manifest_sha256=row["holdout_manifest_sha256"],
        training_source_set_sha256=row["training_source_set_sha256"],
        holdout_source_set_sha256=row["holdout_source_set_sha256"],
        training_manifest=training_manifest,
        holdout_manifest=holdout_manifest,
    )


def curve_logit(model: CurvePreferenceModel, features: Mapping[str, object]) -> float:
    """Return the standardized linear score or raise EvidenceUnavailable."""

    standardized = np.zeros(len(model.feature_names), dtype=np.float64)
    for index, name in enumerate(model.feature_names):
        if name not in features:
            raise EvidenceUnavailable(f"curve feature {name!r} missing")
        value = features[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EvidenceUnavailable(f"curve feature {name!r} is not numeric")
        number = float(value)
        if not math.isfinite(number):
            raise EvidenceUnavailable(f"curve feature {name!r} is not finite")
        standardized[index] = (number - model.mean[index]) / model.std[index]
    return float(standardized @ model.coefficients + model.intercept)


def curve_probability(
    model: CurvePreferenceModel,
    features: Mapping[str, object],
) -> float:
    """Return a stable sigmoid of curve_logit in the closed unit interval."""

    logit = curve_logit(model, features)
    if logit >= 0.0:
        probability = 1.0 / (1.0 + math.exp(-min(logit, 700.0)))
    else:
        probability = math.exp(max(logit, -700.0)) / (1.0 + math.exp(min(logit, 700.0)))
    return float(min(1.0, max(0.0, probability)))


# ---------------------------------------------------------------------------
# Explicit, atomic DRAFT -> PUBLISHED promotion
# ---------------------------------------------------------------------------

REQUIRED_BUNDLE_FILES = (
    "run_state.json",
    "input_manifest.json",
    "profile_snapshot.json",
    "evidence.jsonl",
    "metrics.json",
    "report.md",
    "work.db",
)


@dataclass(frozen=True)
class PromotionBundle:
    qualification_id: str
    metrics: dict[str, Any]
    input_manifest: dict[str, Any]
    hashes: dict[str, str]


def _promotion_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_qualification_bundle(
    run_dir: Path,
) -> tuple[PromotionBundle | None, list[str]]:
    """Load and hash-verify the bundle; return (bundle, refusal_reasons)."""

    reasons: list[str] = []
    for name in REQUIRED_BUNDLE_FILES:
        if not (run_dir / name).is_file():
            reasons.append("bundle_file_missing")
            return None, reasons

    metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    input_manifest = json.loads(
        (run_dir / "input_manifest.json").read_text(encoding="utf-8")
    )
    state = json.loads((run_dir / "run_state.json").read_text(encoding="utf-8"))

    artifacts = metrics.get("artifacts") or {}
    expected_hashes = {
        "input_manifest.json": artifacts.get("input_manifest_sha256"),
        "profile_snapshot.json": artifacts.get("profile_snapshot_sha256"),
        "evidence.jsonl": artifacts.get("evidence_sha256"),
        "report.md": artifacts.get("report_sha256"),
        "baseline_scores.jsonl": artifacts.get("baseline_scores_sha256"),
        "nominal_scores.jsonl": artifacts.get("nominal_scores_sha256"),
        "effective_scores.jsonl": artifacts.get("effective_scores_sha256"),
    }
    for name, expected in expected_hashes.items():
        if expected is None:
            continue
        if _promotion_file_sha256(run_dir / name) != expected:
            reasons.append("bundle_hash_mismatch")
    if _promotion_file_sha256(run_dir / "metrics.json") != metrics.get(
        "metrics_self_sha256"
    ) and metrics.get("metrics_self_sha256") is not None:
        reasons.append("bundle_hash_mismatch")
    if _promotion_file_sha256(run_dir / "report.md") != metrics.get(
        "report_sha256"
    ):
        reasons.append("bundle_hash_mismatch")
    if str(state.get("status")) != "COMPLETED":
        reasons.append("bundle_hash_mismatch")
    if metrics.get("status") != "QUALIFIED":
        reasons.append("qualification_not_qualified")

    if reasons:
        return None, reasons

    hashes = {
        "input_manifest_sha256": _promotion_file_sha256(
            run_dir / "input_manifest.json"
        ),
        "profile_snapshot_sha256": _promotion_file_sha256(
            run_dir / "profile_snapshot.json"
        ),
        "baseline_scores_sha256": _promotion_file_sha256(
            run_dir / "baseline_scores.jsonl"
        ),
        "nominal_scores_sha256": _promotion_file_sha256(
            run_dir / "nominal_scores.jsonl"
        ),
        "effective_scores_sha256": _promotion_file_sha256(
            run_dir / "effective_scores.jsonl"
        ),
        "report_sha256": _promotion_file_sha256(run_dir / "report.md"),
    }
    return (
        PromotionBundle(
            qualification_id=str(state.get("qualification_id")),
            metrics=metrics,
            input_manifest=input_manifest,
            hashes=hashes,
        ),
        [],
    )


def validate_promotion(
    conn: sqlite3.Connection,
    *,
    model_version: str,
    bundle: PromotionBundle,
) -> list[str]:
    """Revalidate the bundle against the live database; return refusal codes."""

    reasons: list[str] = []
    input_manifest = bundle.input_manifest

    if str(input_manifest.get("model_version")) != model_version:
        reasons.append("qualification_model_mismatch")
    if str(input_manifest.get("qualification_id")) != bundle.qualification_id:
        reasons.append("qualification_model_mismatch")

    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        model_row = conn.execute(
            """
            SELECT status, training_manifest_sha256, holdout_manifest_sha256
            FROM curve_preference_models WHERE model_version = :model_version
            """,
            {"model_version": model_version},
        ).fetchone()
        existing = conn.execute(
            """
            SELECT input_manifest_sha256, report_sha256
            FROM curve_preference_qualification_runs
            WHERE qualification_id = :qualification_id
            """,
            {"qualification_id": bundle.qualification_id},
        ).fetchone()
    finally:
        conn.row_factory = previous_factory

    if model_row is None:
        reasons.append("model_not_draft")
        return reasons
    if model_row["status"] != "DRAFT":
        reasons.append("model_not_draft")
    if (
        model_row["training_manifest_sha256"]
        != input_manifest.get("model_training_manifest_sha256")
        or model_row["holdout_manifest_sha256"]
        != input_manifest.get("model_holdout_manifest_sha256")
    ):
        reasons.append("qualification_model_mismatch")

    if existing is not None:
        if (
            existing["input_manifest_sha256"]
            != bundle.hashes["input_manifest_sha256"]
            or existing["report_sha256"] != bundle.hashes["report_sha256"]
        ):
            reasons.append("qualification_id_conflict")

    return reasons


def promotion_backup_database(db_path: Path) -> Path:
    """Timestamped online backup taken before any promotion mutation."""

    repo = Path(__file__).resolve().parents[1]
    backup_dir = repo / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"library.before-curve-promotion.{stamp}.db"
    source = sqlite3.connect(str(db_path), timeout=60)
    source.execute("PRAGMA query_only = ON")
    target = sqlite3.connect(str(backup_path))
    source.backup(target)
    target.close()
    source.close()
    return backup_path


def promote_model(
    conn: sqlite3.Connection,
    *,
    model_version: str,
    run_dir: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Validate the bundle and atomically publish the model.

    Returns a summary dict. On any refusal the database is unchanged and the
    refusal codes are returned. ``dry_run`` performs every validation but no
    backup and no transaction write.
    """

    apply_preference_schema(conn)

    bundle, load_reasons = load_qualification_bundle(run_dir)
    if bundle is None:
        return {"status": "REFUSED", "reasons": load_reasons, "dry_run": dry_run}

    # Must mirror the identity column set captured at qualification time.
    identity_columns = (
        "model_version", "feature_version", "extractor_sha256", "status",
        "coefficients_json", "intercept", "feature_mean_json",
        "feature_std_json", "feature_names_json", "l2_lambda",
        "training_event_watermark", "training_source_set_sha256",
        "holdout_source_set_sha256", "training_manifest_sha256",
        "holdout_manifest_sha256", "training_manifest_json",
        "holdout_manifest_json", "holdout_row_count", "metrics_json",
        "curve_model_confidence",
    )
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        model_row = conn.execute(
            f"""SELECT {', '.join(identity_columns)}
            FROM curve_preference_models WHERE model_version = :model_version""",
            {"model_version": model_version},
        ).fetchone()
    finally:
        conn.row_factory = previous_factory
    if model_row is None or model_row["status"] != "DRAFT":
        return {
            "status": "REFUSED",
            "reasons": ["model_not_draft"],
            "dry_run": dry_run,
        }
    model_row_now = {key: model_row[key] for key in identity_columns}
    recorded_model_sha = str(
        bundle.input_manifest.get("model_row_sha256", "")
    )
    if recorded_model_sha and canonical_json_sha256(model_row_now) != recorded_model_sha:
        return {
            "status": "REFUSED",
            "reasons": ["model_row_changed_after_qualification"],
            "dry_run": dry_run,
        }

    validation = validate_promotion(
        conn, model_version=model_version, bundle=bundle
    )
    if validation:
        return {
            "status": "REFUSED",
            "reasons": validation,
            "dry_run": dry_run,
        }

    effective_confidence = bundle.metrics.get("effective_confidence")
    if dry_run:
        return {
            "status": "DRY_RUN_OK",
            "reasons": [],
            "qualification_id": bundle.qualification_id,
            "metrics_status": bundle.metrics.get("status"),
            "effective_confidence": effective_confidence,
        }

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO curve_preference_qualification_runs (
                qualification_id, model_version, status,
                input_manifest_sha256, profile_snapshot_sha256,
                baseline_scores_sha256, proposed_scores_sha256,
                metrics_json, report_path, report_sha256,
                source_db_snapshot_sha256, created_at, qualified_at
            ) VALUES (
                :qualification_id, :model_version, 'QUALIFIED',
                :input_manifest_sha256, :profile_snapshot_sha256,
                :baseline_scores_sha256, :proposed_scores_sha256,
                :metrics_json, :report_path, :report_sha256,
                :source_db_snapshot_sha256,
                strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            )
            """,
            {
                "qualification_id": bundle.qualification_id,
                "model_version": model_version,
                "input_manifest_sha256": bundle.hashes["input_manifest_sha256"],
                "profile_snapshot_sha256": bundle.hashes[
                    "profile_snapshot_sha256"
                ],
                "baseline_scores_sha256": bundle.hashes[
                    "baseline_scores_sha256"
                ],
                "proposed_scores_sha256": bundle.hashes[
                    "effective_scores_sha256"
                ],
                "metrics_json": json.dumps(
                    bundle.metrics, sort_keys=True, allow_nan=False
                ),
                "report_path": str((run_dir / "report.md").resolve()),
                "report_sha256": bundle.hashes["report_sha256"],
                "source_db_snapshot_sha256": "",
            },
        )
        published_elsewhere = conn.execute(
            "SELECT COUNT(*) FROM curve_preference_models "
            "WHERE status = 'PUBLISHED' AND model_version != :model_version",
            {"model_version": model_version},
        ).fetchone()[0]
        if published_elsewhere:
            conn.execute(
                "UPDATE curve_preference_models SET status = 'SUPERSEDED' "
                "WHERE status = 'PUBLISHED' AND model_version != :model_version",
                {"model_version": model_version},
            )
        conn.execute(
            "UPDATE curve_preference_models SET status = 'PUBLISHED', "
            "curve_model_confidence = :confidence, "
            "published_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE model_version = :model_version AND status = 'DRAFT'",
            {
                "confidence": effective_confidence,
                "model_version": model_version,
            },
        )
        final_published = conn.execute(
            "SELECT COUNT(*) FROM curve_preference_models "
            "WHERE status = 'PUBLISHED'"
        ).fetchone()[0]
        if final_published != 1:
            raise RuntimeError(
                f"expected exactly one PUBLISHED model, found {final_published}"
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    return {
        "status": "PROMOTED",
        "reasons": [],
        "qualification_id": bundle.qualification_id,
        "model_version": model_version,
        "effective_confidence": effective_confidence,
    }

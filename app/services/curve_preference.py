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
    "vectors.jsonl",
    "metrics.json",
    "report.md",
    "work.db",
    "baseline_scores.jsonl",
    "nominal_scores.jsonl",
    "effective_scores.jsonl",
)

REQUIRED_ARTIFACT_HASH_KEYS = (
    ("input_manifest.json", "input_manifest_sha256"),
    ("profile_snapshot.json", "profile_snapshot_sha256"),
    ("evidence.jsonl", "evidence_sha256"),
    ("vectors.jsonl", "vectors_sha256"),
    ("report.md", "report_sha256"),
    ("baseline_scores.jsonl", "baseline_scores_sha256"),
    ("nominal_scores.jsonl", "nominal_scores_sha256"),
    ("effective_scores.jsonl", "effective_scores_sha256"),
)


@dataclass(frozen=True)
class PromotionBundle:
    qualification_id: str
    metrics: dict[str, Any]
    input_manifest: dict[str, Any]
    hashes: dict[str, str]
    run_dir: Path | None = None


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
    metrics_path = run_dir / "metrics.json"
    if metrics_path.is_file():
        try:
            m_pre = json.loads(metrics_path.read_text(encoding="utf-8"))
            if isinstance(m_pre, dict) and m_pre.get("status") != "QUALIFIED":
                reasons.append("qualification_not_qualified")
        except Exception:
            pass

    for name in REQUIRED_BUNDLE_FILES:
        if not (run_dir / name).is_file():
            reasons.append("bundle_file_missing")
            return None, reasons

    try:
        metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
        input_manifest = json.loads(
            (run_dir / "input_manifest.json").read_text(encoding="utf-8")
        )
        state = json.loads((run_dir / "run_state.json").read_text(encoding="utf-8"))
    except Exception:
        reasons.append("bundle_file_missing")
        return None, reasons

    artifacts = metrics.get("artifacts")
    if not isinstance(artifacts, dict):
        reasons.append("bundle_hash_mismatch")
        return None, reasons

    for file_name, hash_key in REQUIRED_ARTIFACT_HASH_KEYS:
        expected = artifacts.get(hash_key)
        if hash_key == "report_sha256" and not expected:
            expected = metrics.get("report_sha256")
        if not expected or not isinstance(expected, str):
            reasons.append("bundle_hash_mismatch")
            continue
        if _promotion_file_sha256(run_dir / file_name) != expected:
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
        "evidence_sha256": _promotion_file_sha256(
            run_dir / "evidence.jsonl"
        ),
        "vectors_sha256": _promotion_file_sha256(
            run_dir / "vectors.jsonl"
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
            run_dir=run_dir,
        ),
        [],
    )


def validate_qualification_evidence(
    conn: sqlite3.Connection,
    *,
    model_version: str,
    bundle: PromotionBundle,
    run_dir: Path | None = None,
    allowed_model_statuses: frozenset[str] = frozenset({"DRAFT"}),
    model_status_error: str = "model_status_not_allowed",
) -> list[str]:
    """Revalidate a qualification bundle against the live database.

    Shared by DRAFT promotion and PUBLISHED deployment activation: bundle
    integrity, model/manifest identity, candidate mapping, score files, and
    the statistical decision are all re-checked here. The only difference is
    which model statuses each caller accepts.
    """

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
        reasons.append(model_status_error)
        return reasons
    if model_row["status"] not in allowed_model_statuses:
        reasons.append(model_status_error)
        return reasons
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

    target_dir = run_dir or bundle.run_dir
    if (
        target_dir is None
        or not (target_dir / "baseline_scores.jsonl").is_file()
        or not (target_dir / "nominal_scores.jsonl").is_file()
        or not (target_dir / "effective_scores.jsonl").is_file()
    ):
        reasons.append("bundle_file_missing")
        return reasons

    try:
        from app.services.curve_preference_qualification import (
            GroupedScoreRow,
            evaluate_source_grouped_scores,
            decide_curve_qualification,
            compute_source_overlap,
            class_counts_from_manifest_rows,
            HOLDOUT_MARGIN_DIVISOR,
            BOOTSTRAP_SEED,
            BOOTSTRAP_REPS,
        )

        model_obj = load_curve_model(
            conn, model_version, allowed_statuses=allowed_model_statuses
        )
        holdout_manifest = model_obj.holdout_manifest or {}
        holdout_rows = holdout_manifest.get("rows")
        if not isinstance(holdout_rows, list) or len(holdout_rows) < 30:
            reasons.append("holdout_too_small")
            return reasons

        # 1. Manifest candidate mapping (source, rating) & duplicate check
        manifest_candidate_map: dict[str, tuple[str, str]] = {}
        for r in holdout_rows:
            cid = str(r.get("candidate_id") or "")
            if not cid or cid in manifest_candidate_map:
                reasons.append("candidate_identity_mismatch")
                return reasons
            manifest_candidate_map[cid] = (
                str(r.get("source") or ""),
                str(r.get("rating") or ""),
            )
        expected_cids = set(manifest_candidate_map.keys())

        # 2. Input manifest records verification
        input_records = bundle.input_manifest.get("records")
        if not isinstance(input_records, list):
            reasons.append("qualification_model_mismatch")
            return reasons
        input_cids: set[str] = set()
        for r in input_records:
            cid = str(r.get("candidate_id") or "")
            if not cid or cid in input_cids or cid not in manifest_candidate_map:
                reasons.append("candidate_identity_mismatch")
                return reasons
            input_cids.add(cid)
            exp_src, exp_rat = manifest_candidate_map[cid]
            if (
                str(r.get("source_video_sha256") or "") != exp_src
                or str(r.get("rating") or "") != exp_rat
            ):
                reasons.append("candidate_identity_mismatch")
                return reasons
        if input_cids != expected_cids:
            reasons.append("qualification_model_mismatch")
            return reasons

        # 3. Baseline scores verification
        baseline_lines = [
            json.loads(line)
            for line in (target_dir / "baseline_scores.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        baseline_map: dict[str, float] = {}
        for line in baseline_lines:
            cid = str(line.get("candidate_id") or "")
            if not cid or cid in baseline_map or cid not in manifest_candidate_map:
                reasons.append("candidate_identity_mismatch")
                return reasons
            exp_src, exp_rat = manifest_candidate_map[cid]
            if (
                str(line.get("source_video_sha256") or "") != exp_src
                or str(line.get("rating") or "") != exp_rat
            ):
                reasons.append("candidate_identity_mismatch")
                return reasons
            baseline_map[cid] = float(line["baseline_score"])
        if set(baseline_map.keys()) != expected_cids:
            reasons.append("qualification_model_mismatch")
            return reasons

        # 4. Nominal scores verification
        nominal_lines = [
            json.loads(line)
            for line in (target_dir / "nominal_scores.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        nominal_map: dict[str, float] = {}
        for line in nominal_lines:
            cid = str(line.get("candidate_id") or "")
            if not cid or cid in nominal_map or cid not in manifest_candidate_map:
                reasons.append("candidate_identity_mismatch")
                return reasons
            nominal_map[cid] = float(line["nominal_score"])
        if set(nominal_map.keys()) != expected_cids:
            reasons.append("qualification_model_mismatch")
            return reasons

        # 5. Effective scores verification
        effective_lines = [
            json.loads(line)
            for line in (target_dir / "effective_scores.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        effective_map: dict[str, float] = {}
        for line in effective_lines:
            cid = str(line.get("candidate_id") or "")
            if not cid or cid in effective_map or cid not in manifest_candidate_map:
                reasons.append("candidate_identity_mismatch")
                return reasons
            effective_map[cid] = float(line["effective_score"])
        if set(effective_map.keys()) != expected_cids:
            reasons.append("qualification_model_mismatch")
            return reasons

        # 6. Vectors artifact verification (if vectors.jsonl present)
        if (target_dir / "vectors.jsonl").is_file():
            vector_lines = [
                json.loads(line)
                for line in (target_dir / "vectors.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            vector_cids: set[str] = set()
            for line in vector_lines:
                cid = str(line.get("candidate_id") or "")
                if (
                    not cid
                    or cid in vector_cids
                    or cid not in manifest_candidate_map
                ):
                    reasons.append("candidate_identity_mismatch")
                    return reasons
                vector_cids.add(cid)
            if vector_cids != expected_cids:
                reasons.append("qualification_model_mismatch")
                return reasons

        # 7. Construct grouped score rows anchored on frozen manifest sources & ratings
        nominal_grouped = [
            GroupedScoreRow(
                candidate_id=cid,
                source=manifest_candidate_map[cid][0],
                rating=manifest_candidate_map[cid][1],
                baseline=baseline_map[cid],
                proposed=nominal_map[cid],
            )
            for cid in sorted(expected_cids)
        ]
        effective_grouped = [
            GroupedScoreRow(
                candidate_id=cid,
                source=manifest_candidate_map[cid][0],
                rating=manifest_candidate_map[cid][1],
                baseline=baseline_map[cid],
                proposed=effective_map[cid],
            )
            for cid in sorted(expected_cids)
        ]

        recomputed_nominal = evaluate_source_grouped_scores(
            nominal_grouped,
            baseline_field="baseline",
            proposed_field="proposed",
            bootstrap_seed=BOOTSTRAP_SEED,
            bootstrap_reps=BOOTSTRAP_REPS,
        )
        recomputed_effective = evaluate_source_grouped_scores(
            effective_grouped,
            baseline_field="baseline",
            proposed_field="proposed",
            bootstrap_seed=BOOTSTRAP_SEED,
            bootstrap_reps=BOOTSTRAP_REPS,
        )

        training_rows = model_obj.training_manifest.get("rows")
        effective_feedback_count = (
            len(training_rows) if isinstance(training_rows, list) else 0
        )
        data_confidence = min(1.0, effective_feedback_count / 100)
        nominal_lcb = recomputed_nominal.pairwise_delta_lcb95
        margin_factor = (
            max(0.0, min(1.0, nominal_lcb / HOLDOUT_MARGIN_DIVISOR))
            if math.isfinite(nominal_lcb)
            else 0.0
        )
        recomputed_confidence = min(data_confidence, margin_factor)

        recorded_confidence = bundle.metrics.get("effective_confidence")
        if (
            recorded_confidence is None
            or not math.isfinite(float(recorded_confidence))
            or abs(float(recorded_confidence) - recomputed_confidence) > 1e-6
        ):
            reasons.append("qualification_not_qualified")

        train_class_counts = class_counts_from_manifest_rows(
            training_rows if isinstance(training_rows, list) else []
        )
        holdout_class_counts = class_counts_from_manifest_rows(holdout_rows)
        decision = decide_curve_qualification(
            # The statistical decision reflects the qualification-time state:
            # the model was a DRAFT when the bundle was produced, whether this
            # call now promotes that DRAFT or activates the PUBLISHED model.
            model_status="DRAFT",
            model_manifest_hashes_ok=(
                model_row["training_manifest_sha256"]
                == input_manifest.get("model_training_manifest_sha256")
                and model_row["holdout_manifest_sha256"]
                == input_manifest.get("model_holdout_manifest_sha256")
            ),
            source_overlap=compute_source_overlap(model_obj),
            holdout_row_count=len(expected_cids),
            train_class_counts=train_class_counts,
            holdout_class_counts=holdout_class_counts,
            blocking=bundle.metrics.get("blocking") or [],
            nominal_pair_count=recomputed_nominal.pair_count,
            nominal_pairwise_delta_lcb95=recomputed_nominal.pairwise_delta_lcb95,
            effective_pairwise_delta_lcb95=recomputed_effective.pairwise_delta_lcb95,
            effective_ndcg_delta=recomputed_effective.ndcg_at_20_delta,
            non_finite_count=(
                recomputed_nominal.non_finite_count
                + recomputed_effective.non_finite_count
            ),
        )

        if decision.status != "QUALIFIED":
            for r in decision.reasons:
                if r not in reasons:
                    reasons.append(r)
            if "qualification_not_qualified" not in reasons:
                reasons.append("qualification_not_qualified")
    except Exception:
        reasons.append("qualification_not_qualified")

    return reasons


def validate_promotion(
    conn: sqlite3.Connection,
    *,
    model_version: str,
    bundle: PromotionBundle,
    run_dir: Path | None = None,
) -> list[str]:
    """Revalidate the bundle against the live database; return refusal codes.

    Promotion only accepts DRAFT models; deployment activation reuses the
    same evidence validation with PUBLISHED-only statuses.
    """
    return validate_qualification_evidence(
        conn,
        model_version=model_version,
        bundle=bundle,
        run_dir=run_dir,
        allowed_model_statuses=frozenset({"DRAFT"}),
        model_status_error="model_not_draft",
    )


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
        conn, model_version=model_version, bundle=bundle, run_dir=run_dir
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

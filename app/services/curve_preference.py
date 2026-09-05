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
from typing import Any, Mapping

import numpy as np


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

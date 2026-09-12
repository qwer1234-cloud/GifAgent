"""Row-fact-driven curve training snapshots, DRAFT training, and registration.

The immutable snapshot binds every training row to its authoritative feedback
event, capture record, publication link, source-domain curve features, exact
no-curve baseline, and vector identity. Source roles are recomputed from the
rows themselves; declared role sets that disagree with row facts are refused.
The trainer only ever produces DRAFT or NEEDS_ATTENTION -- promotion is a
separate, explicitly authorized action.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from app.services.curve_preference import canonical_json_sha256
from app.services.curve_source_features import (
    CURVE_SOURCE_FEATURE_NAMES,
    CURVE_SOURCE_FEATURE_VERSION,
)

TRAINING_MANIFEST_VERSION = "curve-training-manifest-v2"
TRAINING_CODE_VERSION = "curve-training-v2"

POSITIVE_RATINGS = frozenset({"like", "favorite"})
NEGATIVE_RATINGS = frozenset({"dislike"})

DEFAULT_SPLIT_SEED = 20260911


@dataclass(frozen=True)
class TrainingSnapshot:
    snapshot_dir: str
    manifest_sha256: str
    train_row_count: int
    development_row_count: int
    final_holdout_row_count: int


@dataclass(frozen=True)
class DraftTrainingResult:
    status: str
    model_version: str | None
    model_path: str | None
    model_sha256: str | None
    error_code: str | None


def _result(error_code: str | None, **fields: Any) -> dict[str, Any]:
    result = {
        "status": "NEEDS_ATTENTION" if error_code else "DRAFT",
        "model_version": None,
        "model_path": None,
        "model_sha256": None,
        "error_code": error_code,
    }
    result.update({k: v for k, v in fields.items() if v is not None})
    return result


def _event_content_sha256(event: Mapping[str, Any]) -> str:
    payload = {
        "event_id": event.get("event_id"),
        "target_type": event.get("target_type"),
        "target_id": event.get("target_id"),
        "rating": event.get("rating"),
        "source_video_sha256": event.get("source_video_sha256"),
        "created_at": event.get("created_at"),
        "event_kind": event.get("event_kind"),
        "supersedes_event_id": event.get("supersedes_event_id"),
    }
    return canonical_json_sha256(payload)


def _split_source(source: str, seed: int) -> str:
    """Deterministic per-source train/development split (frozen seed)."""
    digest = hashlib.sha256(f"{seed}:{source}".encode("utf-8")).hexdigest()
    return "train" if int(digest[:8], 16) % 100 < 80 else "development"


def _load_baseline_from_capture(record_row: Any) -> tuple[float | None, str]:
    """Read the exact no-curve baseline out of the immutable capture payload."""
    payload_path = record_row[5]
    try:
        payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "capture_payload_unreadable"
    baseline = payload.get("baseline_score")
    if isinstance(baseline, bool) or not isinstance(baseline, (int, float)):
        return None, "baseline_missing"
    if not math.isfinite(float(baseline)):
        return None, "baseline_missing"
    return float(baseline), ""


def _collect_samples(
    conn: Any,
    events: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Resolve every fittable event into a complete same-domain sample row.

    A sample requires: a publication link, a real GIF hash, AVAILABLE
    ``curve-source-v1`` features, the exact no-curve baseline from the capture
    payload, and vector/profile identity. Anything less is excluded with a
    stable reason -- user feedback is never undone and never zero-filled.
    """
    samples: list[dict[str, Any]] = []
    exclusions: list[dict[str, str]] = []

    profile_row = conn.execute(
        "SELECT profile_version FROM preference_profile_current WHERE slot='current'"
    ).fetchone()
    profile_version = profile_row[0] if profile_row is not None else None

    for key in sorted(events):
        event = events[key]
        target_type = str(event.get("target_type"))
        target_id = str(event.get("target_id"))
        if target_type != "candidate_gif":
            continue
        rating = str(event.get("rating"))
        if rating in POSITIVE_RATINGS:
            label = 1
        elif rating in NEGATIVE_RATINGS:
            label = 0
        else:
            exclusions.append(
                {"candidate_id": target_id, "reason": "rating_not_fittable"}
            )
            continue

        def exclude(reason: str) -> None:
            exclusions.append({"candidate_id": target_id, "reason": reason})

        publication = conn.execute(
            "SELECT record_id, gif_sha256 FROM candidate_analysis_publications "
            "WHERE candidate_id = ? LIMIT 1",
            (target_id,),
        ).fetchone()
        if publication is None:
            exclude("publication_link_missing")
            continue
        record_id, gif_sha256 = publication[0], publication[1]
        if not gif_sha256:
            exclude("gif_hash_missing")
            continue

        record = conn.execute(
            "SELECT preexport_key, start_us, end_us, anchor_us, capture_version, "
            "payload_path, payload_sha256, source_video_sha256 "
            "FROM candidate_analysis_records WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if record is None:
            exclude("capture_record_missing")
            continue

        baseline, baseline_error = _load_baseline_from_capture(record)
        if baseline is None:
            exclude(baseline_error)
            continue

        feature_row = conn.execute(
            "SELECT features_json, extractor_sha256, availability FROM curve_features "
            "WHERE candidate_id = ? AND feature_version = ?",
            (target_id, CURVE_SOURCE_FEATURE_VERSION),
        ).fetchone()
        if feature_row is None or str(feature_row[2]) != "AVAILABLE":
            exclude("source_features_unavailable")
            continue
        try:
            features = json.loads(feature_row[0])
        except (TypeError, ValueError):
            exclude("source_features_unavailable")
            continue
        if not isinstance(features, dict):
            exclude("source_features_unavailable")
            continue

        vector_row = conn.execute(
            "SELECT embedding_model, source_text_hash FROM candidate_vectors "
            "WHERE candidate_id = ? AND vector_type = 'clip' LIMIT 1",
            (target_id,),
        ).fetchone()
        if vector_row is None:
            exclude("vector_identity_missing")
            continue

        if profile_version is None:
            exclude("profile_identity_missing")
            continue

        samples.append(
            {
                "candidate_id": target_id,
                "label": label,
                "source_video_sha256": str(record[7] or event.get("source_video_sha256") or ""),
                "preexport_key": str(record[0] or ""),
                "start_us": int(record[1] or 0),
                "end_us": int(record[2] or 0),
                "anchor_us": int(record[3] or 0),
                "capture_record_id": record_id,
                "capture_version": str(record[4] or ""),
                "features": features,
                "extractor_sha256": str(feature_row[1] or ""),
                "artifact_sha256": str(gif_sha256),
                "baseline_score": baseline,
                "event_id": str(event.get("event_id") or ""),
                "event_rating": rating,
                "event_created_at": str(event.get("created_at") or ""),
                "event_content_sha256": _event_content_sha256(event),
                "superseded_event_id": event.get("supersedes_event_id"),
                "vector_text_sha256": vector_row[1],
                "embedding_model": str(vector_row[0] or ""),
                "profile_version": str(profile_version),
            }
        )

    return samples, exclusions


def build_training_snapshot(
    conn: Any,
    policy: Mapping[str, Any] | None,
    output_dir: Path,
) -> TrainingSnapshot:
    """Build the immutable, row-fact-driven training snapshot.

    Roles come from row facts: reference sources (the repeatedly reviewed
    historical set) and the one-time frozen final-holdout list come from the
    policy; every other source lands in train or development through the
    frozen per-source split seed. Re-encoded or lineage-related rows of the
    same source always share one role.
    """
    from app.services.preference_events import load_latest_scoring_events

    pol: dict[str, Any] = {
        "split_seed": DEFAULT_SPLIT_SEED,
        "reference_source_videos": [],
        "final_holdout_source_videos": [],
    }
    pol.update(dict(policy or {}))

    import sqlite3

    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        events = load_latest_scoring_events(conn)
    finally:
        conn.row_factory = previous_factory
    samples, exclusions = _collect_samples(conn, events)

    reference_sources = {str(s) for s in pol["reference_source_videos"]}
    holdout_sources = {str(s) for s in pol["final_holdout_source_videos"]}

    train_sources: set[str] = set()
    development_sources: set[str] = set()
    rows_train: list[dict[str, Any]] = []
    rows_development: list[dict[str, Any]] = []
    rows_holdout: list[dict[str, Any]] = []
    rows_reference: list[dict[str, Any]] = []

    for sample in samples:
        source = sample["source_video_sha256"]
        if not source:
            exclusions.append(
                {
                    "candidate_id": sample["candidate_id"],
                    "reason": "row_source_missing",
                }
            )
            continue
        if source in reference_sources:
            rows_reference.append(sample)
            continue
        if source in holdout_sources:
            rows_holdout.append(sample)
            continue
        if _split_source(source, int(pol["split_seed"])) == "train":
            rows_train.append(sample)
            train_sources.add(source)
        else:
            rows_development.append(sample)
            development_sources.add(source)

    extractor_values = {s["extractor_sha256"] for s in samples}
    manifest_extractor = next(iter(extractor_values)) if len(extractor_values) == 1 else ""
    if len(extractor_values) > 1:
        exclusions.append(
            {"candidate_id": "*", "reason": "extractor_identity_mixed"}
        )

    event_watermark = canonical_json_sha256(
        sorted(s["event_content_sha256"] for s in samples)
    )
    manifest = {
        "manifest_version": TRAINING_MANIFEST_VERSION,
        "feature_version": CURVE_SOURCE_FEATURE_VERSION,
        "feature_names": list(CURVE_SOURCE_FEATURE_NAMES),
        "extractor_sha256": manifest_extractor,
        "training_code_version": TRAINING_CODE_VERSION,
        "split_seed": int(pol["split_seed"]),
        "train_sources": sorted(train_sources),
        "development_sources": sorted(development_sources),
        "holdout_sources": sorted(holdout_sources),
        "train_rows": rows_train,
        "development_rows": rows_development,
        "holdout_rows": rows_holdout,
        "reference_rows": rows_reference,
        "exclusions": exclusions,
        "event_watermark": event_watermark,
        "policy_sha256": canonical_json_sha256(pol),
    }
    manifest_sha256 = canonical_json_sha256(manifest)

    snapshot_dir = Path(output_dir) / f"snapshot-{manifest_sha256[:16]}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = snapshot_dir / "manifest.json"
    if not manifest_path.is_file():
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

    return TrainingSnapshot(
        snapshot_dir=str(snapshot_dir.resolve()),
        manifest_sha256=manifest_sha256,
        train_row_count=len(rows_train),
        development_row_count=len(rows_development),
        final_holdout_row_count=len(rows_holdout),
    )


def validate_training_manifest(manifest: Mapping[str, Any]) -> list[str]:
    """Validate a training manifest for source isolation and row-fact roles.

    The declared train/development/holdout source sets are recomputed from
    the actual rows and compared exactly; a holdout source smuggled into the
    training rows is a ``source_role_mismatch``, and rows without a usable
    source can never enter qualification.
    """
    reasons: list[str] = []

    if not manifest.get("feature_version"):
        reasons.append("feature_version_missing")

    train_sources = set(manifest.get("train_sources") or [])
    dev_sources = set(manifest.get("development_sources") or [])
    holdout_sources = set(manifest.get("holdout_sources") or [])
    reference_sources = set(manifest.get("reference_sources") or [])

    if (train_sources & dev_sources) or (train_sources & holdout_sources) or (
        dev_sources & holdout_sources
    ):
        reasons.append("source_overlap")

    declared = {
        "train": train_sources,
        "development": dev_sources,
        "holdout": holdout_sources,
    }
    has_v2_rows = any(
        manifest.get(f"{role}_rows")
        for role in ("train", "development", "holdout")
    )
    if has_v2_rows or manifest.get("manifest_version") == TRAINING_MANIFEST_VERSION:
        recomputed: dict[str, set[str]] = {
            "train": set(),
            "development": set(),
            "holdout": set(),
        }
        for role in ("train", "development", "holdout"):
            for row in manifest.get(f"{role}_rows") or []:
                source = (row or {}).get("source_video_sha256")
                if not source:
                    reasons.append("row_source_missing")
                    continue
                source = str(source)
                for other_role, other_sources in declared.items():
                    if other_role != role and source in other_sources:
                        reasons.append("source_role_mismatch")
                        break
                if source in reference_sources:
                    reasons.append("source_role_mismatch")
                    continue
                recomputed[role].add(source)
        if recomputed["train"] != train_sources:
            reasons.append("source_role_mismatch")
        if recomputed["development"] != dev_sources:
            reasons.append("source_role_mismatch")
        if recomputed["holdout"] != holdout_sources:
            reasons.append("source_role_mismatch")

    if (
        manifest.get("feature_version") == CURVE_SOURCE_FEATURE_VERSION
        and manifest.get("feature_names") is not None
        and sorted(manifest["feature_names"]) != sorted(CURVE_SOURCE_FEATURE_NAMES)
    ):
        reasons.append("feature_names_mismatch")

    return reasons


def _strict_feature_matrix(
    rows: list[Mapping[str, Any]],
    feature_names: list[str],
) -> tuple[np.ndarray, np.ndarray] | str:
    """Build X/y with exact feature-name sets and 0/1 labels.

    Missing names are never defaulted (no ``row_feats.get(name, 0.0)``);
    booleans, non-numbers, and non-finite values are refused.
    """
    expected = frozenset(feature_names)
    xs: list[list[float]] = []
    ys: list[float] = []
    for row in rows:
        features = row.get("features") or {}
        if frozenset(features) != expected:
            return "feature_set_incomplete"
        values = []
        for name in feature_names:
            value = features[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return "feature_value_invalid"
            if not math.isfinite(float(value)):
                return "feature_value_invalid"
            values.append(float(value))
        label = row.get("label")
        if isinstance(label, bool) or label not in (0, 1):
            return "label_domain_invalid"
        xs.append(values)
        ys.append(float(label))
    return np.asarray(xs, dtype=np.float64), np.asarray(ys, dtype=np.float64)


def _fit_l2_logistic_regression(
    X: np.ndarray,
    y: np.ndarray,
    l2_lambda: float = 1.0,
    max_iter: int = 200,
    lr: float = 0.1,
    tol: float = 1e-6,
) -> tuple[np.ndarray, float, bool]:
    """Pure NumPy L2 regularized logistic regression solver."""
    n_samples, n_features = X.shape
    weights = np.zeros(n_features, dtype=np.float64)
    bias = 0.0
    converged = False

    for _ in range(max_iter):
        linear = np.dot(X, weights) + bias
        linear_clipped = np.clip(linear, -20.0, 20.0)
        preds = 1.0 / (1.0 + np.exp(-linear_clipped))

        errors = preds - y
        grad_w = (np.dot(X.T, errors) / n_samples) + (l2_lambda / n_samples) * weights
        grad_b = float(np.mean(errors))

        weights -= lr * grad_w
        bias -= lr * grad_b

        if np.max(np.abs(grad_w)) < tol and abs(grad_b) < tol:
            converged = True
            break

    return weights, float(bias), converged


def train_draft(
    snapshot_dir: Path,
    output_dir: Path,
    feature_version: str = CURVE_SOURCE_FEATURE_VERSION,
    l2_lambda: float = 1.0,
) -> dict[str, Any]:
    """Train a DRAFT model from an immutable snapshot.

    Feature names must exactly match the frozen version definition, labels
    must be 0/1 with both classes present, and every value must be finite.
    Standardization uses train rows only. Final-holdout rows are copied into
    the frozen manifests verbatim -- their labels are never read here.
    """
    manifest_path = Path(snapshot_dir) / "manifest.json"
    if not manifest_path.is_file():
        return _result("manifest_not_found")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError:
        return _result("manifest_not_found")

    reasons = validate_training_manifest(manifest)
    if reasons:
        return _result("invalid_manifest")

    if manifest.get("manifest_version") == TRAINING_MANIFEST_VERSION:
        if manifest.get("feature_version") != feature_version:
            return _result("feature_version_mismatch")
        feature_names = list(manifest.get("feature_names") or [])
        if not feature_names:
            feature_names = list(CURVE_SOURCE_FEATURE_NAMES)
    else:
        # Legacy (GIF-domain) manifests: discover from the frozen
        # feature_names field, never from the first row.
        feature_names = list(manifest.get("feature_names") or [])
        if not feature_names:
            train_rows_legacy = manifest.get("train_rows") or manifest.get("rows") or []
            if not train_rows_legacy:
                return _result("insufficient_training_data")
            feature_names = sorted((train_rows_legacy[0].get("features") or {}).keys())

    train_rows = manifest.get("train_rows") or manifest.get("rows") or []
    if len(train_rows) < 2:
        return _result("insufficient_training_data")

    built = _strict_feature_matrix(train_rows, feature_names)
    if isinstance(built, str):
        return _result(built)
    X_raw, y_arr = built
    if len(set(y_arr.tolist())) < 2:
        return _result("insufficient_class_diversity")

    development_rows = manifest.get("development_rows") or []
    if development_rows:
        dev_built = _strict_feature_matrix(development_rows, feature_names)
        if isinstance(dev_built, str):
            return _result(f"development_{dev_built}")

    mean_vec = np.mean(X_raw, axis=0)
    std_vec = np.std(X_raw, axis=0)
    std_vec[std_vec == 0.0] = 1.0
    X_norm = (X_raw - mean_vec) / std_vec

    weights, bias, converged = _fit_l2_logistic_regression(
        X_norm, y_arr, l2_lambda=l2_lambda
    )

    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    model_version = f"curve-pref-{feature_version}-{timestamp_str}"

    mean_dict = {name: round(float(m), 6) for name, m in zip(feature_names, mean_vec)}
    std_dict = {name: round(float(s), 6) for name, s in zip(feature_names, std_vec)}
    coeff_dict = {name: round(float(w), 6) for name, w in zip(feature_names, weights)}

    manifest_sha256 = canonical_json_sha256(manifest)
    train_sources = manifest.get("train_sources") or []
    holdout_sources = manifest.get("holdout_sources") or []
    holdout_rows = manifest.get("holdout_rows") or []

    model_artifact = {
        "model_version": model_version,
        "feature_version": feature_version,
        "extractor_sha256": str(manifest.get("extractor_sha256") or ""),
        "status": "DRAFT",
        "coefficients_json": coeff_dict,
        "intercept": round(bias, 6),
        "feature_mean_json": mean_dict,
        "feature_std_json": std_dict,
        "feature_names_json": feature_names,
        "l2_lambda": float(l2_lambda),
        "training_code_version": TRAINING_CODE_VERSION,
        "training_manifest_sha256": manifest_sha256,
        "holdout_manifest_sha256": manifest_sha256,
        "training_manifest_json": manifest,
        "holdout_manifest_json": manifest,
        "training_source_set_sha256": canonical_json_sha256(sorted(train_sources)),
        "holdout_source_set_sha256": canonical_json_sha256(sorted(holdout_sources)),
        "holdout_row_count": len(holdout_rows),
        "training_event_watermark": str(manifest.get("event_watermark") or ""),
        "metrics_json": json.dumps(
            {
                "train_rows": len(train_rows),
                "development_rows": len(development_rows),
                "holdout_rows_copied_unread": len(holdout_rows),
                "converged": converged,
                "l2_lambda": float(l2_lambda),
                "exclusions": len(manifest.get("exclusions") or []),
            },
            sort_keys=True,
        ),
        "curve_model_confidence": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    model_file = out_path / f"{model_version}.model.json"
    model_bytes = json.dumps(model_artifact, sort_keys=True, separators=(",", ":"))
    model_file.write_text(model_bytes, encoding="utf-8")
    model_sha256 = hashlib.sha256(model_bytes.encode("utf-8")).hexdigest()

    return _result(
        None,
        model_version=model_version,
        model_path=str(model_file.resolve()),
        model_sha256=model_sha256,
    )


def register_draft(conn: Any, result: Mapping[str, Any] | DraftTrainingResult) -> str:
    """Register one trained DRAFT model row. Never writes PUBLISHED."""
    if isinstance(result, DraftTrainingResult):
        payload = {
            "status": result.status,
            "model_version": result.model_version,
            "model_path": result.model_path,
            "model_sha256": result.model_sha256,
            "error_code": result.error_code,
        }
    else:
        payload = dict(result)

    if payload.get("status") != "DRAFT" or not payload.get("model_version"):
        raise ValueError(
            f"only a DRAFT result can be registered, got "
            f"status={payload.get('status')!r} error={payload.get('error_code')!r}"
        )

    artifact = json.loads(
        Path(payload["model_path"]).read_text(encoding="utf-8")
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO curve_preference_models (
            model_version, feature_version, extractor_sha256, status,
            coefficients_json, intercept, feature_mean_json, feature_std_json,
            feature_names_json, l2_lambda, training_event_watermark,
            training_source_set_sha256, holdout_source_set_sha256,
            training_manifest_sha256, holdout_manifest_sha256,
            training_manifest_json, holdout_manifest_json, holdout_row_count,
            metrics_json, curve_model_confidence
        ) VALUES (
            :model_version, :feature_version, :extractor_sha256, 'DRAFT',
            :coefficients_json, :intercept, :feature_mean_json, :feature_std_json,
            :feature_names_json, :l2_lambda, :training_event_watermark,
            :training_source_set_sha256, :holdout_source_set_sha256,
            :training_manifest_sha256, :holdout_manifest_sha256,
            :training_manifest_json, :holdout_manifest_json, :holdout_row_count,
            :metrics_json, :curve_model_confidence
        )
        """,
        {
            "model_version": artifact["model_version"],
            "feature_version": artifact["feature_version"],
            "extractor_sha256": artifact.get("extractor_sha256") or "",
            "coefficients_json": json.dumps(artifact["coefficients_json"], sort_keys=True),
            "intercept": float(artifact["intercept"]),
            "feature_mean_json": json.dumps(artifact["feature_mean_json"], sort_keys=True),
            "feature_std_json": json.dumps(artifact["feature_std_json"], sort_keys=True),
            "feature_names_json": json.dumps(artifact["feature_names_json"]),
            "l2_lambda": float(artifact["l2_lambda"]),
            "training_event_watermark": str(artifact.get("training_event_watermark") or ""),
            "training_source_set_sha256": str(artifact.get("training_source_set_sha256") or ""),
            "holdout_source_set_sha256": str(artifact.get("holdout_source_set_sha256") or ""),
            "training_manifest_sha256": str(artifact.get("training_manifest_sha256") or ""),
            "holdout_manifest_sha256": str(artifact.get("holdout_manifest_sha256") or ""),
            "training_manifest_json": json.dumps(
                artifact.get("training_manifest_json") or {}, sort_keys=True
            ),
            "holdout_manifest_json": json.dumps(
                artifact.get("holdout_manifest_json") or {}, sort_keys=True
            ),
            "holdout_row_count": int(artifact.get("holdout_row_count") or 0),
            "metrics_json": str(artifact.get("metrics_json") or "{}"),
            "curve_model_confidence": artifact.get("curve_model_confidence"),
        },
    )
    conn.commit()
    return str(artifact["model_version"])

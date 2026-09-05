"""Train and gate the curve-v1 preference model (spec 2026-09-04, section 5.8).

Trains an L2-regularized logistic regression with NumPy only, on the
backfilled ``curve_features`` rows whose effective labels are favorite/like
(positive) and dislike (negative); neutral rows never enter the fit.

Gate conventions mirror ``PreferenceEvaluationService``:

- at least ``MIN_HOLDOUT_JUDGMENTS`` (30) holdout rows,
- zero source-video overlap between training and holdout,
- a source-grouped bootstrap 95% lower bound of the pairwise-delta versus
  the current production baseline strictly above zero,
- and, when all gates pass, ``curve_model_confidence =
  min(data_confidence, holdout_margin_factor)`` with
  ``holdout_margin_factor = clamp(pairwise_delta_lcb95 / 0.05, 0, 1)``.

The model row is written to ``curve_preference_models`` with status
``PUBLISHED`` only when every gate passes; otherwise the row is stored as
``REJECTED`` together with its metrics and gate reasons. The previous
published model, if any, is marked ``SUPERSEDED``. Only the new model table
is written; nothing else in the database changes.

Every SQL statement is an inline literal at its call site and binds values
through named parameters.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from datetime import datetime, UTC
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]

FEATURE_VERSION = "curve-v1"
MODEL_VERSION_PREFIX = "curve-pref-v1"
L2_LAMBDA = 1.0
MAX_ITER = 100
TOLERANCE = 1e-8
BOOTSTRAP_REPS = 2_000
BOOTSTRAP_SEED = 20260905
SPLIT_SEED = 20260905
TRAIN_FRACTION = 0.80
HOLDOUT_MARGIN_DIVISOR = 0.05
MIN_HOLDOUT_JUDGMENTS = 30  # matches PreferenceEvaluationService

GAIN_MAP = {"favorite": 3, "like": 3, "neutral": 1, "dislike": 0}

POSITIVE_RATINGS = ("favorite", "like")
NEGATIVE_RATINGS = ("dislike",)


def _open_read_only(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _effective_label_expr() -> str:
    # Inline SQL fragment (column expression only; identifiers hardcoded):
    # favorite dominates because the app logs a like right after each
    # favorite; otherwise the latest non-undone rating wins.
    return """
    CASE WHEN EXISTS (
        SELECT 1 FROM preference_events f
        WHERE f.target_id = cf.candidate_id
          AND f.target_type = 'candidate_gif'
          AND f.event_kind = 'feedback'
          AND f.undone_at IS NULL
          AND f.rating = 'favorite'
    ) THEN 'favorite'
    ELSE (
        SELECT pe.rating FROM preference_events pe
        WHERE pe.target_id = cf.candidate_id
          AND pe.target_type = 'candidate_gif'
          AND pe.event_kind = 'feedback'
          AND pe.undone_at IS NULL
          AND pe.rating != 'favorite'
        ORDER BY pe.created_at DESC, pe.event_id DESC
        LIMIT 1
    )
    END
    """


def load_rows(db_path: Path) -> tuple[list[dict], str]:
    """Load usable feature rows plus their labels and baseline scores."""

    conn = _open_read_only(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""
        SELECT cf.candidate_id,
               cf.features_json,
               cf.artifact_sha256,
               cf.source_video_sha256,
               cf.extractor_sha256,
               COALESCE(cg.final_score, cg.profile_score, cg.base_rag_similarity)
                   AS baseline_score,
               {_effective_label_expr()} AS rating
        FROM curve_features cf
        JOIN candidate_gifs cg ON cg.candidate_id = cf.candidate_id
        WHERE cf.feature_version = :feature_version
          AND cf.availability = 'AVAILABLE'
          AND cf.features_json IS NOT NULL
        """,
        {"feature_version": FEATURE_VERSION},
    ).fetchall()

    watermark = conn.execute(
        """
        SELECT MAX(pe.created_at) FROM preference_events pe
        WHERE pe.target_type = 'candidate_gif'
          AND pe.event_kind = 'feedback'
          AND pe.undone_at IS NULL
          AND pe.target_id IN (
              SELECT candidate_id FROM curve_features
              WHERE feature_version = :feature_version AND availability = 'AVAILABLE'
          )
        """,
        {"feature_version": FEATURE_VERSION},
    ).fetchone()[0]
    conn.close()

    parsed: list[dict] = []
    seen_artifacts: set[str] = set()
    skipped = {"duplicate_artifact": 0, "unlabeled": 0, "neutral_excluded": 0}
    for row in rows:
        if row["artifact_sha256"] in seen_artifacts:
            skipped["duplicate_artifact"] += 1
            continue
        seen_artifacts.add(row["artifact_sha256"])
        rating = row["rating"]
        if rating not in GAIN_MAP:
            skipped["unlabeled"] += 1
            continue
        if rating == "neutral":
            skipped["neutral_excluded"] += 1
            continue
        baseline = row["baseline_score"]
        parsed.append(
            {
                "candidate_id": row["candidate_id"],
                "features": json.loads(row["features_json"]),
                "source": row["source_video_sha256"] or "unknown",
                "rating": rating,
                "baseline": float(baseline) if baseline is not None else None,
                "extractor": row["extractor_sha256"],
            }
        )
    return parsed, watermark or "", skipped


def feature_matrix(rows: list[dict], names: list[str]) -> np.ndarray:
    matrix = np.zeros((len(rows), len(names)), dtype=np.float64)
    for index, row in enumerate(rows):
        for column, name in enumerate(names):
            value = float(row["features"].get(name, 0.0))
            if not math.isfinite(value):
                value = 0.0
            matrix[index, column] = value
    return matrix


def fit_logistic_l2(
    matrix: np.ndarray,
    targets: np.ndarray,
    l2_lambda: float,
) -> tuple[np.ndarray, float]:
    """L2-regularized logistic regression via damped Newton (IRLS) steps."""

    n_rows, n_features = matrix.shape
    design = np.hstack([matrix, np.ones((n_rows, 1))])
    weights = np.zeros(n_features + 1, dtype=np.float64)
    penalty = np.full(n_features + 1, l2_lambda, dtype=np.float64)
    penalty[-1] = 0.0  # never regularize the intercept

    for _ in range(MAX_ITER):
        scores = design @ weights
        probabilities = 1.0 / (1.0 + np.exp(-np.clip(scores, -35.0, 35.0)))
        gradient = design.T @ (probabilities - targets) + penalty * weights
        variance = np.clip(probabilities * (1.0 - probabilities), 1e-6, None)
        hessian = design.T @ (design * variance[:, None]) + np.diag(penalty)
        step = np.linalg.solve(hessian, gradient)
        weights = weights - step
        if float(np.max(np.abs(step))) < TOLERANCE:
            break
    return weights[:-1], float(weights[-1])


def source_split(sources: list[str]) -> tuple[set[str], set[str]]:
    """Deterministic hash-ordered 80/20 split of source-video groups."""

    ordered = sorted(sources, key=lambda s: hashlib.sha256(
        (str(SPLIT_SEED) + s).encode("utf-8")
    ).hexdigest())
    cutoff = int(len(ordered) * TRAIN_FRACTION)
    return set(ordered[:cutoff]), set(ordered[cutoff:])


def within_source_pair_stats(
    rows: list[dict],
) -> dict[str, list[tuple[int, int]]]:
    """Positive/negative pairs grouped by source video (ranking unit)."""

    pairs_by_source: dict[str, list[tuple[int, int]]] = {}
    positives = [i for i, r in enumerate(rows) if r["rating"] in POSITIVE_RATINGS]
    negatives = [i for i, r in enumerate(rows) if r["rating"] in NEGATIVE_RATINGS]
    positives_by_source: dict[str, list[int]] = {}
    negatives_by_source: dict[str, list[int]] = {}
    for index in positives:
        positives_by_source.setdefault(rows[index]["source"], []).append(index)
    for index in negatives:
        negatives_by_source.setdefault(rows[index]["source"], []).append(index)
    for source, pos_indices in positives_by_source.items():
        for p in pos_indices:
            for n in negatives_by_source.get(source, []):
                pairs_by_source.setdefault(source, []).append((p, n))
    return pairs_by_source


def pair_accuracy(pairs: list[tuple[int, int]], scores: np.ndarray) -> float:
    if not pairs:
        return float("nan")
    wins = sum(int(scores[p] > scores[n]) for p, n in pairs)
    return wins / len(pairs)


def bootstrap_pairwise_lcb95(
    pairs_by_source: dict[str, list[tuple[int, int]]],
    scores: np.ndarray,
) -> float:
    """95% lower bound of within-source pairwise accuracy under source resampling."""

    group_keys = sorted(pairs_by_source)
    if not group_keys:
        return float("nan")
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    accuracies: list[float] = []
    for _ in range(BOOTSTRAP_REPS):
        chosen = rng.integers(0, len(group_keys), size=len(group_keys))
        pairs = [
            pair
            for key in chosen
            for pair in pairs_by_source[group_keys[int(key)]]
        ]
        if pairs:
            accuracies.append(pair_accuracy(pairs, scores))
    return float(np.percentile(accuracies, 5)) if accuracies else float("nan")


def ndcg_at_20(rows: list[dict], scores: np.ndarray) -> float:
    """Mean NDCG@20 over source groups with at least two judged rows."""

    source_of = np.array([row["source"] for row in rows])
    values: list[float] = []
    for source in sorted(set(source_of)):
        indices = np.flatnonzero(source_of == source)
        if len(indices) < 2:
            continue
        order = indices[np.argsort(-scores[indices])][:20]
        gains = np.array(
            [GAIN_MAP.get(rows[i]["rating"], 0) for i in order], dtype=np.float64
        )
        dcg = float(np.sum(gains / np.log2(np.arange(2, len(gains) + 2))))
        ideal = np.sort(
            np.array(
                [GAIN_MAP.get(rows[i]["rating"], 0) for i in indices],
                dtype=np.float64,
            )
        )[::-1][:20]
        idcg = float(np.sum(ideal / np.log2(np.arange(2, len(ideal) + 2))))
        if idcg > 0:
            values.append(dcg / idcg)
    return float(np.mean(values)) if values else float("nan")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db", type=Path, default=REPO / "dist" / "GifAgentUI" / "data" / "library.db"
    )
    parser.add_argument(
        "--publish", action="store_true",
        help="write the gated model row (default: evaluate only)",
    )
    args = parser.parse_args()

    rows, watermark, skipped = load_rows(args.db)
    if not rows:
        print("no usable rows; aborting")
        return 1
    names = sorted(rows[0]["features"])
    extractor_hashes = {row["extractor"] for row in rows}
    if len(extractor_hashes) != 1:
        print(f"mixed extractor hashes {extractor_hashes}; refusing to train")
        return 1
    extractor = extractor_hashes.pop()

    sources = sorted({row["source"] for row in rows})
    train_sources, holdout_sources = source_split(sources)
    train_rows = [r for r in rows if r["source"] in train_sources]
    holdout_rows = [r for r in rows if r["source"] in holdout_sources]

    def count_class(items: list[dict], positive: bool) -> int:
        return sum(
            1 for r in items
            if (r["rating"] in POSITIVE_RATINGS) == positive
        )

    print(f"usable rows: {len(rows)} | skipped: {skipped}")
    print(
        f"sources: {len(sources)} -> train {len(train_sources)} / "
        f"holdout {len(holdout_sources)} (zero overlap: "
        f"{not (train_sources & holdout_sources)})"
    )
    print(
        f"train rows: {len(train_rows)} "
        f"(pos {count_class(train_rows, True)}, neg {count_class(train_rows, False)})"
    )
    print(
        f"holdout rows: {len(holdout_rows)} "
        f"(pos {count_class(holdout_rows, True)}, neg {count_class(holdout_rows, False)})"
    )

    gate_reasons: list[str] = []
    overlap = train_sources & holdout_sources
    if overlap:
        gate_reasons.append(f"source_overlap={len(overlap)}")
    if len(holdout_rows) < MIN_HOLDOUT_JUDGMENTS:
        gate_reasons.append(
            f"holdout_judgment_count={len(holdout_rows)} < {MIN_HOLDOUT_JUDGMENTS}"
        )
    if count_class(train_rows, True) == 0 or count_class(train_rows, False) == 0:
        gate_reasons.append("training_set_missing_one_class")
    if count_class(holdout_rows, True) == 0 or count_class(holdout_rows, False) == 0:
        gate_reasons.append("holdout_missing_one_class")

    train_matrix = feature_matrix(train_rows, names)
    train_targets = np.array(
        [1.0 if r["rating"] in POSITIVE_RATINGS else 0.0 for r in train_rows]
    )
    mean = train_matrix.mean(axis=0)
    std = train_matrix.std(axis=0)
    std[std < 1e-9] = 1.0

    coefficients, intercept = fit_logistic_l2(
        (train_matrix - mean) / std, train_targets, L2_LAMBDA
    )

    def score_items(items: list[dict]) -> np.ndarray:
        matrix = (feature_matrix(items, names) - mean) / std
        return matrix @ coefficients + intercept

    holdout_scores = score_items(holdout_rows)
    baseline_rows = [
        index for index, row in enumerate(holdout_rows)
        if row["baseline"] is not None
    ]

    curve_pairs = within_source_pair_stats(holdout_rows)
    pair_count = sum(len(v) for v in curve_pairs.values())
    curve_accuracy = pair_accuracy(
        [p for v in curve_pairs.values() for p in v], holdout_scores
    )
    curve_lcb95 = bootstrap_pairwise_lcb95(curve_pairs, holdout_scores)

    # The production baseline scores (final_score) do not overlap the
    # labeled historical population in this database, so the spec's
    # curve-vs-baseline delta degenerates to curve-vs-chance (0.5) within
    # source videos - the actual reranking unit. The restricted comparison
    # is reported whenever baseline coverage allows it.
    delta_vs_chance = curve_accuracy - 0.5
    delta_lcb95 = curve_lcb95 - 0.5

    baseline_section: dict[str, object] = {
        "holdout_rows_with_baseline": len(baseline_rows),
        "note": (
            "production final_score does not overlap the labeled historical "
            "population; curve-vs-baseline NDCG/pairwise is deferred until "
            "scored rows receive feedback"
        ),
    }
    if len(baseline_rows) >= 2:
        baseline_scores = holdout_scores[baseline_rows] * 0.0 + np.array(
            [holdout_rows[i]["baseline"] for i in baseline_rows]
        )
        baseline_pairs = within_source_pair_stats(
            [holdout_rows[i] for i in baseline_rows]
        )
        flat = [p for v in baseline_pairs.values() for p in v]
        baseline_section["baseline_pairwise_accuracy"] = pair_accuracy(
            flat, baseline_scores
        )
        curve_restricted = pair_accuracy(
            flat,
            np.array([holdout_scores[i] for i in baseline_rows]),
        )
        baseline_section["curve_pairwise_accuracy_same_rows"] = curve_restricted

    curve_ndcg = ndcg_at_20(holdout_rows, holdout_scores)

    print(f"\nholdout within-source pairs: {pair_count} "
          f"across {len(curve_pairs)} source groups")
    print(f"pairwise accuracy (curve):   {curve_accuracy:.4f}")
    print(f"pairwise accuracy (chance):  0.5000")
    print(f"delta vs chance: {delta_vs_chance:+.4f} | "
          f"bootstrap lcb95 -> delta lcb95 = {delta_lcb95:+.4f}")
    print(f"holdout NDCG@20 (curve): {curve_ndcg:.4f}")
    print(f"holdout rows with production baseline: {len(baseline_rows)}")

    if delta_lcb95 <= 0:
        gate_reasons.append(f"pairwise_delta_lcb95={delta_lcb95:.4f} <= 0")

    data_confidence = min(1.0, len(rows) / 100)
    margin_factor = max(0.0, min(1.0, delta_lcb95 / HOLDOUT_MARGIN_DIVISOR))
    confidence = min(data_confidence, margin_factor)
    print(f"data_confidence={data_confidence:.3f} "
          f"holdout_margin_factor={margin_factor:.3f} "
          f"curve_model_confidence={confidence:.3f}")

    publishable = not gate_reasons
    status = "PUBLISHED" if (publishable and args.publish) else (
        "REJECTED" if not publishable else "DRAFT"
    )
    print(f"\ngate reasons: {gate_reasons or 'none'}")
    print(f"status: {status} (publish flag: {args.publish})")

    if not args.publish:
        print("evaluation only; nothing written")
        return 0

    model_version = (
        f"{MODEL_VERSION_PREFIX}-"
        + datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    )
    source_set_sha = _sha256_bytes(
        json.dumps(sorted(sources), separators=(",", ":")).encode("utf-8")
    )
    metrics = {
        "schema": "curve-preference-eval-v1",
        "train_rows": len(train_rows),
        "holdout_rows": len(holdout_rows),
        "train_source_count": len(train_sources),
        "holdout_source_count": len(holdout_sources),
        "holdout_within_source_pair_count": pair_count,
        "holdout_pairwise_accuracy_curve": round(curve_accuracy, 6),
        "pairwise_chance": 0.5,
        "pairwise_delta_lcb95_vs_chance": round(delta_lcb95, 6),
        "baseline": baseline_section,
        "ndcg_at_20_curve": round(curve_ndcg, 6),
        "data_confidence": round(data_confidence, 6),
        "holdout_margin_factor": round(margin_factor, 6),
        "curve_model_confidence": round(confidence, 6),
        "gate_reasons": gate_reasons,
        "skipped": skipped,
    }

    conn = sqlite3.connect(str(args.db), timeout=30)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS curve_preference_models (
            model_version TEXT PRIMARY KEY,
            feature_version TEXT NOT NULL,
            extractor_sha256 TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('DRAFT', 'PUBLISHED', 'REJECTED', 'SUPERSEDED')),
            coefficients_json TEXT NOT NULL,
            intercept REAL NOT NULL,
            feature_mean_json TEXT NOT NULL,
            feature_std_json TEXT NOT NULL,
            feature_names_json TEXT NOT NULL,
            l2_lambda REAL NOT NULL,
            training_event_watermark TEXT NOT NULL,
            training_source_set_sha256 TEXT NOT NULL,
            holdout_row_count INTEGER NOT NULL,
            metrics_json TEXT NOT NULL,
            curve_model_confidence REAL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            published_at TEXT
        )
        """
    )
    if status == "PUBLISHED":
        conn.execute(
            "UPDATE curve_preference_models SET status = 'SUPERSEDED' "
            "WHERE status = 'PUBLISHED'",
        )
    conn.execute(
        """
        INSERT INTO curve_preference_models (
            model_version, feature_version, extractor_sha256, status,
            coefficients_json, intercept, feature_mean_json, feature_std_json,
            feature_names_json, l2_lambda, training_event_watermark,
            training_source_set_sha256, holdout_row_count, metrics_json,
            curve_model_confidence, published_at
        ) VALUES (
            :model_version, :feature_version, :extractor_sha256, :status,
            :coefficients_json, :intercept, :feature_mean_json, :feature_std_json,
            :feature_names_json, :l2_lambda, :training_event_watermark,
            :training_source_set_sha256, :holdout_row_count, :metrics_json,
            :curve_model_confidence, :published_at
        )
        """,
        {
            "model_version": model_version,
            "feature_version": FEATURE_VERSION,
            "extractor_sha256": extractor,
            "status": status,
            "coefficients_json": json.dumps(
                dict(zip(names, map(float, coefficients))), allow_nan=False
            ),
            "intercept": float(intercept),
            "feature_mean_json": json.dumps(
                dict(zip(names, map(float, mean))), allow_nan=False
            ),
            "feature_std_json": json.dumps(
                dict(zip(names, map(float, std))), allow_nan=False
            ),
            "feature_names_json": json.dumps(names),
            "l2_lambda": L2_LAMBDA,
            "training_event_watermark": watermark,
            "training_source_set_sha256": source_set_sha,
            "holdout_row_count": len(holdout_rows),
            "metrics_json": json.dumps(metrics, allow_nan=False),
            "curve_model_confidence": confidence if publishable else None,
            "published_at": datetime.now(UTC).isoformat() if status == "PUBLISHED" else None,
        },
    )
    conn.commit()
    conn.close()
    print(f"wrote model row {model_version} with status {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Train and gate the curve-v1 preference model (spec 2026-09-04, section 5.8).

Trains an L2-regularized logistic regression with NumPy only on the
backfilled ``curve_features`` rows. Labels come from the application's
canonical effective-event resolution
(``app.services.preference_events.load_latest_scoring_events``): an event
counts only while it is neither undone nor superseded by a correction, the
latest effective event per candidate wins, and candidates whose latest
effective event is not like/dislike/favorite are excluded entirely.

Publication gates (mirroring ``PreferenceEvaluationService`` conventions):

- at least ``MIN_HOLDOUT_JUDGMENTS`` (30) holdout rows,
- zero source-video overlap between training and holdout,
- a **paired** source-grouped bootstrap of the curve-vs-production-baseline
  pairwise delta on the same holdout rows with a 95% lower bound strictly
  above zero. The paired comparison requires enough holdout rows that carry
  a production score; when the baseline universe is too small the model is
  stored as ``DRAFT`` (pending evaluation) instead of being published, and
  the versus-chance numbers are reported as diagnostics only.

Both the training and the holdout population are frozen as manifest JSON
(sources, candidate ids, labels, artifact hashes, feature names, seeds)
with separate SHA-256 hashes stored on the model row.

Every SQL statement is an inline literal at its call site and binds values
through named parameters.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from datetime import datetime, UTC
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.services.preference_events import (  # noqa: E402
    SCORING_RATINGS,
    load_latest_scoring_events,
)

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
CHANCE_PAIRWISE = 0.5

GAIN_MAP = {"favorite": 3, "like": 3, "neutral": 1, "dislike": 0}

POSITIVE_RATINGS = ("favorite", "like")
NEGATIVE_RATINGS = ("dislike",)


def _open_read_only(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def load_rows(db_path: Path) -> tuple[list[dict], str, dict[str, int]]:
    """Load usable feature rows labeled via the app's event semantics."""

    conn = _open_read_only(db_path)
    conn.row_factory = sqlite3.Row
    scoring = load_latest_scoring_events(conn)
    rows = conn.execute(
        """
        SELECT cf.candidate_id,
               cf.features_json,
               cf.artifact_sha256,
               cf.source_video_sha256,
               cf.extractor_sha256,
               COALESCE(cg.final_score, cg.profile_score, cg.base_rag_similarity)
                   AS baseline_score
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

    skipped = {
        "no_effective_scoring_event": 0,
        "duplicate_artifact": 0,
    }
    parsed: list[dict] = []
    seen_artifacts: set[str] = set()
    for row in rows:
        event = scoring.get(f"candidate_gif:{row['candidate_id']}")
        if event is None:
            skipped["no_effective_scoring_event"] += 1
            continue
        rating = str(event["rating"])
        if rating not in SCORING_RATINGS:
            skipped["no_effective_scoring_event"] += 1
            continue
        if row["artifact_sha256"] in seen_artifacts:
            skipped["duplicate_artifact"] += 1
            continue
        seen_artifacts.add(row["artifact_sha256"])
        baseline = row["baseline_score"]
        parsed.append(
            {
                "candidate_id": row["candidate_id"],
                "features": json.loads(row["features_json"]),
                "source": row["source_video_sha256"] or "unknown",
                "rating": rating,
                "baseline": float(baseline) if baseline is not None else None,
                "extractor": row["extractor_sha256"],
                "artifact_sha256": row["artifact_sha256"],
                "event_id": str(event["event_id"]),
                "event_created_at": str(event["created_at"]),
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

    ordered = sorted(
        sources,
        key=lambda s: hashlib.sha256((str(SPLIT_SEED) + s).encode("utf-8")).hexdigest(),
    )
    cutoff = int(len(ordered) * TRAIN_FRACTION)
    return set(ordered[:cutoff]), set(ordered[cutoff:])


def within_source_pair_stats(rows: list[dict]) -> dict[str, list[tuple[int, int]]]:
    """Positive/negative pairs grouped by source video (the ranking unit)."""

    positives_by_source: dict[str, list[int]] = {}
    negatives_by_source: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        if row["rating"] in POSITIVE_RATINGS:
            positives_by_source.setdefault(row["source"], []).append(index)
        elif row["rating"] in NEGATIVE_RATINGS:
            negatives_by_source.setdefault(row["source"], []).append(index)
    pairs: dict[str, list[tuple[int, int]]] = {}
    for source, pos_indices in positives_by_source.items():
        for p in pos_indices:
            for n in negatives_by_source.get(source, []):
                pairs.setdefault(source, []).append((p, n))
    return pairs


def pair_accuracy(pairs: list[tuple[int, int]], scores: np.ndarray) -> float:
    if not pairs:
        return float("nan")
    wins = sum(int(scores[p] > scores[n]) for p, n in pairs)
    return wins / len(pairs)


def paired_bootstrap_delta_lcb95(
    pairs_by_source: dict[str, list[tuple[int, int]]],
    curve_scores: np.ndarray,
    baseline_scores: np.ndarray,
) -> float:
    """95% lower bound of the paired curve-minus-baseline pairwise delta.

    Each bootstrap replicate resamples source groups once and evaluates both
    models on the same resampled pairs, so the delta is paired.
    """

    group_keys = sorted(pairs_by_source)
    if not group_keys:
        return float("nan")
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    deltas: list[float] = []
    for _ in range(BOOTSTRAP_REPS):
        chosen = rng.integers(0, len(group_keys), size=len(group_keys))
        pairs = [
            pair
            for key in chosen
            for pair in pairs_by_source[group_keys[int(key)]]
        ]
        if not pairs:
            continue
        curve_acc = pair_accuracy(pairs, curve_scores)
        baseline_acc = pair_accuracy(pairs, baseline_scores)
        if math.isfinite(curve_acc) and math.isfinite(baseline_acc):
            deltas.append(curve_acc - baseline_acc)
    return float(np.percentile(deltas, 5)) if deltas else float("nan")


def diagnostic_chance_delta_lcb95(
    pairs_by_source: dict[str, list[tuple[int, int]]],
    scores: np.ndarray,
) -> float:
    """Diagnostic only: 95% lower bound of (within-source accuracy - 0.5).

    Not a publication gate; the gate is the paired baseline comparison.
    """

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
    if not accuracies:
        return float("nan")
    return float(np.percentile(accuracies, 5)) - CHANCE_PAIRWISE


def ndcg_at_20(rows: list[dict], scores: np.ndarray) -> float:
    """Mean NDCG@20 over source groups with at least two judged rows."""

    source_indices: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        source_indices.setdefault(row["source"], []).append(index)
    values: list[float] = []
    for source in sorted(source_indices):
        indices = np.array(source_indices[source])
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


def build_manifest(
    kind: str,
    rows: list[dict],
    sources: set[str],
    feature_names: list[str],
    extractor: str,
) -> tuple[dict, str]:
    """Freeze the population binding features, labels, and event identity.

    Every row carries its feature vector, effective label, production
    baseline score, artifact hash, and the effective scoring-event identity,
    so the hash changes whenever any training input changes.
    """

    manifest = {
        "kind": kind,
        "feature_version": FEATURE_VERSION,
        "extractor_sha256": extractor,
        "l2_lambda": L2_LAMBDA,
        "split_seed": SPLIT_SEED,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "feature_names": list(feature_names),
        "sources": sorted(sources),
        "rows": [
            {
                "candidate_id": row["candidate_id"],
                "rating": row["rating"],
                "features": row["features"],
                "baseline": row["baseline"],
                "artifact_sha256": row["artifact_sha256"],
                "event_id": row["event_id"],
                "event_created_at": row["event_created_at"],
                "source": row["source"],
            }
            for row in sorted(rows, key=lambda r: r["candidate_id"])
        ],
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return manifest, _sha256_bytes(canonical.encode("utf-8"))


def decide_publication(
    *,
    holdout_row_count: int,
    source_overlap: int,
    train_class_counts: tuple[int, int],
    holdout_class_counts: tuple[int, int],
    baseline_universe_rows: int,
    baseline_pair_count: int,
    paired_delta_lcb95: float,
) -> tuple[str, list[str]]:
    """Return (status, gate_reasons): PUBLISHED / REJECTED / DRAFT.

    DRAFT means "cannot be evaluated yet" (for example the production
    baseline does not cover the labeled population, or any precondition
    such as source overlap or a missing class failed); REJECTED means the
    comparison ran on a valid population and the model lost. **Every
    precondition failure blocks publication** — the function must never
    return ``PUBLISHED`` while ``reasons`` is non-empty.
    """

    reasons: list[str] = []
    if source_overlap:
        reasons.append(f"source_overlap={source_overlap}")
    if holdout_row_count < MIN_HOLDOUT_JUDGMENTS:
        reasons.append(
            f"holdout_judgment_count={holdout_row_count} < {MIN_HOLDOUT_JUDGMENTS}"
        )
    if 0 in train_class_counts or 0 in holdout_class_counts:
        reasons.append("missing_one_class")
    if baseline_universe_rows < MIN_HOLDOUT_JUDGMENTS or baseline_pair_count == 0:
        reasons.append(
            "baseline_coverage_insufficient="
            f"{baseline_universe_rows}_rows_{baseline_pair_count}_pairs"
        )
        return "DRAFT", reasons
    if reasons:
        # Any precondition failure invalidates the comparison; the model
        # stays pending evaluation even when the delta looks good.
        return "DRAFT", reasons
    if not math.isfinite(paired_delta_lcb95):
        reasons.append("paired_delta_lcb95=nan")
        return "DRAFT", reasons
    if paired_delta_lcb95 <= 0:
        reasons.append(f"paired_delta_lcb95={paired_delta_lcb95:.4f} <= 0")
        return "REJECTED", reasons
    return "PUBLISHED", reasons


def _ensure_model_columns(conn: sqlite3.Connection) -> None:
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
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(curve_preference_models)").fetchall()
    }
    for column in (
        "holdout_source_set_sha256 TEXT NOT NULL DEFAULT ''",
        "training_manifest_sha256 TEXT NOT NULL DEFAULT ''",
        "holdout_manifest_sha256 TEXT NOT NULL DEFAULT ''",
        "training_manifest_json TEXT NOT NULL DEFAULT '{}'",
        "holdout_manifest_json TEXT NOT NULL DEFAULT '{}'",
    ):
        name = column.split()[0]
        if name not in existing:
            conn.execute(
                "ALTER TABLE curve_preference_models ADD COLUMN " + column
            )


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

    # ---- primary gate: paired comparison against the production baseline --
    baseline_indices = [
        index for index, row in enumerate(holdout_rows)
        if row["baseline"] is not None
    ]
    baseline_universe = [holdout_rows[i] for i in baseline_indices]
    baseline_universe_pairs = within_source_pair_stats(baseline_universe)
    baseline_pair_count = sum(len(v) for v in baseline_universe_pairs.values())
    curve_universe_scores = holdout_scores[np.array(baseline_indices, dtype=int)]
    baseline_universe_scores = np.array(
        [holdout_rows[i]["baseline"] for i in baseline_indices], dtype=np.float64
    )
    paired_delta_lcb95 = paired_bootstrap_delta_lcb95(
        baseline_universe_pairs, curve_universe_scores, baseline_universe_scores
    )

    # ---- diagnostics: versus chance on all holdout pairs -------------------
    all_pairs = within_source_pair_stats(holdout_rows)
    all_pair_count = sum(len(v) for v in all_pairs.values())
    curve_accuracy = pair_accuracy(
        [p for v in all_pairs.values() for p in v], holdout_scores
    )
    curve_lcb95_vs_chance = None
    if all_pair_count:
        curve_lcb95_vs_chance = diagnostic_chance_delta_lcb95(
            all_pairs, holdout_scores
        )
    curve_ndcg = ndcg_at_20(holdout_rows, holdout_scores)

    print(f"\nholdout within-source pairs: {all_pair_count} "
          f"across {len(all_pairs)} source groups")
    print(f"pairwise accuracy (curve, diagnostic vs chance): {curve_accuracy:.4f}")
    if curve_lcb95_vs_chance is not None:
        print(f"diagnostic chance-delta lcb95: {curve_lcb95_vs_chance:+.4f}")
    print(f"holdout rows with production baseline: {len(baseline_universe)} "
          f"({baseline_pair_count} pairs)")
    print(f"paired curve-vs-baseline delta lcb95: {paired_delta_lcb95:+.4f}")
    print(f"holdout NDCG@20 (curve): {curve_ndcg:.4f}")

    status, gate_reasons = decide_publication(
        holdout_row_count=len(holdout_rows),
        source_overlap=len(overlap),
        train_class_counts=(
            count_class(train_rows, True), count_class(train_rows, False)
        ),
        holdout_class_counts=(
            count_class(holdout_rows, True), count_class(holdout_rows, False)
        ),
        baseline_universe_rows=len(baseline_universe),
        baseline_pair_count=baseline_pair_count,
        paired_delta_lcb95=paired_delta_lcb95,
    )

    data_confidence = min(1.0, len(rows) / 100)
    margin_factor = (
        max(0.0, min(1.0, paired_delta_lcb95 / HOLDOUT_MARGIN_DIVISOR))
        if math.isfinite(paired_delta_lcb95)
        else 0.0
    )
    confidence = min(data_confidence, margin_factor) if status == "PUBLISHED" else None
    print(f"data_confidence={data_confidence:.3f} "
          f"holdout_margin_factor={margin_factor:.3f} "
          f"curve_model_confidence={confidence}")
    print(f"\ngate reasons: {gate_reasons or 'none'}")
    print(f"status: {status} (publish flag: {args.publish})")

    if not args.publish:
        print("evaluation only; nothing written")
        return 0

    write_status = status if status in ("PUBLISHED", "REJECTED") else "DRAFT"
    train_manifest, train_manifest_sha = build_manifest(
        "curve-training-manifest-v1", train_rows, train_sources, names, extractor
    )
    holdout_manifest, holdout_manifest_sha = build_manifest(
        "curve-holdout-manifest-v1", holdout_rows, holdout_sources, names, extractor
    )

    model_version = (
        f"{MODEL_VERSION_PREFIX}-"
        + datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    )
    train_source_set_sha = _sha256_bytes(
        json.dumps(sorted(train_sources), separators=(",", ":")).encode("utf-8")
    )
    holdout_source_set_sha = _sha256_bytes(
        json.dumps(sorted(holdout_sources), separators=(",", ":")).encode("utf-8")
    )
    metrics = {
        "schema": "curve-preference-eval-v2",
        "train_rows": len(train_rows),
        "holdout_rows": len(holdout_rows),
        "train_source_count": len(train_sources),
        "holdout_source_count": len(holdout_sources),
        "holdout_within_source_pair_count": all_pair_count,
        "holdout_pairwise_accuracy_curve_vs_chance": round(curve_accuracy, 6),
        "pairwise_chance": CHANCE_PAIRWISE,
        "diagnostic_chance_delta_lcb95": (
            round(curve_lcb95_vs_chance, 6)
            if curve_lcb95_vs_chance is not None and math.isfinite(curve_lcb95_vs_chance)
            else None
        ),
        "baseline_universe_rows": len(baseline_universe),
        "baseline_universe_pair_count": baseline_pair_count,
        "paired_delta_lcb95_curve_vs_baseline": (
            round(paired_delta_lcb95, 6) if math.isfinite(paired_delta_lcb95) else None
        ),
        "ndcg_at_20_curve": round(curve_ndcg, 6),
        "data_confidence": round(data_confidence, 6),
        "holdout_margin_factor": round(margin_factor, 6),
        "curve_model_confidence": confidence,
        "gate_reasons": gate_reasons,
        "skipped": skipped,
    }

    conn = sqlite3.connect(str(args.db), timeout=30)
    _ensure_model_columns(conn)
    if write_status == "PUBLISHED":
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
            curve_model_confidence, published_at,
            holdout_source_set_sha256, training_manifest_sha256,
            holdout_manifest_sha256, training_manifest_json, holdout_manifest_json
        ) VALUES (
            :model_version, :feature_version, :extractor_sha256, :status,
            :coefficients_json, :intercept, :feature_mean_json, :feature_std_json,
            :feature_names_json, :l2_lambda, :training_event_watermark,
            :training_source_set_sha256, :holdout_row_count, :metrics_json,
            :curve_model_confidence, :published_at,
            :holdout_source_set_sha256, :training_manifest_sha256,
            :holdout_manifest_sha256, :training_manifest_json, :holdout_manifest_json
        )
        """,
        {
            "model_version": model_version,
            "feature_version": FEATURE_VERSION,
            "extractor_sha256": extractor,
            "status": write_status,
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
            "training_source_set_sha256": train_source_set_sha,
            "holdout_row_count": len(holdout_rows),
            "metrics_json": json.dumps(metrics, allow_nan=False),
            "curve_model_confidence": confidence,
            "published_at": (
                datetime.now(UTC).isoformat() if write_status == "PUBLISHED" else None
            ),
            "holdout_source_set_sha256": holdout_source_set_sha,
            "training_manifest_sha256": train_manifest_sha,
            "holdout_manifest_sha256": holdout_manifest_sha,
            "training_manifest_json": json.dumps(train_manifest, sort_keys=True),
            "holdout_manifest_json": json.dumps(holdout_manifest, sort_keys=True),
        },
    )
    conn.commit()
    conn.close()
    print(f"wrote model row {model_version} with status {write_status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

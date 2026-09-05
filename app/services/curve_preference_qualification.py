"""Curve preference qualification: evidence freezing and input validation.

Task 4 of the baseline-replay qualification plan. This module freezes the
per-candidate evidence for every holdout manifest row against a writable
*work* snapshot of the production database and never writes to the source
production connection.

Reusable VLM enrichment parsing mirrors the production enrichment shape
(``scripts/enrich_library_candidates.py::_record_from``) so qualification
evidence is identical in semantics to newly ingested candidates.

Task 5 will add paired source-grouped metrics, the two-pass confidence
protocol, and the fail-closed decision function on top of these frozen
records.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.services.preference_events import load_latest_scoring_events
from app.services.preference_profile_snapshot import canonical_json_sha256
from app.services.export_ranking import (
    adult_moe_export_score,
    cinematic_score_from_assessment,
)

if TYPE_CHECKING:
    from app.services.curve_preference import CurvePreferenceModel
    from app.services.reranker import PreferenceReranker

FEATURE_VERSION = "curve-v1"
EMBEDDING_MODEL = "nomic-embed-text:latest"
EMBEDDING_DIM = 768

_ADULT_WEIGHT = 0.80
_CINEMATIC_WEIGHT = 0.20

EMOTIONS = (
    "tension", "melancholy", "awe", "joy", "sadness", "catharsis", "serenity",
    "excitement", "dread", "nostalgia", "admiration", "intimacy",
    "vulnerability", "longing", "desire", "other",
)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_REFUSAL_RE = re.compile(
    r"(?:i\s*(?:cannot|can't|unable to))|(?:(?:无法|不能).{0,20}(?:协助|帮助|评估|提供))",
    re.IGNORECASE,
)


class EnrichmentError(RuntimeError):
    """A VLM enrichment response was refused, invalid, or unparseable."""


def _clamp_score(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 0
    return max(0, min(100, number))


def parse_enrichment_response(
    raw: str,
    *,
    model: str,
    enriched_at: str,
) -> dict[str, object]:
    """Validate one VLM result and return the normalized evidence mapping."""

    if _REFUSAL_RE.search(raw):
        raise EnrichmentError("model refused")
    match = _JSON_OBJECT_RE.search(raw)
    if not match:
        raise EnrichmentError("no JSON object in response")
    try:
        data = json.loads(match.group(0))
    except ValueError as exc:
        raise EnrichmentError("JSON parse failed") from exc
    if not isinstance(data, dict) or not str(data.get("caption") or "").strip():
        raise EnrichmentError("missing caption")

    emotion = str(data.get("emotional_core") or "").strip().lower()
    if emotion not in EMOTIONS:
        emotion = "other"
    tags_raw = data.get("tags") or []
    if isinstance(tags_raw, str):
        tags_raw = [tag.strip() for tag in tags_raw.split(",")]
    tags: list[str] = []
    for tag in tags_raw:
        clean = re.sub(r"\s+", " ", str(tag).strip().lower())
        if clean and clean not in tags:
            tags.append(clean)
    tags = tags[:8]
    scene = re.sub(
        r"[^a-z0-9_]+", "_", str(data.get("scene_type") or "").strip().lower()
    ).strip("_")

    return {
        "caption": str(data.get("caption") or "").strip(),
        "emotional_core": emotion,
        "tags": tags,
        "scene_type": scene or None,
        "sex_act": _clamp_score(data.get("sex_act")),
        "gif_worthiness": _clamp_score(data.get("gif_worthiness")),
        "reason": str(data.get("reason") or "").strip(),
        "enriched_by": model,
        "enriched_at": enriched_at,
    }


def score_evidence_from_summary(
    summary: Mapping[str, Any],
) -> dict[str, int] | None:
    """Return normalized worthiness/sex-act evidence when both are present."""

    if "gif_worthiness" not in summary or "sex_act" not in summary:
        return None
    worthiness_raw = summary["gif_worthiness"]
    sex_act_raw = summary["sex_act"]
    if any(
        isinstance(v, bool) or not isinstance(v, (int, float, str))
        for v in (worthiness_raw, sex_act_raw)
    ):
        return None
    try:
        worthiness = int(worthiness_raw)
        sex_act = int(sex_act_raw)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(worthiness) and math.isfinite(sex_act)):
        return None
    return {"gif_worthiness": worthiness, "sex_act": sex_act}


@dataclass
class QualificationInputs:
    records: list[dict[str, object]]
    input_manifest_sha256: str
    blocking: list[dict[str, str]]
    enrichment_count: int
    cinematic_fallback_count: int
    post_manifest_event_changes: list[str]
    per_candidate_costs_ms: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Task 5: paired source-grouped metrics, two-pass confidence, fail-closed gates
# ---------------------------------------------------------------------------

NDCG_GAIN_MAP = {"favorite": 3, "like": 3, "neutral": 1, "dislike": 0}
POSITIVE_RATINGS = frozenset({"favorite", "like"})
NEGATIVE_RATINGS = frozenset({"dislike"})


@dataclass(frozen=True)
class GroupedScoreRow:
    candidate_id: str
    source: str
    rating: str
    baseline: float
    proposed: float


@dataclass(frozen=True)
class SourceGroupedComparison:
    pair_count: int
    source_pair_groups: int
    baseline_pairwise_accuracy: float
    proposed_pairwise_accuracy: float
    pairwise_delta: float
    pairwise_delta_lcb95: float
    tie_rate: float
    mean_source_ndcg_at_20_baseline: float
    mean_source_ndcg_at_20_proposed: float
    ndcg_at_20_delta: float
    comparable_row_count: int
    source_count: int
    non_finite_count: int


@dataclass(frozen=True)
class QualificationDecision:
    status: str
    reasons: list[str]


def _ndcg_at_20_for_field(
    rows: Sequence[GroupedScoreRow], field: str
) -> float:
    by_source: dict[str, list[GroupedScoreRow]] = {}
    for row in rows:
        by_source.setdefault(row.source, []).append(row)
    values: list[float] = []
    for source_rows in by_source.values():
        if len(source_rows) < 2:
            continue
        ranked = sorted(source_rows, key=lambda r: getattr(r, field), reverse=True)
        gains = np.array(
            [NDCG_GAIN_MAP.get(r.rating, 0) for r in ranked], dtype=np.float64
        )
        dcg = float(np.sum(gains / np.log2(np.arange(2, len(gains) + 2))))
        ideal = np.sort(
            np.array(
                [NDCG_GAIN_MAP.get(r.rating, 0) for r in source_rows],
                dtype=np.float64,
            )
        )[::-1][:20]
        idcg = float(np.sum(ideal / np.log2(np.arange(2, len(ideal) + 2))))
        if idcg > 0:
            values.append(dcg / idcg)
    return float(np.mean(values)) if values else float("nan")


def evaluate_source_grouped_scores(
    rows: Sequence[GroupedScoreRow],
    *,
    baseline_field: str,
    proposed_field: str,
    bootstrap_seed: int,
    bootstrap_reps: int,
) -> SourceGroupedComparison:
    """Return deterministic paired metrics grouped only by source video.

    Positive/negative pairs are formed strictly within a source video using
    the current ``score_pos > score_neg`` contract; ties never win and are
    reported separately through ``tie_rate``. The bootstrap resamples source
    groups and produces the 5th-percentile lower bound of the paired
    proposed-minus-baseline pairwise delta.
    """

    finite_rows = [
        row for row in rows
        if math.isfinite(row.baseline) and math.isfinite(row.proposed)
    ]
    non_finite_count = len(rows) - len(finite_rows)

    pairs_by_source: dict[str, list[tuple[GroupedScoreRow, GroupedScoreRow]]] = {}
    positives_by_source: dict[str, list[GroupedScoreRow]] = {}
    negatives_by_source: dict[str, list[GroupedScoreRow]] = {}
    for row in finite_rows:
        if row.rating in POSITIVE_RATINGS:
            positives_by_source.setdefault(row.source, []).append(row)
        elif row.rating in NEGATIVE_RATINGS:
            negatives_by_source.setdefault(row.source, []).append(row)
    for source, positive_rows in positives_by_source.items():
        for positive in positive_rows:
            for negative in negatives_by_source.get(source, []):
                pairs_by_source.setdefault(source, []).append((positive, negative))
    flat_pairs = [pair for pairs in pairs_by_source.values() for pair in pairs]

    if flat_pairs:
        baseline_wins = sum(
            1
            for positive, negative in flat_pairs
            if getattr(positive, baseline_field) > getattr(negative, baseline_field)
        )
        proposed_wins = sum(
            1
            for positive, negative in flat_pairs
            if getattr(positive, proposed_field) > getattr(negative, proposed_field)
        )
        ties = sum(
            1
            for positive, negative in flat_pairs
            if getattr(positive, proposed_field) == getattr(negative, proposed_field)
        )
        baseline_accuracy = baseline_wins / len(flat_pairs)
        proposed_accuracy = proposed_wins / len(flat_pairs)
        tie_rate = ties / len(flat_pairs)
    else:
        baseline_accuracy = proposed_accuracy = tie_rate = float("nan")

    group_keys = sorted(pairs_by_source)
    if group_keys and bootstrap_reps > 0:
        rng = np.random.default_rng(bootstrap_seed)
        deltas: list[float] = []
        for _ in range(bootstrap_reps):
            chosen = rng.integers(0, len(group_keys), size=len(group_keys))
            sampled = [
                pair
                for key in chosen
                for pair in pairs_by_source[group_keys[int(key)]]
            ]
            if not sampled:
                continue
            b_acc = sum(
                1
                for positive, negative in sampled
                if getattr(positive, baseline_field) > getattr(negative, baseline_field)
            ) / len(sampled)
            p_acc = sum(
                1
                for positive, negative in sampled
                if getattr(positive, proposed_field) > getattr(negative, proposed_field)
            ) / len(sampled)
            deltas.append(p_acc - b_acc)
        delta_lcb95 = float(np.percentile(deltas, 5)) if deltas else float("nan")
    else:
        delta_lcb95 = float("nan")

    baseline_ndcg = _ndcg_at_20_for_field(finite_rows, baseline_field)
    proposed_ndcg = _ndcg_at_20_for_field(finite_rows, proposed_field)

    delta = proposed_accuracy - baseline_accuracy
    ndcg_delta = proposed_ndcg - baseline_ndcg
    return SourceGroupedComparison(
        pair_count=len(flat_pairs),
        source_pair_groups=len(pairs_by_source),
        baseline_pairwise_accuracy=baseline_accuracy,
        proposed_pairwise_accuracy=proposed_accuracy,
        pairwise_delta=delta,
        pairwise_delta_lcb95=delta_lcb95,
        tie_rate=tie_rate,
        mean_source_ndcg_at_20_baseline=baseline_ndcg,
        mean_source_ndcg_at_20_proposed=proposed_ndcg,
        ndcg_at_20_delta=ndcg_delta,
        comparable_row_count=len(finite_rows),
        source_count=len({row.source for row in finite_rows}),
        non_finite_count=non_finite_count,
    )


def decide_curve_qualification(
    *,
    model_status: str,
    model_manifest_hashes_ok: bool,
    source_overlap: int,
    holdout_row_count: int,
    train_class_counts: tuple[int, int],
    holdout_class_counts: tuple[int, int],
    blocking: Sequence[Mapping[str, str]],
    nominal_pair_count: int,
    nominal_pairwise_delta_lcb95: float,
    effective_pairwise_delta_lcb95: float,
    effective_ndcg_delta: float,
    non_finite_count: int,
) -> QualificationDecision:
    """Collect all precondition failures before evaluating outcome.

    Any reason means ``NOT_QUALIFIED``; the outcome metrics (nominal/effective
    pairwise LCB and NDCG delta) are only consulted on a fully clean
    precondition set.
    """

    reasons: list[str] = []
    if model_status != "DRAFT":
        reasons.append("model_not_draft")
    if not model_manifest_hashes_ok:
        reasons.append("model_manifest_hash_mismatch")
    if source_overlap:
        reasons.append("source_overlap")
    if holdout_row_count < 30:
        reasons.append("holdout_too_small")
    if 0 in train_class_counts or 0 in holdout_class_counts:
        reasons.append("missing_class")
    for entry in blocking:
        code = entry.get("code", "")
        if code in {
            "candidate_identity_mismatch", "effective_event_changed",
            "artifact_missing", "artifact_hash_mismatch",
            "base_score_coverage_incomplete",
        } and code not in reasons:
            reasons.append(code)

    if reasons:
        return QualificationDecision(status="NOT_QUALIFIED", reasons=reasons)

    if nominal_pair_count == 0:
        reasons.append("no_within_source_pairs")
    if non_finite_count:
        reasons.append("non_finite_metric")
    if reasons:
        return QualificationDecision(status="NOT_QUALIFIED", reasons=reasons)

    if not math.isfinite(nominal_pairwise_delta_lcb95) or nominal_pairwise_delta_lcb95 <= 0:
        reasons.append("nominal_pairwise_lcb_not_positive")
    if not math.isfinite(effective_pairwise_delta_lcb95) or effective_pairwise_delta_lcb95 <= 0:
        reasons.append("effective_pairwise_lcb_not_positive")
    if not math.isfinite(effective_ndcg_delta) or effective_ndcg_delta < 0:
        reasons.append("effective_ndcg_regression")
    if reasons:
        return QualificationDecision(status="NOT_QUALIFIED", reasons=reasons)
    return QualificationDecision(status="QUALIFIED", reasons=[])


# ---------------------------------------------------------------------------
# Two-pass confidence protocol (nominal -> derived confidence -> effective)
# ---------------------------------------------------------------------------

BASE_SCORE_WEIGHT = 0.50
PREFERENCE_SCORE_WEIGHT = 0.50
HOLDOUT_MARGIN_DIVISOR = 0.05
BOOTSTRAP_SEED = 20260905
BOOTSTRAP_REPS = 2000


@dataclass
class TwoPassScores:
    baseline: dict[str, float]
    nominal: dict[str, float]
    effective: dict[str, float]
    effective_confidence: float
    nominal_comparison: SourceGroupedComparison
    effective_comparison: SourceGroupedComparison
    score_rows: list[GroupedScoreRow]


def run_two_pass_qualification_scores(
    *,
    work_conn: sqlite3.Connection,
    model: "CurvePreferenceModel",
    records: Sequence[Mapping[str, Any]],
    profile_version: str,
    reranker: "PreferenceReranker",
    base_score_weight: float = BASE_SCORE_WEIGHT,
    preference_score_weight: float = PREFERENCE_SCORE_WEIGHT,
    bootstrap_seed: int = BOOTSTRAP_SEED,
    bootstrap_reps: int = BOOTSTRAP_REPS,
) -> TwoPassScores:
    """Score the same frozen records three ways and derive the confidence.

    Pass 1 scores the production baseline (curve absent) and the nominal
    curve variant (confidence 1.0). If the nominal paired LCB is positive,
    the frozen confidence formula derives the effective confidence and pass 2
    re-scores. No weight, seed, or threshold is selected from holdout results.
    """

    from app.services.curve_preference import curve_probability
    from app.services.reranker import score_preference_export_candidate

    baseline_scores: dict[str, float] = {}
    nominal_scores: dict[str, float] = {}
    grouped: list[GroupedScoreRow] = []
    vector_cache: dict[str, np.ndarray] = {}

    def _vector_for(candidate_id: str) -> np.ndarray:
        cached = vector_cache.get(candidate_id)
        if cached is not None:
            return cached
        row = work_conn.execute(
            """
            SELECT vector_blob FROM candidate_vectors
            WHERE candidate_id = :candidate_id
              AND vector_type = 'clip'
              AND embedding_model = :model
            """,
            {"candidate_id": candidate_id, "model": EMBEDDING_MODEL},
        ).fetchone()
        vector = np.frombuffer(bytes(row["vector_blob"]), dtype=np.float32)
        vector_cache[candidate_id] = vector
        return vector

    def _score_record(
        record: Mapping[str, Any],
        *,
        curve_probability_value: float | None,
        confidence_value: float | None,
    ) -> float:
        candidate_id = str(record["candidate_id"])
        worthiness_row = work_conn.execute(
            "SELECT vlm_summary_json FROM candidate_gifs "
            "WHERE candidate_id = :candidate_id",
            {"candidate_id": candidate_id},
        ).fetchone()
        stored_summary = json.loads(worthiness_row["vlm_summary_json"] or "{}")
        worthiness = _clamp_score(
            stored_summary.get("gif_worthiness", 0)
        )
        result = score_preference_export_candidate(
            reranker=reranker,
            candidate_vector=_vector_for(candidate_id),
            gif_worthiness=float(worthiness),
            scenario_keys=_scenario_keys_for(work_conn, candidate_id),
            profile_version=profile_version,
            base_export_score=float(record["base_export_score"]),
            base_score_weight=base_score_weight,
            preference_score_weight=preference_score_weight,
            curve_probability=curve_probability_value,
            curve_model_version=model.model_version,
            curve_model_confidence=confidence_value,
        )
        final_score = result["final_score"]
        return float(str(final_score))

    for record in records:
        candidate_id = str(record["candidate_id"])
        worthiness_row = work_conn.execute(
            "SELECT vlm_summary_json FROM candidate_gifs "
            "WHERE candidate_id = :candidate_id",
            {"candidate_id": candidate_id},
        ).fetchone()
        stored_summary = json.loads(worthiness_row["vlm_summary_json"] or "{}")
        record_worthiness = score_evidence_from_summary(stored_summary)
        worthiness = (
            record_worthiness["gif_worthiness"]
            if record_worthiness is not None
            else float(record["base_export_score"]) * 100.0
        )
        curve_probability_value = curve_probability(
            model, dict(record["curve_features"])
        )
        baseline_scores[candidate_id] = _score_record(
            record, curve_probability_value=None, confidence_value=None
        )
        nominal_scores[candidate_id] = _score_record(
            record,
            curve_probability_value=curve_probability_value,
            confidence_value=1.0,
        )
        grouped.append(
            GroupedScoreRow(
                candidate_id=candidate_id,
                source=str(record["source_video_sha256"]),
                rating=str(record["rating"]),
                baseline=baseline_scores[candidate_id],
                proposed=nominal_scores[candidate_id],
            )
        )

    nominal_comparison = evaluate_source_grouped_scores(
        grouped,
        baseline_field="baseline",
        proposed_field="proposed",
        bootstrap_seed=bootstrap_seed,
        bootstrap_reps=bootstrap_reps,
    )

    training_rows = model.training_manifest.get("rows")
    effective_feedback_count = (
        len(training_rows) if isinstance(training_rows, list) else 0
    )
    data_confidence = min(1.0, effective_feedback_count / 100)
    nominal_lcb = nominal_comparison.pairwise_delta_lcb95
    margin_factor = (
        max(0.0, min(1.0, nominal_lcb / HOLDOUT_MARGIN_DIVISOR))
        if math.isfinite(nominal_lcb)
        else 0.0
    )
    effective_confidence = min(data_confidence, margin_factor)

    effective_scores: dict[str, float] = {}
    effective_grouped: list[GroupedScoreRow] = []
    for row in grouped:
        record = next(
            r for r in records if str(r["candidate_id"]) == row.candidate_id
        )
        curve_probability_value = curve_probability(
            model, dict(record["curve_features"])
        )
        effective_scores[row.candidate_id] = _score_record(
            record,
            curve_probability_value=curve_probability_value,
            confidence_value=effective_confidence,
        )
        effective_grouped.append(
            GroupedScoreRow(
                candidate_id=row.candidate_id,
                source=row.source,
                rating=row.rating,
                baseline=row.baseline,
                proposed=effective_scores[row.candidate_id],
            )
        )

    effective_comparison = evaluate_source_grouped_scores(
        effective_grouped,
        baseline_field="baseline",
        proposed_field="proposed",
        bootstrap_seed=bootstrap_seed,
        bootstrap_reps=bootstrap_reps,
    )
    return TwoPassScores(
        baseline=baseline_scores,
        nominal=nominal_scores,
        effective=effective_scores,
        effective_confidence=effective_confidence,
        nominal_comparison=nominal_comparison,
        effective_comparison=effective_comparison,
        score_rows=grouped,
    )


def _scenario_keys_for(work_conn: sqlite3.Connection, candidate_id: str) -> list[str]:
    row = work_conn.execute(
        "SELECT scenario_keys_json FROM candidate_gifs WHERE candidate_id = :candidate_id",
        {"candidate_id": candidate_id},
    ).fetchone()
    if row is None or not row["scenario_keys_json"]:
        return []
    try:
        parsed = json.loads(row["scenario_keys_json"])
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _sha256_file(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _latest_effective_events(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    return load_latest_scoring_events(conn)


def _vector_blob_sha256(
    conn: sqlite3.Connection, candidate_id: str
) -> str | None:
    row = conn.execute(
        """
        SELECT vector_blob FROM candidate_vectors
        WHERE candidate_id = :candidate_id
          AND vector_type = 'clip'
          AND embedding_model = :model
        """,
        {"candidate_id": candidate_id, "model": EMBEDDING_MODEL},
    ).fetchone()
    if row is None or row["vector_blob"] is None:
        return None
    return hashlib.sha256(bytes(row["vector_blob"])).hexdigest()


def build_qualification_inputs(
    work_conn: sqlite3.Connection,
    *,
    manifest_rows: Sequence[Mapping[str, object]],
    enrich_fn: Callable[[str], str] | None = None,
    vector_fn: Callable[[str], bool] | None = None,
) -> QualificationInputs:
    """Freeze and validate every holdout manifest row against ``work_conn``.

    ``enrich_fn`` receives an artifact path and returns the raw VLM text;
    results are persisted to the work snapshot only. ``vector_fn`` receives a
    candidate id and must generate-and-upsert its text vector in the work
    snapshot, returning success. Both are optional only when every row
    already carries the corresponding evidence.
    """

    work_conn.row_factory = sqlite3.Row
    effective_events = _latest_effective_events(work_conn)

    records: list[dict[str, object]] = []
    blocking: list[dict[str, str]] = []
    enrichment_count = 0
    cinematic_fallback_count = 0
    post_manifest_event_changes: list[str] = []
    per_candidate_costs_ms: dict[str, float] = {}

    def block(code: str, candidate_id: str, detail: str = "") -> None:
        blocking.append(
            {"code": code, "candidate_id": candidate_id, "detail": detail}
        )

    for manifest_row in manifest_rows:
        candidate_id = str(manifest_row["candidate_id"])
        start_ns = None  # timing is recorded by the CLI, not here

        gif_row = work_conn.execute(
            """
            SELECT source_video_sha256, artifact_path, vlm_summary_json
            FROM candidate_gifs WHERE candidate_id = :candidate_id
            """,
            {"candidate_id": candidate_id},
        ).fetchone()
        feature_row = work_conn.execute(
            """
            SELECT features_json FROM curve_features
            WHERE candidate_id = :candidate_id
              AND feature_version = :feature_version
              AND availability = 'AVAILABLE'
            """,
            {"candidate_id": candidate_id, "feature_version": FEATURE_VERSION},
        ).fetchone()
        if gif_row is None or feature_row is None:
            block(
                "candidate_identity_mismatch", candidate_id,
                "missing candidate_gifs or curve_features row",
            )
            continue

        if str(gif_row["source_video_sha256"]) != str(manifest_row["source"]):
            block(
                "candidate_identity_mismatch", candidate_id,
                "source video mismatch",
            )
            continue
        db_features = json.loads(feature_row["features_json"])
        manifest_features = manifest_row["features"]
        if not isinstance(manifest_features, Mapping):
            block("candidate_identity_mismatch", candidate_id, "manifest features not a mapping")
            continue
        if db_features != dict(manifest_features):
            block(
                "candidate_identity_mismatch", candidate_id,
                "curve features mismatch",
            )
            continue

        event_key = f"candidate_gif:{candidate_id}"
        effective = effective_events.get(event_key)
        if (
            effective is None
            or str(effective["event_id"]) != str(manifest_row["event_id"])
            or str(effective["rating"]) != str(manifest_row["rating"])
            or str(effective["created_at"]) != str(manifest_row["event_created_at"])
        ):
            block("effective_event_changed", candidate_id, "label event drifted")
            post_manifest_event_changes.append(candidate_id)
            continue

        artifact_path = gif_row["artifact_path"]
        artifact = Path(artifact_path) if artifact_path else None
        if artifact is None or not artifact.is_file():
            block("artifact_missing", candidate_id, str(artifact_path))
            continue
        artifact_sha = _sha256_file(artifact)
        if artifact_sha != str(manifest_row["artifact_sha256"]):
            block("artifact_hash_mismatch", candidate_id, "GIF bytes changed")
            continue

        try:
            stored_summary = json.loads(gif_row["vlm_summary_json"] or "{}")
        except (TypeError, ValueError):
            stored_summary = {}
        evidence = score_evidence_from_summary(stored_summary)
        vlm_evidence_sha: str | None = None
        if evidence is None:
            if enrich_fn is None:
                block(
                    "base_score_coverage_incomplete", candidate_id,
                    "no VLM summary and no enrich_fn",
                )
                continue
            enrichment_count += 1
            try:
                raw = enrich_fn(str(artifact))
                summary = parse_enrichment_response(
                    raw,
                    model="qualification-enrichment",
                    enriched_at="qualification-time",
                )
            except Exception as exc:
                block(
                    "base_score_coverage_incomplete", candidate_id,
                    f"enrichment failed: {exc}",
                )
                continue
            evidence = score_evidence_from_summary(summary)
            if evidence is None:
                block(
                    "base_score_coverage_incomplete", candidate_id,
                    "enrichment lacked score fields",
                )
                continue
            work_conn.execute(
                "UPDATE candidate_gifs SET vlm_summary_json = :summary "
                "WHERE candidate_id = :candidate_id",
                {
                    "summary": json.dumps(summary, ensure_ascii=False),
                    "candidate_id": candidate_id,
                },
            )
            work_conn.commit()
        vlm_evidence_sha = canonical_json_sha256(
            {"gif_worthiness": evidence["gif_worthiness"], "sex_act": evidence["sex_act"]}
        )

        clip: dict[str, Any] = {
            "gif_worthiness": evidence["gif_worthiness"],
            "sex_act": evidence["sex_act"],
            "quality_assessment": None,
        }
        if cinematic_score_from_assessment(clip["quality_assessment"]) is None:
            # Historical candidates carry no frozen cinematic evidence; the
            # production fallback (0.5) applies and is reported separately.
            cinematic_fallback_count += 1
        base_payload = adult_moe_export_score(
            clip, adult_weight=_ADULT_WEIGHT, cinematic_weight=_CINEMATIC_WEIGHT
        )
        base_export_score = float(base_payload["final_score"])
        if not math.isfinite(base_export_score):
            block("non_finite_metric", candidate_id, "base export score")
            continue

        vector_sha = _vector_blob_sha256(work_conn, candidate_id)
        if vector_sha is None and vector_fn is not None:
            if not vector_fn(candidate_id):
                block(
                    "base_score_coverage_incomplete", candidate_id,
                    "vector generation failed",
                )
                continue
            vector_sha = _vector_blob_sha256(work_conn, candidate_id)
        if vector_sha is None:
            block(
                "base_score_coverage_incomplete", candidate_id,
                "no candidate vector after enrichment",
            )
            continue

        curve_features: dict[str, Any] = {
            str(k): v for k, v in manifest_features.items()
        }
        baseline = manifest_row.get("baseline")
        records.append(
            {
                "candidate_id": candidate_id,
                "source_video_sha256": str(manifest_row["source"]),
                "rating": str(manifest_row["rating"]),
                "event_id": str(manifest_row["event_id"]),
                "event_created_at": str(manifest_row["event_created_at"]),
                "artifact_path": str(artifact),
                "artifact_sha256": str(manifest_row["artifact_sha256"]),
                "curve_features": curve_features,
                "curve_features_sha256": canonical_json_sha256(curve_features),
                "vlm_evidence": evidence,
                "vlm_evidence_sha256": vlm_evidence_sha,
                "base_export_score": base_export_score,
                "candidate_vector_sha256": vector_sha,
                "profile_fallback_reason": None,
            }
        )

    records.sort(key=lambda r: (str(r["source_video_sha256"]), str(r["candidate_id"])))
    input_manifest_sha256 = canonical_json_sha256({"records": records})
    return QualificationInputs(
        records=records,
        input_manifest_sha256=input_manifest_sha256,
        blocking=blocking,
        enrichment_count=enrichment_count,
        cinematic_fallback_count=cinematic_fallback_count,
        post_manifest_event_changes=sorted(set(post_manifest_event_changes)),
        per_candidate_costs_ms=per_candidate_costs_ms,
    )

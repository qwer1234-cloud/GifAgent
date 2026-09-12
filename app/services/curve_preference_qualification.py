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
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from app.services.preference_events import load_latest_scoring_events
from app.services.preference_profile_snapshot import (
    build_source_isolated_profile_snapshot,
)
from app.services.preference_profile_snapshot import canonical_json_sha256
from app.services.export_ranking import (
    adult_moe_export_score,
    cinematic_score_from_assessment,
    normalize_vlm_unit_score,
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


def _parse_score(value: object, field_name: str) -> int:
    """Parse integer or float score using production normalize_vlm_unit_score rules."""
    unit = normalize_vlm_unit_score(value)
    if unit is None:
        raise EnrichmentError(
            f"invalid or out-of-range score {value!r} for {field_name}"
        )
    return int(round(unit * 100.0))


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

    if "sex_act" not in data or "gif_worthiness" not in data:
        raise EnrichmentError("missing sex_act or gif_worthiness in VLM response")
    sex_act = _parse_score(data.get("sex_act"), "sex_act")
    gif_worthiness = _parse_score(data.get("gif_worthiness"), "gif_worthiness")

    return {
        "caption": str(data.get("caption") or "").strip(),
        "emotional_core": emotion,
        "tags": tags,
        "scene_type": scene or None,
        "sex_act": sex_act,
        "gif_worthiness": gif_worthiness,
        "reason": str(data.get("reason") or "").strip(),
        "enriched_by": model,
        "enriched_at": enriched_at,
    }


def score_evidence_from_summary(
    summary: Mapping[str, Any],
) -> dict[str, int] | None:
    """Return normalized worthiness/sex-act evidence when both are present and valid."""

    if not isinstance(summary, Mapping):
        return None
    if "gif_worthiness" not in summary or "sex_act" not in summary:
        return None
    try:
        worthiness = _parse_score(summary["gif_worthiness"], "gif_worthiness")
        sex_act = _parse_score(summary["sex_act"], "sex_act")
        return {"gif_worthiness": worthiness, "sex_act": sex_act}
    except EnrichmentError:
        return None


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
        ranked = sorted(source_rows, key=lambda r: getattr(r, field), reverse=True)[:20]
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

BASE_SCORE_WEIGHT = 0.70
PREFERENCE_SCORE_WEIGHT = 0.30
HOLDOUT_MARGIN_DIVISOR = 0.05
BOOTSTRAP_SEED = 20260905
BOOTSTRAP_REPS = 2000


def load_target_ranking_config(
    config_path: Path | None = None,
) -> tuple[float, float, str | None]:
    """Load target base/preference weights and config hash from models.yaml."""
    if config_path is None:
        default_candidate = Path("dist/GifAgentUI/configs/models.yaml")
        if default_candidate.is_file():
            config_path = default_candidate

    if config_path is None or not config_path.is_file():
        return BASE_SCORE_WEIGHT, PREFERENCE_SCORE_WEIGHT, None

    config_sha256 = _sha256_file(config_path)
    base_weight = BASE_SCORE_WEIGHT
    pref_weight = PREFERENCE_SCORE_WEIGHT

    try:
        import yaml

        content = config_path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(content)
        if isinstance(parsed, dict):
            pref_mem = parsed.get("preference_memory") or {}
            if isinstance(pref_mem, dict):
                bw = pref_mem.get("base_score_weight")
                pw = pref_mem.get("preference_score_weight")
                if bw is not None and math.isfinite(float(bw)):
                    base_weight = float(bw)
                if pw is not None and math.isfinite(float(pw)):
                    pref_weight = float(pw)
    except Exception:
        pass

    return base_weight, pref_weight, config_sha256


@dataclass(frozen=True)
class ScoringCandidateEvidence:
    candidate_id: str
    preference_embedding_text: str
    preference_embedding_text_sha256: str | None
    candidate_vector: list[float] | None
    candidate_vector_sha256: str | None
    scenario_keys: list[str]
    embedding_model: str
    embedding_dim: int


@dataclass
class TwoPassScores:
    baseline: dict[str, float]
    nominal: dict[str, float]
    effective: dict[str, float]
    effective_confidence: float
    nominal_comparison: SourceGroupedComparison
    effective_comparison: SourceGroupedComparison
    score_rows: list[GroupedScoreRow]
    scoring_evidence: dict[str, ScoringCandidateEvidence]


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
    embed_fn: Callable[[str], list[float]] | None = None,
) -> TwoPassScores:
    """Score the same frozen records three ways and derive the confidence.

    Pass 1 scores the production baseline (curve absent) and the nominal
    curve variant (confidence 1.0). If the nominal paired LCB is positive,
    the frozen confidence formula derives the effective confidence and pass 2
    re-scores. No weight, seed, or threshold is selected from holdout results.
    """

    from app.services.curve_preference import curve_probability
    from app.services.reranker import (
        clip_scenario_keys,
        preference_embedding_text,
        score_preference_export_candidate,
    )

    if embed_fn is None:
        from app.services.embedding import compute_text_embedding

        embed_fn = compute_text_embedding

    baseline_scores: dict[str, float] = {}
    nominal_scores: dict[str, float] = {}
    grouped: list[GroupedScoreRow] = []
    vector_cache: dict[str, np.ndarray | None] = {}
    scoring_evidence: dict[str, ScoringCandidateEvidence] = {}

    def _vector_for_text(text: str) -> np.ndarray | None:  # noqa: ANN202
        if not text:
            return None
        if text in vector_cache:
            return vector_cache[text]
        try:
            vec = embed_fn(text)
            arr = np.array(vec, dtype=np.float32)
            vector_cache[text] = arr
            return arr
        except Exception:
            vector_cache[text] = None
            return None

    def _score_record(
        record: Mapping[str, Any],
        candidate_vector: np.ndarray | None,
        scenario_keys: list[str],
        worthiness: float,
        *,
        curve_probability_value: float | None,
        confidence_value: float | None,
    ) -> float:
        assert candidate_vector is not None  # gated by caller (text required)
        result = score_preference_export_candidate(
            reranker=reranker,
            candidate_vector=candidate_vector,
            gif_worthiness=float(worthiness),
            scenario_keys=scenario_keys,
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

    parsed_records = []
    for record in records:
        candidate_id = str(record["candidate_id"])
        cand_row = work_conn.execute(
            "SELECT vlm_summary_json, tags_json FROM candidate_gifs WHERE candidate_id = :candidate_id",
            {"candidate_id": candidate_id},
        ).fetchone()
        stored_summary = (
            json.loads(cand_row["vlm_summary_json"] or "{}") if cand_row else {}
        )
        stored_tags = (
            json.loads(cand_row["tags_json"] or "[]") if cand_row else []
        )
        if not isinstance(stored_tags, list):
            stored_tags = []

        caption = str(stored_summary.get("caption") or "").strip()
        emotion = str(
            stored_summary.get("emotional_core")
            or stored_summary.get("emotion")
            or ""
        ).strip()
        scene = str(stored_summary.get("scene_type") or "").strip()

        emb_text = preference_embedding_text(
            caption=caption, emotion=emotion, scene_type=scene
        )
        candidate_vector = _vector_for_text(emb_text)
        scenario_keys = clip_scenario_keys(
            {
                "emotional_core": emotion,
                "scene_type": scene,
                "tags": stored_tags or stored_summary.get("tags") or [],
            }
        )
        emb_text_sha = (
            hashlib.sha256(emb_text.encode("utf-8")).hexdigest()
            if emb_text
            else None
        )
        vec_list = (
            candidate_vector.tolist() if candidate_vector is not None else None
        )
        vec_sha = (
            hashlib.sha256(candidate_vector.tobytes()).hexdigest()
            if candidate_vector is not None
            else None
        )
        scoring_evidence[candidate_id] = ScoringCandidateEvidence(
            candidate_id=candidate_id,
            preference_embedding_text=emb_text,
            preference_embedding_text_sha256=emb_text_sha,
            candidate_vector=vec_list,
            candidate_vector_sha256=vec_sha,
            scenario_keys=scenario_keys,
            embedding_model=EMBEDDING_MODEL,
            embedding_dim=EMBEDDING_DIM,
        )

        record_worthiness = score_evidence_from_summary(stored_summary)
        worthiness = (
            record_worthiness["gif_worthiness"]
            if record_worthiness is not None
            else float(record["base_export_score"]) * 100.0
        )
        curve_probability_value = curve_probability(
            model, dict(record["curve_features"])
        )
        parsed_records.append(
            (
                record,
                candidate_id,
                candidate_vector,
                scenario_keys,
                worthiness,
                curve_probability_value,
            )
        )

        baseline_scores[candidate_id] = _score_record(
            record,
            candidate_vector=candidate_vector,
            scenario_keys=scenario_keys,
            worthiness=worthiness,
            curve_probability_value=None,
            confidence_value=None,
        )
        nominal_scores[candidate_id] = _score_record(
            record,
            candidate_vector=candidate_vector,
            scenario_keys=scenario_keys,
            worthiness=worthiness,
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
    for (
        record,
        candidate_id,
        candidate_vector,
        scenario_keys,
        worthiness,
        curve_probability_value,
    ) in parsed_records:
        effective_scores[candidate_id] = _score_record(
            record,
            candidate_vector=candidate_vector,
            scenario_keys=scenario_keys,
            worthiness=worthiness,
            curve_probability_value=curve_probability_value,
            confidence_value=effective_confidence,
        )
        effective_grouped.append(
            GroupedScoreRow(
                candidate_id=candidate_id,
                source=str(record["source_video_sha256"]),
                rating=str(record["rating"]),
                baseline=baseline_scores[candidate_id],
                proposed=effective_scores[candidate_id],
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
        scoring_evidence=scoring_evidence,
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
    vector_fn: Callable[[sqlite3.Connection, str], bool] | None = None,
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
            if not vector_fn(work_conn, candidate_id):
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


# ---------------------------------------------------------------------------
# Task 6: run orchestration, snapshot safety, and the immutable bundle
# ---------------------------------------------------------------------------

QUALIFICATION_RUN_SCHEMA_VERSION = "curve-qualification-run-v1"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _open_read_only(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=60)
    conn.execute("PRAGMA query_only = ON")
    return conn


def snapshot_source_database(db_path: Path, work_db_path: Path) -> str:
    """Consistent online backup of the read-only source into *work_db_path*.

    Returns the SHA-256 of the resulting snapshot file.
    """

    source = _open_read_only(db_path)
    try:
        target = sqlite3.connect(str(work_db_path))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    check = sqlite3.connect(str(work_db_path))
    try:
        row = check.execute("PRAGMA integrity_check").fetchone()
    finally:
        check.close()
    if row is None or row[0] != "ok":
        raise RuntimeError(f"work snapshot integrity check failed: {row}")
    return _file_sha256(work_db_path)


def _model_row_identity(conn: sqlite3.Connection, model_version: str) -> dict[str, Any]:
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT model_version, feature_version, extractor_sha256, status,
                   coefficients_json, intercept, feature_mean_json,
                   feature_std_json, feature_names_json, l2_lambda,
                   training_event_watermark, training_source_set_sha256,
                   holdout_source_set_sha256, training_manifest_sha256,
                   holdout_manifest_sha256, training_manifest_json,
                   holdout_manifest_json, holdout_row_count, metrics_json,
                   curve_model_confidence
            FROM curve_preference_models WHERE model_version = :model_version
            """,
            {"model_version": model_version},
        ).fetchone()
    finally:
        conn.row_factory = previous_factory
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


def logical_source_identity(conn: sqlite3.Connection, model_version: str) -> str:
    """Stable logical hash of everything the qualification depends on."""

    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        events = conn.execute(
            """
            SELECT COUNT(*) AS n, COALESCE(MAX(created_at), '') AS max_created
            FROM preference_events WHERE target_type = 'candidate_gif'
            """
        ).fetchone()
        candidates = conn.execute(
            "SELECT COUNT(*) AS n FROM candidate_gifs"
        ).fetchone()
        vectors = conn.execute(
            "SELECT COUNT(*) AS n FROM candidate_vectors WHERE vector_type = 'clip'"
        ).fetchone()
    finally:
        conn.row_factory = previous_factory
    payload = {
        "model_row": _model_row_identity(conn, model_version),
        "events_count": events["n"],
        "events_max_created_at": events["max_created"],
        "candidate_count": candidates["n"],
        "clip_vector_count": vectors["n"],
    }
    return canonical_json_sha256(payload)


def verify_model_for_qualification(
    conn: sqlite3.Connection, model_version: str
) -> tuple[Any | None, list[str]]:
    """Load the DRAFT model with full hash validation; return (model, reasons)."""

    from app.services.curve_preference import load_curve_model

    reasons: list[str] = []
    try:
        model = load_curve_model(
            conn, model_version, allowed_statuses=frozenset({"DRAFT"})
        )
    except ValueError as exc:
        message = str(exc)
        if "status" in message:
            reasons.append("model_not_draft")
        else:
            reasons.append("model_manifest_hash_mismatch")
        return None, reasons

    # Source-set hashes must match the frozen manifests exactly, using the
    # trainer's bare-array formula.
    raw_train_sources = model.training_manifest.get("sources")
    raw_holdout_sources = model.holdout_manifest.get("sources")
    train_sources = sorted(
        str(s)
        for s in (
            raw_train_sources if isinstance(raw_train_sources, list) else []
        )
    )
    holdout_sources = sorted(
        str(s)
        for s in (
            raw_holdout_sources if isinstance(raw_holdout_sources, list) else []
        )
    )
    train_set_sha = hashlib.sha256(
        json.dumps(train_sources, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    holdout_set_sha = hashlib.sha256(
        json.dumps(holdout_sources, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if train_set_sha != model.training_source_set_sha256:
        reasons.append("model_manifest_hash_mismatch")
    if holdout_set_sha != model.holdout_source_set_sha256:
        reasons.append("model_manifest_hash_mismatch")
    if not reasons:
        return model, []
    return None, reasons


def compute_source_overlap(model: Any) -> int:
    train = set(model.training_manifest.get("sources") or [])
    holdout = set(model.holdout_manifest.get("sources") or [])
    return len(train & holdout)


def class_counts_from_manifest_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    positive = sum(
        1 for row in rows if str(row.get("rating")) in POSITIVE_RATINGS
    )
    negative = sum(
        1 for row in rows if str(row.get("rating")) in NEGATIVE_RATINGS
    )
    return positive, negative


def _write_json(path: Path, payload: Mapping[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, allow_nan=False, indent=1)
    path.write_text(text, encoding="utf-8")
    return _file_sha256(path)


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        json.dumps(row, sort_keys=True, allow_nan=False, ensure_ascii=False)
        for row in rows
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return _file_sha256(path)


def count_required_enrichments(
    conn: sqlite3.Connection, manifest_rows: Sequence[Mapping[str, Any]]
) -> int:
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        needed = 0
        for row in manifest_rows:
            candidate_id = str(row["candidate_id"])
            gif_row = conn.execute(
                "SELECT vlm_summary_json FROM candidate_gifs WHERE candidate_id = :cid",
                {"cid": candidate_id},
            ).fetchone()
            if gif_row is None:
                continue
            try:
                stored = json.loads(gif_row["vlm_summary_json"] or "{}")
            except (TypeError, ValueError):
                stored = {}
            if score_evidence_from_summary(stored) is None:
                needed += 1
    finally:
        conn.row_factory = previous_factory
    return needed


def enrichment_needed(work_conn: sqlite3.Connection, candidate_id: str) -> bool:
    row = work_conn.execute(
        "SELECT vlm_summary_json FROM candidate_gifs WHERE candidate_id = :cid",
        {"cid": candidate_id},
    ).fetchone()
    if row is None:
        return False
    try:
        stored = json.loads(row["vlm_summary_json"] or "{}")
    except (TypeError, ValueError):
        return True
    return score_evidence_from_summary(stored) is None


def _decision_from_inputs(
    *,
    model: Any,
    model_manifest_hashes_ok: bool,
    inputs: QualificationInputs,
    two_pass: "TwoPassScores | None",
    limit: int | None,
) -> QualificationDecision:
    holdout_rows = model.holdout_manifest.get("rows") or []
    decision = decide_curve_qualification(
        model_status="DRAFT",
        model_manifest_hashes_ok=model_manifest_hashes_ok,
        source_overlap=compute_source_overlap(model),
        holdout_row_count=len(holdout_rows),
        train_class_counts=class_counts_from_manifest_rows(
            model.training_manifest.get("rows") or []
        ),
        holdout_class_counts=class_counts_from_manifest_rows(holdout_rows),
        blocking=inputs.blocking,
        nominal_pair_count=(
            two_pass.nominal_comparison.pair_count if two_pass else 0
        ),
        nominal_pairwise_delta_lcb95=(
            two_pass.nominal_comparison.pairwise_delta_lcb95
            if two_pass
            else float("nan")
        ),
        effective_pairwise_delta_lcb95=(
            two_pass.effective_comparison.pairwise_delta_lcb95
            if two_pass
            else float("nan")
        ),
        effective_ndcg_delta=(
            two_pass.effective_comparison.ndcg_at_20_delta
            if two_pass
            else float("nan")
        ),
        non_finite_count=(
            two_pass.nominal_comparison.non_finite_count if two_pass else 0
        ),
    )
    if limit is not None:
        decision = QualificationDecision(
            status="NOT_QUALIFIED",
            reasons=decision.reasons + ["diagnostic_limit"],
        )
    return decision


def execute_qualification_run(
    *,
    db_path: Path,
    model_version: str,
    work_dir: Path,
    config_path: Path | None = None,
    enrich_fn: Callable[[str], str] | None = None,
    vector_fn: Callable[[sqlite3.Connection, str], bool] | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    qualification_id: str | None = None,
    build_config: Any | None = None,
) -> dict[str, Any]:
    """Orchestrate one immutable qualification run and return its summary.

    The production database at *db_path* is opened read-only; every write
    lands in the claimed run directory (work.db, JSON artifacts). A dry run
    validates and reports without touching disk or services.
    """

    import uuid

    from app.services.preference_memory import PreferenceMemoryService
    from app.services.preference_types import ProfileBuildConfig
    from app.services.reranker import PreferenceReranker

    base_score_weight, preference_score_weight, config_sha256 = (
        load_target_ranking_config(config_path)
    )

    def now_fn() -> str:
        return datetime.now(timezone.utc).isoformat()

    if dry_run:
        source = _open_read_only(db_path)
        try:
            model, reasons = verify_model_for_qualification(source, model_version)
            holdout_rows = (model.holdout_manifest.get("rows") or []) if model else []
            summary = {
                "dry_run": True,
                "model_version": model_version,
                "model_ok": model is not None,
                "model_reasons": reasons,
                "holdout_rows": len(holdout_rows),
                "holdout_sources": len(
                    set(model.holdout_manifest.get("sources") or [])
                )
                if model
                else 0,
                "required_enrichments": (
                    count_required_enrichments(source, holdout_rows) if model else 0
                ),
                "source_overlap": compute_source_overlap(model) if model else 0,
                "target_ranking_config": {
                    "config_sha256": config_sha256,
                    "base_score_weight": base_score_weight,
                    "preference_score_weight": preference_score_weight,
                },
            }
        finally:
            source.close()
        return summary

    qualification_id = qualification_id or (
        "qual-"
        + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    run_dir = work_dir / qualification_id
    resuming = (run_dir / "run_state.json").is_file()
    run_dir.mkdir(parents=True, exist_ok=True)

    if resuming:
        state = json.loads(
            (run_dir / "run_state.json").read_text(encoding="utf-8")
        )
        if state.get("status") == "COMPLETED":
            raise RuntimeError(
                f"run {qualification_id} already completed; refuse to overwrite"
            )
        probe = _open_read_only(db_path)
        try:
            identity = logical_source_identity(probe, model_version)
        finally:
            probe.close()
        if identity != state.get("source_logical_identity"):
            state["status"] = "ABANDONED"
            state["abandon_reason"] = "source_identity_changed"
            (run_dir / "run_state.json").write_text(
                json.dumps(state, sort_keys=True, indent=1), encoding="utf-8"
            )
            raise RuntimeError(
                "resume refused: source/model logical identity changed"
            )
        work_db_path = run_dir / "work.db"
        source_db_snapshot_sha256 = state.get("source_db_snapshot_sha256", "")
    else:
        work_db_path = run_dir / "work.db"
        source_db_snapshot_sha256 = snapshot_source_database(db_path, work_db_path)

    work_conn = sqlite3.connect(str(work_db_path), timeout=60)
    work_conn.row_factory = sqlite3.Row
    try:
        model, model_reasons = verify_model_for_qualification(
            work_conn, model_version
        )
        if model is None:
            raise RuntimeError(f"model invalid on snapshot: {model_reasons}")
        model_manifest_hashes_ok = not model_reasons

        holdout_rows_all = model.holdout_manifest.get("rows") or []
        manifest_rows = list(holdout_rows_all)
        if limit is not None:
            manifest_rows = manifest_rows[:limit]

        probe_source = _open_read_only(db_path)
        try:
            source_logical_identity = logical_source_identity(
                probe_source, model_version
            )
        finally:
            probe_source.close()

        (run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "kind": QUALIFICATION_RUN_SCHEMA_VERSION,
                    "qualification_id": qualification_id,
                    "status": "INCOMPLETE",
                    "model_version": model_version,
                    "source_db_snapshot_sha256": source_db_snapshot_sha256,
                    "source_logical_identity": source_logical_identity,
                    "input_limit": limit,
                    "updated_at": now_fn(),
                },
                sort_keys=True,
                indent=1,
            ),
            encoding="utf-8",
        )

        snapshot = build_source_isolated_profile_snapshot(
            work_conn,
            excluded_source_videos=set(
                model.holdout_manifest.get("sources") or []
            ),
            config=build_config or ProfileBuildConfig(),
        )

        inputs = build_qualification_inputs(
            work_conn,
            manifest_rows=manifest_rows,
            enrich_fn=enrich_fn,
            vector_fn=vector_fn,
        )

        two_pass: TwoPassScores | None = None
        if not inputs.blocking and inputs.records:
            two_pass = run_two_pass_qualification_scores(
                work_conn=work_conn,
                model=model,
                records=inputs.records,
                profile_version=snapshot.profile_version,
                reranker=PreferenceReranker(work_conn),
                base_score_weight=base_score_weight,
                preference_score_weight=preference_score_weight,
            )
            for r in inputs.records:
                cid = str(r["candidate_id"])
                if cid in two_pass.scoring_evidence:
                    ev = two_pass.scoring_evidence[cid]
                    r["preference_embedding_text"] = ev.preference_embedding_text
                    r["preference_embedding_text_sha256"] = (
                        ev.preference_embedding_text_sha256
                    )
                    r["candidate_vector_sha256"] = ev.candidate_vector_sha256
                    r["scenario_keys"] = ev.scenario_keys

        decision = _decision_from_inputs(
            model=model,
            model_manifest_hashes_ok=model_manifest_hashes_ok,
            inputs=inputs,
            two_pass=two_pass,
            limit=limit,
        )

        model_row_identity = _model_row_identity(work_conn, model_version)
        model_row_sha256 = canonical_json_sha256(model_row_identity)
        input_manifest = {
            "kind": "curve-qualification-input-manifest-v1",
            "qualification_id": qualification_id,
            "model_version": model_version,
            "model_row_sha256": model_row_sha256,
            "model_training_manifest_sha256": model.training_manifest_sha256,
            "model_holdout_manifest_sha256": model.holdout_manifest_sha256,
            "profile_snapshot_sha256": snapshot.manifest_sha256,
            "target_ranking_config_sha256": config_sha256,
            "target_base_score_weight": base_score_weight,
            "target_preference_score_weight": preference_score_weight,
            "embedding_model": EMBEDDING_MODEL,
            "embedding_dim": EMBEDDING_DIM,
            "holdout_row_count": len(holdout_rows_all),
            "evaluated_row_count": len(manifest_rows),
            "records": inputs.records,
        }
        artifacts = {
            "input_manifest_sha256": _write_json(
                run_dir / "input_manifest.json", input_manifest
            ),
            "profile_snapshot_sha256": _write_json(
                run_dir / "profile_snapshot.json",
                {
                    "profile_version": snapshot.profile_version,
                    "event_watermark": snapshot.event_watermark,
                    "excluded_source_videos": list(
                        snapshot.excluded_source_videos
                    ),
                    "config_json": snapshot.config_json,
                    "manifest": snapshot.manifest,
                },
            ),
        }
        if two_pass:
            vector_rows = [
                {
                    "candidate_id": ev.candidate_id,
                    "embedding_model": ev.embedding_model,
                    "embedding_dim": ev.embedding_dim,
                    "preference_embedding_text": ev.preference_embedding_text,
                    "preference_embedding_text_sha256": ev.preference_embedding_text_sha256,
                    "candidate_vector_sha256": ev.candidate_vector_sha256,
                    "scenario_keys": ev.scenario_keys,
                    "candidate_vector": ev.candidate_vector,
                }
                for ev in sorted(
                    two_pass.scoring_evidence.values(),
                    key=lambda x: x.candidate_id,
                )
            ]
            artifacts["vectors_sha256"] = _write_jsonl(
                run_dir / "vectors.jsonl", vector_rows
            )
        if inputs.records:
            evidence_rows = [
                {
                    "candidate_id": str(row["candidate_id"]),
                    "enriched": enrichment_needed(work_conn, str(row["candidate_id"])),
                    "vlm_evidence": row["vlm_evidence"],
                    "vlm_evidence_sha256": row["vlm_evidence_sha256"],
                    "blocking": [
                        b
                        for b in inputs.blocking
                        if b["candidate_id"] == str(row["candidate_id"])
                    ],
                }
                for row in sorted(
                    inputs.records,
                    key=lambda r: (
                        str(r["source_video_sha256"]),
                        str(r["candidate_id"]),
                    ),
                )
            ]
            artifacts["evidence_sha256"] = _write_jsonl(
                run_dir / "evidence.jsonl", evidence_rows
            )

        metrics: dict[str, Any] = {
            "schema": "curve-qualification-metrics-v1",
            "qualification_id": qualification_id,
            "model_version": model_version,
            "holdout_row_count": len(holdout_rows_all),
            "evaluated_row_count": len(manifest_rows),
            "enrichment_count": inputs.enrichment_count,
            "cinematic_fallback_count": inputs.cinematic_fallback_count,
            "post_manifest_event_changes": inputs.post_manifest_event_changes,
            "source_overlap": compute_source_overlap(model),
            "target_ranking_config": {
                "config_sha256": config_sha256,
                "base_score_weight": base_score_weight,
                "preference_score_weight": preference_score_weight,
            },
            "baseline_coverage": len(inputs.records),
            "blocking": inputs.blocking,
            "nominal": (
                vars(two_pass.nominal_comparison) if two_pass else None
            ),
            "effective": (
                vars(two_pass.effective_comparison) if two_pass else None
            ),
            "effective_confidence": (
                two_pass.effective_confidence if two_pass else None
            ),
            "gate_reasons": decision.reasons,
            "status": decision.status,
            "diagnostic_limit": limit,
        }

        if two_pass:
            record_lookup = {
                str(r["candidate_id"]): r for r in inputs.records
            }
            baseline_rows = [
                {
                    "candidate_id": candidate_id,
                    "source_video_sha256": str(
                        record_lookup[candidate_id]["source_video_sha256"]
                    ),
                    "rating": str(record_lookup[candidate_id]["rating"]),
                    "baseline_score": score,
                }
                for candidate_id, score in sorted(two_pass.baseline.items())
            ]
            nominal_rows = [
                {
                    "candidate_id": row.candidate_id,
                    "source_video_sha256": row.source,
                    "rating": row.rating,
                    "nominal_score": row.proposed,
                }
                for row in two_pass.score_rows
            ]
            effective_rows = [
                {
                    "candidate_id": candidate_id,
                    "effective_score": score,
                }
                for candidate_id, score in sorted(two_pass.effective.items())
            ]
            artifacts["baseline_scores_sha256"] = _write_jsonl(
                run_dir / "baseline_scores.jsonl", baseline_rows
            )
            artifacts["nominal_scores_sha256"] = _write_jsonl(
                run_dir / "nominal_scores.jsonl", nominal_rows
            )
            artifacts["effective_scores_sha256"] = _write_jsonl(
                run_dir / "effective_scores.jsonl", effective_rows
            )

        metrics["artifacts"] = artifacts
        metrics_path = run_dir / "metrics.json"
        _write_json(metrics_path, metrics)

        report_lines = [
            "# Curve Preference Qualification Report",
            "",
            f"- qualification_id: `{qualification_id}`",
            f"- model_version: `{model_version}`",
            f"- status: **{decision.status}**",
            f"- gate_reasons: {decision.reasons}",
            f"- holdout rows: {len(holdout_rows_all)} "
            f"(evaluated {len(manifest_rows)})",
            f"- enrichment_count: {inputs.enrichment_count}",
            f"- cinematic_fallback_count: {inputs.cinematic_fallback_count}",
            f"- source_overlap: {compute_source_overlap(model)}",
            "- effective_confidence: "
            f"{two_pass.effective_confidence if two_pass else None}",
            "",
            "## Metrics",
            "",
            "```json",
            json.dumps(
                {
                    k: v
                    for k, v in metrics.items()
                    if k not in ("artifacts", "blocking")
                },
                sort_keys=True,
                indent=1,
                allow_nan=False,
            ),
            "```",
        ]
        (run_dir / "report.md").write_text(
            "\n".join(report_lines) + "\n", encoding="utf-8"
        )
        report_sha = _file_sha256(run_dir / "report.md")
        artifacts["report_sha256"] = report_sha
        metrics["report_sha256"] = report_sha
        metrics["artifacts"] = artifacts
        _write_json(metrics_path, metrics)

        probe_source = _open_read_only(db_path)
        try:
            final_identity = logical_source_identity(probe_source, model_version)
        finally:
            probe_source.close()
        completed = final_identity == source_logical_identity

        state = {
            "kind": QUALIFICATION_RUN_SCHEMA_VERSION,
            "qualification_id": qualification_id,
            "status": "COMPLETED" if completed else "INCOMPLETE",
            "model_version": model_version,
            "source_db_snapshot_sha256": source_db_snapshot_sha256,
            "source_logical_identity": source_logical_identity,
            "input_manifest_sha256": artifacts["input_manifest_sha256"],
            "decision_status": decision.status,
            "updated_at": now_fn(),
        }
        if not completed:
            state["abandon_reason"] = "source_identity_changed_during_run"
        (run_dir / "run_state.json").write_text(
            json.dumps(state, sort_keys=True, indent=1), encoding="utf-8"
        )

        return {
            "qualification_id": qualification_id,
            "run_dir": str(run_dir),
            "status": decision.status,
            "reasons": decision.reasons,
            "metrics": metrics,
        }
    finally:
        work_conn.close()

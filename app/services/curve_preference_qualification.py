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
from typing import Any
from dataclasses import dataclass, field
from pathlib import Path

from app.services.preference_events import load_latest_scoring_events
from app.services.preference_profile_snapshot import canonical_json_sha256
from app.services.export_ranking import (
    adult_moe_export_score,
    cinematic_score_from_assessment,
)

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

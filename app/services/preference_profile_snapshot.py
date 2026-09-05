"""Source-isolated preference profile snapshots for curve qualification.

Builds a Preference Profile inside a work database while excluding every
holdout source video, then freezes every build input and output into a
canonical SHA-256 manifest:

- every effective (non-superseded) training event id/time/rating/source;
- every consumed candidate vector blob hash;
- every generated profile row (scope, counts, confidence, tag weights,
  centroid blob base64 + SHA-256);
- the exact ``ProfileBuildConfig`` JSON used by the build.

The published ``preference_profile_current`` slot is never read and never
written: qualification always scores against the freshly built, isolated
profile version returned here.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.services.preference_memory import (
    REQUIRED_EMBEDDING_DIM,
    REQUIRED_EMBEDDING_MODEL,
    PreferenceMemoryService,
    evaluate_profile_gates,
)
from app.services.preference_types import ProfileBuildConfig

SNAPSHOT_KIND = "preference-profile-snapshot-v1"


def canonical_json(payload: Any) -> str:
    """Deterministic JSON text: sorted keys, compact, NaN/Infinity rejected."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_json_sha256(payload: Any) -> str:
    """SHA-256 of :func:`canonical_json` encoded as UTF-8."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PreferenceProfileSnapshot:
    profile_version: str
    event_watermark: str
    excluded_source_videos: tuple[str, ...]
    config_json: str
    manifest: dict[str, object]
    manifest_sha256: str


def _blob_hash(blob: bytes | None) -> str | None:
    if blob is None:
        return None
    return hashlib.sha256(blob).hexdigest()


def _blob_b64(blob: bytes | None) -> str | None:
    if blob is None:
        return None
    return base64.b64encode(blob).decode("ascii")


def _profile_rows(
    conn: sqlite3.Connection, *, profile_version: str
) -> list[dict[str, object]]:
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT scope, scenario_key, like_count, dislike_count, neutral_count,
                   confidence, liked_centroid_blob, disliked_centroid_blob,
                   tag_weights_json
            FROM preference_profiles
            WHERE profile_version = :profile_version
            ORDER BY scope ASC, scenario_key ASC
            """,
            {"profile_version": profile_version},
        ).fetchall()
    finally:
        conn.row_factory = previous_factory

    exported: list[dict[str, object]] = []
    seen: dict[tuple[str, str | None], dict[str, object]] = {}
    for row in rows:
        try:
            tag_weights = json.loads(row["tag_weights_json"] or "{}")
        except (TypeError, ValueError):
            tag_weights = {}
        if not isinstance(tag_weights, dict):
            tag_weights = {}
        record: dict[str, object] = {
            "scope": str(row["scope"]),
            "scenario_key": (
                None if row["scenario_key"] is None else str(row["scenario_key"])
            ),
            "like_count": int(row["like_count"]),
            "dislike_count": int(row["dislike_count"]),
            "neutral_count": int(row["neutral_count"]),
            "confidence": float(row["confidence"]),
            "tag_weights": tag_weights,
            "liked_centroid_blob_b64": _blob_b64(row["liked_centroid_blob"]),
            "liked_centroid_sha256": _blob_hash(row["liked_centroid_blob"]),
            "disliked_centroid_blob_b64": _blob_b64(row["disliked_centroid_blob"]),
            "disliked_centroid_sha256": _blob_hash(row["disliked_centroid_blob"]),
        }
        # Rebuilding the same deterministic profile_version inserts a second
        # global row (UNIQUE treats NULL scenario_key as distinct), so
        # duplicate keys are allowed only when their content is identical.
        scope_key = str(row["scope"])
        scenario_kw = None if row["scenario_key"] is None else str(row["scenario_key"])
        key = (scope_key, scenario_kw)
        previous = seen.get(key)
        if previous is not None:
            if previous != record:
                raise ValueError(
                    f"inconsistent duplicate profile rows for {key} in "
                    f"profile_version {profile_version!r}"
                )
            continue
        seen[key] = record
        exported.append(record)
    return exported


def _effective_training_events(
    effective: dict[str, dict[str, Any]],
) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for event in effective.values():
        events.append(
            {
                "event_id": str(event["event_id"]),
                "target_type": str(event["target_type"]),
                "target_id": str(event["target_id"]),
                "rating": str(event["rating"]),
                "source_video_sha256": str(event["source_video_sha256"]),
                "scenario_keys_json": str(event.get("scenario_keys_json") or "[]"),
                "created_at": str(event["created_at"]),
            }
        )
    events.sort(key=lambda item: str(item["event_id"]))
    return events


def _consumed_vector_records(
    conn: sqlite3.Connection,
    *,
    target_ids: list[str],
    embedding_model: str,
    embedding_dim: int,
) -> list[dict[str, object]]:
    if not target_ids:
        return []
    placeholders = ",".join(f":tid{i}" for i in range(len(target_ids)))
    params: dict[str, object] = {
        f"tid{i}": tid for i, tid in enumerate(target_ids)
    }
    params["embedding_model"] = embedding_model
    params["embedding_dim"] = embedding_dim
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""
            SELECT candidate_id, embedding_model, embedding_dim, vector_blob,
                   source_text_hash, text_schema_version
            FROM candidate_vectors
            WHERE vector_type = 'clip'
              AND embedding_model = :embedding_model
              AND embedding_dim = :embedding_dim
              AND candidate_id IN ({placeholders})
            ORDER BY candidate_id ASC
            """,
            params,
        ).fetchall()
    finally:
        conn.row_factory = previous_factory

    records: list[dict[str, object]] = []
    for row in rows:
        records.append(
            {
                "candidate_id": str(row["candidate_id"]),
                "embedding_model": str(row["embedding_model"]),
                "embedding_dim": int(row["embedding_dim"]),
                "vector_blob_sha256": _blob_hash(row["vector_blob"]),
                "source_text_hash": (
                    None
                    if row["source_text_hash"] is None
                    else str(row["source_text_hash"])
                ),
                "text_schema_version": int(row["text_schema_version"]),
            }
        )
    return records


def build_source_isolated_profile_snapshot(
    conn: sqlite3.Connection,
    *,
    excluded_source_videos: set[str],
    config: ProfileBuildConfig,
    embedding_model: str = REQUIRED_EMBEDDING_MODEL,
    embedding_dim: int = REQUIRED_EMBEDDING_DIM,
) -> PreferenceProfileSnapshot:
    """Build an isolated profile in *conn* (work.db) and freeze its inputs.

    Raises ``ValueError`` when the profile build gates block the build.
    """
    excluded = frozenset(excluded_source_videos)

    gate_snapshot = evaluate_profile_gates(
        conn,
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        exclude_source_videos=excluded,
    )

    result = PreferenceMemoryService(conn).build_profile(
        dry_run=False,
        config=config,
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        exclude_source_videos=excluded,
    )
    if result.get("status") != "built":
        raise ValueError(
            "profile build blocked: "
            + "; ".join(str(reason) for reason in result.get("gate_reasons", []))
        )

    profile_version = str(result["profile_version"])
    event_watermark = str(result["event_watermark"])

    build_row = conn.execute(
        """
        SELECT config_json FROM preference_profile_builds
        WHERE profile_version = :profile_version
        """,
        {"profile_version": profile_version},
    ).fetchone()
    if build_row is None:
        raise ValueError(
            f"profile build row missing for version {profile_version!r}"
        )
    config_json = str(build_row["config_json"])

    events = _effective_training_events(gate_snapshot.effective)
    target_ids = sorted({str(event["target_id"]) for event in events})
    vectors = _consumed_vector_records(
        conn,
        target_ids=target_ids,
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
    )
    profile_rows = _profile_rows(conn, profile_version=profile_version)

    manifest: dict[str, object] = {
        "kind": SNAPSHOT_KIND,
        "profile_version": profile_version,
        "event_watermark": event_watermark,
        "embedding_model": embedding_model,
        "embedding_dim": embedding_dim,
        "excluded_source_videos": sorted(excluded),
        "config_json": config_json,
        "effective_feedback_count": int(gate_snapshot.effective_count),
        "like_count": int(gate_snapshot.like_count),
        "dislike_count": int(gate_snapshot.dislike_count),
        "source_video_count": int(gate_snapshot.source_video_count),
        "events": events,
        "vectors": vectors,
        "profile_rows": profile_rows,
    }

    return PreferenceProfileSnapshot(
        profile_version=profile_version,
        event_watermark=event_watermark,
        excluded_source_videos=tuple(sorted(excluded)),
        config_json=config_json,
        manifest=manifest,
        manifest_sha256=canonical_json_sha256(manifest),
    )

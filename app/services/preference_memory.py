"""P1-5: PreferenceMemoryService — build immutable preference profiles.

Gate-minimum profile builds with deterministic versioning, global and scenario
centroids, and manual publish workflow.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

import numpy as np

from app.services.preference_events import load_latest_scoring_events
from app.services.preference_types import (
    ProfileBuildConfig,
    ProfileBuildResult,
    ProfilePreview,
    RerankerScoreBreakdown,
)
from app.services.vector_math import blob_to_vector, l2_normalize, vector_to_blob, weighted_kmeans


# ---------------------------------------------------------------------------
# Gate constants
# ---------------------------------------------------------------------------

MIN_EFFECTIVE_FEEDBACK = 30
MIN_LIKE_COUNT = 15
MIN_DISLIKE_COUNT = 10
MIN_SOURCE_VIDEOS = 3
MAX_SINGLE_VIDEO_SHARE = 0.40

REQUIRED_EMBEDDING_MODEL = "nomic-embed-text:latest"
REQUIRED_EMBEDDING_DIM = 768

MIN_SCENARIO_EVENTS = 5
MIN_SCENARIO_CONFIDENCE = 0.25


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json_dumps(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


@dataclass
class ProfileGateSnapshot:
    """Shared gate inputs for ``preview_profile`` and ``build_profile``."""

    effective: dict[str, dict[str, Any]]
    like_events: list[dict[str, Any]]
    dislike_events: list[dict[str, Any]]
    effective_count: int
    like_count: int
    dislike_count: int
    source_video_count: int
    max_single_video_share: float
    candidate_vector_count: int
    gate_reasons: list[str]
    event_watermark: str
    effective_target_ids: list[str]


def evaluate_profile_gates(
    conn: sqlite3.Connection,
    *,
    embedding_model: str = REQUIRED_EMBEDDING_MODEL,
    embedding_dim: int = REQUIRED_EMBEDDING_DIM,
) -> ProfileGateSnapshot:
    """Evaluate build gates against the latest scoring event per target."""
    effective = load_latest_scoring_events(conn)
    effective_list = list(effective.values())
    like_events = [
        event for event in effective_list if event["rating"] in ("like", "favorite")
    ]
    dislike_events = [
        event for event in effective_list if event["rating"] == "dislike"
    ]
    effective_count = len(effective_list)
    like_count = len(like_events)
    dislike_count = len(dislike_events)

    video_counts: dict[str, int] = {}
    for event in effective_list:
        vid = str(event["source_video_sha256"])
        video_counts[vid] = video_counts.get(vid, 0) + 1
    source_video_count = len(video_counts)
    max_single_video_share = (
        max(video_counts.values()) / effective_count if effective_count > 0 else 0.0
    )

    required_count = conn.execute(
        """SELECT COUNT(*) FROM candidate_vectors
           WHERE embedding_model=? AND embedding_dim=?""",
        (embedding_model, embedding_dim),
    ).fetchone()[0]
    any_count = conn.execute("SELECT COUNT(*) FROM candidate_vectors").fetchone()[0]
    vectors_exist = required_count > 0
    model_ok = vectors_exist
    dim_ok = vectors_exist

    effective_target_ids = sorted(set(str(event["target_id"]) for event in effective_list))
    candidate_vector_count = 0
    if effective_target_ids and model_ok and dim_ok:
        placeholders = ",".join(["?"] * len(effective_target_ids))
        candidate_vector_count = conn.execute(
            f"""SELECT COUNT(DISTINCT candidate_id)
                FROM candidate_vectors
                WHERE candidate_id IN ({placeholders})
                  AND vector_type='clip'
                  AND embedding_model=?
                  AND embedding_dim=?""",
            (*effective_target_ids, embedding_model, embedding_dim),
        ).fetchone()[0]

    gate_reasons: list[str] = []
    if effective_count < MIN_EFFECTIVE_FEEDBACK:
        gate_reasons.append(
            f"effective_feedback_count={effective_count} < {MIN_EFFECTIVE_FEEDBACK}"
        )
    if like_count < MIN_LIKE_COUNT:
        gate_reasons.append(f"like_count={like_count} < {MIN_LIKE_COUNT}")
    if dislike_count < MIN_DISLIKE_COUNT:
        gate_reasons.append(
            f"dislike_count={dislike_count} < {MIN_DISLIKE_COUNT}"
        )
    if source_video_count < MIN_SOURCE_VIDEOS:
        gate_reasons.append(
            f"source_video_count={source_video_count} < {MIN_SOURCE_VIDEOS}"
        )
    if max_single_video_share > MAX_SINGLE_VIDEO_SHARE:
        gate_reasons.append(
            f"max_single_video_share={max_single_video_share:.2f} > {MAX_SINGLE_VIDEO_SHARE}"
        )
    if required_count == 0:
        if any_count == 0:
            gate_reasons.append("no_vectors_found in candidate_vectors")
        else:
            gate_reasons.append(
                f"embedding_model mismatch: required={embedding_model} "
                f"dim={embedding_dim} coverage=0"
            )
    elif candidate_vector_count < effective_count:
        gate_reasons.append(
            f"candidate_vector_count={candidate_vector_count} "
            f"< effective_feedback_count={effective_count}"
        )

    event_watermark = ""
    if effective_list:
        event_watermark = max(str(event["created_at"]) for event in effective_list)
    else:
        max_ts = conn.execute(
            "SELECT MAX(created_at) FROM preference_events"
        ).fetchone()[0]
        if max_ts:
            event_watermark = max_ts

    return ProfileGateSnapshot(
        effective=effective,
        like_events=like_events,
        dislike_events=dislike_events,
        effective_count=effective_count,
        like_count=like_count,
        dislike_count=dislike_count,
        source_video_count=source_video_count,
        max_single_video_share=max_single_video_share,
        candidate_vector_count=candidate_vector_count,
        gate_reasons=gate_reasons,
        event_watermark=event_watermark,
        effective_target_ids=effective_target_ids,
    )


def _compute_profile_version(
    *,
    embedding_model: str,
    embedding_dim: int,
    event_watermark: str,
    sorted_target_ids: list[str],
    config_json: str,
) -> str:
    """Deterministic profile version hash."""
    hash_input = (
        f"{embedding_model}|{embedding_dim}|{event_watermark}|"
        f"{','.join(sorted_target_ids)}|{config_json}"
    )
    digest = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()
    return f"profile_{digest[:16]}"


# ---------------------------------------------------------------------------
# preview_profile  (standalone — computes gates but writes nothing)
# ---------------------------------------------------------------------------


def preview_profile(
    conn: sqlite3.Connection,
    config: ProfileBuildConfig,
    *,
    embedding_model: str = REQUIRED_EMBEDDING_MODEL,
    embedding_dim: int = REQUIRED_EMBEDDING_DIM,
) -> ProfilePreview:
    """Preview whether a build with *config* would pass all gates.

    Returns a ``ProfilePreview`` with status ``"ready"`` (gates pass) or
    ``"blocked"`` (gates fail) along with gate reasons and summary metrics.
    *Nothing is written to the database.*
    """
    snapshot = evaluate_profile_gates(
        conn, embedding_model=embedding_model, embedding_dim=embedding_dim
    )
    config_obj = _config_to_dict(config, embedding_model, embedding_dim)
    config_json = _json_dumps(config_obj)
    profile_version = _compute_profile_version(
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        event_watermark=snapshot.event_watermark,
        sorted_target_ids=snapshot.effective_target_ids,
        config_json=config_json,
    )
    metrics: dict[str, float] = {
        "effective_feedback_count": float(snapshot.effective_count),
        "like_count": float(snapshot.like_count),
        "dislike_count": float(snapshot.dislike_count),
        "source_video_count": float(snapshot.source_video_count),
        "max_single_video_share": snapshot.max_single_video_share,
        "candidate_vector_count": float(snapshot.candidate_vector_count),
    }
    return ProfilePreview(
        profile_version=profile_version,
        status="blocked" if snapshot.gate_reasons else "ready",
        gate_reasons=tuple(snapshot.gate_reasons),
        metrics=metrics,
    )


def _config_to_dict(
    config: ProfileBuildConfig,
    embedding_model: str,
    embedding_dim: int,
) -> dict[str, object]:
    """Serialize a ``ProfileBuildConfig`` into a deterministic dict."""
    return {
        "embedding_model": embedding_model,
        "embedding_dim": embedding_dim,
        "recency_enabled": config.recency_enabled,
        "recency_half_life_days": config.recency_half_life_days,
        "favorite_weight": config.favorite_weight,
        "like_weight": config.like_weight,
        "dislike_weight": config.dislike_weight,
        "scenario_min_feedback": config.scenario_min_feedback,
        "multi_centroid_k": int(config.multi_centroid_k),
    }


def _compute_per_candidate_weights(
    events: list[dict[str, object]],
    reference_dt: datetime,
    config: ProfileBuildConfig,
    rating_to_weight: dict[str, float],
) -> dict[str, float]:
    """Compute per-candidate recency weight for a list of events.

    Each candidate gets ``0.5 ^ (age_days / half_life_days)`` when
    recency is enabled, or ``1.0`` when disabled.  The result is
    **not** multiplied by the rating weight — that is done separately
    by the caller so that ``favorite`` and ``like`` can be
    distinguished.
    """
    weights: dict[str, float] = {}
    for evt in events:
        cid = str(evt["target_id"])
        if config.recency_enabled:
            evt_dt = datetime.fromisoformat(str(evt["created_at"]))
            age_days = (reference_dt - evt_dt).total_seconds() / 86400.0
            weights[cid] = 0.5 ** (age_days / config.recency_half_life_days)
        else:
            weights[cid] = 1.0
    return weights


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PreferenceMemoryService:
    """Build immutable preference profiles from feedback events and candidate vectors.

    Constructed with a `sqlite3.Connection` that already has the preference
    schema applied (via ``apply_preference_schema``).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_builds(self) -> dict:
        """List every profile build plus the currently published version.

        Moved here from the router so the HTTP layer only serializes and
        maps busy errors; the SQL stays next to the rest of the schema.
        """
        rows = self.conn.execute(
            """SELECT profile_version, event_watermark, embedding_model, embedding_dim,
                      effective_feedback_count, source_video_count, status, gate_reasons_json,
                      created_at, completed_at
               FROM preference_profile_builds
               ORDER BY created_at DESC"""
        ).fetchall()

        results = []
        for row in rows:
            results.append(
                {
                    "profile_version": row["profile_version"],
                    "event_watermark": row["event_watermark"],
                    "embedding_model": row["embedding_model"],
                    "embedding_dim": row["embedding_dim"],
                    "effective_feedback_count": row["effective_feedback_count"],
                    "source_video_count": row["source_video_count"],
                    "status": row["status"],
                    "gate_reasons": json.loads(row["gate_reasons_json"]),
                    "created_at": row["created_at"],
                    "completed_at": row["completed_at"],
                }
            )

        current = self.conn.execute(
            "SELECT profile_version, published_at FROM preference_profile_current WHERE slot='current'"
        ).fetchone()

        current_payload = None
        if current is not None:
            build = self.conn.execute(
                """SELECT event_watermark FROM preference_profile_builds
                   WHERE profile_version=?""",
                (current["profile_version"],),
            ).fetchone()
            watermark = build["event_watermark"] if build is not None else ""
            new_feedback_count = 0
            for event in load_latest_scoring_events(self.conn).values():
                if not watermark or str(event["created_at"]) > watermark:
                    new_feedback_count += 1
            current_payload = {
                "profile_version": current["profile_version"],
                "published_at": current["published_at"],
                "event_watermark": watermark,
                "new_feedback_count": new_feedback_count,
            }

        return {
            "profiles": results,
            "current": current_payload,
        }

    def build_profile(
        self,
        dry_run: bool = False,
        *,
        config: ProfileBuildConfig | None = None,
        embedding_model: str = REQUIRED_EMBEDDING_MODEL,
        embedding_dim: int = REQUIRED_EMBEDDING_DIM,
    ) -> ProfileBuildResult:
        """Run all gates and, when they pass, compute global + scenario profiles.

        *config* controls recency weighting, rating weights, and scenario
        thresholds.  When ``None`` (default) a ``ProfileBuildConfig()`` with
        stock values is used.

        Returns a ``ProfileBuildResult`` dict with ``status`` either ``"built"``
        or ``"blocked"``.  When ``dry_run`` is ``True`` nothing is written to
        the database.
        """
        config = config or ProfileBuildConfig()
        centroid_k = max(1, min(3, int(config.multi_centroid_k)))

        snapshot = evaluate_profile_gates(
            self.conn,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
        )
        effective = snapshot.effective
        like_events = snapshot.like_events
        dislike_events = snapshot.dislike_events
        effective_count = snapshot.effective_count
        like_count = snapshot.like_count
        dislike_count = snapshot.dislike_count
        source_video_count = snapshot.source_video_count
        gate_reasons = list(snapshot.gate_reasons)
        event_watermark = snapshot.event_watermark
        effective_target_ids = snapshot.effective_target_ids

        config_obj = _config_to_dict(config, embedding_model, embedding_dim)
        config_json = _json_dumps(config_obj)

        profile_version = _compute_profile_version(
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
            event_watermark=event_watermark,
            sorted_target_ids=effective_target_ids,
            config_json=config_json,
        )

        # ---- Blocked path
        if gate_reasons:
            result: ProfileBuildResult = {
                "profile_version": profile_version,
                "event_watermark": event_watermark,
                "effective_feedback_count": effective_count,
                "status": "blocked",
                "gate_reasons": gate_reasons,
            }
            if not dry_run:
                self._insert_build_row(
                    profile_version=profile_version,
                    event_watermark=event_watermark,
                    embedding_model=embedding_model,
                    embedding_dim=embedding_dim,
                    effective_feedback_count=effective_count,
                    source_video_count=source_video_count,
                    config_json=config_json,
                    status="blocked",
                    gate_reasons_json=_json_dumps(gate_reasons),
                )
            return result

        # ---- Building path: compute weighted centroids
        liked_target_ids = [e["target_id"] for e in like_events]
        disliked_target_ids = [e["target_id"] for e in dislike_events]

        reference_dt = datetime.fromisoformat(event_watermark)

        positive_weights = _compute_per_candidate_weights(
            events=like_events,
            reference_dt=reference_dt,
            config=config,
            rating_to_weight={},
        )
        for evt in like_events:
            cid = evt["target_id"]
            base = positive_weights.get(cid, 1.0)
            if evt["rating"] == "favorite":
                positive_weights[cid] = base * config.favorite_weight
            else:
                positive_weights[cid] = base * config.like_weight

        negative_weights = _compute_per_candidate_weights(
            events=dislike_events,
            reference_dt=reference_dt,
            config=config,
            rating_to_weight={},
        )
        for evt in dislike_events:
            cid = evt["target_id"]
            base = negative_weights.get(cid, 1.0)
            negative_weights[cid] = base * config.dislike_weight

        liked_centroid_blob = self._compute_centroid(
            liked_target_ids,
            embedding_model,
            embedding_dim,
            weights=positive_weights,
            k=centroid_k,
        )
        disliked_centroid_blob = self._compute_centroid(
            disliked_target_ids,
            embedding_model,
            embedding_dim,
            weights=negative_weights,
            k=centroid_k,
        )

        centroid_missing = (liked_centroid_blob is None and liked_target_ids) or (
            disliked_centroid_blob is None and disliked_target_ids
        )
        if centroid_missing:
            missing_info = []
            if liked_centroid_blob is None and liked_target_ids:
                missing_info.append("no liked vectors")
            if disliked_centroid_blob is None and disliked_target_ids:
                missing_info.append("no disliked vectors")
            gate_reasons.append(f"insufficient_vectors: {'; '.join(missing_info)}")

            result = {
                "profile_version": profile_version,
                "event_watermark": event_watermark,
                "effective_feedback_count": effective_count,
                "status": "blocked",
                "gate_reasons": gate_reasons,
            }
            if not dry_run:
                self._insert_build_row(
                    profile_version=profile_version,
                    event_watermark=event_watermark,
                    embedding_model=embedding_model,
                    embedding_dim=embedding_dim,
                    effective_feedback_count=effective_count,
                    source_video_count=source_video_count,
                    config_json=config_json,
                    status="blocked",
                    gate_reasons_json=_json_dumps(gate_reasons),
                )
            return result

        tag_weights = self._compute_tag_weights(like_events, dislike_events)
        global_confidence = min(1.0, effective_count / 100.0)

        result = {
            "profile_version": profile_version,
            "event_watermark": event_watermark,
            "effective_feedback_count": effective_count,
            "status": "built",
            "gate_reasons": [],
        }

        if not dry_run:
            self._insert_build_row(
                profile_version=profile_version,
                event_watermark=event_watermark,
                embedding_model=embedding_model,
                embedding_dim=embedding_dim,
                effective_feedback_count=effective_count,
                source_video_count=source_video_count,
                config_json=config_json,
                status="completed",
                gate_reasons_json="[]",
            )

            self._insert_profile(
                profile_version=profile_version,
                scope="global",
                scenario_key=None,
                like_count=like_count,
                dislike_count=dislike_count,
                neutral_count=0,
                confidence=global_confidence,
                liked_centroid_blob=liked_centroid_blob,
                disliked_centroid_blob=disliked_centroid_blob,
                tag_weights_json=_json_dumps(tag_weights),
            )

            self._build_scenario_profiles(
                effective=effective,
                embedding_model=embedding_model,
                embedding_dim=embedding_dim,
                profile_version=profile_version,
                min_events=config.scenario_min_feedback,
                positive_weights=positive_weights,
                negative_weights=negative_weights,
                k=centroid_k,
            )

            self.conn.commit()

        return result

    def publish(self, profile_version: str) -> None:
        """Promote a completed build to the ``preference_profile_current`` slot.

        Also appends a row to ``preference_profile_publications`` for
        append-only history.

        Raises ``ValueError`` when the build does not exist or is not completed.
        """
        row = self.conn.execute(
            "SELECT status, config_json FROM preference_profile_builds WHERE profile_version=?",
            (profile_version,),
        ).fetchone()

        if row is None:
            raise ValueError(f"Build not found: {profile_version}")
        if row["status"] != "completed":
            raise ValueError(
                f"Build {profile_version} is not completed (status={row['status']})"
            )

        now = datetime.now(timezone.utc).isoformat()
        config_json = row["config_json"]

        previous = self.conn.execute(
            "SELECT profile_version FROM preference_profile_current WHERE slot='current'"
        ).fetchone()
        previous_version = previous["profile_version"] if previous else None

        # Append publication row
        self.conn.execute(
            """INSERT INTO preference_profile_publications
               (profile_version, previous_profile_version, published_at, config_json)
               VALUES (?, ?, ?, ?)""",
            (profile_version, previous_version, now, config_json),
        )

        # Update current slot
        self.conn.execute(
            """INSERT OR REPLACE INTO preference_profile_current
               (slot, profile_version, published_at) VALUES ('current', ?, ?)""",
            (profile_version, now),
        )
        self.conn.commit()

    def rollback(self, profile_version: str) -> None:
        """Point the current slot at a prior *profile_version*.

        The rollback itself is recorded as a publication entry so that
        history is fully preserved.  Raises ``ValueError`` when the
        build does not exist or is not completed.
        """
        row = self.conn.execute(
            "SELECT status, config_json FROM preference_profile_builds WHERE profile_version=?",
            (profile_version,),
        ).fetchone()

        if row is None:
            raise ValueError(f"Build not found: {profile_version}")
        if row["status"] != "completed":
            raise ValueError(
                f"Build {profile_version} is not completed (status={row['status']})"
            )

        now = datetime.now(timezone.utc).isoformat()
        config_json = row["config_json"]

        previous = self.conn.execute(
            "SELECT profile_version FROM preference_profile_current WHERE slot='current'"
        ).fetchone()
        previous_version = previous["profile_version"] if previous else None

        # Append publication row for the rollback
        self.conn.execute(
            """INSERT INTO preference_profile_publications
               (profile_version, previous_profile_version, published_at, config_json)
               VALUES (?, ?, ?, ?)""",
            (profile_version, previous_version, now, config_json),
        )

        # Update current slot
        self.conn.execute(
            """INSERT OR REPLACE INTO preference_profile_current
               (slot, profile_version, published_at) VALUES ('current', ?, ?)""",
            (profile_version, now),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _effective_events(self) -> dict[str, dict[str, Any]]:
        """Latest like/dislike/favorite event per target, excluding superseded rows.

        A later ``neutral`` / ``skip`` / ``quality_reject`` evicts the target.
        """
        return load_latest_scoring_events(self.conn)

    def _compute_centroid(
        self,
        candidate_ids: list[str],
        embedding_model: str,
        embedding_dim: int,
        weights: dict[str, float] | None = None,
        k: int = 1,
    ) -> bytes | None:
        """Mean (or weighted-mean / k prototypes) across candidate vectors.

        Each stored vector is L2-normalized before averaging so mixed-norm
        blobs cannot dominate favorite/recency weights.  When *k* > 1 the
        blob packs ``k`` concatenated float32 centroids.
        """
        if not candidate_ids:
            return None

        placeholders = ",".join(["?"] * len(candidate_ids))
        rows = self.conn.execute(
            f"""SELECT cv.candidate_id, cv.vector_blob
                 FROM candidate_vectors cv
                 WHERE cv.candidate_id IN ({placeholders})
                   AND cv.embedding_model = ?
                   AND cv.embedding_dim = ?""",
            (*candidate_ids, embedding_model, embedding_dim),
        ).fetchall()

        if not rows:
            return None

        vectors = []
        weight_list = []
        for r in rows:
            vec = l2_normalize(blob_to_vector(r["vector_blob"]))
            w = weights.get(r["candidate_id"], 1.0) if weights is not None else 1.0
            if w <= 0:
                continue
            vectors.append(vec)
            weight_list.append(w)

        if not vectors:
            return None

        stacked = np.stack(vectors, axis=0)
        weight_arr = np.asarray(weight_list, dtype=np.float64)
        k = max(1, min(int(k), stacked.shape[0]))
        if k <= 1:
            centroid = np.average(stacked, axis=0, weights=weight_arr)
            # Centroid blobs (single or k * dim prototypes) stay raw float32
            # concats: the weighted average is intentionally not re-normalized
            # (max_cosine normalizes at read time), so vector_to_blob's
            # unit-vector contract must not be applied here.
            return np.asarray(centroid, dtype=np.float32).tobytes()

        centers = weighted_kmeans(stacked, weight_arr, k, seed=0)
        return centers.astype(np.float32).tobytes()

    def _compute_tag_weights(
        self,
        like_events: list[dict[str, Any]],
        dislike_events: list[dict[str, Any]] | None = None,
    ) -> dict[str, float]:
        """Discriminative tag weights with Laplace smoothing.

        ``(p_like - p_dislike) / (p_like + p_dislike)`` in ``(-1, 1)``. Keys
        are stored both bare and as ``tag:{name}`` so the reranker can match
        scenario keys.
        """
        dislike_events = dislike_events or []

        def _counts(events: list[dict[str, Any]]) -> dict[str, int]:
            if not events:
                return {}
            target_ids = [e["target_id"] for e in events]
            placeholders = ",".join(["?"] * len(target_ids))
            rows = self.conn.execute(
                f"SELECT tags_json FROM candidate_gifs WHERE candidate_id IN ({placeholders})",
                target_ids,
            ).fetchall()
            counts: dict[str, int] = {}
            for (tags_json,) in rows:
                try:
                    tags = json.loads(tags_json)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(tags, list):
                    continue
                for tag in tags:
                    if tag:
                        counts[str(tag)] = counts.get(str(tag), 0) + 1
            return counts

        like_counts = _counts(like_events)
        dislike_counts = _counts(dislike_events)
        all_tags = set(like_counts) | set(dislike_counts)
        if not all_tags:
            return {}

        alpha = 0.5
        n_like = max(1, len(like_events))
        n_dislike = max(1, len(dislike_events))
        weights: dict[str, float] = {}
        for tag in all_tags:
            p_like = (like_counts.get(tag, 0) + alpha) / (n_like + 2 * alpha)
            p_dislike = (dislike_counts.get(tag, 0) + alpha) / (n_dislike + 2 * alpha)
            denom = p_like + p_dislike
            signed = (p_like - p_dislike) / denom if denom else 0.0
            weights[tag] = float(signed)
            tagged = tag if tag.startswith("tag:") else f"tag:{tag}"
            weights[tagged] = float(signed)
        return weights

    def _build_scenario_profiles(
        self,
        *,
        effective: dict[str, dict[str, Any]],
        embedding_model: str,
        embedding_dim: int,
        profile_version: str,
        min_events: int = MIN_SCENARIO_EVENTS,
        positive_weights: dict[str, float] | None = None,
        negative_weights: dict[str, float] | None = None,
        k: int = 1,
    ) -> None:
        """Compute and insert scenario-level profiles for keys meeting thresholds."""
        from collections import defaultdict

        scenario_likes: dict[str, set[str]] = defaultdict(set)
        scenario_dislikes: dict[str, set[str]] = defaultdict(set)
        scenario_like_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        scenario_dislike_events: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for evt in effective.values():
            try:
                scenario_keys = json.loads(evt.get("scenario_keys_json") or "[]")
            except (TypeError, json.JSONDecodeError):
                scenario_keys = []
            rating = evt["rating"]
            tid = evt["target_id"]
            for key in scenario_keys:
                if rating in ("like", "favorite"):
                    scenario_likes[key].add(tid)
                    scenario_like_events[key].append(evt)
                elif rating == "dislike":
                    scenario_dislikes[key].add(tid)
                    scenario_dislike_events[key].append(evt)

        all_keys = set(scenario_likes.keys()) | set(scenario_dislikes.keys())

        for key in sorted(all_keys):
            like_ids = scenario_likes.get(key, set())
            dislike_ids = scenario_dislikes.get(key, set())
            total = len(like_ids) + len(dislike_ids)

            if total < min_events:
                continue

            confidence = abs(len(like_ids) - len(dislike_ids)) / total
            if confidence < MIN_SCENARIO_CONFIDENCE:
                continue

            liked_centroid = self._compute_centroid(
                list(like_ids),
                embedding_model,
                embedding_dim,
                weights=positive_weights,
                k=k,
            )
            disliked_centroid = self._compute_centroid(
                list(dislike_ids),
                embedding_model,
                embedding_dim,
                weights=negative_weights,
                k=k,
            )

            tag_weights = self._compute_tag_weights(
                scenario_like_events.get(key, []),
                scenario_dislike_events.get(key, []),
            )

            self._insert_profile(
                profile_version=profile_version,
                scope="scenario",
                scenario_key=key,
                like_count=len(like_ids),
                dislike_count=len(dislike_ids),
                neutral_count=0,
                confidence=confidence,
                liked_centroid_blob=liked_centroid,
                disliked_centroid_blob=disliked_centroid,
                tag_weights_json=_json_dumps(tag_weights),
            )

    def _insert_build_row(
        self,
        *,
        profile_version: str,
        event_watermark: str,
        embedding_model: str,
        embedding_dim: int,
        effective_feedback_count: int,
        source_video_count: int,
        config_json: str,
        status: str,
        gate_reasons_json: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        completed_at = now if status in ("completed", "blocked", "failed") else None

        self.conn.execute(
            """INSERT OR REPLACE INTO preference_profile_builds
               (profile_version, event_watermark, embedding_model, embedding_dim,
                effective_feedback_count, source_video_count, config_json,
                status, gate_reasons_json, created_at, completed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                profile_version,
                event_watermark,
                embedding_model,
                embedding_dim,
                effective_feedback_count,
                source_video_count,
                config_json,
                status,
                gate_reasons_json,
                now,
                completed_at,
            ),
        )

    def _insert_profile(
        self,
        *,
        profile_version: str,
        scope: Literal["global", "scenario"],
        scenario_key: str | None,
        like_count: int,
        dislike_count: int,
        neutral_count: int,
        confidence: float,
        liked_centroid_blob: bytes | None,
        disliked_centroid_blob: bytes | None,
        tag_weights_json: str,
    ) -> None:
        import uuid

        profile_id = f"prof_{uuid.uuid4().hex[:16]}"
        now = datetime.now(timezone.utc).isoformat()

        self.conn.execute(
            """INSERT OR REPLACE INTO preference_profiles
               (profile_id, profile_version, scope, scenario_key,
                like_count, dislike_count, neutral_count, confidence,
                liked_centroid_blob, disliked_centroid_blob,
                tag_weights_json, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                profile_id,
                profile_version,
                scope,
                scenario_key,
                like_count,
                dislike_count,
                neutral_count,
                confidence,
                liked_centroid_blob,
                disliked_centroid_blob,
                tag_weights_json,
                now,
            ),
        )

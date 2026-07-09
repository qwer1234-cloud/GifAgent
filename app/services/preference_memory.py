"""P1-5: PreferenceMemoryService — build immutable preference profiles.

Gate-minimum profile builds with deterministic versioning, global and scenario
centroids, and manual publish workflow.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Literal

import numpy as np

from app.services.preference_types import (
    ProfileBuildResult,
    ScoreBreakdown,
)


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


def _serialize_vector(vec: np.ndarray) -> bytes:
    return vec.astype(np.float32).tobytes()


def _deserialize_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


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

    def build_profile(
        self,
        dry_run: bool = False,
        *,
        embedding_model: str = REQUIRED_EMBEDDING_MODEL,
        embedding_dim: int = REQUIRED_EMBEDDING_DIM,
    ) -> ProfileBuildResult:
        """Run all gates and, when they pass, compute global + scenario profiles.

        Returns a ``ProfileBuildResult`` dict with ``status`` either ``"built"``
        or ``"blocked"``.  When ``dry_run`` is ``True`` nothing is written to
        the database.
        """
        # ---- 1. Gather effective feedback (latest per target, like/dislike only)
        effective = self._effective_events()
        effective_list = list(effective.values())

        like_events = [e for e in effective_list if e["rating"] == "like"]
        dislike_events = [e for e in effective_list if e["rating"] == "dislike"]

        effective_count = len(effective_list)
        like_count = len(like_events)
        dislike_count = len(dislike_events)

        # ---- 2. Source-video diversity
        video_counts: dict[str, int] = {}
        for e in effective_list:
            vid = e["source_video_sha256"]
            video_counts[vid] = video_counts.get(vid, 0) + 1

        source_video_count = len(video_counts)
        max_single_video_share = (
            max(video_counts.values()) / effective_count if effective_count > 0 else 0.0
        )

        # ---- 3. Embedding model / dimension check
        model_row = self.conn.execute(
            "SELECT DISTINCT embedding_model, embedding_dim FROM candidate_vectors"
        ).fetchone()

        vectors_exist = model_row is not None
        model_ok = vectors_exist and model_row["embedding_model"] == embedding_model
        dim_ok = vectors_exist and model_row["embedding_dim"] == embedding_dim
        effective_target_ids = sorted(set(e["target_id"] for e in effective_list))
        candidate_vector_count = 0
        if effective_target_ids and model_ok and dim_ok:
            placeholders = ",".join(["?"] * len(effective_target_ids))
            candidate_vector_count = self.conn.execute(
                f"""SELECT COUNT(DISTINCT candidate_id)
                    FROM candidate_vectors
                    WHERE candidate_id IN ({placeholders})
                      AND vector_type='clip'
                      AND embedding_model=?
                      AND embedding_dim=?""",
                (*effective_target_ids, embedding_model, embedding_dim),
            ).fetchone()[0]

        # ---- 4. Evaluate gates
        gate_reasons: list[str] = []

        if effective_count < MIN_EFFECTIVE_FEEDBACK:
            gate_reasons.append(
                f"effective_feedback_count={effective_count} < {MIN_EFFECTIVE_FEEDBACK}"
            )
        if like_count < MIN_LIKE_COUNT:
            gate_reasons.append(f"like_count={like_count} < {MIN_LIKE_COUNT}")
        if dislike_count < MIN_DISLIKE_COUNT:
            gate_reasons.append(f"dislike_count={dislike_count} < {MIN_DISLIKE_COUNT}")
        if source_video_count < MIN_SOURCE_VIDEOS:
            gate_reasons.append(
                f"source_video_count={source_video_count} < {MIN_SOURCE_VIDEOS}"
            )
        if max_single_video_share > MAX_SINGLE_VIDEO_SHARE:
            gate_reasons.append(
                f"max_single_video_share={max_single_video_share:.2f} > {MAX_SINGLE_VIDEO_SHARE}"
            )
        if not vectors_exist:
            gate_reasons.append("no_vectors_found in candidate_vectors")
        else:
            if not model_ok:
                gate_reasons.append(
                    f"embedding_model mismatch: "
                    f"found={model_row['embedding_model']} required={embedding_model}"
                )
            if not dim_ok:
                gate_reasons.append(
                    f"embedding_dim mismatch: "
                    f"found={model_row['embedding_dim']} required={embedding_dim}"
                )
            if model_ok and dim_ok and candidate_vector_count < effective_count:
                gate_reasons.append(
                    f"candidate_vector_count={candidate_vector_count} "
                    f"< effective_feedback_count={effective_count}"
                )

        # ---- 5. Event watermark
        event_watermark = ""
        if effective_list:
            event_watermark = max(e["created_at"] for e in effective_list)
        else:
            max_ts = self.conn.execute(
                "SELECT MAX(created_at) FROM preference_events"
            ).fetchone()[0]
            if max_ts:
                event_watermark = max_ts

        # Sorted target IDs for deterministic versioning
        sorted_target_ids = sorted(
            set(e["target_id"] for e in effective_list)
        )
        config_obj = {"embedding_model": embedding_model, "embedding_dim": embedding_dim}
        config_json = _json_dumps(config_obj)

        profile_version = _compute_profile_version(
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
            event_watermark=event_watermark,
            sorted_target_ids=sorted_target_ids,
            config_json=config_json,
        )

        # ---- 6. Blocked path
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

        # ---- 7. Building path: compute centroids
        liked_target_ids = [e["target_id"] for e in like_events]
        disliked_target_ids = [e["target_id"] for e in dislike_events]

        liked_centroid_blob = self._compute_centroid(
            liked_target_ids, embedding_model, embedding_dim
        )
        disliked_centroid_blob = self._compute_centroid(
            disliked_target_ids, embedding_model, embedding_dim
        )

        # Individual vectors may be missing — centroids can still be computed
        # if at least one vector is found per category.
        if liked_centroid_blob is None or disliked_centroid_blob is None:
            # Technically the gate would have blocked on no_vectors_found above,
            # but this handles the edge case where vectors exist for one
            # category but not the other.
            missing_info = []
            if liked_centroid_blob is None:
                missing_info.append("no liked vectors")
            if disliked_centroid_blob is None:
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

        # ---- 8. Global profile
        tag_weights = self._compute_tag_weights(like_events)

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

            # ---- 9. Scenario profiles
            self._build_scenario_profiles(
                effective=effective,
                embedding_model=embedding_model,
                embedding_dim=embedding_dim,
                profile_version=profile_version,
            )

            self.conn.commit()

        return result

    def publish(self, profile_version: str) -> None:
        """Promote a completed build to the ``preference_profile_current`` slot.

        Raises ``ValueError`` when the build does not exist or is not completed.
        """
        row = self.conn.execute(
            "SELECT status FROM preference_profile_builds WHERE profile_version=?",
            (profile_version,),
        ).fetchone()

        if row is None:
            raise ValueError(f"Build not found: {profile_version}")
        if row["status"] != "completed":
            raise ValueError(
                f"Build {profile_version} is not completed (status={row['status']})"
            )

        now = datetime.now(timezone.utc).isoformat()
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
        """Return the latest like/dislike event per (target_type, target_id).

        Excludes neutral, skip, and quality_reject events.
        """
        rows = self.conn.execute(
            """SELECT event_id, target_type, target_id, rating,
                      source_video_sha256, scenario_keys_json, created_at
               FROM preference_events
               ORDER BY created_at ASC"""
        ).fetchall()

        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            rating = row["rating"]
            if rating not in ("like", "dislike"):
                continue
            key = f"{row['target_type']}:{row['target_id']}"
            latest[key] = dict(row)

        return latest

    def _compute_centroid(
        self,
        candidate_ids: list[str],
        embedding_model: str,
        embedding_dim: int,
    ) -> bytes | None:
        """Mean vector across all candidate_ids that have vectors.

        Returns the serialized float32 array or ``None`` if no vectors found.
        """
        if not candidate_ids:
            return None

        placeholders = ",".join(["?"] * len(candidate_ids))
        rows = self.conn.execute(
            f"""SELECT cv.vector_blob
                 FROM candidate_vectors cv
                 WHERE cv.candidate_id IN ({placeholders})
                   AND cv.embedding_model = ?
                   AND cv.embedding_dim = ?""",
            (*candidate_ids, embedding_model, embedding_dim),
        ).fetchall()

        if not rows:
            return None

        vectors = [_deserialize_vector(r[0]) for r in rows]
        stacked = np.stack(vectors, axis=0)
        centroid = np.mean(stacked, axis=0)
        return _serialize_vector(centroid)

    def _compute_tag_weights(self, like_events: list[dict[str, Any]]) -> dict[str, float]:
        """Count tag frequency in liked candidates, normalize to [0, 1]."""
        if not like_events:
            return {}

        target_ids = [e["target_id"] for e in like_events]
        placeholders = ",".join(["?"] * len(target_ids))

        rows = self.conn.execute(
            f"SELECT tags_json FROM candidate_gifs WHERE candidate_id IN ({placeholders})",
            target_ids,
        ).fetchall()

        tag_counts: dict[str, int] = {}
        for (tags_json,) in rows:
            try:
                tags = json.loads(tags_json)
            except (json.JSONDecodeError, TypeError):
                continue
            for tag in tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

        if not tag_counts:
            return {}

        max_count = max(tag_counts.values())
        return {tag: count / max_count for tag, count in tag_counts.items()}

    def _build_scenario_profiles(
        self,
        *,
        effective: dict[str, dict[str, Any]],
        embedding_model: str,
        embedding_dim: int,
        profile_version: str,
    ) -> None:
        """Compute and insert scenario-level profiles for keys meeting thresholds."""
        # Group events by scenario key
        from collections import defaultdict

        scenario_likes: dict[str, set[str]] = defaultdict(set)
        scenario_dislikes: dict[str, set[str]] = defaultdict(set)

        for evt in effective.values():
            scenario_keys = json.loads(evt.get("scenario_keys_json", "[]"))
            rating = evt["rating"]
            tid = evt["target_id"]
            for key in scenario_keys:
                if rating == "like":
                    scenario_likes[key].add(tid)
                elif rating == "dislike":
                    scenario_dislikes[key].add(tid)

        all_keys = set(scenario_likes.keys()) | set(scenario_dislikes.keys())

        for key in sorted(all_keys):
            like_ids = scenario_likes.get(key, set())
            dislike_ids = scenario_dislikes.get(key, set())
            total = len(like_ids) + len(dislike_ids)

            if total < MIN_SCENARIO_EVENTS:
                continue

            # Confidence: signal clarity — 1.0 = all same rating
            confidence = abs(len(like_ids) - len(dislike_ids)) / total
            if confidence < MIN_SCENARIO_CONFIDENCE:
                continue

            liked_centroid = self._compute_centroid(
                list(like_ids), embedding_model, embedding_dim
            )
            disliked_centroid = self._compute_centroid(
                list(dislike_ids), embedding_model, embedding_dim
            )

            # Build tag weights from liked events in this scenario
            tag_weights: dict[str, float] = {}
            if like_ids:
                tag_weights = self._compute_tag_weights(
                    [{"target_id": tid} for tid in like_ids]
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

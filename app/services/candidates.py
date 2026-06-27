"""P1-3: CandidateService — materialize run candidates into candidate_gifs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.services.preference_types import MaterializedCandidate
from app.services.scenario import normalize_scenario_keys, json_dumps


def _build_candidate_id(
    source_video_sha256: str,
    start_sec: float,
    end_sec: float,
    run_candidate_id: str,
) -> str:
    raw = f"{source_video_sha256}{start_sec}{end_sec}{run_candidate_id}"
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return f"cand_{digest}"


class CandidateService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def materialize_run_candidate(
        self, run_id: str, run_candidate: dict[str, Any]
    ) -> MaterializedCandidate:
        source_video_sha256 = run_candidate["source_video_sha256"]
        start_sec = float(run_candidate["start_sec"])
        end_sec = float(run_candidate["end_sec"])
        run_candidate_id = run_candidate["run_candidate_id"]

        candidate_id = _build_candidate_id(
            source_video_sha256, start_sec, end_sec, run_candidate_id
        )

        # Check if already exists
        existing = self.conn.execute(
            "SELECT candidate_id, source_run_id, source_run_candidate_id, "
            "source_video_sha256, start_sec, end_sec, status "
            "FROM candidate_gifs WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()

        if existing is not None:
            return MaterializedCandidate(
                candidate_id=existing["candidate_id"],
                source_run_id=existing["source_run_id"],
                source_run_candidate_id=existing["source_run_candidate_id"],
                source_video_sha256=existing["source_video_sha256"],
                start_sec=existing["start_sec"],
                end_sec=existing["end_sec"],
                status=existing["status"],
            )

        source_video_path = run_candidate.get("source_video_path", "")
        artifact_path = run_candidate.get("artifact_path")
        preview_path = run_candidate.get("preview_path")
        vlm_summary = run_candidate.get("vlm_summary", {})
        tags = run_candidate.get("tags", [])
        base_rag_similarity = run_candidate.get("base_rag_similarity")
        final_score = run_candidate.get("final_score")

        # Build scenario keys from vlm_summary and tags
        emotion = vlm_summary.get("emotion") if isinstance(vlm_summary, dict) else None
        scene_type = vlm_summary.get("scene_type") if isinstance(vlm_summary, dict) else None
        scenario_keys = normalize_scenario_keys(
            emotion=emotion, scene_type=scene_type, tags=tags
        )

        now = datetime.now(timezone.utc).isoformat()

        self.conn.execute(
            """INSERT OR IGNORE INTO candidate_gifs
               (candidate_id, source_run_id, source_run_candidate_id,
                source_video_sha256, source_video_path, start_sec, end_sec,
                artifact_path, preview_path,
                vlm_summary_json, tags_json, scenario_keys_json,
                base_rag_similarity, final_score, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                candidate_id,
                run_id,
                run_candidate_id,
                source_video_sha256,
                source_video_path,
                start_sec,
                end_sec,
                artifact_path,
                preview_path,
                json_dumps(vlm_summary),
                json_dumps(tags),
                json_dumps(scenario_keys),
                base_rag_similarity,
                final_score,
                "candidate",
                now,
                now,
            ),
        )
        self.conn.commit()

        # If INSERT OR IGNORE silently skipped (race), re-read
        row = self.conn.execute(
            "SELECT candidate_id, source_run_id, source_run_candidate_id, "
            "source_video_sha256, start_sec, end_sec, status "
            "FROM candidate_gifs WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()

        return MaterializedCandidate(
            candidate_id=row["candidate_id"],
            source_run_id=row["source_run_id"],
            source_run_candidate_id=row["source_run_candidate_id"],
            source_video_sha256=row["source_video_sha256"],
            start_sec=row["start_sec"],
            end_sec=row["end_sec"],
            status=row["status"],
        )

#!/usr/bin/env python3
"""Undo feedback events for candidates whose GIF artifacts are gone.

Found by the curve preference qualification replay: 696 effective training
targets reference candidate GIFs that no longer exist on disk, so their clip
vectors can never be generated and the profile-build gate
(``candidate_vector_count >= effective_feedback_count``) is structurally
un-passable. Removing those candidates from the effective population
requires undoing their feedback events — an explicit, human-approved
production data decision (spec §12).

Semantics mirror ``PreferenceEventService.undo_last_candidate_action``:
every effective (non-undone, non-superseded) feedback event of an affected
candidate gets ``undone_at``/``undone_reason``; the candidate status is
restored to the earliest recorded ``previous_status``; ``favorite_gifs``
rows are removed for undone favorites. Nothing is deleted from
``preference_events``; the append-only history is preserved.

Run with ``--dry-run`` first (default) to review; ``--execute`` performs the
transaction after taking a timestamped online backup.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.preference_events import load_latest_scoring_events  # noqa: E402
from app.services.preference_schema import apply_preference_schema  # noqa: E402

UNDO_REASON = "artifact_missing_qualification_undo"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _backup(db_path: Path) -> Path:
    repo = Path(__file__).resolve().parents[1]
    backup_dir = repo / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"library.before-artifact-undo.{stamp}.db"
    source = sqlite3.connect(str(db_path), timeout=60)
    source.execute("PRAGMA query_only = ON")
    target = sqlite3.connect(str(backup_path))
    source.backup(target)
    target.close()
    source.close()
    return backup_path


def identify_candidates(
    conn: sqlite3.Connection, *, holdout_sources: set[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return affected candidates and any post-undo remaining issues."""

    conn.row_factory = sqlite3.Row
    effective = load_latest_scoring_events(conn)
    affected: list[dict[str, Any]] = []
    for key, event in sorted(effective.items()):
        if not key.startswith("candidate_gif:"):
            continue
        source = str(event.get("source_video_sha256") or "")
        if source in holdout_sources:
            continue
        candidate_id = key.split(":", 1)[1]
        row = conn.execute(
            "SELECT artifact_path FROM candidate_gifs WHERE candidate_id = :cid",
            {"cid": candidate_id},
        ).fetchone()
        artifact_path = row["artifact_path"] if row else None
        if artifact_path and Path(artifact_path).is_file():
            continue
        events = conn.execute(
            """
            SELECT event_id, rating, created_at, previous_status,
                   COALESCE(note, '') AS note
            FROM preference_events
            WHERE target_type = 'candidate_gif'
              AND target_id = :cid
              AND event_kind = 'feedback'
              AND undone_at IS NULL
              AND event_id NOT IN (
                  SELECT supersedes_event_id FROM preference_events
                  WHERE supersedes_event_id IS NOT NULL
              )
            ORDER BY created_at ASC, event_id ASC
            """,
            {"cid": candidate_id},
        ).fetchall()
        affected.append(
            {
                "candidate_id": candidate_id,
                "artifact_path": artifact_path or "",
                "events": [
                    {
                        "event_id": e["event_id"],
                        "rating": e["rating"],
                        "created_at": e["created_at"],
                        "previous_status": e["previous_status"],
                        "note": e["note"],
                    }
                    for e in events
                ],
            }
        )
    return affected, []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default="dist/GifAgentUI/data/library.db"
    )
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(str(args.db), timeout=60)
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)

    model_row = conn.execute(
        "SELECT holdout_manifest_json FROM curve_preference_models "
        "WHERE model_version = :model_version",
        {"model_version": args.model_version},
    ).fetchone()
    if model_row is None:
        print(f"model {args.model_version!r} not found", file=sys.stderr)
        return 2
    holdout_sources = set(
        json.loads(model_row["holdout_manifest_json"]).get("sources") or []
    )

    affected, _issues = identify_candidates(
        conn, holdout_sources=holdout_sources
    )
    event_count = sum(len(c["events"]) for c in affected)
    favorite_count = sum(
        1
        for c in affected
        for e in c["events"]
        if e["rating"] == "favorite" or (e["rating"] == "like" and e["note"] == "favorite")
    )
    print(f"affected candidates: {len(affected)}")
    print(f"effective events to undo: {event_count}")
    print(f"favorite_gifs rows to remove: {favorite_count}")

    if not args.execute:
        print("DRY-RUN complete; nothing was written.")
        return 0

    backup_path = _backup(Path(args.db))
    print(f"backup: {backup_path}")

    now = _now()
    audit = {
        "kind": "artifact-undo-audit-v1",
        "executed_at": now,
        "backup": str(backup_path),
        "undo_reason": UNDO_REASON,
        "candidates": affected,
    }
    repo = Path(__file__).resolve().parents[1]
    audit_path = (
        repo
        / "data"
        / "backups"
        / f"artifact-undo-audit.{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    )

    conn.execute("BEGIN IMMEDIATE")
    try:
        for candidate in affected:
            candidate_id = str(candidate["candidate_id"])
            events = candidate["events"]
            earliest_previous_status = events[0]["previous_status"]
            had_favorite = any(
                e["rating"] == "favorite"
                or (e["rating"] == "like" and e["note"] == "favorite")
                for e in events
            )
            for event in events:
                conn.execute(
                    "UPDATE preference_events SET undone_at = :now, "
                    "undone_reason = :reason WHERE event_id = :event_id",
                    {"now": now, "reason": UNDO_REASON, "event_id": event["event_id"]},
                )
            if earliest_previous_status is not None:
                conn.execute(
                    "UPDATE candidate_gifs SET status = :status, "
                    "updated_at = :now WHERE candidate_id = :cid",
                    {
                        "status": earliest_previous_status,
                        "now": now,
                        "cid": candidate_id,
                    },
                )
            if had_favorite:
                conn.execute(
                    "DELETE FROM favorite_gifs WHERE candidate_id = :cid",
                    {"cid": candidate_id},
                )
        remaining, _issues = identify_candidates(
            conn, holdout_sources=holdout_sources
        )
        if remaining:
            raise RuntimeError(
                f"{len(remaining)} effective scoring candidates still "
                "reference missing artifacts after the undo"
            )
        audit_path.write_text(
            json.dumps(audit, sort_keys=True, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    conn.close()
    print(f"audit manifest: {audit_path}")
    print(f"undone {len(affected)} candidates / {event_count} events")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Split preference events by source video into train and holdout sets.

Ensures zero source-video overlap between train and holdout, and enforces
that both splits satisfy gating requirements.

Output files:
- <output_dir>/holdout.jsonl: Holdout judgment entries (favorite mapped to like)
- <output_dir>/exclude_source_videos.json: List of source video sha256s in holdout
- <output_dir>/split_summary.json: Split statistics and verification metrics
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Ensure project root in sys.path
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from app.services.preference_events import load_latest_scoring_events
from app.services.preference_schema import apply_preference_schema

MIN_HOLDOUT_JUDGMENTS = 30
MIN_HOLDOUT_SOURCE_VIDEOS = 3
MAX_HOLDOUT_SINGLE_VIDEO_SHARE = 0.40

MIN_TRAIN_EFFECTIVE_FEEDBACK = 30
MIN_TRAIN_LIKE_COUNT = 15
MIN_TRAIN_DISLIKE_COUNT = 10
MIN_TRAIN_SOURCE_VIDEOS = 3
MAX_TRAIN_SINGLE_VIDEO_SHARE = 0.40


def split_source_videos(
    conn: sqlite3.Connection,
    *,
    seed: int = 20260902,
) -> dict[str, Any]:
    """Perform deterministic source-video train/holdout split."""
    effective_events = load_latest_scoring_events(conn)
    if not effective_events:
        raise ValueError("No effective scoring events found in database")

    # Group effective events by source_video_sha256
    video_to_events: dict[str, list[dict[str, Any]]] = {}
    for event in effective_events.values():
        vid = str(event["source_video_sha256"])
        video_to_events.setdefault(vid, []).append(event)

    all_vids = sorted(video_to_events.keys())
    rng = random.Random(seed)
    shuffled_vids = list(all_vids)
    rng.shuffle(shuffled_vids)

    holdout_vids: list[str] = []
    holdout_events: list[dict[str, Any]] = []
    success = False

    for vid in shuffled_vids:
        tentative_h_vids = holdout_vids + [vid]
        tentative_h_evts = holdout_events + video_to_events[vid]

        h_count = len(tentative_h_evts)
        max_h_share = (
            max(len(video_to_events[v]) for v in tentative_h_vids) / h_count
            if h_count > 0
            else 1.0
        )

        t_vids = [v for v in all_vids if v not in tentative_h_vids]
        t_evts = [e for v in t_vids for e in video_to_events[v]]
        t_count = len(t_evts)
        t_likes = sum(1 for e in t_evts if e["rating"] in ("like", "favorite"))
        t_dislikes = sum(1 for e in t_evts if e["rating"] == "dislike")
        max_t_share = (
            max(len(video_to_events[v]) for v in t_vids) / t_count
            if t_count > 0
            else 1.0
        )

        holdout_vids = tentative_h_vids
        holdout_events = tentative_h_evts

        if (
            len(holdout_vids) >= MIN_HOLDOUT_SOURCE_VIDEOS
            and h_count >= MIN_HOLDOUT_JUDGMENTS
            and max_h_share <= MAX_HOLDOUT_SINGLE_VIDEO_SHARE
            and len(t_vids) >= MIN_TRAIN_SOURCE_VIDEOS
            and t_count >= MIN_TRAIN_EFFECTIVE_FEEDBACK
            and t_likes >= MIN_TRAIN_LIKE_COUNT
            and t_dislikes >= MIN_TRAIN_DISLIKE_COUNT
            and max_t_share <= MAX_TRAIN_SINGLE_VIDEO_SHARE
        ):
            success = True
            break

    if not success:
        raise ValueError(
            "Could not find a train/holdout split satisfying both train and holdout gates"
        )

    train_vids = [v for v in all_vids if v not in holdout_vids]
    train_events = [e for v in train_vids for e in video_to_events[v]]

    h_likes = sum(1 for e in holdout_events if e["rating"] in ("like", "favorite"))
    h_dislikes = sum(1 for e in holdout_events if e["rating"] == "dislike")
    t_likes = sum(1 for e in train_events if e["rating"] in ("like", "favorite"))
    t_dislikes = sum(1 for e in train_events if e["rating"] == "dislike")

    return {
        "holdout_videos": sorted(holdout_vids),
        "train_videos": sorted(train_vids),
        "holdout_events": holdout_events,
        "train_events": train_events,
        "metrics": {
            "seed": seed,
            "total_effective_events": len(effective_events),
            "total_source_videos": len(all_vids),
            "holdout": {
                "source_video_count": len(holdout_vids),
                "judgment_count": len(holdout_events),
                "like_count": h_likes,
                "dislike_count": h_dislikes,
                "max_single_video_share": round(
                    max(len(video_to_events[v]) for v in holdout_vids)
                    / len(holdout_events),
                    4,
                ),
            },
            "train": {
                "source_video_count": len(train_vids),
                "effective_feedback_count": len(train_events),
                "like_count": t_likes,
                "dislike_count": t_dislikes,
                "max_single_video_share": round(
                    max(len(video_to_events[v]) for v in train_vids)
                    / len(train_events),
                    4,
                ),
            },
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Split preference events by source video into train and holdout"
    )
    parser.add_argument(
        "--db",
        required=True,
        help="Path to SQLite database",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Directory to save holdout.jsonl, exclude_source_videos.json, split_summary.json",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260902,
        help="Random seed for deterministic split (default: 20260902)",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: Database file not found: {db_path}", file=sys.stderr)
        sys.exit(2)

    try:
        conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except Exception:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

    try:
        apply_preference_schema(conn)
    except sqlite3.OperationalError:
        # DB might be read-only; schema is expected to already exist
        pass

    try:
        split_res = split_source_videos(conn, seed=args.seed)
    except Exception as exc:
        print(f"ERROR: Split failed: {exc}", file=sys.stderr)
        sys.exit(2)
    finally:
        conn.close()

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. holdout.jsonl
    holdout_jsonl_path = output_dir / "holdout.jsonl"
    with open(holdout_jsonl_path, "w", encoding="utf-8") as f:
        for event in split_res["holdout_events"]:
            raw_rating = str(event["rating"])
            # In GAIN_MAP / evaluate: favorite is mapped to like
            rating = "like" if raw_rating == "favorite" else raw_rating
            line_obj = {
                "candidate_id": str(event["target_id"]),
                "rating": rating,
                "judged_at": str(event.get("created_at", "")),
            }
            f.write(json.dumps(line_obj, ensure_ascii=False) + "\n")

    # 2. exclude_source_videos.json
    exclude_path = output_dir / "exclude_source_videos.json"
    with open(exclude_path, "w", encoding="utf-8") as f:
        json.dump(split_res["holdout_videos"], f, ensure_ascii=False, indent=2)

    # 3. split_summary.json
    summary_path = output_dir / "split_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(split_res["metrics"], f, ensure_ascii=False, indent=2)

    print(json.dumps(split_res["metrics"], ensure_ascii=False, indent=2))
    print(f"Split completed successfully. Output written to {output_dir}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Import adaptive test result clips as candidate_gifs rows.

Usage:
    python scripts/import_adaptive_candidates.py --input data/adaptive_test_result.json --dry-run
    python scripts/import_adaptive_candidates.py --input data/adaptive_test_result.json --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

# Add project root to path so we can import app
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))

from app.db import init_db, get_connection
from app.services.candidates import CandidateService


def _compute_video_sha256(video_path: str) -> str:
    """Compute sha256 of the video file, or fall back to a path-based hash."""
    if os.path.isfile(video_path):
        h = hashlib.sha256()
        with open(video_path, "rb") as f:
            while chunk := f.read(8192):
                h.update(chunk)
        return h.hexdigest()
    # Fallback: hash the normalized path as a stand-in
    return hashlib.sha256(os.path.normpath(video_path).encode()).hexdigest()


def _build_run_candidate(clip: dict[str, Any], video_path: str, video_sha256: str) -> dict[str, Any]:
    """Convert a top_clips entry into the payload that CandidateService expects."""
    rank = clip["rank"]
    return {
        "run_candidate_id": f"adaptive-clip-{rank:04d}",
        "source_video_sha256": video_sha256,
        "source_video_path": video_path,
        "start_sec": float(clip["start_ts"]),
        "end_sec": float(clip["end_ts"]),
        "artifact_path": clip.get("exported_path"),
        "preview_path": clip.get("exported_path"),
        "vlm_summary": {
            "emotion": clip.get("emotional_core"),
            "scene_type": clip.get("scene_type"),
        },
        "tags": [],  # Tags come from the synthesis block if available, not per-clip
        "base_rag_similarity": clip.get("gif_worthiness"),
        "final_score": clip.get("gif_worthiness"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Import adaptive test result candidates")
    parser.add_argument("--input", required=True, help="Path to adaptive_test_result.json")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    parser.add_argument("--apply", action="store_true", help="Actually write to the database")
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        print("ERROR: pass --dry-run or --apply")
        sys.exit(1)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: input file not found: {args.input}")
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    video_path = data.get("video", "")
    video_sha256 = _compute_video_sha256(video_path)
    top_clips = data.get("top_clips", [])

    print(f"Source video:       {video_path}")
    print(f"Source SHA256:      {video_sha256}")
    print(f"Top clips count:    {len(top_clips)}")
    print()

    init_db(apply_preference=True)
    conn = get_connection()
    service = CandidateService(conn)

    if args.dry_run:
        print("DRY RUN — NO WRITES PERFORMED")
        print()
        for clip in top_clips:
            payload = _build_run_candidate(clip, video_path, video_sha256)
            print(f"  Rank {clip['rank']:>3}  "
                  f"start={payload['start_sec']:>6.1f}  "
                  f"end={payload['end_sec']:>6.1f}  "
                  f"worthiness={clip.get('gif_worthiness', 0):.2f}")
        print()
        print(f"Would materialize {len(top_clips)} candidate(s).")
        return

    # --apply
    materialized = 0
    for clip in top_clips:
        payload = _build_run_candidate(clip, video_path, video_sha256)
        candidate = service.materialize_run_candidate("adaptive-import", payload)
        materialized += 1
        print(f"  {candidate.candidate_id}  rank={clip['rank']:>3}  status={candidate.status}")

    print()
    print(f"Materialized {materialized} candidate(s).")
    conn.close()


if __name__ == "__main__":
    main()

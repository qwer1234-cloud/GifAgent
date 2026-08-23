#!/usr/bin/env python3
"""Fit a frozen isotonic calibrator from library.db preference events.

Read-only against the library database.  Refuses to write fewer than 200
labeled like/dislike samples.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.quality_lab.calibration import calibration_curve, fit_monotonic_calibrator


MIN_SAMPLES = 200


def _effective_events(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        """
        SELECT e.target_id, e.rating
        FROM preference_events e
        WHERE e.event_kind = 'feedback'
          AND e.rating IN ('like', 'dislike', 'favorite')
          AND e.event_id NOT IN (
              SELECT supersedes_event_id FROM preference_events
              WHERE supersedes_event_id IS NOT NULL
          )
        """
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def _worthiness_from_summary(raw: str) -> float | None:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("gif_worthiness", payload.get("gif_worthiness_raw"))
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score < 0.0 or score > 1.0:
        return None
    return score


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/library.db")
    parser.add_argument("--out", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--prompt-mode", default="adult")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        events = _effective_events(conn)
        scores: list[float] = []
        labels: list[int] = []
        for target_id, rating in events:
            row = conn.execute(
                "SELECT vlm_summary_json FROM candidate_gifs WHERE candidate_id=?",
                (target_id,),
            ).fetchone()
            if row is None:
                continue
            worth = _worthiness_from_summary(row[0])
            if worth is None:
                continue
            scores.append(worth)
            labels.append(0 if rating == "dislike" else 1)
    finally:
        conn.close()

    if len(scores) < MIN_SAMPLES:
        print(
            f"Need at least {MIN_SAMPLES} labeled samples, got {len(scores)}",
            file=sys.stderr,
        )
        return 2

    curve = calibration_curve(scores, labels)
    calibrator = fit_monotonic_calibrator(scores, labels)
    payload = {
        "model_id": args.model_id,
        "prompt_mode": args.prompt_mode,
        "sample_count": len(scores),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "thresholds": list(calibrator.thresholds),
        "values": list(calibrator.values),
        "reliability": [
            {
                "lower": bin.lower,
                "upper": bin.upper,
                "mean_score": bin.mean_score,
                "positive_rate": bin.positive_rate,
                "count": bin.count,
            }
            for bin in curve
        ],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {out} ({len(scores)} samples)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

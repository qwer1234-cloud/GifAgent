#!/usr/bin/env python3
"""Smoke test for the Quality Lab — configs, manifest, runs, blind A/B, promotion, rollback.

Verifies the full quality-lab lifecycle without running VLM or creating real GIFs.
All stage results are injected directly into the quality-lab database.

Usage:
    uv run python scripts/smoke_quality_lab.py --data-dir <temp-dir>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Ensure the project root is on sys.path so that ``from app…`` works when
# the script is invoked directly (not via ``uv run``).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.quality_lab import (
    BlindReviewService,
    connect_quality_db,
)
from app.quality_lab.promotion import (
    list_champion_history,
    promote_config,
    rollback,
    CURRENT_CONFIG_PATH,
)

# Production data directories that must NOT be used for smoke testing.
_FORBIDDEN_DIRS = frozenset({
    os.path.abspath("data"),
    os.path.abspath("dist/GifAgentUI/data"),
    os.path.realpath("data"),
    os.path.realpath("dist/GifAgentUI/data"),
})

# Files to checksum before and after to verify no source changes.
_SOURCE_CHECKS = [
    Path("app/quality_lab/__init__.py"),
    Path("app/quality_lab/models.py"),
    Path("app/quality_lab/schema.py"),
    Path("app/quality_lab/manifests.py"),
    Path("app/quality_lab/runner.py"),
    Path("app/quality_lab/metrics.py"),
    Path("app/quality_lab/calibration.py"),
    Path("app/quality_lab/ab_review.py"),
    Path("app/quality_lab/promotion.py"),
    Path("app/routers/quality_lab.py"),
]


def _require_safe_data_dir(data_dir: str) -> Path:
    resolved = Path(data_dir).resolve()
    abspath = os.path.abspath(str(resolved))
    real = os.path.realpath(str(resolved))
    if abspath in _FORBIDDEN_DIRS or real in _FORBIDDEN_DIRS:
        print(
            f"ERROR: data directory '{resolved}' is the configured production "
            f"data directory. Refusing to run smoke test against production data.",
            file=sys.stderr,
        )
        sys.exit(2)
    return resolved


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "MISSING"


def _snapshot_source_hashes() -> dict[str, str]:
    return {str(p): _sha256(p) for p in _SOURCE_CHECKS}


def _verify_source_hashes(before: dict[str, str]) -> bool:
    ok = True
    for path_str, old_hash in before.items():
        new_hash = _sha256(Path(path_str))
        if new_hash != old_hash:
            print(f"  [fail] source file changed: {path_str}")
            ok = False
    return ok


def _main(data_dir: Path) -> int:
    db_path = data_dir / "quality_lab.db"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Create data/ subdir for promotion module's hardcoded paths
    (data_dir / "data" / "config_versions").mkdir(parents=True, exist_ok=True)

    # ---- Connect to isolated quality lab DB --------------------------------
    conn = connect_quality_db(str(db_path))
    print(f"Quality lab DB created at: {db_path}")

    # Snapshot source hashes before any operations
    source_before = _snapshot_source_hashes()

    ok = True

    # ---- 1. Create two experiment configs ----------------------------------
    config_a_id = uuid.uuid4().hex
    config_b_id = uuid.uuid4().hex
    now = _utcnow()

    conn.execute(
        "INSERT INTO experiment_configs (config_id, config_json, provenance_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (config_a_id,
         json.dumps({"name": "config_a", "threshold": 0.5}),
         json.dumps({"git_commit": "smoke-test-a", "config_hash": "abc"}),
         now),
    )
    conn.execute(
        "INSERT INTO experiment_configs (config_id, config_json, provenance_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (config_b_id,
         json.dumps({"name": "config_b", "threshold": 0.6}),
         json.dumps({"git_commit": "smoke-test-b", "config_hash": "def"}),
         now),
    )
    conn.commit()
    print("  [ok] created two experiment configs")

    # ---- 2. Create a benchmark manifest with 4 items -----------------------
    manifest_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO benchmark_manifests (manifest_id, version, item_count, created_at) "
        "VALUES (?, ?, ?, ?)",
        (manifest_id, 1, 4, now),
    )

    # All 4 items share the same video_fingerprint so AB session pairing works.
    # 2 items split=tune, 2 items split=holdout.
    fingerprint = "smoke-video-fp-001"
    item_ids: list[str] = []
    for i, split in enumerate(["tune", "tune", "holdout", "holdout"]):
        item_id = uuid.uuid4().hex
        item_ids.append(item_id)
        conn.execute(
            "INSERT INTO benchmark_items "
            "(item_id, manifest_id, source_path, video_fingerprint, "
            " duration_bucket, resolution_bucket, pace_bucket, difficulty_tags, split) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, manifest_id, str(data_dir / f"smoke_video_{i}.mp4"),
             fingerprint, "short", "720p", "medium",
             json.dumps(["action", "dialog"]), split),
        )
    conn.commit()
    print("  [ok] created benchmark manifest with 4 items")

    # ---- 3. Create experiment runs for each config -------------------------
    # Each config needs: one tune run + one holdout run (both completed)
    runs: dict[str, list[str]] = {"config_a": [], "config_b": []}

    for config_key, config_id in [("config_a", config_a_id), ("config_b", config_b_id)]:
        for split in ["tune", "holdout"]:
            run_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO experiment_runs "
                "(run_id, manifest_id, config_id, split, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'running', ?, ?)",
                (run_id, manifest_id, config_id, split, now, now),
            )
            conn.commit()
            runs[config_key].append(run_id)
            print(f"  [ok] created run {run_id} ({config_key}, split={split})")

    # ---- 4. Complete the runs with fake stage results ----------------------
    tune_items = [it for i, it in enumerate(item_ids) if i < 2]
    holdout_items = [it for i, it in enumerate(item_ids) if i >= 2]

    for config_key, config_id in [("config_a", config_a_id), ("config_b", config_b_id)]:
        for split, item_list in [("tune", tune_items), ("holdout", holdout_items)]:
            run_row = conn.execute(
                "SELECT run_id FROM experiment_runs "
                "WHERE config_id=? AND split=? AND status='running'",
                (config_id, split),
            ).fetchone()
            run_id = run_row["run_id"]

            for item_id in item_list:
                conn.execute(
                    "INSERT OR IGNORE INTO experiment_items "
                    "(item_id, run_id, status, wall_time_seconds, vlm_calls, "
                    " token_count, artifact_bytes, candidate_count, created_at) "
                    "VALUES (?, ?, 'completed', ?, ?, ?, ?, ?, ?)",
                    (item_id, run_id, 120.5, 15, 8000, 450000, 8, now),
                )
                # Add high export_integrity so gate 6 passes
                conn.execute(
                    "INSERT INTO metric_values "
                    "(metric_id, run_id, metric_name, value, item_id, created_at) "
                    "VALUES (?, ?, 'export_integrity', ?, ?, ?)",
                    (uuid.uuid4().hex, run_id, 1.0, item_id, now),
                )
                conn.execute(
                    "INSERT INTO metric_values "
                    "(metric_id, run_id, metric_name, value, item_id, created_at) "
                    "VALUES (?, ?, 'temporal_coverage', ?, ?, ?)",
                    (uuid.uuid4().hex, run_id, 0.85, item_id, now),
                )

            # Mark run as completed
            conn.execute(
                "UPDATE experiment_runs SET status='completed', updated_at=? WHERE run_id=?",
                (now, run_id),
            )
            conn.commit()
            print(f"  [ok] completed run {run_id} with fake stage results")

    # ---- 5. Create an AB blind session and record judgments ----------------
    # Session between Config A tune run and Config B tune run
    run_a_tune = runs["config_a"][0]  # tune run for config_a
    run_b_tune = runs["config_b"][0]  # tune run for config_b
    service = BlindReviewService(conn)

    session = service.create_session(run_a=run_a_tune, run_b=run_b_tune, seed=42)
    session_id = session.session_id
    print(f"  [ok] created AB session {session_id}")

    # Record judgments for all pairs
    while True:
        pair = service.next_pair(session_id)
        if pair is None:
            break
        service.record(session_id, str(pair.pair_index), "left")
        print(f"  [ok] recorded judgment for pair {pair.pair_index}")

    # Reveal results
    result = service.reveal(session_id)
    print(f"  [ok] revealed AB result: A wins={result.run_a_wins}, B wins={result.run_b_wins}")

    # Mark session as completed (required for promotion gate)
    conn.execute("UPDATE ab_sessions SET status='completed' WHERE session_id=?", (session_id,))
    conn.commit()

    # ---- 6. Change CWD so promotion module writes to our data dir ----------
    orig_cwd = os.getcwd()
    os.chdir(data_dir)
    try:
        # ---- 7. Promote Config A ------------------------------------------
        promote_result = promote_config(
            config_a_id,
            db_conn=conn,
            confirmation=config_a_id,
        )
        print(f"  [ok] promoted config {config_a_id}: {promote_result['status']}")

        # Verify champion history has 1 promote event
        history = list_champion_history(db_conn=conn)
        if len(history) >= 1 and history[0]["action"] == "promote":
            print(f"  [ok] champion history has promote event")
        else:
            print(f"  [fail] champion history missing promote event")
            ok = False

        # Verify current_config.json was created
        current_config_path = data_dir / CURRENT_CONFIG_PATH
        if current_config_path.exists():
            print(f"  [ok] current_config.json created")
        else:
            print(f"  [fail] current_config.json not created")
            ok = False

        # ---- 8. Roll back --------------------------------------------------
        rollback_result = rollback(db_conn=conn)
        print(f"  [ok] rolled back: {rollback_result['status']}")

        # Verify champion history now has 2 events
        history = list_champion_history(db_conn=conn)
        if len(history) >= 2 and history[0]["action"] == "rollback":
            print(f"  [ok] champion history has rollback event")
        else:
            print(f"  [fail] champion history missing rollback event")
            ok = False

        # ---- 9. Verify no source files changed -----------------------------
        if _verify_source_hashes(source_before):
            print(f"  [ok] no source files changed")
        else:
            ok = False

    finally:
        os.chdir(orig_cwd)

    # ---- Cleanup -----------------------------------------------------------
    conn.close()

    # Clean up only if everything passed
    if ok:
        import shutil
        shutil.rmtree(data_dir, ignore_errors=True)
        print(f"  [ok] cleaned up temporary data directory")

    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Smoke test for GifAgent Quality Lab"
    )
    parser.add_argument(
        "--data-dir", required=True,
        help="Temporary directory for smoke test data "
             "(must NOT be the production data directory)",
    )
    args = parser.parse_args()

    data_dir = _require_safe_data_dir(args.data_dir)
    sys.exit(_main(data_dir))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Backfill missing training/holdout evidence inside a qualification snapshot.

Found by the qualification rehearsal: 1,084 effective training targets lack
clip vectors *and* VLM summaries, so the source-isolated profile build gate
(``candidate_vector_count < effective_feedback_count``) blocks every run.
This driver claims a qualification run directory, takes the read-only
production snapshot, and completes the missing evidence **inside the work
snapshot only** (production stays read-only):

1. enumerate targets: effective training candidates (holdout sources
   excluded) and holdout manifest rows that lack a clip vector or a usable
   VLM summary;
2. for each: load a contact sheet from the GIF, ask the local Ollama VLM via
   the existing production enrichment primitives, persist the normalized
   summary (production shape), and upsert the text vector;
3. write the standard ``run_state.json`` (INCOMPLETE) so the qualification
   CLI can resume the same run directory with the completed evidence.

Rows are committed individually with a JSONL checkpoint; re-running skips
rows that already have both summary and vector. Nothing here touches the
production database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.candidate_vectors import upsert_candidate_vector  # noqa: E402
from app.services.preference_schema import apply_preference_schema  # noqa: E402
from app.services.preference_events import load_latest_scoring_events  # noqa: E402
from app.services.curve_preference_qualification import (  # noqa: E402
    _open_read_only,
    logical_source_identity,
    snapshot_source_database,
)

POSITIVE_OR_NEGATIVE = ("like", "dislike", "favorite")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_backfill_targets(
    work_conn, *, model_version: str
) -> tuple[list[str], int]:
    """Candidate ids needing evidence: train-side + holdout-side."""

    conn = work_conn
    conn.row_factory = sqlite3.Row
    model_row = conn.execute(
        "SELECT holdout_manifest_json FROM curve_preference_models "
        "WHERE model_version = :model_version",
        {"model_version": model_version},
    ).fetchone()
    if model_row is None:
        raise RuntimeError(f"model {model_version!r} not in snapshot")
    holdout_manifest = json.loads(model_row["holdout_manifest_json"])
    holdout_sources = set(holdout_manifest.get("sources") or [])
    holdout_rows = holdout_manifest.get("rows") or []

    def has_usable_summary(cid: str) -> bool:
        row = conn.execute(
            "SELECT vlm_summary_json FROM candidate_gifs WHERE candidate_id = :cid",
            {"cid": cid},
        ).fetchone()
        if row is None:
            return False
        try:
            summary = json.loads(row["vlm_summary_json"] or "{}")
        except (TypeError, ValueError):
            return False
        return (
            summary.get("gif_worthiness") is not None
            and summary.get("sex_act") is not None
        )

    def has_clip_vector(cid: str) -> bool:
        row = conn.execute(
            """
            SELECT 1 FROM candidate_vectors
            WHERE candidate_id = :cid AND vector_type = 'clip'
              AND embedding_model = 'nomic-embed-text:latest'
              AND embedding_dim = 768
            """,
            {"cid": cid},
        ).fetchone()
        return row is not None

    effective = load_latest_scoring_events(conn)
    train_targets: list[str] = []
    skipped_no_artifact = 0
    for key, event in effective.items():
        if not key.startswith("candidate_gif:"):
            continue
        if str(event.get("source_video_sha256") or "") in holdout_sources:
            continue
        if str(event.get("rating")) not in POSITIVE_OR_NEGATIVE:
            continue
        candidate_id = key.split(":", 1)[1]
        if has_clip_vector(candidate_id) and has_usable_summary(candidate_id):
            continue
        row = conn.execute(
            "SELECT artifact_path FROM candidate_gifs WHERE candidate_id = :cid",
            {"cid": candidate_id},
        ).fetchone()
        artifact_path = row["artifact_path"] if row else None
        if not artifact_path or not Path(artifact_path).is_file():
            # The GIF is gone; no enrichment is possible. These rows keep
            # the profile gate blocked until a production decision (event
            # undo or re-export) — reported, not silently skipped.
            skipped_no_artifact += 1
            continue
        train_targets.append(candidate_id)

    holdout_targets: list[str] = []
    for row in holdout_rows:
        candidate_id = str(row.get("candidate_id"))
        if has_clip_vector(candidate_id) and has_usable_summary(candidate_id):
            continue
        if not has_usable_summary(candidate_id):
            artifact_row = conn.execute(
                "SELECT artifact_path FROM candidate_gifs WHERE candidate_id = :cid",
                {"cid": candidate_id},
            ).fetchone()
            artifact_path = artifact_row["artifact_path"] if artifact_row else None
            if not artifact_path or not Path(artifact_path).is_file():
                skipped_no_artifact += 1
                continue
        holdout_targets.append(candidate_id)

    print(
        f"skipped (GIF artifact gone; needs production decision): "
        f"{skipped_no_artifact}"
    )
    return train_targets, holdout_targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default="dist/GifAgentUI/data/library.db",
        help="production DB (read-only; snapshot is taken from it)",
    )
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--qualification-id", required=True)
    parser.add_argument(
        "--base-url", default="http://127.0.0.1:11434",
        help="Ollama endpoint for the VLM and the embedding model",
    )
    parser.add_argument(
        "--vlm-model", default="huihui_ai/qwen3-vl-abliterated:8b-instruct-q8_0"
    )
    parser.add_argument("--limit", type=int, default=0, help="process at most N rows")
    parser.add_argument("--frames-per-gif", type=int, default=4)
    parser.add_argument("--max-side", type=int, default=512)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)

    import httpx

    from app.quality_moe.evaluator import _contact_sheet
    from scripts.enrich_library_candidates import (
        _ask_vlm,
        _load_frames,
        _record_from,
    )

    run_dir = args.work_dir / args.qualification_id
    run_dir.mkdir(parents=True, exist_ok=True)
    work_db_path = run_dir / "work.db"

    probe = _open_read_only(Path(args.db))
    try:
        source_identity = logical_source_identity(probe, args.model_version)
    finally:
        probe.close()

    resuming = (run_dir / "run_state.json").is_file()
    if resuming:
        state = json.loads(
            (run_dir / "run_state.json").read_text(encoding="utf-8")
        )
        if state.get("source_logical_identity") != source_identity:
            print(
                "resume refused: source identity changed since snapshot",
                file=sys.stderr,
            )
            return 2
        if not work_db_path.is_file():
            print("resume refused: work.db missing", file=sys.stderr)
            return 2
        print(f"resuming run {args.qualification_id} (state INCOMPLETE)")
    else:
        source_db_snapshot_sha256 = snapshot_source_database(
            Path(args.db), work_db_path
        )
        (run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "kind": "curve-qualification-run-v1",
                    "qualification_id": args.qualification_id,
                    "status": "INCOMPLETE",
                    "model_version": args.model_version,
                    "source_db_snapshot_sha256": source_db_snapshot_sha256,
                    "source_logical_identity": source_identity,
                    "phase": "evidence-backfill",
                    "updated_at": _now(),
                },
                sort_keys=True,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"claimed {run_dir} (snapshot sha {source_db_snapshot_sha256[:16]}...)")

    conn = sqlite3.connect(str(work_db_path), timeout=60)
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)

    train_targets, holdout_targets = build_backfill_targets(
        conn, model_version=args.model_version
    )
    targets: list[tuple[str, str]] = [
        (cid, "train") for cid in train_targets
    ] + [(cid, "holdout") for cid in holdout_targets]
    if args.limit:
        targets = targets[: args.limit]
    print(
        f"backfill targets: train={len(train_targets)} "
        f"holdout={len(holdout_targets)} total={len(targets)}"
    )

    checkpoint_path = run_dir / "backfill_checkpoint.jsonl"
    done: set[str] = set()
    if checkpoint_path.exists():
        for line in checkpoint_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("status") == "ok":
                done.add(str(record["candidate_id"]))
    todo = [(cid, kind) for cid, kind in targets if cid not in done]
    print(f"already done: {len(done)} | todo: {len(todo)}")

    client = httpx.Client()
    sheet_dir = run_dir / "backfill_sheets"
    latencies: list[float] = []
    enriched = failed = 0

    for index, (candidate_id, kind) in enumerate(todo, 1):
        started = time.monotonic()
        status = "ok"
        detail = ""
        try:
            row = conn.execute(
                "SELECT artifact_path FROM candidate_gifs "
                "WHERE candidate_id = :cid",
                {"cid": candidate_id},
            ).fetchone()
            artifact = Path(row["artifact_path"])
            if not artifact.is_file():
                raise FileNotFoundError("artifact missing on disk")
            frames = _load_frames(
                artifact, args.frames_per_gif, args.max_side
            )
            sheet_name = hashlib.sha256(
                candidate_id.encode("utf-8")
            ).hexdigest()[:16]
            sheet = _contact_sheet(
                sheet_dir, f"backfill-{sheet_name}", frames
            )
            try:
                data = _ask_vlm(
                    client, args.base_url, args.vlm_model,
                    sheet, args.timeout,
                )
            finally:
                sheet.unlink(missing_ok=True)
            summary_json, tags_json, scenario_json = _record_from(
                data, args.vlm_model
            )
            conn.execute(
                "UPDATE candidate_gifs SET vlm_summary_json = :summary, "
                "tags_json = :tags, scenario_keys_json = :scenario "
                "WHERE candidate_id = :cid",
                {
                    "summary": summary_json,
                    "tags": tags_json,
                    "scenario": scenario_json,
                    "cid": candidate_id,
                },
            )
            conn.commit()
            if not upsert_candidate_vector(conn, candidate_id=candidate_id):
                raise RuntimeError("vector upsert returned False")
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the batch
            status = "failed"
            failed += 1
            detail = f"{type(exc).__name__}: {str(exc)[:120]}"
            conn.rollback()
        else:
            enriched += 1
        secs = time.monotonic() - started
        latencies.append(secs)
        with checkpoint_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "ts": _now(),
                        "candidate_id": candidate_id,
                        "kind": kind,
                        "status": status,
                        "secs": round(secs, 1),
                        "detail": detail,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        if index % 10 == 0 or index == len(todo):
            avg = sum(latencies) / len(latencies)
            remaining = (len(todo) - index) * avg
            print(
                f"[{index}/{len(todo)}] enriched={enriched} failed={failed} "
                f"avg={avg:.1f}s eta={remaining / 3600:.2f}h",
                flush=True,
            )

    conn.close()
    print(f"BACKFILL DONE enriched={enriched} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

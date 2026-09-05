#!/usr/bin/env python3
"""Qualify a DRAFT curve preference model against a production-semantics baseline.

Replays the current production ranking algorithm (curve off) and the
curve-aware variant (nominal and confidence-scaled) over the frozen holdout
population of one DRAFT model, inside a consistent work snapshot of the
production database. The production database is opened read-only; every
write lands in the claimed run directory.

Usage
-----
    python scripts/qualify_curve_preference_model.py \
        --db dist/GifAgentUI/data/library.db \
        --model-version curve-pref-v1-20260905120400 \
        --work-dir data/curve_preference_qualification \
        --dry-run

    python scripts/qualify_curve_preference_model.py \
        --db dist/GifAgentUI/data/library.db \
        --model-version curve-pref-v1-20260905120400 \
        --work-dir data/curve_preference_qualification

A ``--limit`` run is a diagnostic rehearsal that can never emit QUALIFIED.
Promotion is a separate, explicit command that requires a QUALIFIED run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.curve_preference_qualification import (  # noqa: E402
    execute_qualification_run,
)


def _make_enrich_fn(
    sheet_dir: Path, base_url: str, model: str
) -> "callable[[str], str]":
    """Reuse the production enrichment primitives for missing VLM evidence."""

    import httpx

    from app.quality_moe.evaluator import _contact_sheet
    from scripts.enrich_library_candidates import _ask_vlm, _load_frames

    client = httpx.Client()
    sheet_dir.mkdir(parents=True, exist_ok=True)

    def enrich(artifact_path: str) -> str:
        import json as json_module

        frames = _load_frames(Path(artifact_path), 4, 512)
        sheet_name = hashlib.sha256(
            artifact_path.encode("utf-8")
        ).hexdigest()[:16]
        sheet = _contact_sheet(sheet_dir, f"qual-{sheet_name}", frames)
        try:
            data = _ask_vlm(client, base_url, model, sheet, 300.0)
        finally:
            sheet.unlink(missing_ok=True)
        return json_module.dumps(data, ensure_ascii=False)

    return enrich


def _make_vector_fn():
    """Vector generation uses the production upsert path inside work.db."""

    from app.services.candidate_vectors import upsert_candidate_vector

    def generate(work_conn, candidate_id: str) -> bool:
        try:
            return bool(
                upsert_candidate_vector(work_conn, candidate_id=candidate_id)
            )
        except Exception:
            return False

    return generate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default="dist/GifAgentUI/data/library.db", help="production DB (read-only)"
    )
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--work-dir", default="data/curve_preference_qualification")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="validate and report; no snapshot, no VLM, no writes",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="diagnostic rehearsal cap; the run can never emit QUALIFIED",
    )
    parser.add_argument(
        "--base-url", default="http://127.0.0.1:11434",
        help="Ollama endpoint used only for missing VLM evidence",
    )
    parser.add_argument(
        "--model", default="huihui_ai/qwen3-vl-abliterated:8b-instruct-q8_0",
        help="VLM model used only for missing VLM evidence",
    )
    args = parser.parse_args(argv)

    enrich_fn = None
    if not args.dry_run and not args.limit:
        # Real runs may enrich; rehearsals (limit) exercise the plumbing with
        # the same code path but cannot qualify anyway.
        enrich_fn = _make_enrich_fn(
            Path(args.work_dir) / "sheets", args.base_url, args.model
        )
    vector_fn = _make_vector_fn() if not args.dry_run else None

    summary = execute_qualification_run(
        db_path=Path(args.db),
        model_version=args.model_version,
        work_dir=Path(args.work_dir),
        enrich_fn=enrich_fn,
        vector_fn=vector_fn,
        dry_run=args.dry_run,
        limit=args.limit,
        qualification_id=(
            "rehearsal-"
            + uuid.uuid4().hex[:8]
            if args.limit
            else None
        ),
    )

    print(
        json.dumps(
            summary,
            sort_keys=True,
            allow_nan=False,
            indent=1,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Batch VLM enrichment for folder-import candidate rows with empty text.

Fills ``candidate_gifs.vlm_summary_json / tags_json / scenario_keys_json``
for rows that folder-import registered with empty values.  For each row the
artifact GIF is sampled, a contact sheet is built, and the local Ollama VLM
(the same model the adaptive pipeline uses) returns one structured JSON
record.  Rows are committed individually and logged to a checkpoint JSONL so
an interrupted run resumes where it stopped.

The GIF bytes only travel from this process to the local Ollama endpoint;
they are never printed, logged, or hashed.

Usage
-----
    python scripts/enrich_library_candidates.py --dry-run
    python scripts/enrich_library_candidates.py --limit 3
    python scripts/enrich_library_candidates.py            # full run
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402
import httpx  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageSequence  # noqa: E402

from app.quality_moe.evaluator import _contact_sheet  # noqa: E402
from app.services.scenario import normalize_scenario_keys  # noqa: E402

DEFAULT_DB = "dist/GifAgentUI/data/library.db"
DEFAULT_BASE_URL = os.environ.get("GIFAGENT_OLLAMA_BASE", "http://127.0.0.1:11434")
DEFAULT_MODEL = "huihui_ai/qwen3-vl-abliterated:8b-instruct-q8_0"
DEFAULT_WORK_DIR = "data/token_sprint/enrichment_v1"

_EMOTIONS = (
    "tension", "melancholy", "awe", "joy", "sadness", "catharsis", "serenity",
    "excitement", "dread", "nostalgia", "admiration", "intimacy",
    "vulnerability", "longing", "desire", "other",
)

ENRICH_PROMPT = (
    "You are annotating one short adult GIF for a personal media library. "
    "The image is a contact sheet of frames sampled in chronological order from "
    "one GIF. Be literal; do not euphemize and do not refuse sex. Output ONLY "
    "one valid JSON object, no markdown:\n"
    '{"caption":"2-3 sentences describing the visible people, bodies, acts, '
    'clothing, setting, lighting, and camera","emotional_core":"ONE lowercase '
    'word from the provided emotion list","tags":["3-8 lowercase descriptive '
    'keywords"],"scene_type":"short snake_case scene label like '
    'bedroom_closeup or outdoor_night","sex_act":12,"gif_worthiness":47,'
    '"reason":"one sentence on motion quality and GIF potential"}\n\n'
    "emotional_core must be exactly one of: "
    + "|".join(_EMOTIONS)
    + "\n"
    + "sex_act is 0-100 explicitness of THIS gif (0 none, 50 clear sexual "
    "contact, 100 peak explicit motion). gif_worthiness is 0-100 how good this "
    "gif is as a short loop (0 static/boring, 100 peak readable motion).\n"
    "Tags describe body features, acts, clothing, setting, and style that "
    "would help text search. Never output anything outside the JSON object."
)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_REFUSAL_RE = re.compile(
    r"(?:i\s*(?:cannot|can't|unable to))|(?:(?:无法|不能).{0,20}(?:协助|帮助|评估|提供))",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_frames(path: Path, max_frames: int, max_side: int) -> list[np.ndarray]:
    with Image.open(path) as img:
        n = getattr(img, "n_frames", 1)
        picks = sorted({round(i * (n - 1) / max(1, max_frames - 1)) for i in range(max_frames)})
        frames = []
        for idx in picks:
            img.seek(idx)
            frame = img.convert("RGB")
            scale = max_side / max(frame.size)
            if scale < 1:
                frame = frame.resize(
                    (round(frame.size[0] * scale), round(frame.size[1] * scale))
                )
            frames.append(np.asarray(frame)[:, :, ::-1].copy())  # RGB -> BGR
    return frames


def _ask_vlm(client: httpx.Client, base_url: str, model: str, sheet: Path, timeout: float) -> dict:
    payload = {
        "model": model,
        "prompt": ENRICH_PROMPT,
        "images": [base64.b64encode(sheet.read_bytes()).decode("ascii")],
        "stream": False,
        "options": {"temperature": 0, "num_predict": 512},
    }
    last_err = "no attempt"
    for _attempt in range(2):
        try:
            resp = client.post(f"{base_url}/api/generate", json=payload, timeout=timeout)
            resp.raise_for_status()
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_err = f"ollama request failed: {exc}"
            continue
        text = (resp.json() or {}).get("response") or ""
        if _REFUSAL_RE.search(text):
            last_err = "model refused"
            continue
        match = _JSON_OBJECT_RE.search(text)
        if not match:
            last_err = "no JSON object in response"
            continue
        try:
            data = json.loads(match.group(0))
        except ValueError:
            last_err = "JSON parse failed"
            continue
        if not isinstance(data, dict) or not str(data.get("caption") or "").strip():
            last_err = "missing caption"
            continue
        return data
    raise RuntimeError(last_err)


def _record_from(data: dict, model: str) -> tuple[str, str, str]:
    emotion = str(data.get("emotional_core") or "").strip().lower()
    if emotion not in _EMOTIONS:
        emotion = "other"
    tags_raw = data.get("tags") or []
    if isinstance(tags_raw, str):
        tags_raw = [t.strip() for t in tags_raw.split(",")]
    tags: list[str] = []
    for tag in tags_raw:
        clean = re.sub(r"\s+", " ", str(tag).strip().lower())
        if clean and clean not in tags:
            tags.append(clean)
    tags = tags[:8]
    scene = re.sub(r"[^a-z0-9_]+", "_", str(data.get("scene_type") or "").strip().lower()).strip("_")
    scenario = normalize_scenario_keys(emotion=emotion, scene_type=scene or None, tags=tags)
    summary = {
        "caption": str(data.get("caption") or "").strip(),
        "emotional_core": emotion,
        "scene_type": scene or None,
        "sex_act": int(data.get("sex_act") or 0),
        "gif_worthiness": int(data.get("gif_worthiness") or 0),
        "reason": str(data.get("reason") or "").strip(),
        "enriched_by": model,
        "enriched_at": _now(),
    }
    return (
        json.dumps(summary, ensure_ascii=False),
        json.dumps(tags, ensure_ascii=False),
        json.dumps(scenario, ensure_ascii=False),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--work-dir", default=DEFAULT_WORK_DIR)
    parser.add_argument("--limit", type=int, default=0, help="process at most N rows (0 = all)")
    parser.add_argument("--frames-per-gif", type=int, default=4)
    parser.add_argument("--max-side", type=int, default=512, help="per-frame long-edge cap in px")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--dry-run", action="store_true", help="validate rows only; no VLM, no writes")
    args = parser.parse_args(argv)

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = work_dir / "checkpoint.jsonl"

    import sqlite3

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA busy_timeout=5000")
    rows = conn.execute(
        "SELECT rowid, candidate_id, artifact_path FROM candidate_gifs "
        "WHERE (vlm_summary_json IN ('{}', '') OR vlm_summary_json IS NULL) "
        "AND artifact_path IS NOT NULL AND artifact_path != '' "
        "ORDER BY rowid"
    ).fetchall()
    done: set[int] = set()
    if checkpoint.exists():
        for line in checkpoint.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("status") == "enriched":
                done.add(int(rec["rowid"]))
    todo = [(rid, cid, p) for rid, cid, p in rows if rid not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"rows empty={len(rows)} already_done={len(done)} todo={len(todo)}")

    if args.dry_run:
        bad = 0
        for rid, _cid, path in todo[:50]:
            try:
                _load_frames(Path(path), args.frames_per_gif, args.max_side)
            except Exception as exc:  # noqa: BLE001 - report every bad artifact
                bad += 1
                print(f"BAD rowid={rid} {type(exc).__name__}: {str(exc)[:80]} :: {path[:70]}")
        print(f"dry-run: sampled {min(50, len(todo))} rows, unreadable={bad}")
        conn.close()
        return 0

    with httpx.Client() as client:
        try:
            client.get(f"{args.base_url}/api/tags", timeout=10.0).raise_for_status()
        except httpx.HTTPError as exc:
            print(f"error: Ollama not reachable at {args.base_url}: {exc}", file=sys.stderr)
            conn.close()
            return 2

        latencies: list[float] = []
        enriched = skipped = 0
        sheet_dir = work_dir / "sheets"
        for i, (rid, cid, path) in enumerate(todo, 1):
            t0 = time.monotonic()
            status = "enriched"
            detail = ""
            try:
                artifact = Path(path)
                if not artifact.is_file():
                    raise FileNotFoundError("artifact missing on disk")
                frames = _load_frames(artifact, args.frames_per_gif, args.max_side)
                sheet = _contact_sheet(sheet_dir, f"row{rid}", frames)
                data = _ask_vlm(client, args.base_url, args.model, sheet, args.timeout)
                summary_json, tags_json, scenario_json = _record_from(data, args.model)
                conn.execute(
                    "UPDATE candidate_gifs SET vlm_summary_json = ?, "
                    "tags_json = ?, scenario_keys_json = ?, updated_at = ? "
                    "WHERE rowid = ?",
                    (summary_json, tags_json, scenario_json, _now(), rid),
                )
                conn.commit()
                sheet.unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the batch
                status = "skipped"
                detail = f"{type(exc).__name__}: {str(exc)[:120]}"
                conn.rollback()
            secs = time.monotonic() - t0
            latencies.append(secs)
            if status == "enriched":
                enriched += 1
            else:
                skipped += 1
            with checkpoint.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": _now(), "rowid": rid, "candidate_id": cid,
                                     "status": status, "secs": round(secs, 1),
                                     "detail": detail}, ensure_ascii=False) + "\n")
            if i % 25 == 0 or i == len(todo):
                avg = sum(latencies) / len(latencies)
                remaining = (len(todo) - i) * avg
                print(f"[{i}/{len(todo)}] enriched={enriched} skipped={skipped} "
                      f"avg={avg:.1f}s eta={remaining / 3600:.1f}h", flush=True)
    conn.close()
    print(f"DONE enriched={enriched} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Build stratified text-only shards from the ingested export-corpus JSONs.

W1 export-corpus source (``--source export-corpus`` alternative to
``make_shards.py`` reading the empty production snapshot text columns). Input is
the immutable copy tree produced by ``ingest_export_corpus.py``:
``<sprint-root>/inputs/exports/**/*result.json``. Every consumed clip must have
a non-empty ``caption`` and an ``export_path`` whose GIF still exists on disk
(``Path.exists()`` boolean only -- GIF/JPG bytes are never read).

Output rows are text-only, fields aligned with the W1 probe input schema::

    {"candidate_id": "exp_<sha256(resolved export_path)[:12]>",
     "vlm_summary_json": "{\\"caption\\": ..., \\"emotional_core\\": ..., \\"reason\\": ...}",
     "tags_json": "[...]", "scenario_keys_json": "[...]",
     "final_score": 0.87, "stratum": "high|mid|low",
     "source_result_json": "inputs/exports/...",
     "export_path": "C:/.../foo.gif", "gif_exists": true}

``export_path``/``gif_exists`` are provenance only: the P3 prompt reads the
summary/tags/scenario text fields, never the path, and no media is opened.

Stratification reuses ``make_shards.classify_strata`` (``EXPLICIT_MARKERS``
word-start match over summary/tags/scenario text; ``final_score`` tertile for
the remaining rows). ``--probe`` writes ``shards/shard-probe-export.input.jsonl``
with up to 20 rows per layer (60 total), filling deficits from adjacent layers
with a stderr notice. The failing-baseline ``shard-probe.input.jsonl`` is never
touched; existing outputs are never overwritten without ``--force``.

Relative ``export_path`` values are resolved against the main-checkout repo root
(default: ``<sprint-root>/../../``, i.e. the shared root lives at
``<repo>/data/token_sprint``); override with ``--repo-root``.

Usage:
    python scripts/token_sprint/make_export_shards.py --sprint-root <path> \
        [--probe] [--size 100] [--out-prefix shard-export] [--force]

Exit codes: 0 success, 2 fatal input/argument error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from scripts.token_sprint.coordinator.common import (  # noqa: E402
    add_sprint_root_argument,
    atomic_write_text,
    resolve_sprint_root,
)
from scripts.token_sprint.make_shards import (  # noqa: E402
    MAX_SHARD_SIZE,
    MIN_SHARD_SIZE,
    build_probe_rows,
    classify_strata,
)

CORPUS_DIR_REL = "inputs/exports"
SHARDS_DIRNAME = "shards"
PROBE_OUT_NAME = "shard-probe-export.input.jsonl"
LAYERS = ("high", "mid", "low")

#: Locked output fields for the export-corpus probe input.
EXPORT_CORPUS_FIELDS = (
    "candidate_id",
    "vlm_summary_json",
    "tags_json",
    "scenario_keys_json",
    "final_score",
    "stratum",
    "source_result_json",
    "export_path",
    "gif_exists",
)


class ExportShardError(RuntimeError):
    """Fatal export-corpus shard problem; surfaces as exit code 2."""


def _valid_score(value) -> float | None:
    """A float in [0, 1]; anything else (incl. bool) is not a score."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if 0.0 <= value <= 1.0 else None


def _candidate_id(resolved_export_path: Path) -> str:
    digest = hashlib.sha256(resolved_export_path.as_posix().encode("utf-8")).hexdigest()
    return f"exp_{digest[:12]}"


def _resolve_export_path(raw: str, repo_root: Path) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path.resolve()
    return (repo_root / path).resolve()


def _string_list(*chunks) -> str:
    """Lowercase, strip, dedupe (order-preserving) string list -> JSON text."""
    seen: set[str] = set()
    items: list[str] = []
    for chunk in chunks:
        if isinstance(chunk, str):
            chunk = [chunk]
        if not isinstance(chunk, list):
            continue
        for item in chunk:
            if not isinstance(item, str):
                continue
            token = item.strip().lower()
            if token and token not in seen:
                seen.add(token)
                items.append(token)
    return json.dumps(items, ensure_ascii=False)


def load_corpus_rows(sprint_root: Path, repo_root: Path) -> tuple[list[dict], dict]:
    """Load text rows from the ingested corpus; return (rows, stats)."""
    corpus_dir = sprint_root / CORPUS_DIR_REL
    if not corpus_dir.is_dir():
        raise ExportShardError(
            f"语料目录不存在: {corpus_dir}（请先运行 ingest_export_corpus.py）"
        )

    stats = {
        "result_json_files": 0,
        "parse_errors": [],
        "clips_seen": 0,
        "no_export_path": 0,
        "caption_empty": 0,
        "gif_missing": 0,
        "duplicate_path": 0,
        "pool": 0,
    }

    json_files = sorted(corpus_dir.rglob("*.json"), key=lambda p: p.as_posix())
    rows: list[dict] = []
    seen_ids: dict[str, dict] = {}

    for json_file in json_files:
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            stats["parse_errors"].append(f"{json_file.relative_to(sprint_root).as_posix()}: {exc}")
            continue
        if not isinstance(data, dict):
            stats["parse_errors"].append(
                f"{json_file.relative_to(sprint_root).as_posix()}: top level is not an object"
            )
            continue
        stats["result_json_files"] += 1
        synthesis = data.get("synthesis") if isinstance(data.get("synthesis"), dict) else {}
        source_rel = (Path(CORPUS_DIR_REL) / json_file.relative_to(corpus_dir)).as_posix()
        clips = data.get("top_clips")
        if not isinstance(clips, list):
            continue
        for clip in clips:
            if not isinstance(clip, dict):
                continue
            stats["clips_seen"] += 1
            export_path_raw = clip.get("export_path")
            if not isinstance(export_path_raw, str) or not export_path_raw.strip():
                stats["no_export_path"] += 1
                continue
            resolved = _resolve_export_path(export_path_raw, repo_root)
            caption = clip.get("caption")
            caption = caption if isinstance(caption, str) else ""
            if not caption.strip():
                stats["caption_empty"] += 1
                continue
            gif_exists = resolved.is_file()
            if not gif_exists:
                stats["gif_missing"] += 1
                continue

            score = _valid_score(clip.get("final_score"))
            if score is None:
                score = _valid_score(clip.get("gif_worthiness"))

            summary = json.dumps(
                {
                    "caption": caption,
                    "emotional_core": clip.get("emotional_core")
                    if isinstance(clip.get("emotional_core"), str)
                    else "",
                    "reason": clip.get("reason") if isinstance(clip.get("reason"), str) else "",
                },
                ensure_ascii=False,
            )
            tags = _string_list(clip.get("aesthetic_notes"), synthesis.get("tags"))
            scenario = _string_list(
                clip.get("emotional_core") if isinstance(clip.get("emotional_core"), str) else "",
                synthesis.get("scene_type"),
            )

            rank = clip.get("rank")
            row = {
                "candidate_id": _candidate_id(resolved),
                "vlm_summary_json": summary,
                "tags_json": tags,
                "scenario_keys_json": scenario,
                "final_score": score,
                "stratum": None,  # filled by classify_strata
                "source_result_json": source_rel,
                "export_path": resolved.as_posix(),
                "gif_exists": gif_exists,
                "_rank": rank if isinstance(rank, (int, float)) and not isinstance(rank, bool) else -1,
            }
            existing = seen_ids.get(row["candidate_id"])
            if existing is not None:
                stats["duplicate_path"] += 1
                # deterministic winner: earlier source file, then smaller rank
                if (source_rel, row["_rank"]) >= (existing["source_result_json"], existing["_rank"]):
                    continue
                rows.remove(existing)
            seen_ids[row["candidate_id"]] = row
            rows.append(row)

    if stats["parse_errors"]:
        raise ExportShardError(
            "语料 JSON 解析失败（不做静默跳过）:\n  " + "\n  ".join(stats["parse_errors"])
        )

    rows.sort(key=lambda r: (r["source_result_json"], r["_rank"], r["candidate_id"]))
    for row in rows:
        row.pop("_rank", None)
    stats["pool"] = len(rows)
    return rows, stats


def _export_payload(row: dict) -> dict:
    return {field: row.get(field) for field in EXPORT_CORPUS_FIELDS}


def _dump_line(row: dict) -> str:
    return json.dumps(_export_payload(row), ensure_ascii=False, sort_keys=True)


def _ensure_no_overwrite(paths: list[Path], *, force: bool) -> None:
    if force:
        return
    collisions = [str(path) for path in paths if path.exists()]
    if collisions:
        raise ExportShardError(
            "已存在同名产物，拒绝覆盖（旧探针产物必须保留；换名或加 --force）: "
            + ", ".join(collisions)
        )


def write_probe(sprint_root: Path, rows: list[dict], *, force: bool) -> tuple[Path, list[str]]:
    """Write the 60-row T0 export-corpus probe; reuse make_shards probe logic."""
    probe_rows, notes = build_probe_rows(rows)
    target = sprint_root / SHARDS_DIRNAME / PROBE_OUT_NAME
    _ensure_no_overwrite([target], force=force)
    body = "".join(_dump_line(row) + "\n" for row in probe_rows)
    atomic_write_text(target, body)
    total = len(probe_rows)
    if total < 3 * 20:
        notes.append(f"note: probe total {total} < 60 (probeable pool only has {len(rows)} rows)")
    return target, notes


def write_regular(
    sprint_root: Path,
    rows: list[dict],
    *,
    size: int,
    out_prefix: str,
    force: bool,
) -> list[Path]:
    """Round-robin across layers into 50-200 row shards (Stage A/B inputs)."""
    if not (MIN_SHARD_SIZE <= size <= MAX_SHARD_SIZE):
        raise ExportShardError(
            f"--size 必须在 {MIN_SHARD_SIZE}-{MAX_SHARD_SIZE} 之间，当前为 {size}"
        )
    buckets = {layer: [row for row in rows if row["stratum"] == layer] for layer in LAYERS}
    iterators = {layer: iter(bucket) for layer, bucket in buckets.items()}
    active = [layer for layer in LAYERS if buckets[layer]]
    interleaved: list[dict] = []
    while active:
        for layer in list(active):
            try:
                interleaved.append(next(iterators[layer]))
            except StopIteration:
                active.remove(layer)

    n_shards = math.ceil(len(interleaved) / size) if interleaved else 0
    targets = [
        sprint_root / SHARDS_DIRNAME / f"{out_prefix}-{i + 1:03d}.input.jsonl"
        for i in range(n_shards)
    ]
    _ensure_no_overwrite(targets, force=force)
    for index, target in enumerate(targets):
        chunk = interleaved[index * size : (index + 1) * size]
        atomic_write_text(target, "".join(_dump_line(row) + "\n" for row in chunk))
    return targets


def make_export_shards(
    sprint_root: Path,
    *,
    probe: bool = False,
    size: int = 100,
    out_prefix: str = "shard-export",
    force: bool = False,
    repo_root: Path | None = None,
) -> dict:
    repo_root = repo_root if repo_root is not None else sprint_root.parent.parent
    rows, stats = load_corpus_rows(sprint_root, repo_root)
    strata_counts = classify_strata(rows)
    if probe:
        target, notes = write_probe(sprint_root, rows, force=force)
        written = [target]
    else:
        written = write_regular(
            sprint_root, rows, size=size, out_prefix=out_prefix, force=force
        )
        notes = []
    return {"paths": written, "stats": stats, "counts": strata_counts, "notes": notes}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build stratified text-only shards from the ingested export corpus."
    )
    add_sprint_root_argument(parser)
    parser.add_argument("--probe", action="store_true", help="write the 60-row T0 probe")
    parser.add_argument(
        "--size", type=int, default=100, help=f"rows per shard ({MIN_SHARD_SIZE}-{MAX_SHARD_SIZE})"
    )
    parser.add_argument("--out-prefix", default="shard-export", help="regular-mode shard prefix")
    parser.add_argument("--force", action="store_true", help="overwrite existing outputs")
    parser.add_argument(
        "--repo-root",
        default=None,
        help="repo root for resolving relative export_path values "
        "(default: <sprint-root>/../../ main checkout)",
    )
    args = parser.parse_args(argv)
    sprint_root = resolve_sprint_root(args.sprint_root)

    try:
        summary = make_export_shards(
            sprint_root,
            probe=args.probe,
            size=args.size,
            out_prefix=args.out_prefix,
            force=args.force,
            repo_root=Path(args.repo_root).resolve() if args.repo_root else None,
        )
    except ExportShardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    stats = summary["stats"]
    for note in summary["notes"]:
        print(note, file=sys.stderr)
    print(
        f"语料分片完成: result_json={stats['result_json_files']} clips_seen={stats['clips_seen']} "
        f"caption_empty={stats['caption_empty']} gif_missing={stats['gif_missing']} "
        f"duplicate_path={stats['duplicate_path']} pool={stats['pool']} "
        f"分层={summary['counts']}"
    )
    for path in summary["paths"]:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

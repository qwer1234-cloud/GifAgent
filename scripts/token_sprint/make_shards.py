#!/usr/bin/env python3
"""Export stratified text-only shards (and the 60-row T0 probe) from the snapshot.

W1 deterministic tool (zero model tokens). Reads the W1 snapshot
``<sprint-root>/library_snapshot.db`` (created by ``snapshot_library.py``) and
writes JSONL input shards into ``<sprint-root>/shards/``.

Exported fields are text-only -- never media bytes and never path fields
(``artifact_path`` / ``preview_path`` / ``source_video_path`` are not written)::

    {"candidate_id": "...", "vlm_summary_json": "...", "tags_json": "...",
     "scenario_keys_json": "...", "final_score": 0.0, "stratum": "high|mid|low"}

Stratification (shared by probe and trial sets):
- ``high``: tags/summary/scenario *content text* contains a marker from the
  built-in lowercase English constant ``EXPLICIT_MARKERS`` (a code constant,
  not model output). Content text means the JSON string values of the payloads
  -- a numeric metadata field NAME such as ``"sex_act": 45`` in the enriched
  v2 snapshot must not force a clean drama row into the high layer (see
  ``_summary_content_text``);
- ``mid``: no marker and ``final_score`` is in the top tertile (>= q2/3) of
  the snapshot's non-null scores;
- ``low``: everything else (including NULL scores).

``--probe`` writes ``{prefix}-probe.input.jsonl`` with up to 20 rows per layer
(60 total); shortfalls are filled from adjacent layers with a stderr notice and
are never silently reduced. Regular mode round-robins across layers and chunks
into 50-200 rows per shard named ``{prefix}-XXX.input.jsonl`` (XXX zero-padded
to 3 digits). Existing input shards are never overwritten without ``--force``.

``--pool-filter caption+gif`` (W1 T0 rerun on the enriched v2 snapshot) restricts
the exportable pool to rows where the ``vlm_summary_json`` payload has a
non-empty ``caption`` string AND the row's ``artifact_path`` still exists on
disk. The GIF check is a ``Path.is_file()`` boolean only -- media bytes are
never opened, and ``artifact_path`` is used for the existence test but is never
written to any shard (path fields stay excluded from the export schema).
Without the flag the behaviour is unchanged (all snapshot rows are exportable).

Usage:
    python scripts/token_sprint/make_shards.py --sprint-root <path> \
        [--probe] [--size 100] [--out-prefix shard] [--force] \
        [--pool-filter caption+gif]

Exit codes: 0 success, 2 fatal input/argument error.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
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

SNAPSHOT_NAME = "library_snapshot.db"
SHARDS_DIRNAME = "shards"
LAYERS = ("high", "mid", "low")
PROBE_TARGET_PER_LAYER = 20
MIN_SHARD_SIZE = 50
MAX_SHARD_SIZE = 200
#: Only supported value for ``--pool-filter`` (v2-snapshot T0 rerun).
POOL_FILTER_CAPTION_GIF = "caption+gif"
# Adjacent-layer fill order for probe deficits.
_NEIGHBOR_LAYERS = {
    "high": ("mid", "low"),
    "mid": ("high", "low"),
    "low": ("mid", "high"),
}

#: Built-in lowercase English explicit markers (code constant, not model
#: output). Matched case-insensitively as word-start prefixes, so stems such as
#: ``nude``/``fuck``/``sex`` also catch inflections (``nudes``, ``fucking``,
#: ``sexy``). Deliberately excludes short stems that would false-positive on
#: ordinary words (e.g. ``ass`` in "assist", ``cum`` in "document").
EXPLICIT_MARKERS: tuple[str, ...] = (
    "anal",
    "bdsm",
    "blowjob",
    "bondage",
    "cumshot",
    "erotic",
    "fetish",
    "fuck",
    "genital",
    "handjob",
    "hardcore",
    "intercourse",
    "lingerie",
    "masturb",
    "naked",
    "nsfw",
    "nude",
    "nipple",
    "orgasm",
    "penetrat",
    "penis",
    "porn",
    "pussy",
    "sex",
    "softcore",
    "topless",
    "vagina",
    "vulva",
)

_MARKER_RES = tuple(
    re.compile(rf"\b{re.escape(marker)}") for marker in EXPLICIT_MARKERS
)

EXPORT_FIELDS = (
    "candidate_id",
    "vlm_summary_json",
    "tags_json",
    "scenario_keys_json",
    "final_score",
    "stratum",
)


class ShardExportError(RuntimeError):
    """Fatal shard-export problem; surfaces as exit code 2."""


def text_has_explicit_marker(text: str | None) -> bool:
    """Case-insensitive word-start prefix match against ``EXPLICIT_MARKERS``."""
    if not text:
        return False
    lowered = text.lower()
    return any(pattern.search(lowered) for pattern in _MARKER_RES)


def _summary_content_text(vlm_summary_json: str | None) -> str:
    """Content text of a ``vlm_summary_json`` payload: its JSON string values.

    W1 T0 v2 rerun fix: matching markers over the raw serialized JSON let the
    numeric metadata field NAME ``"sex_act"`` (written by the enrichment
    pipeline on every row) satisfy the ``\\bsex`` word-start match, forcing
    100% of the v2 pool into the ``high`` layer and degenerating the probe
    stratification. Only string values are content; JSON keys and numbers are
    not. Malformed or non-object payloads fall back to the raw serialized text
    -- conservative, since a raw blob can only add matches, never hide them.
    """
    if not vlm_summary_json:
        return ""
    try:
        data = json.loads(vlm_summary_json)
    except json.JSONDecodeError:
        return vlm_summary_json
    if isinstance(data, dict):
        return "\n".join(v for v in data.values() if isinstance(v, str))
    return vlm_summary_json


def row_content_text(row: sqlite3.Row | dict) -> str:
    """Concatenated content text of one row for explicit-marker matching.

    ``vlm_summary_json`` contributes its string values only (see
    ``_summary_content_text``); ``tags_json`` / ``scenario_keys_json`` are
    arrays of content strings, so their serialized text equals their content.
    Shared with ``validate_shard`` so the fidelity denominator and the
    stratification always agree.
    """
    parts = [_summary_content_text(_row_field(row, "vlm_summary_json"))]
    for field in ("tags_json", "scenario_keys_json"):
        parts.append(str(_row_field(row, field) or ""))
    return "\n".join(parts)


def _row_field(row, name: str):
    """Field access working for both dicts and ``sqlite3.Row`` (missing -> None)."""
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def _row_is_explicit(row: sqlite3.Row | dict) -> bool:
    return text_has_explicit_marker(row_content_text(row))


def _tertiles(scores: list[float | None]) -> tuple[float | None, float | None]:
    """Return (q1/3, q2/3) over the non-null scores (deterministic nearest index)."""
    clean = sorted(s for s in scores if s is not None)
    if not clean:
        return None, None

    def q(p: float) -> float:
        return clean[min(len(clean) - 1, int(p * len(clean)))]

    return q(1.0 / 3.0), q(2.0 / 3.0)


def load_rows(snapshot_path: Path) -> list[dict]:
    """Load exportable rows from the snapshot ordered by (created_at, candidate_id).

    ``artifact_path`` is selected for the optional gif-existence pool filter
    only; it never enters the text-only export payload (see ``EXPORT_FIELDS``).
    """
    if not snapshot_path.is_file():
        raise ShardExportError(f"快照不存在: {snapshot_path}（请先运行 snapshot_library.py）")
    conn = sqlite3.connect(f"file:{snapshot_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT candidate_id, vlm_summary_json, tags_json,
                      scenario_keys_json, final_score, artifact_path
               FROM candidate_gifs
               ORDER BY created_at ASC, candidate_id ASC"""
        ).fetchall()
    except sqlite3.Error as exc:
        raise ShardExportError(f"快照库不可读: {snapshot_path} ({exc})") from exc
    finally:
        conn.close()
    return [dict(row) for row in rows]


def caption_from_summary(vlm_summary_json: str | None) -> str:
    """Extract the caption text from a ``vlm_summary_json`` payload ('' when absent).

    Fail-closed: malformed JSON, non-object payloads, or non-string captions
    all yield an empty caption.
    """
    if not vlm_summary_json:
        return ""
    try:
        data = json.loads(vlm_summary_json)
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    caption = data.get("caption")
    return caption if isinstance(caption, str) else ""


def filter_pool(
    rows: list[dict], *, require_caption_gif: bool
) -> tuple[list[dict], dict]:
    """Optionally restrict rows to caption-nonempty AND gif-artifact-exists.

    The GIF check is a ``Path.is_file()`` boolean on ``artifact_path`` -- media
    bytes are never opened. Returns ``(kept_rows, stats)``; stats is empty when
    the filter is off. Counting is sequential (caption first, then gif among
    caption-ok rows), matching the ``make_export_shards`` stats semantics.
    """
    if not require_caption_gif:
        return rows, {}
    stats = {
        "rows_total": len(rows),
        "caption_empty": 0,
        "gif_missing": 0,
        "pool": 0,
    }
    kept: list[dict] = []
    for row in rows:
        if not caption_from_summary(row.get("vlm_summary_json")).strip():
            stats["caption_empty"] += 1
            continue
        artifact_path = row.get("artifact_path")
        if not (
            isinstance(artifact_path, str)
            and artifact_path.strip()
            and Path(artifact_path).is_file()
        ):
            stats["gif_missing"] += 1
            continue
        kept.append(row)
    stats["pool"] = len(kept)
    return kept, stats


def classify_strata(rows: list[dict]) -> dict[str, int]:
    """Assign ``stratum`` in place; return per-layer counts."""
    _, q66 = _tertiles([row.get("final_score") for row in rows])
    counts = {layer: 0 for layer in LAYERS}
    for row in rows:
        if _row_is_explicit(row):
            stratum = "high"
        elif row.get("final_score") is not None and q66 is not None and row["final_score"] >= q66:
            stratum = "mid"
        else:
            stratum = "low"
        row["stratum"] = stratum
        counts[stratum] += 1
    return counts


def _export_payload(row: dict) -> dict:
    """Project a snapshot row onto the locked text-only export fields."""
    return {field: row.get(field) for field in EXPORT_FIELDS}


def _dump_line(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _shard_path(sprint_root: Path, name: str) -> Path:
    return sprint_root / SHARDS_DIRNAME / name


def _ensure_no_overwrite(paths: list[Path], *, force: bool) -> None:
    if force:
        return
    collisions = [str(path) for path in paths if path.exists()]
    if collisions:
        raise ShardExportError(
            "已存在同名 input 片，拒绝覆盖（换 --out-prefix 或加 --force）: "
            + ", ".join(collisions)
        )


def build_probe_rows(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Pick up to 20 rows per layer, filling deficits from adjacent layers."""
    buckets = {layer: [row for row in rows if row["stratum"] == layer] for layer in LAYERS}
    picked = {layer: list(buckets[layer][:PROBE_TARGET_PER_LAYER]) for layer in LAYERS}
    leftover = {layer: list(buckets[layer][PROBE_TARGET_PER_LAYER:]) for layer in LAYERS}

    notes: list[str] = []
    for layer in LAYERS:
        deficit = PROBE_TARGET_PER_LAYER - len(picked[layer])
        if deficit <= 0:
            continue
        for neighbor in _NEIGHBOR_LAYERS[layer]:
            while deficit > 0 and leftover[neighbor]:
                picked[layer].append(leftover[neighbor].pop(0))
                deficit -= 1
            if deficit == 0:
                break
        if deficit > 0:
            notes.append(
                f"note: probe layer '{layer}' short by {deficit} even after adjacent-layer fill"
            )
        else:
            notes.append(f"note: probe layer '{layer}' short, filled from adjacent layers")

    probe_rows: list[dict] = []
    for layer in LAYERS:
        probe_rows.extend(picked[layer])

    total = len(probe_rows)
    if total < 3 * PROBE_TARGET_PER_LAYER:
        notes.append(
            f"note: probe total {total} < {3 * PROBE_TARGET_PER_LAYER} "
            f"(snapshot only has {len(rows)} exportable rows)"
        )
    return probe_rows, notes


def _round_robin(rows: list[dict]) -> list[dict]:
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
    return interleaved


def write_shard_files(
    sprint_root: Path,
    rows: list[dict],
    *,
    size: int,
    out_prefix: str,
    force: bool,
) -> list[Path]:
    """Chunk *rows* round-robin into 50-200 row shards, atomically written."""
    if not (MIN_SHARD_SIZE <= size <= MAX_SHARD_SIZE):
        raise ShardExportError(
            f"--size 必须在 {MIN_SHARD_SIZE}-{MAX_SHARD_SIZE} 之间，当前为 {size}"
        )
    interleaved = _round_robin(rows)
    n_shards = math.ceil(len(interleaved) / size) if interleaved else 0
    targets = [
        _shard_path(sprint_root, f"{out_prefix}-{i + 1:03d}.input.jsonl")
        for i in range(n_shards)
    ]
    _ensure_no_overwrite(targets, force=force)
    for index, target in enumerate(targets):
        chunk = interleaved[index * size : (index + 1) * size]
        body = "".join(_dump_line(_export_payload(row)) + "\n" for row in chunk)
        atomic_write_text(target, body)
    return targets


def write_probe_file(
    sprint_root: Path,
    rows: list[dict],
    *,
    out_prefix: str,
    force: bool,
) -> tuple[Path, list[str]]:
    """Write the 60-row T0 probe input shard; return (path, stderr notes)."""
    probe_rows, notes = build_probe_rows(rows)
    target = _shard_path(sprint_root, f"{out_prefix}-probe.input.jsonl")
    _ensure_no_overwrite([target], force=force)
    body = "".join(_dump_line(_export_payload(row)) + "\n" for row in probe_rows)
    atomic_write_text(target, body)
    return target, notes


def make_shards(
    sprint_root: Path,
    *,
    probe: bool = False,
    size: int = 100,
    out_prefix: str = "shard",
    force: bool = False,
    pool_filter: str | None = None,
) -> dict:
    """Run the export; returns a summary dict for the CLI to print."""
    if pool_filter is not None and pool_filter != POOL_FILTER_CAPTION_GIF:
        raise ShardExportError(
            f"未定义的 --pool-filter 值: {pool_filter!r}（仅支持 {POOL_FILTER_CAPTION_GIF}）"
        )
    rows = load_rows(sprint_root / SNAPSHOT_NAME)
    rows, pool_stats = filter_pool(
        rows, require_caption_gif=pool_filter == POOL_FILTER_CAPTION_GIF
    )
    counts = classify_strata(rows)
    if probe:
        target, notes = write_probe_file(sprint_root, rows, out_prefix=out_prefix, force=force)
        return {
            "probe": True,
            "paths": [target],
            "rows": len(rows),
            "counts": counts,
            "pool_stats": pool_stats,
            "probe_rows": sum(1 for line in target.read_text(encoding="utf-8").splitlines() if line),
            "notes": notes,
        }
    targets = write_shard_files(
        sprint_root, rows, size=size, out_prefix=out_prefix, force=force
    )
    return {
        "probe": False,
        "paths": targets,
        "rows": len(rows),
        "counts": counts,
        "pool_stats": pool_stats,
        "notes": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export stratified text-only input shards from the W1 snapshot."
    )
    add_sprint_root_argument(parser)
    parser.add_argument("--probe", action="store_true", help="export the 60-row T0 probe")
    parser.add_argument(
        "--size", type=int, default=100, help=f"rows per shard ({MIN_SHARD_SIZE}-{MAX_SHARD_SIZE})"
    )
    parser.add_argument("--out-prefix", default="shard", help="output file name prefix")
    parser.add_argument(
        "--force", action="store_true", help="overwrite existing input shards"
    )
    parser.add_argument(
        "--pool-filter",
        default=None,
        choices=(POOL_FILTER_CAPTION_GIF,),
        help="restrict the pool to rows with non-empty caption AND an "
        "existing GIF artifact (Path.is_file() check only, bytes never opened)",
    )
    args = parser.parse_args(argv)
    sprint_root = resolve_sprint_root(args.sprint_root)

    try:
        summary = make_shards(
            sprint_root,
            probe=args.probe,
            size=args.size,
            out_prefix=args.out_prefix,
            force=args.force,
            pool_filter=args.pool_filter,
        )
    except ShardExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for note in summary["notes"]:
        print(note, file=sys.stderr)
    pool_stats = summary["pool_stats"]
    stats_note = ""
    if pool_stats:
        stats_note = (
            f", pool_filter: rows_total={pool_stats['rows_total']} "
            f"caption_empty={pool_stats['caption_empty']} "
            f"gif_missing={pool_stats['gif_missing']} pool={pool_stats['pool']}"
        )
    label = "probe" if summary["probe"] else "shards"
    print(
        f"导出完成: {len(summary['paths'])} 个 {label} 文件, "
        f"快照可导出行 {summary['rows']}, 分层 {summary['counts']}{stats_note}"
    )
    for path in summary["paths"]:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

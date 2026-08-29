#!/usr/bin/env python3
"""Merge only reviewer-approved (``.done``) shards into the enrichment cache.

W1 deterministic tool (zero model tokens). ``enrichment.db`` is a rebuildable
cache -- the canonical inputs stay the approved JSONL shards; the db can be
deleted and rebuilt from them at any time.

A shard is merged only when ALL of the following hold (global constraints 6/12,
W1 额外禁区):

- a ``.done`` sidecar exists for the shard;
- ``.done.artifact_sha256`` equals the shard file's actual SHA-256;
- a receipt under ``<sprint-root>/receipts/processed/`` or
  ``<sprint-root>/receipts/`` binds the same artifact SHA-256.

Anything else is skipped with a stderr listing. Inserted rows carry the
``shard_sha256``; refusal rows (``{"candidate_id": ..., "refused": true}``) and
rows flagged as sanitized by the shard's ``.qc.json`` go to
``enrichment_refusals`` (reason ``'refused'`` / ``'sanitized'``) instead of
``candidate_enrichment``. Primary-key conflicts are resolved with replace (the
later shard in sorted order wins) because the canonical data is the JSONL.

Sidecar schema (locked):

    candidate_enrichment   PK (candidate_id, pass_id, prompt_version)
    enrichment_refusals    PK (candidate_id, pass_id, prompt_version)

Usage:
    python scripts/token_sprint/merge_shards.py --sprint-root <path>

Exit codes: 0 success, 2 fatal input error.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from scripts.token_sprint.coordinator.common import (  # noqa: E402
    add_sprint_root_argument,
    resolve_sprint_root,
    sha256_file,
)

SHARDS_DIRNAME = "shards"
DB_NAME = "enrichment.db"
RECEIPTS_DIRNAME = "receipts"
PROCESSED_DIRNAME = "processed"

ENRICHMENT_DDL = """
CREATE TABLE IF NOT EXISTS candidate_enrichment (
    candidate_id      TEXT NOT NULL,
    pass_id           TEXT NOT NULL,
    prompt_version    TEXT NOT NULL,
    output_json       TEXT NOT NULL,
    shard_id          TEXT NOT NULL,
    shard_sha256      TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (candidate_id, pass_id, prompt_version)
);
CREATE TABLE IF NOT EXISTS enrichment_refusals (
    candidate_id   TEXT NOT NULL,
    pass_id        TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    shard_id       TEXT NOT NULL,
    shard_sha256   TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT 'refused',
    PRIMARY KEY (candidate_id, pass_id, prompt_version)
);
"""

_KNOWN_PASSES = ("p1a", "p1b", "p2", "p3", "p4", "p5")


class MergeError(RuntimeError):
    """Fatal merge problem; surfaces as exit code 2."""


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def receipt_shas(sprint_root: Path) -> set[str]:
    """Collect every artifact SHA-256 bound by any receipt (processed or not)."""
    shas: set[str] = set()
    receipts_dir = sprint_root / RECEIPTS_DIRNAME
    for directory in (receipts_dir / PROCESSED_DIRNAME, receipts_dir):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            data = _read_json(path)
            if data and data.get("artifact_sha256"):
                shas.add(str(data["artifact_sha256"]))
    return shas


def output_shards(shards_dir: Path) -> list[Path]:
    """Eligible output shard files (never ``*.input.jsonl``, ``.qc.json`` etc.)."""
    if not shards_dir.is_dir():
        return []
    candidates = []
    for path in sorted(shards_dir.glob("*.jsonl")):
        if path.name.endswith(".input.jsonl"):
            continue
        candidates.append(path)
    return candidates


def parse_shard_name(path: Path) -> tuple[str, str, str] | None:
    """Split ``<prefix>.<pass_id>.<prompt_version>.<attempt_id>.jsonl``.

    Returns ``(pass_id, prompt_version, shard_id)`` or None when the name does
    not follow the versioned convention.
    """
    parts = path.stem.split(".")
    if len(parts) != 4:
        return None
    prefix, pass_id, prompt_version, _attempt_id = parts
    if not prefix or pass_id not in _KNOWN_PASSES or not prompt_version:
        return None
    return pass_id, prompt_version, path.stem


def _load_sanitized(path: Path) -> set[str]:
    """candidate_ids flagged as suspected_sanitized by the shard's QC file."""
    data = _read_json(path.with_name(path.name + ".qc.json"))
    if not data:
        return set()
    listed = data.get("suspected_sanitized")
    if not isinstance(listed, list):
        return set()
    return {str(item) for item in listed}


def merge_shards(sprint_root: Path) -> dict:
    """Merge all eligible shards; returns a summary for the CLI to print."""
    shards_dir = sprint_root / SHARDS_DIRNAME
    db_path = sprint_root / DB_NAME
    bound_shas = receipt_shas(sprint_root)

    merged: list[str] = []
    skipped: list[str] = []
    rows_inserted = 0
    refusals_inserted = 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(ENRICHMENT_DDL)
        conn.commit()

        for shard in output_shards(shards_dir):
            parsed = parse_shard_name(shard)
            if parsed is None:
                skipped.append(f"{shard.name}: 文件名不符合 <prefix>.<pass>.<version>.<attempt>.jsonl")
                continue
            pass_id, prompt_version, shard_id = parsed
            shard_sha = sha256_file(shard)

            done = _read_json(shard.with_name(shard.name + ".done"))
            if done is None:
                skipped.append(f"{shard.name}: 缺少 .done")
                continue
            if done.get("artifact_sha256") != shard_sha:
                skipped.append(
                    f"{shard.name}: .done.artifact_sha256 与实际文件不一致"
                    f" (done={done.get('artifact_sha256')}, actual={shard_sha})"
                )
                continue
            if shard_sha not in bound_shas:
                skipped.append(f"{shard.name}: 没有绑定同一 SHA 的 receipt")
                continue

            sanitized = _load_sanitized(shard)
            shard_rows = 0
            shard_refusals = 0
            for line_number, line in enumerate(
                shard.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    skipped.append(f"{shard.name} line {line_number}: 不是合法 JSON ({exc})")
                    continue
                if not (isinstance(row, dict) and row.get("candidate_id") is not None):
                    skipped.append(f"{shard.name} line {line_number}: 缺少 candidate_id")
                    continue
                candidate_id = str(row["candidate_id"])

                if row.get("refused") is True:
                    conn.execute(
                        """INSERT OR REPLACE INTO enrichment_refusals
                           (candidate_id, pass_id, prompt_version, shard_id,
                            shard_sha256, reason)
                           VALUES (?, ?, ?, ?, ?, 'refused')""",
                        (candidate_id, pass_id, prompt_version, shard_id, shard_sha),
                    )
                    shard_refusals += 1
                elif candidate_id in sanitized:
                    conn.execute(
                        """INSERT OR REPLACE INTO enrichment_refusals
                           (candidate_id, pass_id, prompt_version, shard_id,
                            shard_sha256, reason)
                           VALUES (?, ?, ?, ?, ?, 'sanitized')""",
                        (candidate_id, pass_id, prompt_version, shard_id, shard_sha),
                    )
                    shard_refusals += 1
                else:
                    conn.execute(
                        """INSERT OR REPLACE INTO candidate_enrichment
                           (candidate_id, pass_id, prompt_version, output_json,
                            shard_id, shard_sha256)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (candidate_id, pass_id, prompt_version, line, shard_id, shard_sha),
                    )
                    shard_rows += 1

            conn.commit()
            merged.append(
                f"{shard.name} (rows={shard_rows}, refusals={shard_refusals}, "
                f"sha256={shard_sha})"
            )
            rows_inserted += shard_rows
            refusals_inserted += shard_refusals

        total_rows = conn.execute("SELECT COUNT(*) FROM candidate_enrichment").fetchone()[0]
        total_refusals = conn.execute(
            "SELECT COUNT(*) FROM enrichment_refusals"
        ).fetchone()[0]
    finally:
        conn.close()

    return {
        "db_path": str(db_path),
        "merged": merged,
        "skipped": skipped,
        "rows_inserted": rows_inserted,
        "refusals_inserted": refusals_inserted,
        "total_rows": total_rows,
        "total_refusals": total_refusals,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge .done shards (hash-chain verified) into the rebuildable enrichment cache."
    )
    add_sprint_root_argument(parser)
    args = parser.parse_args(argv)
    sprint_root = resolve_sprint_root(args.sprint_root)

    try:
        summary = merge_shards(sprint_root)
    except (MergeError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for reason in summary["skipped"]:
        print(f"skip: {reason}", file=sys.stderr)
    print(
        f"merge 完成: 本次合并 {len(summary['merged'])} 片, "
        f"插入 {summary['rows_inserted']} 行, 拒绝/消毒 {summary['refusals_inserted']} 行"
    )
    for item in summary["merged"]:
        print(f"  merged: {item}")
    print(f"缓存总计: candidate_enrichment={summary['total_rows']}, "
          f"enrichment_refusals={summary['total_refusals']}")
    print(f"  {summary['db_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

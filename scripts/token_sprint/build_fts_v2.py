#!/usr/bin/env python3
"""Build the shadow FTS v2 index from approved immutable enrichment shards.

W8 token-sprint deterministic tool (zero model tokens).  Consumes **only** the
coordinator-authored ``approved_enrichment_manifest.json`` in the sprint root:
every shard file is re-hashed and accepted by the exact
``(pass_id, prompt_version, shard_id, sha256)`` tuple recorded in the manifest,
and only after its ``.ready``/``.done``/receipt four-way hash chain verifies
(:func:`scripts.token_sprint.coordinator.protocol.verify_hash_chain`).  Any
missing file, byte-level drift, or chain disagreement fails the whole build
closed (exit 2) before the database is opened for writing -- no partial index.

The database is rebuilt idempotently in a single transaction:

1. ``DELETE`` all rows from ``fts_v2_content`` and insert the merged corpus
   (ordered by ``candidate_id``);
2. ``DELETE`` all rows from the ``candidate_search_fts_v2`` virtual table and
   reinsert ``SELECT ... FROM fts_v2_content ORDER BY candidate_id``.

Upserting into the FTS5 virtual table is forbidden: its UNINDEXED
``candidate_id`` column has no unique constraint, so replaced rows would leave
duplicates behind.  Uniqueness lives only in ``fts_v2_content`` (PRIMARY KEY).

Merge rules (per W8 plan, deterministic):
- ``p1b`` rows contribute their ``tags`` list to the tags text;
- ``p3`` rows contribute their ``keywords`` list, appended after ``p1b`` tags
  (first-occurrence dedup);
- ``p2`` rows contribute their ``scene`` object, serialized to
  ``key=value`` tokens in sorted-key order (``None`` values dropped) as the
  summary text;
- rows are processed in manifest shard order, then file order; the last write
  per ``(candidate_id, pass_id)`` wins (JSONL "later write wins"), which makes
  cross-shard conflicts resolve by manifest list order;
- ``{"refused": true}`` rows are skipped;
- rows that are neither refusals nor pass-schema-conformant fail the build.

``source_path`` is empty for corpus rows: the approved shards carry no path
material and nothing outside the approved manifest may be consumed.

The full-build (delete + reinsert) instead of per-row upsert also guarantees
that rows dropped from a future manifest revision disappear from the index.

Usage::

    python scripts/token_sprint/build_fts_v2.py --sprint-root <path> \
        --db <snapshot-copy-or-tmp.db>

``--sprint-root`` falls back to ``GIFAGENT_SPRINT_ROOT`` only when omitted;
if neither is set, exit 2.  ``--db`` must point at a snapshot copy or a temp
database; production paths (``inputs_manifest.json`` ``source_db_abs``, any
``dist`` directory, the read-only snapshot original) are rejected.  The
``enrichment.db`` working database is never read.

Exit codes: 0 success, 2 fatal/fail-closed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from scripts.token_sprint.coordinator.common import (  # noqa: E402
    add_sprint_root_argument,
    resolve_sprint_root,
    sha256_bytes,
    sha256_file,
)
from scripts.token_sprint.coordinator.protocol import (  # noqa: E402
    HashChainError,
    verify_hash_chain,
)
from app.services.workbench_schema import apply_search_schema  # noqa: E402

MANIFEST_NAME = "approved_enrichment_manifest.json"
MANIFEST_SCHEMA = "gifagent.token_sprint.approved_enrichment_manifest.v1"
INPUTS_MANIFEST_NAME = "inputs_manifest.json"
SNAPSHOT_ORIGINAL_NAME = "library_snapshot.db"
ENRICHMENT_DB_NAME = "enrichment.db"  # never read; kept for documentation only

#: Fixed single-token MATCH queries used for the cross-build determinism
#: digest.  Single tokens need no FTS5 query escaping by construction.
FIXED_QUERY_SET = (
    "kitchen",
    "bedroom",
    "couple",
    "smiling",
    "intimacy",
    "nudity",
    "test",
)

#: Payload field per approved pass id.
PASS_PAYLOAD_FIELD = {
    "p1b": "tags",
    "p2": "scene",
    "p3": "keywords",
}


class BuildError(RuntimeError):
    """Fatal build problem; surfaces as exit code 2."""


# ---------------------------------------------------------------------------
# fail-closed input validation
# ---------------------------------------------------------------------------


def _guard_db_target(
    db: Path, sprint_root: Path, *, allow_production: bool = False
) -> None:
    """Reject production/protected database targets unless explicitly allowed."""
    resolved = db.resolve()
    if "dist" in resolved.parts and not allow_production:
        raise BuildError(
            f"--db must not point under a dist/ tree without --allow-production: "
            f"{resolved.as_posix()}"
        )

    inputs_manifest_path = sprint_root / INPUTS_MANIFEST_NAME
    if not inputs_manifest_path.is_file():
        raise BuildError(
            f"missing coordinator inputs manifest: {inputs_manifest_path.as_posix()}"
        )
    try:
        inputs_manifest = json.loads(inputs_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"cannot read {inputs_manifest_path.as_posix()}: {exc}") from exc
    source_db_abs = inputs_manifest.get("source_db_abs")
    if not source_db_abs:
        raise BuildError("inputs_manifest.json is missing 'source_db_abs'")
    if resolved == Path(source_db_abs).resolve() and not allow_production:
        raise BuildError(
            f"--db must not be the production library pinned by "
            f"inputs_manifest.json without --allow-production: {resolved.as_posix()}"
        )

    snapshot_original = sprint_root / SNAPSHOT_ORIGINAL_NAME
    if resolved == snapshot_original.resolve():
        raise BuildError(
            f"--db must not be the read-only snapshot original; build on a copy: "
            f"{resolved.as_posix()}"
        )


def _load_approved_manifest(sprint_root: Path) -> tuple[dict[str, Any], str]:
    manifest_path = sprint_root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise BuildError(f"approved manifest not found: {manifest_path.as_posix()}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"cannot read {manifest_path.as_posix()}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise BuildError(
            f"{MANIFEST_NAME} schema must be {MANIFEST_SCHEMA!r}, "
            f"got {manifest.get('schema')!r}"
        )
    shards = manifest.get("shards")
    if not isinstance(shards, list) or not shards:
        raise BuildError(f"{MANIFEST_NAME} has no shard entries")
    manifest_sha = sha256_file(manifest_path)
    return manifest, manifest_sha


def _require(entry: dict[str, Any], key: str) -> Any:
    value = entry.get(key)
    if value is None or value == "":
        raise BuildError(f"manifest shard entry missing {key!r}: {entry!r}")
    return value


def _find_receipt(sprint_root: Path, entry: dict[str, Any], artifact_sha: str) -> Path:
    """Locate the receipt binding this shard; ambiguous or absent -> fail."""
    receipts_dir = sprint_root / "receipts"
    artifact_id = str(_require(entry, "manifest_artifact_id"))
    artifact_path = str(_require(entry, "artifact_path"))
    by_id: list[Path] = []
    by_artifact: list[Path] = []
    if receipts_dir.is_dir():
        for path in sorted(receipts_dir.rglob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            if data.get("receipt_id") == artifact_id:
                by_id.append(path)
            if (
                data.get("artifact_path") == Path(artifact_path).resolve().as_posix()
                and data.get("artifact_sha256") == artifact_sha
            ):
                by_artifact.append(path)
    if len(by_id) == 1:
        return by_id[0]
    if len(by_id) > 1:
        raise BuildError(
            f"ambiguous receipts for manifest_artifact_id {artifact_id}: "
            f"{[p.as_posix() for p in by_id]}"
        )
    if len(by_artifact) == 1:
        return by_artifact[0]
    if len(by_artifact) > 1:
        raise BuildError(
            f"ambiguous receipts for artifact {artifact_path}: "
            f"{[p.as_posix() for p in by_artifact]}"
        )
    raise BuildError(
        f"no receipt found binding shard {artifact_path} "
        f"(manifest_artifact_id={artifact_id})"
    )


def _verify_shard_chain(sprint_root: Path, entry: dict[str, Any]) -> Path:
    """Validate one manifest entry; return the shard path on success."""
    artifact = Path(str(_require(entry, "artifact_path")))
    pass_id = str(_require(entry, "pass_id"))
    prompt_version = str(_require(entry, "prompt_version"))
    shard_id = str(_require(entry, "shard_id"))
    expected_sha = str(_require(entry, "sha256")).lower()
    if pass_id not in PASS_PAYLOAD_FIELD:
        raise BuildError(f"unknown pass_id {pass_id!r} in approved manifest")

    if not artifact.is_file():
        raise BuildError(f"approved shard file missing: {artifact.as_posix()}")
    if not artifact.name.startswith("shard-") or not artifact.name.endswith(
        f".{pass_id}.{prompt_version}.{shard_id}.jsonl"
    ):
        raise BuildError(
            f"shard filename does not match manifest tuple "
            f"({pass_id}, {prompt_version}, {shard_id}): {artifact.name}"
        )
    actual_sha = sha256_file(artifact)
    if actual_sha != expected_sha:
        raise BuildError(
            f"approved shard byte drift: {artifact.as_posix()} "
            f"manifest sha {expected_sha} != actual {actual_sha}"
        )

    ready = artifact.with_name(artifact.name + ".ready")
    done = artifact.with_name(artifact.name + ".done")
    if not ready.is_file():
        raise BuildError(f"approved shard .ready missing: {ready.as_posix()}")
    if not done.is_file():
        raise BuildError(f"approved shard .done missing: {done.as_posix()}")
    receipt = _find_receipt(sprint_root, entry, actual_sha)
    try:
        verify_hash_chain(artifact, ready, done, receipt)
    except HashChainError as exc:
        raise BuildError(
            f"approved shard hash chain invalid: {artifact.as_posix()}: {exc}"
        ) from exc
    return artifact


# ---------------------------------------------------------------------------
# shard parsing + merge
# ---------------------------------------------------------------------------


def _iter_shard_rows(artifact: Path):
    try:
        text = artifact.read_text(encoding="utf-8")
    except OSError as exc:
        raise BuildError(f"cannot read shard {artifact.as_posix()}: {exc}") from exc
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BuildError(
                f"{artifact.as_posix()}:{line_no}: invalid JSON: {exc}"
            ) from exc
        yield line_no, row


def _validate_payload(pass_id: str, row: dict[str, Any], where: str) -> Any:
    field = PASS_PAYLOAD_FIELD[pass_id]
    if field not in row:
        raise BuildError(f"{where}: {pass_id} row missing {field!r}")
    payload = row[field]
    if pass_id in ("p1b", "p3"):
        if not isinstance(payload, list) or not all(isinstance(t, str) for t in payload):
            raise BuildError(f"{where}: {field} must be a list[str]")
    else:  # p2 scene
        if not isinstance(payload, dict):
            raise BuildError(f"{where}: {field} must be an object")
        for key, value in payload.items():
            if value is not None and not isinstance(value, str):
                raise BuildError(f"{where}: scene.{key} must be a string or null")
    return payload


def _serialize_scene(scene: dict[str, Any]) -> str:
    return " ".join(f"{k}={scene[k]}" for k in sorted(scene) if scene[k] is not None)


def _merge_manifest_rows(
    sprint_root: Path, manifest: dict[str, Any]
) -> tuple[list[tuple[str, str, str]], dict[str, int], int]:
    """Parse every approved shard and merge rows into
    ``(candidate_id, summary, tags_text)`` tuples sorted by candidate_id.

    Processing order is manifest shard order, then file order; the last write
    per ``(candidate_id, pass_id)`` wins.  Returns ``(rows, per_pass,
    same_pass_overwrites)``.
    """
    latest: dict[tuple[str, str], Any] = {}
    candidate_ids: set[str] = set()
    per_pass: dict[str, int] = {}
    overwrites = 0

    for entry in manifest["shards"]:
        pass_id = str(entry["pass_id"])
        artifact = Path(str(entry["artifact_path"]))
        for line_no, row in _iter_shard_rows(artifact):
            where = f"{artifact.as_posix()}:{line_no}"
            if not isinstance(row, dict):
                raise BuildError(f"{where}: row must be a JSON object")
            candidate_id = row.get("candidate_id")
            if not isinstance(candidate_id, str) or not candidate_id.strip():
                raise BuildError(f"{where}: missing/blank candidate_id")
            if row.get("refused") is True:
                continue
            payload = _validate_payload(pass_id, row, where)
            if (candidate_id, pass_id) in latest:
                overwrites += 1
            latest[(candidate_id, pass_id)] = payload
            candidate_ids.add(candidate_id)
            per_pass[pass_id] = per_pass.get(pass_id, 0) + 1

    rows: list[tuple[str, str, str]] = []
    for candidate_id in candidate_ids:
        tags_tokens: list[str] = []
        seen: set[str] = set()
        for source_pass in ("p1b", "p3"):
            payload = latest.get((candidate_id, source_pass))
            if payload is None:
                continue
            for token in payload:
                if token and token not in seen:
                    seen.add(token)
                    tags_tokens.append(token)
        scene = latest.get((candidate_id, "p2"))
        summary = _serialize_scene(scene) if scene is not None else ""
        rows.append((candidate_id, summary, " ".join(tags_tokens)))
    rows.sort(key=lambda r: r[0])
    return rows, per_pass, overwrites


# ---------------------------------------------------------------------------
# database build
# ---------------------------------------------------------------------------


def _fixed_query_digests(conn: sqlite3.Connection) -> tuple[dict[str, str], str]:
    per_query: dict[str, str] = {}
    for query in FIXED_QUERY_SET:
        ids = [
            row[0]
            for row in conn.execute(
                "SELECT candidate_id FROM candidate_search_fts_v2 "
                "WHERE candidate_search_fts_v2 MATCH ? "
                "ORDER BY candidate_id",
                (query,),
            ).fetchall()
        ]
        per_query[query] = sha256_bytes("\n".join(ids).encode("utf-8"))
    aggregate = sha256_bytes(
        "\n".join(f"{q}:{per_query[q]}" for q in FIXED_QUERY_SET).encode("utf-8")
    )
    return per_query, aggregate


def _rebuild_database(db: Path, rows: list[tuple[str, str, str, str, str]]) -> dict[str, Any]:
    conn = sqlite3.connect(str(db))
    try:
        conn.row_factory = sqlite3.Row
        apply_search_schema(conn)

        conn.isolation_level = None  # explicit transaction control
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute("DELETE FROM fts_v2_content")
            cur.executemany(
                "INSERT INTO fts_v2_content "
                "(candidate_id, summary, tags, source_path, enrichment_manifest_sha) "
                "VALUES (?,?,?,?,?)",
                rows,
            )
            cur.execute("DELETE FROM candidate_search_fts_v2")
            cur.execute(
                "INSERT INTO candidate_search_fts_v2 "
                "(candidate_id, summary, tags, source_path) "
                "SELECT candidate_id, summary, tags, source_path "
                "FROM fts_v2_content ORDER BY candidate_id"
            )
            content_count = cur.execute(
                "SELECT COUNT(*) FROM fts_v2_content"
            ).fetchone()[0]
            content_distinct = cur.execute(
                "SELECT COUNT(DISTINCT candidate_id) FROM fts_v2_content"
            ).fetchone()[0]
            fts_count = cur.execute(
                "SELECT COUNT(*) FROM candidate_search_fts_v2"
            ).fetchone()[0]
            fts_distinct = cur.execute(
                "SELECT COUNT(*) FROM "
                "(SELECT DISTINCT candidate_id FROM candidate_search_fts_v2)"
            ).fetchone()[0]
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        if content_count != content_distinct or fts_count != fts_distinct:
            cur.execute("ROLLBACK")
            raise BuildError(
                "post-build uniqueness check failed: "
                f"content {content_count}/{content_distinct} "
                f"fts {fts_count}/{fts_distinct}"
            )
        if fts_count != content_count:
            cur.execute("ROLLBACK")
            raise BuildError(
                f"FTS rows {fts_count} != content rows {content_count}"
            )
        cur.execute("COMMIT")

        per_query, aggregate = _fixed_query_digests(conn)
    finally:
        conn.close()

    return {
        "content_rows": content_count,
        "content_distinct_candidate_id": content_distinct,
        "fts_rows": fts_count,
        "fts_distinct_candidate_id": fts_distinct,
        "fixed_query_digests": per_query,
        "fixed_query_digests_sha256": aggregate,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build shadow FTS v2 from approved immutable enrichment shards."
    )
    add_sprint_root_argument(parser)
    parser.add_argument(
        "--db",
        required=True,
        help="Snapshot copy or temp database to build the shadow index into.",
    )
    parser.add_argument(
        "--allow-production",
        action="store_true",
        help="Explicitly permit building into production or dist/ databases.",
    )
    args = parser.parse_args(argv)

    try:
        sprint_root = resolve_sprint_root(args.sprint_root)
        db = Path(args.db)
        _guard_db_target(db, sprint_root, allow_production=args.allow_production)
        if not db.is_file():
            raise BuildError(
                f"--db does not exist; build into a snapshot copy or a prepared "
                f"temp database: {db.resolve().as_posix()}"
            )

        manifest, manifest_sha = _load_approved_manifest(sprint_root)
        shard_paths = [_verify_shard_chain(sprint_root, entry) for entry in manifest["shards"]]
        merged, per_pass, overwrites = _merge_manifest_rows(sprint_root, manifest)
        rows = [
            (candidate_id, summary, tags, "", manifest_sha)
            for (candidate_id, summary, tags) in merged
        ]
        summary = _rebuild_database(db, rows)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (HashChainError, sqlite3.Error, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    summary.update(
        {
            "db": db.resolve().as_posix(),
            "manifest_sha256": manifest_sha,
            "shards_accepted": len(shard_paths),
            "rows_per_pass": per_pass,
            "same_pass_overwrites": overwrites,
        }
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

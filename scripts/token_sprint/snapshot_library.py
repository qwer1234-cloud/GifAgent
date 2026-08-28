#!/usr/bin/env python3
"""Snapshot the production library into the sprint root and force-rebuild FTS.

W1 deterministic tool (zero model tokens). Contract (implementation plan
``2026-08-token-sprint-w1-enrichment-implementation.md``, "Snapshot rebuild
contract (lock)" -- order must not change):

1. Read ``<sprint-root>/inputs_manifest.json`` and verify its SHA-256 against
   the canonical ``sprint_manifest.json`` ``control_plane`` record (if the
   canonical manifest has no such record yet, only recompute the file SHA).
2. ``source_db_abs`` is resolved with ``strict=True`` from the manifest and
   opened read-only to confirm it is a SQLite database. The production
   database is never written.
3. Refuse to overwrite an existing ``library_snapshot.db`` unless ``--force``.
4. ``VACUUM INTO`` the snapshot. On failure (e.g. database is locked) print
   the Chinese GUI-busy hint and exit 2. Never fall back to a hand-rolled
   Python table copy (that could silently diverge).
5. Open the snapshot with WAL + ``busy_timeout=30000`` + ``row_factory=Row``.
6. Delete ``candidate_search_fts`` **and** ``search_index_state`` first.
7. Call the existing ``LibrarySearchService.rebuild_index()`` (it resumes
   incrementally from ``search_index_state``, hence step 6 is mandatory).
8. Acceptance: ``errors == 0`` and ``COUNT(*) FROM candidate_search_fts``
   == ``COUNT(DISTINCT candidate_id)`` == ``COUNT(*) FROM candidate_gifs``.
9. Print the snapshot SHA-256 and the three counts.

Usage:
    python scripts/token_sprint/snapshot_library.py --sprint-root <path> [--force]

Exit codes: 0 success, 2 fatal input/VACUUM error (incl. GUI busy),
3 rebuild-contract violation.
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

from app.services.library_search import LibrarySearchService  # noqa: E402
from app.services.workbench_schema import RebuildReport  # noqa: E402
from scripts.token_sprint.coordinator.common import (  # noqa: E402
    add_sprint_root_argument,
    resolve_sprint_root,
    sha256_file,
)

SNAPSHOT_NAME = "library_snapshot.db"
INPUTS_MANIFEST_NAME = "inputs_manifest.json"
CANONICAL_MANIFEST_NAME = "sprint_manifest.json"
EXPECTED_INPUTS_SCHEMA = "gifagent.token_sprint.inputs.v1"

GUI_BUSY_HINT = "请先关闭 GifAgent GUI 后重试"


class SnapshotError(RuntimeError):
    """Fatal snapshot problem; surfaces as exit code 2."""


class GuiBusyError(SnapshotError):
    """The source database could not be read (typically held by the GUI)."""


class RebuildContractError(RuntimeError):
    """FTS rebuild contract violated (non-zero errors or three counts differ)."""


def verify_inputs_manifest(sprint_root: Path) -> dict:
    """Load ``inputs_manifest.json`` and check its SHA against the canonical manifest.

    Returns the parsed manifest. Raises :class:`SnapshotError` when the file is
    missing/unreadable, its schema differs, or the canonical ``control_plane``
    pins a different SHA-256.
    """
    manifest_path = sprint_root / INPUTS_MANIFEST_NAME
    if not manifest_path.is_file():
        raise SnapshotError(f"缺少 {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"无法读取 inputs_manifest.json: {exc}") from exc

    schema = manifest.get("schema")
    if schema is not None and schema != EXPECTED_INPUTS_SCHEMA:
        raise SnapshotError(
            f"inputs_manifest.json schema 不匹配: {schema!r} != {EXPECTED_INPUTS_SCHEMA!r}"
        )
    if not manifest.get("source_db_abs"):
        raise SnapshotError("inputs_manifest.json 缺少 source_db_abs")

    actual_sha = sha256_file(manifest_path)
    canonical_path = sprint_root / CANONICAL_MANIFEST_NAME
    if canonical_path.is_file():
        try:
            canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotError(f"无法读取 canonical manifest: {exc}") from exc
        record = (canonical.get("control_plane") or {}).get(INPUTS_MANIFEST_NAME) or {}
        expected_sha = record.get("sha256")
        if expected_sha and expected_sha != actual_sha:
            raise SnapshotError(
                "inputs_manifest.json SHA-256 与 canonical manifest control_plane "
                f"记录不一致: actual={actual_sha} expected={expected_sha}"
            )
    else:
        print(
            f"warn: canonical manifest 未找到，仅重算 inputs_manifest.json SHA: {actual_sha}",
            file=sys.stderr,
        )
    return manifest


def open_source_readonly(source_db: Path) -> sqlite3.Connection:
    """Open the production database strictly read-only and confirm it is SQLite."""
    try:
        conn = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)
        conn.execute("SELECT count(*) FROM sqlite_master")
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise GuiBusyError(f"{GUI_BUSY_HINT}（源库被占用: {exc}）") from exc
        raise SnapshotError(f"源库不是可读的 SQLite 数据库: {source_db} ({exc})") from exc
    except sqlite3.Error as exc:
        raise SnapshotError(f"源库不是可读的 SQLite 数据库: {source_db} ({exc})") from exc
    return conn


def vacuum_into(source_conn: sqlite3.Connection, target: Path) -> None:
    """``VACUUM INTO`` the snapshot file. The target must not exist."""
    try:
        source_conn.execute("VACUUM INTO ?", (target.as_posix(),))
    except sqlite3.OperationalError as exc:
        raise GuiBusyError(
            f"{GUI_BUSY_HINT}（VACUUM INTO 失败: {exc}; source 只读快照未完成）"
        ) from exc


def force_rebuild_fts(conn: sqlite3.Connection) -> RebuildReport:
    """Force a full FTS rebuild on *conn* (snapshot contract steps 6-8).

    ``rebuild_index()`` resumes incrementally from
    ``search_index_state.last_candidate_id``; an "FTS empty but state complete"
    database would therefore be skipped entirely. Deleting both tables first
    forces the full reinsert. Raises :class:`RebuildContractError` when the
    three counts disagree or the report carries errors.
    """
    conn.execute("DELETE FROM candidate_search_fts")
    conn.execute("DELETE FROM search_index_state")
    conn.commit()

    report = LibrarySearchService(conn).rebuild_index()

    fts_rows = conn.execute("SELECT COUNT(*) FROM candidate_search_fts").fetchone()[0]
    fts_ids = conn.execute(
        "SELECT COUNT(DISTINCT candidate_id) FROM candidate_search_fts"
    ).fetchone()[0]
    candidate_rows = conn.execute("SELECT COUNT(*) FROM candidate_gifs").fetchone()[0]

    if report.errors != 0:
        raise RebuildContractError(
            f"rebuild_index 报告 {report.errors} 个错误: {report.error_details}"
        )
    if not (fts_rows == fts_ids == candidate_rows):
        raise RebuildContractError(
            "三计数不一致: "
            f"candidate_search_fts COUNT(*)={fts_rows}, "
            f"COUNT(DISTINCT candidate_id)={fts_ids}, "
            f"candidate_gifs COUNT(*)={candidate_rows}"
        )
    return report


def snapshot_library(sprint_root: Path, *, force: bool = False) -> dict:
    """Run the locked snapshot contract; return the acceptance summary."""
    manifest = verify_inputs_manifest(sprint_root)

    source_db = Path(str(manifest["source_db_abs"])).expanduser()
    try:
        source_db = source_db.resolve(strict=True)
    except OSError as exc:
        raise SnapshotError(f"source_db_abs 不存在: {manifest['source_db_abs']} ({exc})") from exc

    target = sprint_root / SNAPSHOT_NAME
    if target.exists():
        if not force:
            raise SnapshotError(
                f"快照已存在，拒绝覆盖: {target}（如需重建请加 --force）"
            )
        for suffix in ("-wal", "-shm"):
            sidecar = target.with_name(target.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
        target.unlink()

    source_conn = open_source_readonly(source_db)
    try:
        vacuum_into(source_conn, target)
    finally:
        source_conn.close()

    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        report = force_rebuild_fts(conn)
        fts_rows = conn.execute("SELECT COUNT(*) FROM candidate_search_fts").fetchone()[0]
        fts_ids = conn.execute(
            "SELECT COUNT(DISTINCT candidate_id) FROM candidate_search_fts"
        ).fetchone()[0]
        candidate_rows = conn.execute("SELECT COUNT(*) FROM candidate_gifs").fetchone()[0]
    finally:
        # Close before hashing so WAL is checkpointed into the main file.
        conn.close()

    snapshot_sha = sha256_file(target)
    return {
        "snapshot_path": str(target),
        "snapshot_sha256": snapshot_sha,
        "fts_rows": fts_rows,
        "fts_distinct_ids": fts_ids,
        "candidate_rows": candidate_rows,
        "scanned": report.scanned,
        "inserted": report.inserted,
        "errors": report.errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="VACUUM INTO the production library and force-rebuild snapshot FTS."
    )
    add_sprint_root_argument(parser)
    parser.add_argument(
        "--force",
        action="store_true",
        help="delete an existing library_snapshot.db and rebuild it",
    )
    args = parser.parse_args(argv)
    sprint_root = resolve_sprint_root(args.sprint_root)

    try:
        summary = snapshot_library(sprint_root, force=args.force)
    except GuiBusyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(GUI_BUSY_HINT, file=sys.stderr)
        return 2
    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RebuildContractError as exc:
        print(f"error: 快照验收失败: {exc}", file=sys.stderr)
        return 3

    print(
        "快照完成: {snapshot_path}\n"
        "snapshot_sha256: {snapshot_sha256}\n"
        "candidate_search_fts COUNT(*) = {fts_rows}\n"
        "candidate_search_fts COUNT(DISTINCT candidate_id) = {fts_distinct_ids}\n"
        "candidate_gifs COUNT(*) = {candidate_rows}\n"
        "rebuild errors = {errors} (scanned={scanned}, inserted={inserted})".format(**summary)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""W3 relink inventory: cross-check a relink source directory against broken
artifact findings and the full candidate/favorite path tables, then emit
dry-run relink proposals (JSONL).

Design (W3 relink-inventory work order, 2026-08-29):

- The relink source (e.g. ``D:\\data``) is **inventoried with metadata only**:
  ``os.scandir`` recursion + ``entry.stat()`` for size/mtime. This tool never
  opens, reads, or hashes a single byte of any media file.
- Targets come from two places:
    1. the SHA-verified W3 audit findings (``broken_artifact`` entries) --
       media E:-volume paths and batch-checkpoint inputs;
    2. the W1 ``library_snapshot.db`` opened ``mode=ro`` + ``query_only`` --
       the *full* candidate_gifs artifact/preview table (12,833 rows) and
       favorite_gifs full_path table, because relink mapping can be relevant
       to the whole library, not only rows already flagged broken.
- Matching is basename-exact, case-insensitive. Same-name-multiple-files
  targets are recorded as ``ambiguous`` (never auto-proposed). Targets whose
  basename does not appear but whose parent directory name exists in the
  source are recorded as ``unknown`` (rename/re-encode blind spot) instead of
  being force-matched.
- Proposals are dry-run only: there is no ``--apply`` mode and passing
  ``--apply`` exits 2. Nothing in this tool executes SQL or touches the
  production database.

Usage:
    python scripts/token_sprint/relink_proposals.py --sprint-root <path> \
        --relink-source <dir> [--findings <json>] [--snapshot <db>] \
        [--inventory-out <json>] [--proposals-out <jsonl>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from scripts.token_sprint.coordinator.common import (  # noqa: E402
    add_sprint_root_argument,
    resolve_sprint_root,
)

DEFAULT_FINDINGS_SUBPATH = "w3/w3-audit-findings.json"
DEFAULT_INVENTORY_SUBPATH = "w3/relink_source_inventory.json"
DEFAULT_PROPOSALS_SUBPATH = "w3/w3-relink-proposals.v1.jsonl"
MANIFEST_NAME = "sprint_manifest.json"

INVENTORY_SCHEMA = "gifagent.token_sprint.w3_relink_inventory.v1"
PROPOSAL_SCHEMA = "gifagent.token_sprint.w3_relink_proposals.v1"
PRODUCER = "w3-relink-executor"

MEDIA_SUFFIXES = (".gif", ".jpg", ".jpeg", ".mp4", ".webm")

_EXT_CATEGORY = {".gif": "gif", ".jpg": "jpg", ".jpeg": "jpg",
                 ".mp4": "mp4", ".webm": "webm"}

# detail prefix written by audit_data.py for broken_artifact findings
_PATH_DETAIL_RE = re.compile(
    r"^path does not exist \(stat only, (?P<kind>relative_to_data_root|absolute)\): "
)

_BY_EXT_CHUNK = 5000  # batch size for streaming inventory JSON writes


class RelinkRefusal(RuntimeError):
    """Raised when the invocation asks for something this tool must not do."""


def categorize(name: str) -> str:
    """Classify a file name into gif/jpg/mp4/webm/other by extension."""
    return _EXT_CATEGORY.get(Path(name).suffix.lower(), "other")


def is_media_name(name: str) -> bool:
    return name.lower().endswith(MEDIA_SUFFIXES)


# ---------------------------------------------------------------------------
# Source inventory (metadata only -- no byte of any file is ever read)
# ---------------------------------------------------------------------------


def inventory_source(source_root: Path) -> dict:
    """Walk *source_root* and collect per-file metadata (path/size/mtime)."""
    if not source_root.is_dir():
        raise RelinkRefusal(f"relink source is not a directory: {source_root}")
    files: list[dict] = []
    dir_names: set[str] = set()
    visited_dirs = 0
    by_ext = {"gif": 0, "jpg": 0, "mp4": 0, "webm": 0, "other": 0}
    total_bytes = 0
    stat_errors = 0
    stack: list[str] = [""]
    while stack:
        rel_dir = stack.pop()
        visited_dirs += 1
        if rel_dir:
            dir_names.add(Path(rel_dir).name.lower())
        directory = source_root / rel_dir if rel_dir else source_root
        try:
            with os.scandir(directory) as entries:
                listing = list(entries)
        except OSError:
            stat_errors += 1
            continue
        for entry in listing:
            try:
                if entry.is_dir(follow_symlinks=False):
                    child = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                    stack.append(child)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                # A file may vanish while the re-download is in progress.
                stat_errors += 1
                continue
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            by_ext[categorize(entry.name)] += 1
            total_bytes += st.st_size
            files.append({
                "path": rel,
                "size": st.st_size,
                "mtime": int(st.st_mtime),
            })
    return {
        "files": files,
        "by_extension": by_ext,
        "total_dirs": visited_dirs,
        "total_bytes": total_bytes,
        "stat_errors": stat_errors,
        "dir_names": dir_names,
    }


def build_source_indexes(inventory: dict) -> dict:
    """Lowercase basename/dir-name/stem indexes over the inventoried files."""
    by_basename: dict[str, list[dict]] = {}
    by_stem: dict[str, list[dict]] = {}
    for file_entry in inventory["files"]:
        name = Path(file_entry["path"]).name
        by_basename.setdefault(name.lower(), []).append(file_entry)
        stem = Path(name).stem.lower()
        by_stem.setdefault(stem, []).append(file_entry)
    return {
        "by_basename": by_basename,
        "by_stem": by_stem,
        "dir_names": inventory["dir_names"],
    }


def write_inventory_json(out_path: Path, source_root: Path, inventory: dict,
                         extra_top: dict, as_of: str) -> None:
    """Stream the inventory JSON (batched, bounded memory for big trees)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "schema": INVENTORY_SCHEMA,
        "producer": PRODUCER,
        "generated_at": as_of,
        "as_of": as_of,
        "download_in_progress_note": (
            "The relink source was still being re-downloaded at as_of; all "
            "counts are point-in-time. Re-run this tool to refresh. Only "
            "metadata (path/size/mtime) was collected; no media byte was "
            "opened or hashed."
        ),
        "source_root": str(source_root),
        "stats": {
            "total_files": len(inventory["files"]),
            "total_dirs": inventory["total_dirs"],
            "total_bytes": inventory["total_bytes"],
            "by_extension": inventory["by_extension"],
            "stat_errors": inventory["stat_errors"],
            "truncated": False,
        },
    }
    header.update(extra_top)
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("{\n")
        sections = list(header.items()) + [("files", None)]
        files = inventory["files"]
        for index, (key, value) in enumerate(sections):
            if index:
                handle.write(",\n")
            handle.write(f"  {json.dumps(key)}: ")
            if key == "files":
                handle.write("[\n")
                for start in range(0, len(files), _BY_EXT_CHUNK):
                    chunk = files[start:start + _BY_EXT_CHUNK]
                    for offset, file_entry in enumerate(chunk):
                        handle.write("    ")
                        handle.write(json.dumps(file_entry, ensure_ascii=False))
                        if start + offset + 1 < len(files):
                            handle.write(",")
                        handle.write("\n")
                handle.write("  ]")
            else:
                handle.write(json.dumps(value, ensure_ascii=False, indent=2)
                             .replace("\n", "\n  "))
        handle.write("\n}\n")


# ---------------------------------------------------------------------------
# Findings + snapshot targets
# ---------------------------------------------------------------------------


def _basename_of(path_text: str) -> str:
    return Path(path_text.replace("\\", "/")).name


def _parent_name_of(path_text: str) -> str:
    parts = path_text.replace("\\", "/").rstrip("/").split("/")
    return parts[-2] if len(parts) >= 2 else ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_against_manifest(sprint_root: Path, artifact_path: Path,
                            kind: str) -> str:
    """Fail closed if the canonical manifest lists a stale SHA for a file.

    Returns ``verified`` / ``not_listed`` / ``no_manifest``. A mismatch raises
    :class:`RelinkRefusal` (exit 2).
    """
    manifest_path = sprint_root / MANIFEST_NAME
    if not manifest_path.is_file():
        return "no_manifest"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = None
    wanted = os.path.normcase(os.path.abspath(artifact_path))
    for entry in (manifest.get("artifacts") or {}).values():
        listed = entry.get("artifact_path") if isinstance(entry, dict) else None
        if listed and os.path.normcase(os.path.abspath(listed)) == wanted:
            expected = entry.get("artifact_sha256")
            break
    if expected is None:
        return "not_listed"
    actual = sha256_file(artifact_path)
    if actual != expected:
        raise RelinkRefusal(
            f"{kind} sha256 mismatch vs canonical manifest: expected "
            f"{expected}, got {actual} ({artifact_path})"
        )
    return "verified"


def load_broken_findings(findings_path: Path) -> tuple[list[dict], str, set]:
    """Extract broken_artifact path targets from the W3 findings JSON.

    Returns ``(targets, data_root, broken_refs)`` where ``broken_refs`` is the
    set of ``(record_ref, field)`` pairs flagged broken (used to mark the
    full-library snapshot rows).
    """
    data = json.loads(findings_path.read_text(encoding="utf-8"))
    data_root = data.get("data_root") or ""
    group_by_record_type = {
        "candidate_gifs": "candidate_gifs",
        "favorite_gifs": "favorites",
        "media": "media_e_volume",
        "completed_entry": "checkpoint",
    }
    broken_refs: set[tuple[str, str]] = set()
    targets: list[dict] = []
    for finding in data.get("findings", []):
        if finding.get("rule") != "broken_artifact":
            continue
        record_type = finding.get("record_type") or ""
        group = group_by_record_type.get(record_type)
        if group is None:
            continue
        detail = finding.get("detail", "")
        match = _PATH_DETAIL_RE.match(detail)
        if not match:
            continue
        path_text = detail[match.end():].strip()
        if not path_text:
            continue
        broken_refs.add((finding.get("record_ref") or "", finding.get("field") or ""))
        if record_type in ("candidate_gifs", "favorite_gifs"):
            # Full-library rows come from the snapshot DB; findings only mark
            # these rows broken, they do not create duplicate targets.
            continue
        targets.append({
            "source_group": group,
            "table": record_type if record_type != "completed_entry" else None,
            "row_ref": finding.get("record_ref") or "",
            "column": finding.get("field") or "",
            "old_path": path_text,
            "old_path_kind": match.group("kind"),
            "in_broken_findings": True,
            "data_root": data_root,
        })
    return targets, data_root, broken_refs


def load_snapshot_targets(snapshot_path: Path, data_root: str,
                          broken_refs: set[tuple[str, str]]) -> list[dict]:
    """Read the full candidate_gifs + favorite_gifs path tables (read-only)."""
    uri = f"file:{snapshot_path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    targets: list[dict] = []
    try:
        connection.execute("PRAGMA query_only=ON")
        cursor = connection.cursor()
        cursor.execute(
            "SELECT rowid, candidate_id, artifact_path, preview_path "
            "FROM candidate_gifs"
        )
        for rowid, candidate_id, artifact_path, preview_path in cursor:
            ref = f"candidate_gifs#{rowid} ({candidate_id})"
            for column, value in (("artifact_path", artifact_path),
                                  ("preview_path", preview_path)):
                if value:
                    targets.append({
                        "source_group": "candidate_gifs",
                        "table": "candidate_gifs",
                        "row_ref": ref,
                        "column": column,
                        "old_path": value,
                        "old_path_kind": "relative_to_data_root",
                        "in_broken_findings": (ref, column) in broken_refs,
                        "data_root": data_root,
                    })
        cursor.execute(
            "SELECT rowid, favorite_id, full_path FROM favorite_gifs"
        )
        for rowid, favorite_id, full_path in cursor:
            if not full_path:
                continue
            ref = f"favorite_gifs#{rowid} ({favorite_id})"
            targets.append({
                "source_group": "favorites",
                "table": "favorite_gifs",
                "row_ref": ref,
                "column": "full_path",
                "old_path": full_path,
                "old_path_kind": "absolute",
                "in_broken_findings": (ref, "full_path") in broken_refs,
                "data_root": data_root,
            })
    finally:
        connection.close()
    return targets


def resolve_old_path(target: dict) -> Path:
    """Absolute path for stat-only existence checks (never opened)."""
    raw = target["old_path"]
    if target["old_path_kind"] == "relative_to_data_root" and target["data_root"]:
        return Path(target["data_root"]) / raw
    return Path(raw)


def match_target(target: dict, old_abs: Path, indexes: dict) -> dict:
    """Classify one target against the source indexes (basename tier first)."""
    result = {
        "status": "unmatched",
        "old_path_exists_now": old_abs.exists(),  # stat only, never opened
        "candidates": [],
        "preferred_new_path": None,
        "disambiguation": None,
        "unknown_reason": None,
    }
    name = _basename_of(target["old_path"])
    hits = indexes["by_basename"].get(name.lower(), [])
    if len(hits) == 1:
        result["status"] = "matched"
        result["candidates"] = [hits[0]["path"]]
        return result
    if len(hits) > 1:
        result["status"] = "ambiguous"
        result["candidates"] = [hit["path"] for hit in hits]
        old_parent = _parent_name_of(target["old_path"]).lower()
        preferred = [hit for hit in hits
                     if _parent_name_of(hit["path"]).lower() == old_parent
                     and old_parent]
        if len(preferred) == 1:
            result["preferred_new_path"] = preferred[0]["path"]
            result["disambiguation"] = "parent_dir_name"
        return result
    # Basename not present -- honest "unknown" tiers, no force-fitting.
    old_parent = _parent_name_of(target["old_path"]).lower()
    if old_parent and old_parent in indexes["dir_names"]:
        result["status"] = "unknown"
        result["unknown_reason"] = "parent_dir_present_basenames_differ"
        return result
    stem = Path(name).stem.lower()
    stem_hits = [hit for hit in indexes["by_stem"].get(stem, [])
                 if Path(hit["path"]).name.lower() != name.lower()]
    if stem_hits:
        result["status"] = "unknown"
        result["unknown_reason"] = "stem_match_extension_differs"
        result["candidates"] = [hit["path"] for hit in stem_hits]
    return result


def build_match_results(targets: list[dict], indexes: dict) -> list[dict]:
    """Match every target; old paths are matched once and memoized."""
    memo: dict[str, dict] = {}
    results = []
    for target in targets:
        key = f'{target["old_path_kind"]}\x00{target["old_path"]}'
        old_abs = resolve_old_path(target)
        cached = memo.get(key)
        if cached is None:
            cached = match_target(target, old_abs, indexes)
            memo[key] = cached
        merged = dict(cached)
        merged["old_path_exists_now"] = old_abs.exists()
        results.append(merged)
    return results


def summarize(results: list[dict], targets: list[dict]) -> dict:
    """Per-source-group matched/unmatched/ambiguous/unknown counts."""
    summary: dict[str, dict] = {}
    for target, result in zip(targets, results):
        group = target["source_group"]
        bucket = summary.setdefault(group, {
            "total": 0, "matched": 0, "unmatched": 0, "ambiguous": 0,
            "unknown": 0, "in_broken_findings": 0,
            "matched_broken": 0, "matched_exists": 0,
            "unique_old_paths": set(),
        })
        bucket["total"] += 1
        if target["in_broken_findings"]:
            bucket["in_broken_findings"] += 1
        bucket[result["status"]] += 1
        if result["status"] == "matched":
            if target["in_broken_findings"] or not result["old_path_exists_now"]:
                bucket["matched_broken"] += 1
            else:
                bucket["matched_exists"] += 1
        bucket["unique_old_paths"].add(target["old_path"])
    rendered = {}
    for group, bucket in summary.items():
        rendered[group] = {k: v for k, v in bucket.items()
                           if k != "unique_old_paths"}
        rendered[group]["unique_old_paths"] = len(bucket["unique_old_paths"])
    return rendered


def build_proposals(targets: list[dict], results: list[dict],
                    source_root: Path) -> list[dict]:
    """Dry-run relink proposals for uniquely basename-matched targets."""
    proposals = []
    for target, result in zip(targets, results):
        if result["status"] != "matched" or not result["candidates"]:
            continue
        rel_new = result["candidates"][0]
        absolute_new = (source_root / rel_new) if rel_new else source_root
        broken = target["in_broken_findings"] or not result["old_path_exists_now"]
        proposals.append({
            "schema": PROPOSAL_SCHEMA,
            "table": target["table"],
            "row_ref": target["row_ref"],
            "column": target["column"],
            "old_path": target["old_path"],
            "new_path": str(absolute_new),
            "match": "basename",
            "source_group": target["source_group"],
            "in_broken_findings": target["in_broken_findings"],
            "old_path_exists_now": result["old_path_exists_now"],
            "old_path_status": "broken" if broken else "exists",
            "apply_recommendation": broken,
            "new_path_rel_source": rel_new,
            "note": (
                "dry-run suggestion only; never executed by this tool; only "
                "apply rows with apply_recommendation=true (old path broken)"
            ),
        })
    return proposals


def write_proposals_jsonl(out_path: Path, proposals: list[dict]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        for proposal in proposals:
            handle.write(json.dumps(proposal, ensure_ascii=False))
            handle.write("\n")


def proposal_summary(proposals: list[dict]) -> dict:
    """Bounded accounting over the proposal lines (diagnostics for the report)."""
    by_new_path: dict[str, set[str]] = {}
    for proposal in proposals:
        by_new_path.setdefault(proposal["new_path"], set()).add(
            proposal["old_path"])
    multi_old = {path: olds for path, olds in by_new_path.items()
                 if len(olds) > 1}
    return {
        "lines": len(proposals),
        "unique_new_paths": len(by_new_path),
        "new_path_targets_with_multiple_old_paths": len(multi_old),
        "multi_old_path_note": (
            "A new_path claimed by several old_paths is expected when one "
            "artifact is referenced from more than one table (e.g. "
            "favorite_gifs.full_path stores the absolute form of the same "
            "export a candidate_gifs row stores relatively); every such "
            "mapping kept the same basename. These rows still need a human "
            "spot-check before any apply."
        ),
    }


def ambiguous_detail(targets: list[dict], results: list[dict], max_list: int,
                     detail_limit: int) -> list[dict]:
    """Bounded detail listing for ambiguous/unknown targets (diagnostics)."""
    detail = []
    for target, result in zip(targets, results):
        if result["status"] not in ("ambiguous", "unknown"):
            continue
        if len(detail) >= detail_limit:
            break
        detail.append({
            "status": result["status"],
            "unknown_reason": result["unknown_reason"],
            "source_group": target["source_group"],
            "row_ref": target["row_ref"],
            "column": target["column"],
            "old_path": target["old_path"],
            "candidate_count": len(result["candidates"]),
            "candidates": result["candidates"][:max_list],
            "preferred_new_path": result["preferred_new_path"],
            "disambiguation": result["disambiguation"],
        })
    return detail


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_relink(args: argparse.Namespace) -> str:
    as_of = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    sprint_root = resolve_sprint_root(args.sprint_root)
    source_root = Path(args.relink_source).expanduser().resolve()
    findings_path = (Path(args.findings) if args.findings
                     else sprint_root / DEFAULT_FINDINGS_SUBPATH)
    snapshot_path = (Path(args.snapshot) if args.snapshot
                     else sprint_root / "library_snapshot.db")
    if not findings_path.is_file():
        raise RelinkRefusal(
            f"findings JSON not found: {findings_path} -- run audit_data.py first"
        )
    manifest_status = verify_against_manifest(sprint_root, findings_path,
                                              "findings JSON")
    broken_targets, data_root, broken_refs = load_broken_findings(findings_path)

    targets = list(broken_targets)
    snapshot_status = "skipped"
    snapshot_manifest_status = "not_checked"
    if not args.skip_snapshot:
        if not snapshot_path.is_file():
            raise RelinkRefusal(f"snapshot DB not found: {snapshot_path}")
        snapshot_manifest_status = verify_against_manifest(
            sprint_root, snapshot_path, "snapshot DB")
        targets += load_snapshot_targets(snapshot_path, data_root, broken_refs)
        snapshot_status = "loaded"

    inventory = inventory_source(source_root)
    indexes = build_source_indexes(inventory)
    results = build_match_results(targets, indexes)
    match_summary = summarize(results, targets)
    proposals = build_proposals(targets, results, source_root)

    inventory_out = (Path(args.inventory_out) if args.inventory_out
                     else sprint_root / DEFAULT_INVENTORY_SUBPATH)
    proposals_out = (Path(args.proposals_out) if args.proposals_out
                     else sprint_root / DEFAULT_PROPOSALS_SUBPATH)
    extra_top = {
        "manifest_verification": {
            "findings": manifest_status,
            "snapshot_db": snapshot_manifest_status,
        },
        "snapshot_db_loaded": snapshot_status,
        "match_summary": match_summary,
        "proposal_summary": proposal_summary(proposals),
        "ambiguous_unknown_detail": ambiguous_detail(
            targets, results,
            args.max_ambiguous_list, args.ambiguous_detail_limit),
    }
    write_inventory_json(inventory_out, source_root, inventory, extra_top, as_of)
    write_proposals_jsonl(proposals_out, proposals)

    lines = [
        "W3 relink inventory -- DRY RUN ONLY (no UPDATE executed)",
        f"as_of: {as_of}",
        f"source: {source_root} "
        f"({len(inventory['files'])} files, {inventory['by_extension']})",
        "",
        "match rates by source group:",
    ]
    for group in sorted(match_summary):
        bucket = match_summary[group]
        lines.append(
            f"  {group}: total={bucket['total']} matched={bucket['matched']} "
            f"unmatched={bucket['unmatched']} ambiguous={bucket['ambiguous']} "
            f"unknown={bucket['unknown']} "
            f"(matched_broken={bucket['matched_broken']}, "
            f"matched_exists={bucket['matched_exists']})"
        )
    lines += [
        "",
        f"proposals written: {len(proposals)} -> {proposals_out}",
        f"inventory written: {inventory_out}",
        "No database, media file, or manifest was modified; --apply does not exist.",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_sprint_root_argument(parser)
    parser.add_argument("--relink-source", required=True,
                        help="directory being re-downloaded (e.g. D:\\data); "
                             "inventoried with metadata only")
    parser.add_argument("--findings", default=None,
                        help="audit findings JSON (default "
                             "<sprint-root>/w3/w3-audit-findings.json)")
    parser.add_argument("--snapshot", default=None,
                        help="W1 snapshot DB (default "
                             "<sprint-root>/library_snapshot.db)")
    parser.add_argument("--skip-snapshot", action="store_true",
                        help="only match the findings paths (no snapshot DB)")
    parser.add_argument("--inventory-out", default=None,
                        help="inventory JSON output path")
    parser.add_argument("--proposals-out", default=None,
                        help="proposals JSONL output path")
    parser.add_argument("--max-ambiguous-list", type=int, default=20,
                        help="candidate paths listed per ambiguous target")
    parser.add_argument("--ambiguous-detail-limit", type=int, default=1000,
                        help="maximum ambiguous/unknown entries kept in detail")
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = argv if argv is not None else sys.argv[1:]
    if "--apply" in raw_argv:
        # Rejected before argparse: this tool is dry-run only by design.
        print("error: this tool is dry-run only by design; there is no --apply mode",
              file=sys.stderr)
        return 2
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    try:
        print(run_relink(args))
    except RelinkRefusal as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

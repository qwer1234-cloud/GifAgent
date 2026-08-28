#!/usr/bin/env python3
"""Ingest the adaptive-pipeline export result JSONs into the sprint shared root.

W1 follow-up to T-1 ``inputs_manifest.json``: that manifest recorded
``export_results`` as ``absent`` because its glob only matched the top level of
``data/exports/``. This script (executor-owned, not coordinator code) makes the
missing **immutable text copies**:

- every ``result.json`` / ``*_result.json`` under ``--exports-root`` is copied
  with ``shutil.copy2`` into ``<sprint-root>/inputs/exports/<relative path>``
  preserving directory structure;
- media files (``.gif``/``.jpg``/``.mp4``/...) are **never** copied and never
  opened; any that end up in the copy candidate list are skipped and recorded
  under ``skipped_media``;
- copies are immutable: an existing target is never overwritten. Same content
  -> idempotent ``already_present``; different content -> fatal error (the
  source changed after a previous run; a re-copy would break immutability);
- a manifest ``<sprint-root>/inputs/exports_corpus_manifest.json`` records
  ``source_abs`` / ``copy_rel`` / ``sha256`` / ``as_of`` per file. The frozen
  coordinator ``inputs_manifest.json`` history is left untouched.

Usage:
    python scripts/token_sprint/ingest_export_corpus.py --sprint-root <path> \
        [--exports-root <abs>]

Exit codes: 0 success, 2 fatal input/immutability error.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from scripts.token_sprint.coordinator.common import (  # noqa: E402
    add_sprint_root_argument,
    atomic_write_text,
    resolve_sprint_root,
    sha256_file,
)

MANIFEST_SCHEMA = "gifagent.token_sprint.w1.exports_corpus_manifest.v1"
MANIFEST_NAME = "exports_corpus_manifest.json"
CORPUS_DIRNAME = "inputs/exports"

DEFAULT_EXPORTS_ROOT = Path("C:/Users/sunhao/Desktop/code/GifAgent/data/exports")

#: Media extensions that must never enter the corpus copies (bytes never read).
MEDIA_SUFFIXES = {
    ".gif", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff",
    ".mp4", ".webm", ".mov", ".mkv", ".avi", ".wmv", ".flv", ".m4v",
}

#: Result JSONs to ingest: ``result.json`` and ``*_result.json``.
RESULT_JSON_NAMES = ("result.json",)


class IngestError(RuntimeError):
    """Fatal corpus-ingest problem; surfaces as exit code 2."""


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _is_result_json(path: Path) -> bool:
    if path.suffix.lower() != ".json":
        return False
    return path.name.lower() in RESULT_JSON_NAMES or path.name.lower().endswith(
        "_result.json"
    )


def collect_candidates(exports_root: Path) -> tuple[list[Path], list[dict], int]:
    """Walk *exports_root*; return (json candidates, skipped_media records, media_seen count).

    Only file names/metadata are inspected -- no file contents are read here and
    media bytes are never opened.
    """
    if not exports_root.is_dir():
        raise IngestError(f"exports root does not exist or is not a directory: {exports_root}")

    candidates: list[Path] = []
    skipped_media: list[dict] = []
    media_seen = 0
    for path in sorted(exports_root.rglob("*"), key=lambda p: p.as_posix().lower()):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in MEDIA_SUFFIXES:
            media_seen += 1
            # Defensive: a media file whose name still looks like a result JSON
            # must be recorded, not silently dropped.
            if path.name.lower() in RESULT_JSON_NAMES or path.name.lower().endswith("_result.json"):
                skipped_media.append(
                    {
                        "source_abs": path.resolve().as_posix(),
                        "reason": "media_result_json_name",
                    }
                )
            continue
        if _is_result_json(path):
            candidates.append(path)
    return candidates, skipped_media, media_seen


def ingest_corpus(sprint_root: Path, exports_root: Path) -> dict:
    """Copy result JSONs immutably and build the corpus manifest."""
    candidates, skipped_media, media_seen = collect_candidates(exports_root)

    corpus_dir = sprint_root / "inputs" / "exports"
    as_of = _now_iso()
    files: list[dict] = []
    conflicts: list[str] = []

    for source in candidates:
        rel = source.relative_to(exports_root)
        target = corpus_dir / rel
        source_abs = source.resolve().as_posix()
        if target.exists():
            target_sha = sha256_file(target)
            source_sha = sha256_file(source)
            if target_sha != source_sha:
                conflicts.append(
                    f"immutable copy conflict: {target} exists with different content "
                    f"than source {source_abs} (target {target_sha[:12]} vs source {source_sha[:12]}); "
                    "refusing to overwrite"
                )
                files.append(
                    {
                        "source_abs": source_abs,
                        "copy_rel": None,
                        "sha256": None,
                        "as_of": as_of,
                        "status": "conflict",
                        "bytes": None,
                    }
                )
                continue
            files.append(
                {
                    "source_abs": source_abs,
                    "copy_rel": target.relative_to(sprint_root).as_posix(),
                    "sha256": target_sha,
                    "as_of": as_of,
                    "status": "already_present",
                    "bytes": target.stat().st_size,
                }
            )
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        files.append(
            {
                "source_abs": source_abs,
                "copy_rel": target.relative_to(sprint_root).as_posix(),
                "sha256": sha256_file(target),
                "as_of": as_of,
                "status": "copied",
                "bytes": target.stat().st_size,
            }
        )

    if conflicts:
        raise IngestError("\n".join(conflicts))

    copied = sum(1 for f in files if f["status"] == "copied")
    already = sum(1 for f in files if f["status"] == "already_present")
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "as_of": as_of,
        "exports_root_abs": exports_root.resolve().as_posix(),
        "corpus_dir_rel": CORPUS_DIRNAME,
        "counts": {
            "result_json_candidates": len(candidates),
            "copied": copied,
            "already_present": already,
            "skipped_media": len(skipped_media),
            "media_files_seen": media_seen,
        },
        "files": files,
        "skipped_media": skipped_media,
        "notes": [
            "Immutable text-only copies of data/exports result JSONs; media bytes never read or copied.",
            "Follow-up to T-1 inputs_manifest.json whose export_results glob matched only the top level.",
        ],
    }
    atomic_write_text(
        sprint_root / MANIFEST_NAME,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Copy data/exports result JSONs immutably into the sprint shared root."
    )
    add_sprint_root_argument(parser)
    parser.add_argument(
        "--exports-root",
        default=str(DEFAULT_EXPORTS_ROOT),
        help="absolute path to the exports directory (default: main checkout data/exports)",
    )
    args = parser.parse_args(argv)
    sprint_root = resolve_sprint_root(args.sprint_root)

    exports_root = Path(args.exports_root).expanduser()
    try:
        manifest = ingest_corpus(sprint_root, exports_root)
    except IngestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    counts = manifest["counts"]
    print(
        f"语料入库完成: candidates={counts['result_json_candidates']} "
        f"copied={counts['copied']} already_present={counts['already_present']} "
        f"skipped_media={counts['skipped_media']} media_seen={counts['media_files_seen']}"
    )
    print(sprint_root / MANIFEST_NAME)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

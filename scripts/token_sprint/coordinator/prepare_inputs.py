#!/usr/bin/env python3
"""Pin the production library path and copy immutable W3 inputs.

Coordinator-owned (design v4.1 Step 2):
- the production library is pinned by ``Path.resolve(strict=True)`` into
  ``source_db_abs`` (forward slashes) and hashed; it is **not** copied and
  **not** opened (W1 snapshots it later);
- W3 inputs (adaptive result JSON, batch checkpoint, export result JSONs) are
  copied as immutable copies into ``<sprint-root>/inputs/`` with per-file
  SHA-256 and an explicit ``copied``/``absent`` status;
- media files matching the export glob are skipped and listed under
  ``skipped_media`` -- their bytes are never read.

Usage:
    python scripts/token_sprint/coordinator/prepare_inputs.py \
        --sprint-root <path> \
        [--source-db <abs>] [--input-adaptive <abs>] [--input-checkpoint <abs>] \
        [--input-export-glob <abs-glob>]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path so we can import the coordinator package (same
# pattern as scripts/import_adaptive_candidates.py).
_project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from scripts.token_sprint.coordinator.common import (  # noqa: E402
    add_sprint_root_argument,
    atomic_write_text,
    resolve_sprint_root,
    sha256_file,
)

INPUTS_SCHEMA = "gifagent.token_sprint.inputs.v1"
INPUTS_MANIFEST_NAME = "inputs_manifest.json"

DEFAULT_SOURCE_DB = Path("C:/Users/sunhao/Desktop/code/GifAgent/dist/GifAgentUI/data/library.db")
DEFAULT_ADAPTIVE = Path("C:/Users/sunhao/Desktop/code/GifAgent/data/adaptive_test_result.json")
DEFAULT_CHECKPOINT = Path("C:/Users/sunhao/Desktop/code/GifAgent/data/batch_checkpoint.json")
DEFAULT_EXPORT_GLOB = "C:/Users/sunhao/Desktop/code/GifAgent/data/exports/*.json"

MEDIA_SUFFIXES = {
    ".gif", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff",
    ".mp4", ".webm", ".mov", ".mkv", ".avi", ".wmv",
}
ALLOWED_SUFFIXES = {".json", ".jsonl", ".db", ".sqlite", ".sqlite3", ".txt", ".md"}


class PrepareInputsError(RuntimeError):
    """Fatal input-collection problem; surfaces as exit code 2."""


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _posix(path: Path) -> str:
    return path.as_posix()


def _resolve_source(raw: str | Path, *, strict: bool) -> Path:
    return Path(raw).expanduser().resolve(strict=strict)


def _glob_matches(pattern: str) -> list[Path]:
    pattern_path = Path(pattern).expanduser()
    return sorted(pattern_path.parent.glob(pattern_path.name))


def collect_inputs(args: argparse.Namespace) -> dict:
    try:
        source_db = _resolve_source(args.source_db, strict=True)
    except OSError as exc:
        raise PrepareInputsError(f"source db cannot be resolved (strict): {args.source_db}: {exc}") from exc
    if not source_db.is_file():
        raise PrepareInputsError(f"source db does not exist: {source_db}")
    if source_db.suffix.lower() in MEDIA_SUFFIXES:
        raise PrepareInputsError(f"refusing to treat a media file as the source db: {source_db}")

    try:
        source_db_sha = sha256_file(source_db)
    except OSError as exc:
        raise PrepareInputsError(f"cannot hash source db (locked?): {source_db}: {exc}") from exc

    inputs_dir = Path(args.sprint_root) / "inputs"

    specs: list[tuple[str, Path | str, bool, bool]] = [
        # (key, source (Path for single file, str glob pattern), required, is_glob)
        ("adaptive_test_result", Path(args.input_adaptive), False, False),
        ("batch_checkpoint", Path(args.input_checkpoint), False, False),
        ("export_results", args.input_export_glob, False, True),
    ]

    files: list[dict] = []
    skipped_media: list[dict] = []
    planned: list[tuple[str, Path]] = []  # (basename, resolved source) for collision check

    for key, source, required, is_glob in specs:
        if is_glob:
            matches = _glob_matches(str(source))
            copied_any = False
            for match in matches:
                suffix = match.suffix.lower()
                if suffix in MEDIA_SUFFIXES:
                    skipped_media.append({"key": key, "source_abs": _posix(match), "reason": "media"})
                    continue
                if not match.is_file() or suffix not in ALLOWED_SUFFIXES:
                    skipped_media.append({"key": key, "source_abs": _posix(match), "reason": "disallowed_type"})
                    continue
                files.append(
                    {
                        "key": key,
                        "source_abs": _posix(match.resolve()),
                        "copy_rel": f"inputs/{match.name}",
                        "sha256": None,  # filled after copy
                        "required": required,
                        "status": "copied",
                    }
                )
                planned.append((match.name, match))
                copied_any = True
            if not copied_any and not matches:
                # Windows cannot resolve() a path containing wildcards (WinError 123),
                # so resolve only the parent directory and keep the pattern textual.
                pattern_norm = Path(str(source)).expanduser()
                absent_source = pattern_norm.parent.resolve(strict=False) / pattern_norm.name
                files.append(
                    {
                        "key": key,
                        "source_abs": _posix(absent_source),
                        "copy_rel": None,
                        "sha256": None,
                        "required": required,
                        "status": "absent",
                    }
                )
            continue

        source = Path(source)
        try:
            resolved = _resolve_source(source, strict=True)
        except OSError:
            resolved = None
        if resolved is None or not resolved.is_file():
            if required:
                raise PrepareInputsError(f"required input is missing: {source}")
            files.append(
                {
                    "key": key,
                    "source_abs": _posix(source.resolve(strict=False)),
                    "copy_rel": None,
                    "sha256": None,
                    "required": required,
                    "status": "absent",
                }
            )
            continue
        if resolved.suffix.lower() in MEDIA_SUFFIXES:
            raise PrepareInputsError(f"refusing to copy a media file as input: {resolved}")
        if resolved.suffix.lower() not in ALLOWED_SUFFIXES:
            raise PrepareInputsError(f"input is not a text/JSON/SQLite file: {resolved}")
        files.append(
            {
                "key": key,
                "source_abs": _posix(resolved),
                "copy_rel": f"inputs/{resolved.name}",
                "sha256": None,
                "required": required,
                "status": "copied",
            }
        )
        planned.append((resolved.name, resolved))

    # Two different sources mapping to the same basename would silently
    # overwrite each other in inputs/ -- fail closed instead.
    seen: dict[str, Path] = {}
    for basename, source in planned:
        if basename in seen and seen[basename] != source:
            raise PrepareInputsError(
                f"basename collision in inputs/: {basename} from {seen[basename]} and {source}"
            )
        seen[basename] = source

    inputs_dir.mkdir(parents=True, exist_ok=True)
    for entry in files:
        if entry["status"] != "copied":
            continue
        source = Path(entry["source_abs"])
        target = inputs_dir / Path(entry["copy_rel"]).name
        try:
            shutil.copy2(source, target)
            entry["sha256"] = sha256_file(target)
        except OSError as exc:
            raise PrepareInputsError(f"cannot copy {source} -> {target}: {exc}") from exc

    return {
        "schema": INPUTS_SCHEMA,
        "as_of": _now_iso(),
        "source_db_abs": _posix(source_db),
        "source_db_sha256": source_db_sha,
        "files": files,
        "skipped_media": skipped_media,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_sprint_root_argument(parser)
    parser.add_argument("--source-db", default=str(DEFAULT_SOURCE_DB))
    parser.add_argument("--input-adaptive", default=str(DEFAULT_ADAPTIVE))
    parser.add_argument("--input-checkpoint", default=str(DEFAULT_CHECKPOINT))
    parser.add_argument("--input-export-glob", default=DEFAULT_EXPORT_GLOB)
    args = parser.parse_args(argv)

    sprint_root = resolve_sprint_root(args.sprint_root)
    args.sprint_root = sprint_root

    try:
        manifest = collect_inputs(args)
    except PrepareInputsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    atomic_write_text(
        sprint_root / INPUTS_MANIFEST_NAME,
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
    )
    print(sprint_root / INPUTS_MANIFEST_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())

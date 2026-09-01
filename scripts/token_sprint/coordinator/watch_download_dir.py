#!/usr/bin/env python3
"""One monitoring cycle for a continuously-downloading media directory.

Coordinator control-plane tool (single writer: coordinator session). Each
invocation performs ONE cycle:

1. Inventory the watch root (metadata only: rel path / size / mtime).
   Media bytes are NEVER read -- no opens, no hashing of media files.
2. Extract newly-complete, password-free archives into a staging folder
   (``<_extracted>/<archive-stem>/``), guarded against path traversal.
   Multi-volume sets are only extracted when complete and download-quiet.
3. Re-compute relink match counts: broken/candidate library paths (from the
   canonical W3 findings + the frozen candidate-paths sidecar) matched by
   basename against the watch-root file listing.
4. Atomically update ``w3/monitor/state.json`` and append one line to
   ``w3/monitor/cycles.log`` (control-plane telemetry; no receipts).

The candidate set is immutable during the sprint (production database is
read-only by design), so ``w3/monitor/candidate_paths.json`` is a one-time
sidecar export. Required CLI options: a sprint root (see
common.add_sprint_root_argument), a watch root directory, a per-cycle
extraction cap, a download quiet window in seconds, and a no-extract switch.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from scripts.token_sprint.coordinator.common import (  # noqa: E402
    add_sprint_root_argument,
    atomic_write_text,
    resolve_sprint_root,
)

MEDIA_SUFFIXES = {".gif", ".jpg", ".jpeg", ".png", ".webp", ".mp4", ".webm", ".mov", ".mkv", ".avi", ".wmv"}
ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z"}
PART_RE = re.compile(r"^(?P<stem>.+)\.(?:(?P<part>part\d+)\.(?P<ext>rar)|(?P<num>\d{3}))$", re.IGNORECASE)
EXTRACT_SUBDIR = "_extracted"
MONITOR_DIRNAME = "w3/monitor"
FINDINGS_REL = "w3/w3-audit-findings.json"
CANDIDATE_SIDECAR_REL = "w3/monitor/candidate_paths.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def list_watch_root(watch_root: Path) -> tuple[list[dict], dict[str, int]]:
    files: list[dict] = []
    by_suffix: dict[str, int] = {}
    for dirpath, _dirnames, filenames in os.walk(watch_root):
        for name in filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(watch_root).as_posix()
            try:
                st = full.stat()
            except OSError:
                continue
            suffix = full.suffix.lower()
            by_suffix[suffix] = by_suffix.get(suffix, 0) + 1
            files.append({"rel": rel, "size": st.st_size, "mtime": st.st_mtime})
    return files, by_suffix


def _quiet(path: Path, quiet_window_sec: float, now: float) -> bool:
    try:
        return (now - path.stat().st_mtime) >= quiet_window_sec
    except OSError:
        return False


def find_ready_archives(files: list[dict], watch_root: Path, quiet_window_sec: float) -> tuple[list[Path], list[dict]]:
    """Return (extractable archive paths, tracked-but-not-ready set summaries)."""
    now = time.time()
    ready: list[Path] = []
    pending: dict[str, dict] = {}

    for f in files:
        rel = f["rel"]
        name = Path(rel).name
        suffix = Path(rel).suffix.lower()
        if suffix in ARCHIVE_SUFFIXES and not PART_RE.match(name):
            pending.setdefault(str(Path(rel).with_suffix("")), {"parts": [], "kind": "single"})["parts"].append(rel)
            continue
        m = PART_RE.match(name)
        if not m:
            continue
        stem = str((watch_root / rel).with_name(m.group("stem")))
        kind = "partN" if m.group("part") else "num"
        pending.setdefault(stem, {"parts": [], "kind": kind})["parts"].append(rel)

    for stem, info in list(pending.items()):
        parts = sorted(set(info["parts"]))
        if info["kind"] == "partN":
            numbered = sorted(int(Path(p).name.lower().split(".part")[1].split(".")[0]) for p in parts)
            complete = numbered == list(range(1, len(numbered) + 1)) and any(p.lower().endswith(".part1.rar") for p in parts)
            entry = next((p for p in parts if p.lower().endswith(".part1.rar")), None)
        elif info["kind"] == "num":
            numbered = sorted(int(Path(p).suffix.lstrip(".")) for p in parts if Path(p).suffix.lstrip(".").isdigit())
            complete = numbered == list(range(1, len(numbered) + 1)) and numbered[0] == 1
            entry = parts[0]
        else:
            complete = len(parts) == 1
            entry = parts[0]
        all_quiet = all(_quiet(watch_root / p, quiet_window_sec, now) for p in parts)
        if complete and all_quiet and entry is not None:
            ready.append(watch_root / entry)
            del pending[stem]
        else:
            info["parts"] = len(parts)

    return ready, [
        {"stem": stem, "kind": info["kind"], "parts": info["parts"]} for stem, info in sorted(pending.items())
    ]


def _find_extractor() -> tuple[str | None, str]:
    for exe in ("7z", "7za"):
        found = shutil.which(exe)
        if found:
            return found, "7z"
    for candidate in (r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if Path(candidate).is_file():
            return candidate, "7z"
    for candidate in (r"C:\Program Files\WinRAR\UnRAR.exe", r"C:\Program Files (x86)\WinRAR\UnRAR.exe"):
        if Path(candidate).is_file():
            return candidate, "unrar"
    return None, "none"


def _entry_names_safe(extractor: str, exe: str, archive: Path) -> tuple[bool, bool]:
    """Return (safe_to_extract, listable). Not listable usually means a
    multi-volume set is still missing parts -- retry next cycle."""
    if extractor == "unrar":
        cmd = [exe, "lb", "-p", str(archive)]
    else:
        # -sccUTF-8: 7z otherwise echoes paths in the console codepage (GBK on
        # zh-CN Windows), which mangles non-ASCII self-paths and defeats the
        # self-entry exemption below.
        cmd = [exe, "l", "-slt", "-sccUTF-8", "-p", str(archive)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace", shell=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, False
    if proc.returncode != 0:
        return False, False
    names: list[str] = []
    for line in proc.stdout.splitlines():
        if extractor == "unrar":
            names.append(line.strip())
        elif line.startswith("Path = "):
            names.append(line[len("Path = "):].strip())
    # 7z -slt lists the archive itself (absolute path with drive letter) as
    # the first entry; it is not an archive member -- exempt exactly that.
    archive_variants = {str(archive).lower(), archive.resolve().as_posix().lower()}
    for name in names:
        if name.lower() in archive_variants:
            continue
        norm = name.replace("\\", "/")
        if ".." in norm.split("/") or norm.startswith("/") or (len(norm) > 1 and norm[1] == ":"):
            return False, True
    return True, True


def extract_archive(exe: str, extractor: str, archive: Path, watch_root: Path) -> tuple[bool, str, int]:
    safe, listable = _entry_names_safe(extractor, exe, archive)
    if not listable:
        return False, "list_failed_missing_volumes_will_retry", 0
    if not safe:
        return False, "unsafe_paths_refused", 0
    rel_stem = archive.relative_to(watch_root).with_suffix("")
    target = watch_root / EXTRACT_SUBDIR / rel_stem
    # Idempotence: a non-empty target means this archive was already
    # extracted in an earlier cycle -- never re-extract over it.
    if target.is_dir() and any(target.iterdir()):
        return False, "already_extracted", 0
    target.mkdir(parents=True, exist_ok=True)
    # Extract with cwd=target and a relative destination: no path is ever
    # concatenated into a command argument; every argument is a fixed literal
    # except the archive path, which is passed as its own argv entry.
    if extractor == "unrar":
        cmd = [exe, "x", "-y", "-p", str(archive), "."]
    else:
        cmd = [exe, "x", "-y", "-p", "-o.", str(archive)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=900,
            encoding="utf-8",
            errors="replace",
            shell=False,
            cwd=str(target),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"extract_error:{exc}", 0
    if proc.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        return False, f"extract_failed_rc={proc.returncode}", 0
    count = sum(1 for child in target.rglob("*") if child.is_file())
    return True, "ok", count


def load_reference_paths(sprint_root: Path) -> tuple[list[str], list[str], str | None]:
    """Broken paths from the canonical W3 findings + frozen candidate paths sidecar."""
    broken: list[str] = []
    findings_path = sprint_root / FINDINGS_REL
    if findings_path.is_file():
        data = json.loads(findings_path.read_text(encoding="utf-8"))

        def walk(obj):
            if isinstance(obj, dict):
                for value in obj.values():
                    walk(value)
            elif isinstance(obj, list):
                for value in obj:
                    walk(value)
            elif isinstance(obj, str):
                low = obj.lower()
                if any(low.endswith(ext) for ext in MEDIA_SUFFIXES) and ("\\" in obj or "/" in obj):
                    broken.append(obj)

        walk(data)

    candidates: list[str] = []
    note = None
    sidecar = sprint_root / CANDIDATE_SIDECAR_REL
    if sidecar.is_file():
        doc = json.loads(sidecar.read_text(encoding="utf-8"))
        candidates = [p for p in doc.get("paths", []) if p]
    else:
        note = f"candidate sidecar missing: {CANDIDATE_SIDECAR_REL}"
    return broken, candidates, note


def match_counts(broken: list[str], candidates: list[str], watch_names: set[str]) -> dict:
    def match(paths: list[str]) -> dict:
        matched = unmatched = 0
        for raw in paths:
            base = Path(raw.replace("\\", "/")).name.lower()
            if base in watch_names:
                matched += 1
            else:
                unmatched += 1
        return {"total": len(paths), "matched_by_basename": matched, "unmatched": unmatched}

    return {"broken_paths": match(broken), "library_candidate_paths": match(candidates)}


def run_cycle(sprint_root: Path, watch_root: Path, max_extract: int, quiet_window_sec: float, no_extract: bool) -> dict:
    files, by_suffix = list_watch_root(watch_root)
    watch_names = {Path(f["rel"]).name.lower() for f in files}

    extracted: list[dict] = []
    skipped_archives: list[dict] = []
    ready, pending_sets = find_ready_archives(files, watch_root, quiet_window_sec)
    if no_extract:
        ready = []
    else:
        exe, extractor = _find_extractor()
        if exe is None:
            skipped_archives.append({"reason": "no_extractor_available", "ready_count": len(ready)})
            ready = []
        else:
            for archive in ready[: max(0, max_extract)]:
                ok, reason, count = extract_archive(exe, extractor, archive, watch_root)
                rel = archive.relative_to(watch_root).as_posix()
                if ok:
                    extracted.append({"archive": rel, "extractor": extractor, "files": count})
                else:
                    skipped_archives.append({"archive": rel, "reason": reason})

    if extracted:
        files, by_suffix = list_watch_root(watch_root)
        watch_names = {Path(f["rel"]).name.lower() for f in files}

    broken, candidates, note = load_reference_paths(sprint_root)
    matches = match_counts(broken, candidates, watch_names)

    media_total = sum(n for s, n in by_suffix.items() if s in MEDIA_SUFFIXES)
    summary = {
        "schema": "gifagent.token_sprint.monitor_cycle.v1",
        "as_of": _now_iso(),
        "watch_root": str(watch_root),
        "files_total": len(files),
        "media_files": media_total,
        "suffix_counts": {k: v for k, v in sorted(by_suffix.items()) if k in MEDIA_SUFFIXES or k in ARCHIVE_SUFFIXES},
        "archives_ready": len(ready),
        "archives_pending_incomplete": len(pending_sets),
        "extracted_this_cycle": extracted,
        "skipped_archives": skipped_archives,
        "matches": matches,
        "note": note,
    }

    monitor_dir = sprint_root / MONITOR_DIRNAME
    monitor_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        monitor_dir / "state.json",
        json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
    )
    log_line = (
        f"| {summary['as_of']} | files={summary['files_total']} media={media_total} "
        f"| extracted={len(extracted)} "
        f"| broken_matched={matches['broken_paths']['matched_by_basename']}/{matches['broken_paths']['total']} "
        f"| candidates_matched={matches['library_candidate_paths']['matched_by_basename']}/{matches['library_candidate_paths']['total']} |\n"
    )
    with open(monitor_dir / "cycles.log", "a", encoding="utf-8") as handle:
        handle.write(log_line)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one download-directory monitoring cycle.")
    add_sprint_root_argument(parser)
    parser.add_argument("--watch-root", required=True)
    parser.add_argument("--max-extract", type=int, default=5)
    parser.add_argument("--quiet-window-sec", type=float, default=120.0)
    parser.add_argument("--no-extract", action="store_true")
    args = parser.parse_args(argv)

    sprint_root = resolve_sprint_root(args.sprint_root)
    watch_root = Path(args.watch_root).expanduser().resolve()
    if not watch_root.is_dir():
        print(f"error: watch root does not exist: {watch_root}", file=sys.stderr)
        return 2

    summary = run_cycle(sprint_root, watch_root, args.max_extract, args.quiet_window_sec, args.no_extract)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

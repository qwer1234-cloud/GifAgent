"""Desktop synchronization for Favorite GIFs and PotPlayer PBF bookmarks.

The service performs an idempotent, copy-only reconciliation:

- Favorite GIF rows in ``library.db`` whose existing source file lives under
  the adaptive export root are copied into a flat Favorite destination
  directory (original basename).
- Every ``.pbf`` file found recursively under the adaptive export root is
  copied into a flat PBF destination directory (original basename).
- Nothing is ever deleted from the source or destination, and per-file
  problems (missing sources, copy errors, basename collisions) are collected
  into a structured report instead of aborting the whole run.

An unchanged destination is skipped without copying; a changed source is
updated.  Copies use :func:`shutil.copy2` followed by an atomic
``os.replace`` of the temporary file so a destination is never left
half-written on Windows.

All three root paths can be overridden through clearly named environment
variables (see :func:`get_config`).
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENV_LIBRARY_DB = "GIFAGENT_LIBRARY_DB"
ENV_ADAPTIVE_SOURCE_ROOT = "GIFAGENT_ADAPTIVE_SOURCE_ROOT"
ENV_FAVORITE_GIF_DEST = "GIFAGENT_FAVORITE_GIF_DEST"
ENV_PBF_DEST = "GIFAGENT_PBF_DEST"


def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def get_config() -> dict[str, Path]:
    """Resolve sync roots from environment overrides with portable defaults.

    Defaults resolve to the requested production layout:
    ``data/exports/adaptive_test`` (CWD-relative, i.e. the packaged EXE
    directory at runtime) and ``<home>/Desktop/entertainment/...``.
    """
    return {
        "library_db": Path(
            os.environ.get(ENV_LIBRARY_DB, "data/library.db")
        ),
        "source_root": Path(
            os.environ.get(
                ENV_ADAPTIVE_SOURCE_ROOT, "data/exports/adaptive_test"
            )
        ),
        "favorite_dest": Path(
            os.environ.get(
                ENV_FAVORITE_GIF_DEST,
                _home() / "Desktop" / "entertainment" / "favorite_gifs",
            )
        ),
        "pbf_dest": Path(
            os.environ.get(
                ENV_PBF_DEST,
                _home() / "Desktop" / "entertainment" / "bookmarks" / "PBF",
            )
        ),
    }


def _norm(path: str | Path) -> str:
    """Absolute, normalized, case-normalized path key (Windows-friendly)."""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def _is_inside(path: str | Path, root: str | Path) -> bool:
    p = _norm(path)
    r = _norm(root)
    if p == r:
        return True
    prefix = r.rstrip("\\/") + os.sep
    return p.startswith(prefix)


def _relocate_favorite_source(path: Path, source_root: Path) -> Path | None:
    """Resolve a stale absolute Favorite path under the configured root.

    Packaged data can retain absolute paths from the source checkout even
    after the export tree has moved under ``dist/GifAgentUI``.  Preserve the
    path below the last matching source-root directory name and only accept a
    candidate that actually exists under the configured root.
    """
    root_name = source_root.name.casefold()
    for index in range(len(path.parts) - 1, -1, -1):
        if path.parts[index].casefold() != root_name:
            continue
        candidate = source_root.joinpath(*path.parts[index + 1 :])
        if candidate.is_file() and _is_inside(candidate, source_root):
            return candidate
    return None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_file(path: str | Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Report model
# ---------------------------------------------------------------------------


@dataclass
class SyncEntry:
    source: str
    destination: str = ""
    reason: str = ""
    conflicted: bool = False

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "destination": self.destination,
            "reason": self.reason,
            "conflicted": self.conflicted,
        }


@dataclass
class DesktopSyncReport:
    started_at: str = ""
    finished_at: str = ""
    gifs: dict[str, list[SyncEntry]] = field(default_factory=dict)
    pbfs: dict[str, list[SyncEntry]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for section in (self.gifs, self.pbfs):
            section.setdefault("copied", [])
            section.setdefault("updated", [])
            section.setdefault("skipped", [])
            section.setdefault("missing", [])
            section.setdefault("conflicts", [])
            section.setdefault("errors", [])

    def add(self, section: dict, kind: str, entry: SyncEntry) -> None:
        section.setdefault(kind, []).append(entry)

    @property
    def gif_summary(self) -> dict[str, int]:
        return {kind: len(items) for kind, items in self.gifs.items()}

    @property
    def pbf_summary(self) -> dict[str, int]:
        return {kind: len(items) for kind, items in self.pbfs.items()}

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "gifs": {k: [e.to_dict() for e in v] for k, v in self.gifs.items()},
            "pbfs": {k: [e.to_dict() for e in v] for k, v in self.pbfs.items()},
            "gif_summary": self.gif_summary,
            "pbf_summary": self.pbf_summary,
        }

    def log_line(self) -> str:
        g = self.gif_summary
        p = self.pbf_summary
        return (
            "[desktop-sync] "
            f"gifs copied={g['copied']} updated={g['updated']} "
            f"skipped={g['skipped']} missing={g['missing']} "
            f"conflicts={g['conflicts']} errors={g['errors']} | "
            f"pbfs copied={p['copied']} updated={p['updated']} "
            f"skipped={p['skipped']} missing={p['missing']} "
            f"conflicts={p['conflicts']} errors={p['errors']}"
        )


# ---------------------------------------------------------------------------
# Synchronization core
# ---------------------------------------------------------------------------


def _create_report() -> DesktopSyncReport:
    report = DesktopSyncReport(started_at=_iso_now())
    return report


def _atomic_copy2(source: str | Path, destination: str | Path) -> None:
    """Copy preserving metadata and make the final replacement atomic."""
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{dest.name}.sync-", suffix=".tmp", dir=dest.parent
    )
    try:
        os.close(fd)
        shutil.copy2(source, temporary)
        os.replace(temporary, dest)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _hashes_equal(source: str | Path, destination: str | Path) -> bool:
    try:
        return _sha256_file(source) == _sha256_file(destination)
    except OSError:
        return False


def _needs_copy(source: str | Path, destination: str | Path) -> bool:
    """Fast path on size+mtime; fall back to content hashing when ambiguous."""
    try:
        s_stat = os.stat(source)
        d_stat = os.stat(destination)
    except OSError:
        return True
    if s_stat.st_size != d_stat.st_size:
        return True
    if abs(s_stat.st_mtime - d_stat.st_mtime) < 0.0001:
        return False
    return not _hashes_equal(source, destination)


def _copy_single_file(
    report: DesktopSyncReport,
    section: dict,
    src: Path,
    dest_dir: Path,
) -> None:
    """Copy/skip/update one preflighted source into the flat destination."""
    destination = dest_dir / src.name
    try:
        if destination.exists():
            if destination.is_dir():
                raise OSError("destination exists and is a directory")
            if _needs_copy(src, destination):
                _atomic_copy2(src, destination)
                report.add(
                    section,
                    "updated",
                    SyncEntry(str(src), str(destination)),
                )
            else:
                report.add(
                    section,
                    "skipped",
                    SyncEntry(str(src), str(destination), reason="unchanged"),
                )
        else:
            _atomic_copy2(src, destination)
            report.add(
                section, "copied", SyncEntry(str(src), str(destination))
            )
    except Exception as exc:
        report.add(
            section,
            "errors",
            SyncEntry(
                str(src),
                str(destination),
                reason=f"{type(exc).__name__}: {exc}",
            ),
        )


def _sync_files_preflight(
    report: DesktopSyncReport,
    section: dict,
    sources: list[Path],
    dest_dir: Path,
) -> None:
    """Detect case-insensitive basename collisions before any copy.

    Every source in a multi-source group is marked conflicted and none of the
    group is copied, so traversal/SQL order can never choose an arbitrary
    winner.  Unrelated files are copied normally afterwards.
    """
    groups: dict[str, list[Path]] = {}
    for src in sorted(sources):
        if not src.is_file():
            report.add(
                section, "missing", SyncEntry(str(src), reason="source not found")
            )
            continue
        groups.setdefault(os.path.normcase(src.name), []).append(src)

    for key, group in groups.items():
        distinct = {_norm(src) for src in group}
        if len(distinct) > 1:
            for src in group:
                report.add(
                    section,
                    "conflicts",
                    SyncEntry(
                        str(src),
                        str(dest_dir / src.name),
                        reason=(
                            "case-insensitive basename collision with "
                            + ", ".join(
                                sorted(
                                    str(other)
                                    for other in group
                                    if _norm(other) != _norm(src)
                                )
                            )
                        ),
                        conflicted=True,
                    ),
                )
            continue
        _copy_single_file(report, section, group[0], dest_dir)


def _sync_favorites(
    report: DesktopSyncReport,
    conn: sqlite3.Connection,
    source_root: Path,
    favorite_dest: Path,
) -> None:
    try:
        rows = conn.execute(
            "SELECT candidate_id, full_path FROM favorite_gifs ORDER BY full_path"
        ).fetchall()
    except sqlite3.Error as exc:
        raise sqlite3.OperationalError(
            f"favorite_gifs query failed: {type(exc).__name__}: {exc}"
        ) from exc

    sources: list[Path] = []
    for row in rows:
        full_path = str(row["full_path"])
        src = Path(full_path)
        if not (src.is_file() and _is_inside(src, source_root)):
            relocated = _relocate_favorite_source(src, source_root)
            if relocated is not None:
                src = relocated
            elif src.is_file():
                report.add(
                    report.gifs,
                    "missing",
                    SyncEntry(
                        full_path,
                        reason="source outside adaptive export root",
                    ),
                )
                continue
            else:
                report.add(
                    report.gifs,
                    "missing",
                    SyncEntry(full_path, reason="source not found"),
                )
                continue
        sources.append(src)
    _sync_files_preflight(report, report.gifs, sources, favorite_dest)


def _validate_library_db(conn: sqlite3.Connection) -> None:
    """Raise if the library database cannot serve the Favorite query."""
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type='table' AND name='favorite_gifs'"
        ).fetchone()
    except sqlite3.Error as exc:
        raise sqlite3.DatabaseError(
            f"library database unreadable: {type(exc).__name__}: {exc}"
        ) from exc
    if row is None:
        raise sqlite3.DatabaseError(
            "library database has no favorite_gifs table"
        )


def _sync_pbfs(
    report: DesktopSyncReport,
    source_root: Path,
    pbf_dest: Path,
) -> None:
    if not source_root.is_dir():
        report.add(
            report.pbfs,
            "errors",
            SyncEntry(
                str(source_root),
                reason="adaptive source root not found",
            ),
        )
        return
    sources: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(source_root):
        for filename in sorted(filenames):
            if not filename.lower().endswith(".pbf"):
                continue
            sources.append(Path(dirpath) / filename)
    _sync_files_preflight(report, report.pbfs, sources, pbf_dest)


def run_reconciliation(
    library_db: str | Path | None = None,
    source_root: str | Path | None = None,
    favorite_dest: str | Path | None = None,
    pbf_dest: str | Path | None = None,
) -> DesktopSyncReport:
    """Run one full Favorite-GIF + PBF reconciliation and return a report.

    Only top-level configuration failures (missing library DB, unreadable
    database) raise; per-file problems are collected in the report.
    """
    config = get_config()
    db_path = Path(library_db or config["library_db"])
    root = Path(source_root or config["source_root"])
    fav_dest = Path(favorite_dest or config["favorite_dest"])
    pbf_dest = Path(pbf_dest or config["pbf_dest"])

    if not db_path.is_file():
        raise FileNotFoundError(f"Library database not found: {db_path}")

    report = _create_report()
    fav_dest.mkdir(parents=True, exist_ok=True)
    pbf_dest.mkdir(parents=True, exist_ok=True)
    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        _validate_library_db(conn)
        _sync_favorites(report, conn, root, fav_dest)
        _sync_pbfs(report, root, pbf_dest)
    finally:
        if conn is not None:
            conn.close()
    report.finished_at = _iso_now()
    return report


# ---------------------------------------------------------------------------
# Serialized, coalescing background scheduler
# ---------------------------------------------------------------------------


class DesktopSyncScheduler:
    """Background sync runner that serializes runs and coalesces triggers."""

    def __init__(self, run: Callable[[], DesktopSyncReport] | None = None) -> None:
        self._run_callback = run or (lambda: run_reconciliation())
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._pending = False
        self._active = False
        self._thread: threading.Thread | None = None

    def request_sync(self) -> None:
        """Mark one pending reconciliation.  Never raises, never blocks."""
        with self._wake:
            self._pending = True
            self._wake.notify_all()

    def start(self, stop_event: threading.Event) -> threading.Thread:
        """Start the daemon loop; a second call is a no-op."""
        with self._lock:
            if self._thread is not None:
                return self._thread
            thread = threading.Thread(
                target=self._run_loop,
                args=(stop_event,),
                name="desktop-export-sync",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return thread

    def stop(self, stop_event: threading.Event | None = None) -> None:
        with self._lock:
            thread = self._thread
            self._thread = None
        if stop_event is not None:
            stop_event.set()
        if thread is not None:
            thread.join(timeout=3.0)

    def wait_until_idle(self, timeout: float = 3600.0) -> None:
        """Block until no run is active and no follow-up is pending.

        Used by queue workers at shutdown so a daemon process cannot exit in
        the middle of an atomic copy of the final requested reconciliation.
        ``_pending`` is consumed and ``_active`` is set atomically under the
        same condition lock, so a waiter can never observe both flags false
        between consumption and callback start.
        """
        deadline = time.monotonic() + timeout
        with self._wake:
            while (self._active or self._pending) and time.monotonic() < deadline:
                self._wake.wait(timeout=0.25)

    def _log_result(self, report: DesktopSyncReport | None, exc: BaseException | None) -> None:
        if exc is not None:
            logging.warning(
                "[desktop-sync] background reconciliation failed: "
                "%s: %s",
                type(exc).__name__,
                exc,
            )
        elif report is not None:
            logging.info("%s", report.log_line())

    def _run_loop(self, stop_event: threading.Event) -> None:
        while True:
            with self._wake:
                while not self._pending and not stop_event.is_set():
                    self._wake.wait(timeout=0.5)
                if not self._pending and stop_event.is_set():
                    return
                # Consume the trigger and mark the run active under the same
                # condition lock so no waiter can race between the two.
                self._pending = False
                self._active = True
            report = None
            error = None
            try:
                report = self._run_callback()
            except Exception as exc:
                error = exc
            finally:
                with self._wake:
                    self._active = False
                    self._wake.notify_all()
            self._log_result(report, error)


_default_scheduler: DesktopSyncScheduler | None = None
_default_scheduler_lock = threading.Lock()


def _get_default_scheduler() -> DesktopSyncScheduler:
    global _default_scheduler
    with _default_scheduler_lock:
        if _default_scheduler is None:
            _default_scheduler = DesktopSyncScheduler()
        return _default_scheduler


def request_background_sync() -> None:
    """Schedule a full reconciliation after folder/job completion."""
    _get_default_scheduler().request_sync()


def wait_until_idle(timeout: float = 3600.0) -> None:
    """Wait for the shared scheduler to finish its final requested run."""
    _get_default_scheduler().wait_until_idle(timeout)


def start_background_sync(
    stop_event: threading.Event,
    run: Callable[[], DesktopSyncReport] | None = None,
) -> threading.Thread | None:
    """Start the shared background scheduler loop (launcher startup)."""
    scheduler = DesktopSyncScheduler(run) if run is not None else _get_default_scheduler()
    return scheduler.start(stop_event)


def stop_background_sync(stop_event: threading.Event | None = None) -> None:
    _get_default_scheduler().stop(stop_event)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="desktop-export-sync",
        description=(
            "One-time reconciliation: Favorite GIFs + PBF bookmarks copied "
            "flat into the configured desktop directories."
        ),
    )
    parser.add_argument(
        "--library-db",
        help=f"Library SQLite database (default: {ENV_LIBRARY_DB} or data/library.db)",
    )
    parser.add_argument(
        "--source-root",
        help=f"Adaptive export root (default: {ENV_ADAPTIVE_SOURCE_ROOT} or data/exports/adaptive_test)",
    )
    parser.add_argument(
        "--favorite-dest",
        help=f"Flat Favorite GIF destination (default: {ENV_FAVORITE_GIF_DEST} or ~/Desktop/entertainment/favorite_gifs)",
    )
    parser.add_argument(
        "--pbf-dest",
        help=f"Flat PBF destination (default: {ENV_PBF_DEST} or ~/Desktop/entertainment/bookmarks/PBF)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full structured report as JSON",
    )
    args = parser.parse_args(argv)

    try:
        report = run_reconciliation(
            library_db=args.library_db,
            source_root=args.source_root,
            favorite_dest=args.favorite_dest,
            pbf_dest=args.pbf_dest,
        )
    except Exception as exc:
        print(f"[desktop-sync] FATAL: {type(exc).__name__}: {exc}", flush=True)
        return 1

    if args.json:
        import json

        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.log_line(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

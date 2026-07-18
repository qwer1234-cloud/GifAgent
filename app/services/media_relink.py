"""Phase 4 Task 5: Relink moved source and artifact paths by fingerprint.

Provides two core functions:

- :func:`propose_relinks` — scan a directory and build relink proposals by
  matching SHA-256 (exact) or head/tail video fingerprint (probable).
- :func:`apply_relink` — apply a single proposal inside a short transaction.

API endpoints are mounted in :mod:`app.routers.workbench`.
"""

from __future__ import annotations

import os
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.task_engine.fingerprints import fingerprint_video, sha256_file

# Recognised media-file extensions (lowercase).
MEDIA_EXTENSIONS: set[str] = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".mp4", ".mkv", ".webm", ".mov", ".avi",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _same_location(a: str, b: str) -> bool:
    """Return ``True`` when *a* and *b* point to the same filesystem entry.

    On case-insensitive filesystems (Windows, macOS) differences in
    path case are ignored.  The check uses ``os.path.realpath`` to
    resolve symlinks and ``os.path.normcase`` for case folding.
    """
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(
            os.path.realpath(b)
        )
    except (FileNotFoundError, OSError):
        return False


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelinkProposal:
    """A single proposed file-path relink."""

    media_id: str
    old_path: str
    new_path: str
    confidence: Literal["exact", "probable", "conflict"]
    fingerprint: str


@dataclass(frozen=True)
class RelinkResult:
    """Outcome of an applied relink."""

    media_id: str
    updated_media_rows: int
    updated_candidate_rows: int
    new_path: str


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def propose_relinks(
    conn: sqlite3.Connection, search_root: Path
) -> list[RelinkProposal]:
    """Scan *search_root* and propose relinks for moved media files.

    Matching strategy
    -----------------
    *Exact* proposals are created when the SHA-256 digest of a scanned
    file matches a ``media.sha256`` value in the database.

    *Probable* proposals use the head/tail fingerprint (see
    :func:`~app.task_engine.fingerprints.fingerprint_video`).  The
    old file must still exist on disk for fingerprint comparison.

    When two or more proposals share the same ``new_path`` their
    confidence is downgraded to ``"conflict"`` so the caller can
    decide how to resolve the ambiguity.

    Parameters
    ----------
    conn:
        Open connection to the library database (``sqlite3.Row``
        factory is recommended but not required).
    search_root:
        Directory to walk for media files.

    Returns
    -------
    list[RelinkProposal]
        Ordered list of proposals (sorted by scanned file path).
    """
    root = Path(search_root).resolve()
    if not root.is_dir():
        return []

    media_rows = conn.execute(
        "SELECT media_id, file_path, sha256 FROM media WHERE sha256 IS NOT NULL"
    ).fetchall()

    # Index media rows by sha256 (multiple rows may share the same hash).
    sha256_index: dict[str, list[sqlite3.Row]] = {}
    for row in media_rows:
        sha256_index.setdefault(row["sha256"], []).append(row)

    proposals: list[RelinkProposal] = []

    for fpath in sorted(root.rglob("*")):
        if not fpath.is_file():
            continue
        if fpath.suffix.lower() not in MEDIA_EXTENSIONS:
            continue

        new_path = str(fpath)
        sha256_digest = sha256_file(new_path)
        matched_ids: set[str] = set()

        # --- 1. Exact match: full-file SHA-256 ---
        if sha256_digest in sha256_index:
            for row in sha256_index[sha256_digest]:
                if _same_location(row["file_path"], new_path):
                    continue  # file hasn't moved
                proposals.append(
                    RelinkProposal(
                        media_id=row["media_id"],
                        old_path=row["file_path"],
                        new_path=new_path,
                        confidence="exact",
                        fingerprint=sha256_digest,
                    )
                )
                matched_ids.add(row["media_id"])

        # --- 2. Probable match: head/tail fingerprint ---
        fp = fingerprint_video(new_path)
        for row in media_rows:
            if row["media_id"] in matched_ids:
                continue
            if row["sha256"] == sha256_digest:
                continue  # already handled as exact above
            if not os.path.exists(row["file_path"]):
                continue  # old file gone — can't compute fingerprint
            if _same_location(row["file_path"], new_path):
                continue

            try:
                old_fp = fingerprint_video(row["file_path"])
            except (FileNotFoundError, OSError):
                continue

            if old_fp == fp:
                proposals.append(
                    RelinkProposal(
                        media_id=row["media_id"],
                        old_path=row["file_path"],
                        new_path=new_path,
                        confidence="probable",
                        fingerprint=fp,
                    )
                )

    # --- 3. Mark conflicts (two+ proposals sharing new_path) ---
    new_path_counts: dict[str, int] = Counter(p.new_path for p in proposals)
    final: list[RelinkProposal] = []
    for p in proposals:
        if new_path_counts[p.new_path] > 1:
            final.append(
                RelinkProposal(
                    media_id=p.media_id,
                    old_path=p.old_path,
                    new_path=p.new_path,
                    confidence="conflict",
                    fingerprint=p.fingerprint,
                )
            )
        else:
            final.append(p)

    return final


def apply_relink(
    conn: sqlite3.Connection,
    proposal: RelinkProposal,
    *,
    confirmed: bool,
) -> RelinkResult:
    """Apply *proposal*, updating ``media.file_path`` and the three path
    columns on ``candidate_gifs`` (``source_video_path``,
    ``artifact_path``, ``preview_path``).

    When *confirmed* is ``False`` the function returns a dry-run result
    with zero counters and does **not** modify the database.

    Raises
    ------
    ValueError
        * The fingerprint no longer matches the file on disk (the file
          was changed between proposal creation and apply).
        * The target ``new_path`` is already claimed by another media
          row (duplicate target).
    """
    if not confirmed:
        return RelinkResult(
            media_id=proposal.media_id,
            updated_media_rows=0,
            updated_candidate_rows=0,
            new_path=proposal.new_path,
        )

    # --- Re-verify fingerprint ---
    if proposal.confidence == "exact":
        actual = sha256_file(proposal.new_path)
        if actual != proposal.fingerprint:
            raise ValueError(
                f"SHA-256 mismatch for {proposal.new_path}: "
                f"expected {proposal.fingerprint}, got {actual}"
            )
    else:
        actual = fingerprint_video(proposal.new_path)
        if actual != proposal.fingerprint:
            raise ValueError(
                f"Fingerprint mismatch for {proposal.new_path}: "
                f"expected {proposal.fingerprint}, got {actual}"
            )

    # --- Short transaction ---
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Prevent two media rows from claiming the same file_path.
        clash = conn.execute(
            "SELECT media_id FROM media WHERE file_path = ? AND media_id != ?",
            (proposal.new_path, proposal.media_id),
        ).fetchone()
        if clash is not None:
            raise ValueError(
                f"new_path {proposal.new_path} already claimed by "
                f"media {clash['media_id']}"
            )

        # Update media.file_path
        cur = conn.execute(
            "UPDATE media SET file_path = ? WHERE media_id = ?",
            (proposal.new_path, proposal.media_id),
        )
        media_rows = cur.rowcount

        # Update candidate_gifs path columns that reference the old path.
        candidate_rows = 0
        for col in ("source_video_path", "artifact_path", "preview_path"):
            cur = conn.execute(
                f"UPDATE candidate_gifs SET {col} = ? WHERE {col} = ?",
                (proposal.new_path, proposal.old_path),
            )
            candidate_rows += cur.rowcount

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return RelinkResult(
        media_id=proposal.media_id,
        updated_media_rows=media_rows,
        updated_candidate_rows=candidate_rows,
        new_path=proposal.new_path,
    )

"""Tests for Phase 4 Task 5: Relink moved source and artifact paths by fingerprint.

Covers
------
- Exact SHA-256 match between scanned file and media row.
- Head/tail fingerprint conflict (two media rows claim the same new file).
- Path case normalisation (same file, different spelling in DB vs. disk).
- Media + candidate path updates in a single apply transaction.
- Duplicate-target rejection when another media row already has the new path.
- Dry-run (confirmed=False) does not write anything.
"""

from __future__ import annotations

import os
import sqlite3
import struct
from pathlib import Path

import pytest

from app.services.media_relink import (
    RelinkProposal,
    RelinkResult,
    propose_relinks,
    apply_relink,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _library_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS media (
            media_id TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'video',
            sha256 TEXT UNIQUE,
            phash TEXT,
            width INTEGER, height INTEGER, duration REAL, frame_count INTEGER,
            cluster_id TEXT, is_representative INTEGER DEFAULT 0,
            created_at TEXT NOT NULL, indexed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS candidate_gifs (
            candidate_id TEXT PRIMARY KEY,
            source_run_id TEXT NOT NULL,
            source_run_candidate_id TEXT NOT NULL,
            source_video_sha256 TEXT NOT NULL,
            source_video_path TEXT NOT NULL,
            start_sec REAL NOT NULL, end_sec REAL NOT NULL,
            artifact_path TEXT, preview_path TEXT,
            vlm_summary_json TEXT NOT NULL DEFAULT '{}',
            tags_json TEXT NOT NULL DEFAULT '[]',
            scenario_keys_json TEXT NOT NULL DEFAULT '[]',
            base_rag_similarity REAL, profile_score REAL, final_score REAL,
            score_profile_version TEXT,
            status TEXT NOT NULL DEFAULT 'candidate',
            promoted_media_id INTEGER,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(source_run_id, source_run_candidate_id)
        );
    """
    )
    conn.commit()


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _make_medium_file(path: Path, middle_byte: int, ts: float) -> None:
    """Write a 2_097_153-byte file whose head/tail 1 MB are zero bytes.

    Files with different *middle_byte* values have the same head/tail
    fingerprint but different SHA-256.
    """
    BLOCK = 1_048_576
    data = b"\x00" * BLOCK + struct.pack("B", middle_byte) + b"\x00" * BLOCK
    path.write_bytes(data)
    os.utime(path, (ts, ts))


# ===================================================================
# Test 1 — exact SHA-256 match
# ===================================================================


def test_exact_sha256_match(tmp_path: Path) -> None:
    """A file in search_root whose sha256 matches media.sha256 → exact proposal."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _library_schema(conn)

    content = b"unique video content"
    src = tmp_path / "old" / "video.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(content)
    sha = _sha256_bytes(content)

    conn.execute(
        "INSERT INTO media (media_id, file_path, media_type, sha256, created_at, indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("media_001", str(src), "video", sha, "now", "now"),
    )
    conn.commit()

    dst = tmp_path / "new" / "video.mp4"
    dst.parent.mkdir()
    dst.write_bytes(content)

    proposals = propose_relinks(conn, tmp_path / "new")

    assert len(proposals) == 1
    p = proposals[0]
    assert p.media_id == "media_001"
    assert p.old_path == str(src)
    assert p.new_path == str(dst)
    assert p.confidence == "exact"
    assert p.fingerprint == sha


# ===================================================================
# Test 2 — cheap-fingerprint conflict
# ===================================================================


def test_fingerprint_conflict_becomes_conflict(tmp_path: Path) -> None:
    """Two media rows with different sha256 but identical head/tail fingerprints
    produce conflict proposals when they match the same new file."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _library_schema(conn)

    ts = 1234567890.0  # fixed timestamp so fingerprints are reproducible

    old = tmp_path / "old"
    old.mkdir()
    old_a = old / "a.mp4"
    old_b = old / "b.mp4"
    _make_medium_file(old_a, 0xAA, ts)
    _make_medium_file(old_b, 0xBB, ts)

    sha_a = _sha256_bytes(old_a.read_bytes())
    sha_b = _sha256_bytes(old_b.read_bytes())
    assert sha_a != sha_b, "test requires different SHA-256 values"

    for mid, fpath, sha in [("media_A", old_a, sha_a), ("media_B", old_b, sha_b)]:
        conn.execute(
            "INSERT INTO media (media_id, file_path, media_type, sha256, created_at, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mid, str(fpath), "video", sha, "now", "now"),
        )
    conn.commit()

    # New file with same head/tail → matches A by sha256, B by fingerprint
    new_root = tmp_path / "new"
    new_root.mkdir()
    new_file = new_root / "merged.mp4"
    _make_medium_file(new_file, 0xAA, ts)  # content = A

    proposals = propose_relinks(conn, new_root)

    assert len(proposals) == 2
    for p in proposals:
        assert p.new_path == str(new_file)
        assert p.confidence == "conflict", f"{p.media_id} expected conflict"
    assert {p.media_id for p in proposals} == {"media_A", "media_B"}


# ===================================================================
# Test 3 — path case normalisation
# ===================================================================


def test_path_case_normalization_skips_self(tmp_path: Path) -> None:
    """A media row whose file_path differs only in case from the scanned file
    does *not* produce a proposal (same file, different spelling)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _library_schema(conn)

    content = b"case test content"
    fpath = tmp_path / "video.mp4"
    fpath.write_bytes(content)
    sha = _sha256_bytes(content)

    # Register with UPPER-case extension
    cased_path = tmp_path / "Video.MP4"
    conn.execute(
        "INSERT INTO media (media_id, file_path, media_type, sha256, created_at, indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("media_case", str(cased_path), "video", sha, "now", "now"),
    )
    conn.commit()

    proposals = propose_relinks(conn, tmp_path)
    # On case-insensitive filesystems (Windows) the resolved paths are
    # identical → no proposal.  On Linux the paths are genuinely
    # different, but the test host is Windows.
    assert len(proposals) == 0


# ===================================================================
# Test 4 — media + candidate path updates
# ===================================================================


def test_apply_updates_media_and_candidate_paths(tmp_path: Path) -> None:
    """apply_relink(confirmed=True) updates media.file_path AND
    candidate_gifs.source_video_path / artifact_path / preview_path."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _library_schema(conn)

    content = b"apply test content"
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    old_path = old_dir / "source.mp4"
    old_path.write_bytes(content)
    sha = _sha256_bytes(content)

    new_dir = tmp_path / "new"
    new_dir.mkdir()
    new_path = new_dir / "source.mp4"
    new_path.write_bytes(content)

    conn.execute(
        "INSERT INTO media (media_id, file_path, media_type, sha256, created_at, indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("media_apply", str(old_path), "video", sha, "now", "now"),
    )
    conn.execute(
        """INSERT INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path,
            artifact_path, preview_path,
            start_sec, end_sec, status,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "cand_1",
            "run1",
            "rc1",
            sha,
            str(old_path),
            str(old_path),
            str(old_path),
            0.0,
            5.0,
            "candidate",
            "now",
            "now",
        ),
    )
    conn.commit()

    proposal = RelinkProposal(
        media_id="media_apply",
        old_path=str(old_path),
        new_path=str(new_path),
        confidence="exact",
        fingerprint=sha,
    )

    result = apply_relink(conn, proposal, confirmed=True)

    assert result.updated_media_rows == 1
    assert result.updated_candidate_rows == 3  # source_video_path + artifact + preview
    assert result.new_path == str(new_path)

    # Verify media
    media_row = conn.execute(
        "SELECT file_path FROM media WHERE media_id=?", ("media_apply",)
    ).fetchone()
    assert media_row["file_path"] == str(new_path)

    # Verify candidate
    cand = conn.execute(
        "SELECT source_video_path, artifact_path, preview_path FROM candidate_gifs WHERE candidate_id=?",
        ("cand_1",),
    ).fetchone()
    assert cand["source_video_path"] == str(new_path)
    assert cand["artifact_path"] == str(new_path)
    assert cand["preview_path"] == str(new_path)


# ===================================================================
# Test 5 — duplicate target rejection
# ===================================================================


def test_duplicate_target_rejected(tmp_path: Path) -> None:
    """apply_relink raises ValueError when new_path is already claimed by
    another media row (different media_id, same file_path string)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _library_schema(conn)

    content_a = b"content A"
    content_b = b"content B"
    sha_a = _sha256_bytes(content_a)
    sha_b = _sha256_bytes(content_b)

    old_dir = tmp_path / "old"
    other_dir = tmp_path / "other"
    old_dir.mkdir()
    other_dir.mkdir()

    # Media A — the row we will try to relink
    old_a = old_dir / "a.mp4"
    old_a.write_bytes(content_a)

    # Media B already claims file_path == target_path
    target_path = other_dir / "relocated.mp4"
    # The file on disk contains content_a (matches the proposal's fingerprint)
    target_path.write_bytes(content_a)

    conn.execute(
        "INSERT INTO media (media_id, file_path, media_type, sha256, created_at, indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("mA", str(old_a), "video", sha_a, "now", "now"),
    )
    conn.execute(
        "INSERT INTO media (media_id, file_path, media_type, sha256, created_at, indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        # mB's sha256 is sha_b, even though the file on disk has content_a.
        # The db only enforces UNIQUE on sha256, not consistency with the file.
        ("mB", str(target_path), "video", sha_b, "now", "now"),
    )
    conn.commit()

    proposal = RelinkProposal(
        media_id="mA",
        old_path=str(old_a),
        new_path=str(target_path),
        confidence="exact",
        fingerprint=sha_a,
    )

    with pytest.raises(ValueError, match="already claimed"):
        apply_relink(conn, proposal, confirmed=True)


# ===================================================================
# Test 6 — no write when confirmed=False
# ===================================================================


def test_no_write_when_not_confirmed(tmp_path: Path) -> None:
    """apply_relink(confirmed=False) returns a dry-run result without
    modifying the database."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _library_schema(conn)

    content = b"dry-run content"
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    old_path = old_dir / "source.mp4"
    old_path.write_bytes(content)
    sha = _sha256_bytes(content)

    new_dir = tmp_path / "new"
    new_dir.mkdir()
    new_path = new_dir / "source.mp4"
    new_path.write_bytes(content)

    conn.execute(
        "INSERT INTO media (media_id, file_path, media_type, sha256, created_at, indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("media_dry", str(old_path), "video", sha, "now", "now"),
    )
    conn.commit()

    proposal = RelinkProposal(
        media_id="media_dry",
        old_path=str(old_path),
        new_path=str(new_path),
        confidence="exact",
        fingerprint=sha,
    )

    result = apply_relink(conn, proposal, confirmed=False)

    assert result.updated_media_rows == 0
    assert result.updated_candidate_rows == 0
    assert result.new_path == str(new_path)

    # DB unchanged
    row = conn.execute(
        "SELECT file_path FROM media WHERE media_id=?", ("media_dry",)
    ).fetchone()
    assert row["file_path"] == str(old_path)

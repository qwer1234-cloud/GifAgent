"""W4 kill tests for task_engine.fingerprints mutation survivors.

Targets survivors from the cosmic-ray run recorded in
``<sprint-root>/w4/mutation_scores/cosmic-ray/fingerprints.json``:

- ``fingerprint_video``: the fingerprint hashes the FIRST ``block_bytes``
  bytes plus the LAST ``block_bytes`` bytes (``size - block_bytes`` seek);
  the ``size > block_bytes`` guard; the 1 MiB default. All verified against
  an independent hashlib computation on synthetic text files in tmp_path.
- ``canonical_json``: key sorting and non-ASCII handling feed the hash.
- ``sha256_file`` streams the whole file (zero-iteration mutant).

Equivalent mutants: 22 annotation unions (``str | Path``), and the
``sha256_file`` chunk size (1048576 vs 1048575/1048577) — hashing is
chunk-order and chunk-size independent for a byte stream.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.task_engine.fingerprints import (
    canonical_hash,
    canonical_json,
    fingerprint_video,
    sha256_file,
)

BLOCK = 1_048_576


def _write(tmp_path: Path, name: str, chunk: bytes, times: int) -> Path:
    data = chunk * times
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_fingerprint_video_hashes_head_and_tail_blocks(tmp_path: Path):
    head = b"A" * BLOCK
    tail = b"B" * BLOCK
    data = head + b"MIDDLE-MARKER" + tail
    path = tmp_path / "video.bin"
    path.write_bytes(data)

    fingerprint = fingerprint_video(path)
    size_part, mtime_part, digest = fingerprint.split(":")
    assert int(size_part) == len(data)
    assert mtime_part.isdigit()
    expected = hashlib.sha256(head + tail).hexdigest()
    assert digest == expected


def test_fingerprint_video_single_block_file_hashes_head_only(tmp_path: Path):
    data = b"C" * BLOCK  # exactly one block: no tail pass
    path = tmp_path / "one_block.bin"
    path.write_bytes(data)

    digest = fingerprint_video(path).split(":")[2]
    assert digest == hashlib.sha256(data).hexdigest()


def test_fingerprint_video_default_block_size_is_one_mib(tmp_path: Path):
    head = b"D" * BLOCK
    tail = b"E" * BLOCK
    path = tmp_path / "default_block.bin"
    path.write_bytes(head + b"middle" + tail)

    digest = fingerprint_video(path).split(":")[2]
    assert digest == hashlib.sha256(head + tail).hexdigest()


def test_canonical_json_sorts_keys_and_keeps_unicode_raw():
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})
    expected = hashlib.sha256(
        json.dumps({"k": "场"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert canonical_hash({"k": "场"}) == expected


def test_sha256_file_streams_whole_file(tmp_path: Path):
    path = _write(tmp_path, "stream.bin", b"0123456789", 1000)
    assert sha256_file(path) == hashlib.sha256(b"0123456789" * 1000).hexdigest()

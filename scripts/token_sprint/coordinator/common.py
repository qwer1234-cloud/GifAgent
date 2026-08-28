"""Shared primitives for token-sprint coordinator scripts.

Conventions (design doc v4.1, global constraint 4/6):
- ``--sprint-root`` is the primary channel; ``GIFAGENT_SPRINT_ROOT`` is only an
  explicit fallback. Missing/invalid -> ``SystemExit(2)`` with ``error:`` on stderr.
- Atomic writes always stage the temp file in the target's own directory
  (never ``%TEMP%``) and finish with ``os.replace``.
- Receipt filenames are Windows-safe: no colons, timestamp ``YYYYMMDDTHHMMSSfffZ``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

SPRINT_ROOT_ENV = "GIFAGENT_SPRINT_ROOT"

_WORKSTREAM_RE = re.compile(r"^[a-z0-9-]+$")


def add_sprint_root_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sprint-root",
        default=None,
        help=(
            "Token-sprint shared root directory. Falls back to the "
            f"{SPRINT_ROOT_ENV} environment variable only when omitted."
        ),
    )


def resolve_sprint_root(cli_value: str | None) -> Path:
    raw = cli_value if cli_value not in (None, "") else os.environ.get(SPRINT_ROOT_ENV)
    if not raw:
        print(
            f"error: --sprint-root missing and ${SPRINT_ROOT_ENV} is not set",
            file=sys.stderr,
        )
        raise SystemExit(2)
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        print(
            f"error: sprint root does not exist or is not a directory: {path}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _staging_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = _staging_path(path)
    try:
        staging.write_bytes(data)
        os.replace(staging, path)
    finally:
        if staging.exists():
            staging.unlink()


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding))


def windows_safe_ts(dt: datetime | None = None) -> str:
    moment = (dt if dt is not None else datetime.now(timezone.utc)).astimezone(timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%S") + f"{moment.microsecond // 1000:03d}Z"


def receipt_filename(
    workstream: str,
    *,
    dt: datetime | None = None,
    receipt_id: str | None = None,
) -> str:
    if not _WORKSTREAM_RE.fullmatch(workstream):
        raise ValueError(
            f"workstream must match [a-z0-9-]+, got {workstream!r}"
        )
    unique = receipt_id if receipt_id is not None else str(uuid.uuid4())
    return f"{workstream}_{windows_safe_ts(dt)}_{unique}.json"

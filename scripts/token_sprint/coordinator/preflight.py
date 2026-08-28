#!/usr/bin/env python3
"""Cross-invocation preflight for the token-sprint shared root.

Two independent command invocations are required (design v4.1 Step 4):

    python scripts/token_sprint/coordinator/preflight.py \
        --sprint-root <path> --write-nonce
    python scripts/token_sprint/coordinator/preflight.py \
        --sprint-root <path> --verify-nonce

The nonce lives at ``<sprint-root>/_preflight_nonce.txt`` (UTF-8, one line of
32 hex chars). Proves that path resolution and file visibility survive across
separate processes without relying on session env vars or PowerShell.
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
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
)

NONCE_FILENAME = "_preflight_nonce.txt"
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")


def _nonce_path(sprint_root: Path) -> Path:
    return sprint_root / NONCE_FILENAME


def _write_nonce(sprint_root: Path) -> int:
    nonce = secrets.token_hex(16)
    target = _nonce_path(sprint_root)
    atomic_write_text(target, nonce + "\n")
    print(target)
    return 0


def _verify_nonce(sprint_root: Path) -> int:
    target = _nonce_path(sprint_root)
    if not target.is_file():
        print(f"error: nonce file missing: {target}", file=sys.stderr)
        return 2
    content = target.read_text(encoding="utf-8").strip()
    if not _NONCE_RE.fullmatch(content):
        print(
            f"error: nonce file is not a single line of 32 hex chars: {target}",
            file=sys.stderr,
        )
        return 2
    print("preflight ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_sprint_root_argument(parser)
    parser.add_argument("--write-nonce", action="store_true", help="write a fresh nonce")
    parser.add_argument("--verify-nonce", action="store_true", help="verify the written nonce")
    args = parser.parse_args(argv)

    if args.write_nonce == args.verify_nonce:
        print(
            "error: exactly one of --write-nonce / --verify-nonce is required",
            file=sys.stderr,
        )
        return 2

    sprint_root = resolve_sprint_root(args.sprint_root)
    if args.write_nonce:
        return _write_nonce(sprint_root)
    return _verify_nonce(sprint_root)


if __name__ == "__main__":
    sys.exit(main())

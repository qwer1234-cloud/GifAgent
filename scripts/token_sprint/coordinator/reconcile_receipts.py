#!/usr/bin/env python3
"""Reconcile reviewer receipts into the canonical token-sprint manifest.

Single-writer protocol (design v4.1, global constraint 6): only the
coordinator session runs this script. For every pending receipt under
``<sprint-root>/receipts/`` it re-verifies the four-way hash chain
(artifact / .ready / .done / receipt), applies the receipt to the canonical
``sprint_manifest.json`` ``artifacts`` mapping keyed by ``receipt_id``
(idempotent: identical replay is a no-op; field conflict is rejected without
overwriting), rewrites the manifest atomically, and only then archives the
receipt into ``receipts/processed/``. A crash between the manifest write and
the archive move is safe: replaying the same receipt yields a byte-identical
manifest.

Usage:
    python scripts/token_sprint/coordinator/reconcile_receipts.py \
        --sprint-root <path> --once
"""

from __future__ import annotations

import argparse
import copy
import json
import os
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
from scripts.token_sprint.coordinator.protocol import (  # noqa: E402
    RECEIPT_REQUIRED_FIELDS,
    HashChainError,
    verify_hash_chain,
)

MANIFEST_NAME = "sprint_manifest.json"
PROCESSED_DIRNAME = "processed"

MANIFEST_SCHEMA = "gifagent.token_sprint.manifest.v1"

# name -> schema annotation (None = plain SHA-only entry)
CONTROL_PLANE_FILES: dict[str, str | None] = {
    "inputs_manifest.json": "gifagent.token_sprint.inputs.v1",
    "approved_enrichment_manifest.json": "gifagent.token_sprint.approved_enrichment.v1",
    "usage-log.md": None,
}


class ReceiptConflictError(RuntimeError):
    """Raised when a receipt_id already exists with different content."""


def default_manifest(as_of: str | None = None) -> dict:
    return {
        "schema": MANIFEST_SCHEMA,
        "revision": 1,
        "as_of": as_of or _now_iso(),
        "refactor_base_sha": "",
        "sprint_base_sha": "",
        "fallback_frozen": False,
        "fallback_reason": None,
        "cosmic_ray": {"status": "undecided", "reason": ""},
        "vector_math_in_baseline": False,
        "cleanup_pending": [],
        "control_plane": {
            "inputs_manifest.json": {
                "sha256": None,
                "schema": "gifagent.token_sprint.inputs.v1",
            },
            "approved_enrichment_manifest.json": {
                "sha256": None,
                "schema": "gifagent.token_sprint.approved_enrichment.v1",
            },
            "usage-log.md": {"sha256": None},
        },
        "artifacts": {},
    }


def apply_receipt(manifest: dict, receipt: dict) -> dict:
    """Return a new manifest with ``receipt`` applied under its ``receipt_id``.

    Pure function: identical replay returns an equal manifest; a conflicting
    receipt (same id, different fields) raises :class:`ReceiptConflictError`.
    """
    receipt_id = receipt["receipt_id"]
    entry = {key: receipt[key] for key in RECEIPT_REQUIRED_FIELDS}
    existing = manifest.get("artifacts", {}).get(receipt_id)
    if existing is not None and existing != entry:
        raise ReceiptConflictError(
            f"receipt_id {receipt_id} already recorded with different content"
        )
    updated = copy.deepcopy(manifest)
    updated.setdefault("artifacts", {})[receipt_id] = entry
    return updated


def update_control_plane_shas(
    manifest: dict,
    sprint_root: Path,
    usage_log: Path | None = None,
) -> None:
    """Refresh recorded SHAs for control-plane files that exist on disk."""
    control_plane = manifest.setdefault("control_plane", {})
    for name, schema in CONTROL_PLANE_FILES.items():
        entry = control_plane.setdefault(name, {"sha256": None})
        entry.setdefault("sha256", None)
        if schema is not None:
            entry.setdefault("schema", schema)
    for name in CONTROL_PLANE_FILES:
        if name == "usage-log.md" and usage_log is not None:
            target: Path | None = usage_log
        else:
            target = sprint_root / name
        if target is not None and target.is_file():
            control_plane[name]["sha256"] = sha256_file(target)


def reconcile_once(sprint_root: Path, usage_log: Path | None = None) -> int:
    manifest_path = sprint_root / MANIFEST_NAME
    receipts_dir = sprint_root / "receipts"
    processed_dir = receipts_dir / PROCESSED_DIRNAME
    processed_dir.mkdir(parents=True, exist_ok=True)

    pending = sorted(
        path
        for path in receipts_dir.glob("*.json")
        if path.is_file() and ".tmp." not in path.name
    )

    verified: list[tuple[Path, dict]] = []
    failures = 0
    for receipt_path in pending:
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            missing = [key for key in RECEIPT_REQUIRED_FIELDS if key not in receipt]
            if missing:
                raise HashChainError(f"receipt missing required fields: {missing}")
            verify_hash_chain(
                Path(receipt["artifact_path"]),
                Path(receipt["ready_path"]),
                Path(receipt["done_path"]),
                receipt_path,
            )
            verified.append((receipt_path, receipt))
        except (HashChainError, json.JSONDecodeError, OSError) as exc:
            failures += 1
            print(
                f"error: receipt rejected, left in place: {receipt_path.name}: {exc}",
                file=sys.stderr,
            )

    manifest_existed = manifest_path.is_file()
    old_bytes = manifest_path.read_bytes() if manifest_existed else None
    manifest = (
        json.loads(old_bytes.decode("utf-8"))
        if old_bytes is not None
        else default_manifest()
    )

    staged: list[tuple[Path, dict]] = []
    for receipt_path, receipt in verified:
        try:
            manifest = apply_receipt(manifest, receipt)
            staged.append((receipt_path, receipt))
        except ReceiptConflictError as exc:
            failures += 1
            print(
                f"error: receipt conflict, left in place: {receipt_path.name}: {exc}",
                file=sys.stderr,
            )

    update_control_plane_shas(manifest, sprint_root, usage_log)

    candidate = _serialize(manifest)
    if manifest_existed:
        if candidate != old_bytes:
            manifest["revision"] = int(manifest.get("revision", 0)) + 1
            manifest["as_of"] = _now_iso()
            atomic_write_text(manifest_path, _serialize(manifest).decode("utf-8"))
        # else: pure idempotent replay -- manifest stays byte-identical, no bump.
    else:
        atomic_write_text(manifest_path, candidate.decode("utf-8"))

    for receipt_path, _receipt in staged:
        try:
            os.replace(receipt_path, processed_dir / receipt_path.name)
        except OSError as exc:
            failures += 1
            print(
                f"error: could not archive receipt: {receipt_path.name}: {exc}",
                file=sys.stderr,
            )

    return 1 if failures else 0


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _serialize(manifest: dict) -> bytes:
    return (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_sprint_root_argument(parser)
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument(
        "--usage-log",
        default=None,
        help="Optional absolute path to usage-log.md for control-plane SHA refresh.",
    )
    args = parser.parse_args(argv)
    sprint_root = resolve_sprint_root(args.sprint_root)
    usage_log = Path(args.usage_log).resolve() if args.usage_log else None
    return reconcile_once(sprint_root, usage_log)


if __name__ == "__main__":
    sys.exit(main())

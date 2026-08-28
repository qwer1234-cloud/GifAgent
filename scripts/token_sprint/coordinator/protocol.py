"""Ready/done/receipt hash-chain protocol for token-sprint work packages.

Binding rules (design v4.1, global constraints 6/12):
- executor writes ``<artifact>.ready`` binding the artifact's SHA-256;
- reviewer writes ``<artifact>.done`` only after the artifact SHA still matches
  the ``.ready`` binding, then publishes exactly one receipt binding all four;
- ``verify_hash_chain`` recomputes every file hash and rejects on any
  disagreement (including path/producer/reviewer field drift);
- this module deliberately provides no API to modify an artifact in place
  after its ``.ready`` exists.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from scripts.token_sprint.coordinator.common import (
    atomic_write_text,
    receipt_filename,
    sha256_file,
)

READY_SUFFIX = ".ready"
DONE_SUFFIX = ".done"

RECEIPT_REQUIRED_FIELDS = (
    "receipt_id",
    "artifact_path",
    "artifact_sha256",
    "ready_path",
    "ready_sha256",
    "done_path",
    "done_sha256",
    "producer",
    "reviewer",
    "workstream",
    "ts",
)


class HashChainError(RuntimeError):
    """Raised when artifact/ready/done/receipt disagree on any binding."""


def _posix_abs(path: Path) -> str:
    return path.resolve().as_posix()


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HashChainError(f"cannot read JSON sidecar {path}: {exc}") from exc


def write_ready(artifact: Path, *, producer: str, qc_summary: dict) -> Path:
    artifact = artifact.resolve()
    if not artifact.is_file():
        raise HashChainError(f"artifact does not exist: {artifact}")
    ready_path = artifact.with_name(artifact.name + READY_SUFFIX)
    payload = {
        "artifact_path": _posix_abs(artifact),
        "artifact_sha256": sha256_file(artifact),
        "qc_summary": qc_summary,
        "producer": producer,
        "ts": _now_iso(),
    }
    atomic_write_text(ready_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return ready_path


def write_done(
    artifact: Path,
    ready: Path,
    *,
    reviewer: str,
    qc_evidence: dict,
    verdict: str = "pass",
) -> Path:
    artifact = artifact.resolve()
    ready = ready.resolve()
    ready_data = _read_json(ready)
    artifact_sha = sha256_file(artifact)
    if ready_data.get("artifact_sha256") != artifact_sha:
        raise HashChainError(
            f"artifact sha256 changed after .ready: {artifact} "
            f"(ready says {ready_data.get('artifact_sha256')}, actual {artifact_sha})"
        )
    if ready_data.get("artifact_path") != _posix_abs(artifact):
        raise HashChainError(
            f".ready binds a different artifact path: {ready_data.get('artifact_path')!r}"
        )
    done_path = artifact.with_name(artifact.name + DONE_SUFFIX)
    payload = {
        "artifact_path": _posix_abs(artifact),
        "artifact_sha256": artifact_sha,
        "ready_path": _posix_abs(ready),
        "ready_sha256": sha256_file(ready),
        "qc_evidence": qc_evidence,
        "review_verdict": verdict,
        "reviewer": reviewer,
        "ts": _now_iso(),
    }
    atomic_write_text(done_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return done_path


def write_receipt(
    *,
    sprint_root: Path,
    workstream: str,
    producer: str,
    reviewer: str,
    artifact: Path,
    ready: Path,
    done: Path,
) -> Path:
    sprint_root = sprint_root.resolve()
    artifact = artifact.resolve()
    ready = ready.resolve()
    done = done.resolve()

    artifact_sha = sha256_file(artifact)
    ready_sha = sha256_file(ready)
    done_sha = sha256_file(done)

    ready_data = _read_json(ready)
    if ready_data.get("artifact_sha256") != artifact_sha:
        raise HashChainError(f".ready does not bind artifact {artifact}")
    done_data = _read_json(done)
    if done_data.get("artifact_sha256") != artifact_sha:
        raise HashChainError(f".done does not bind artifact {artifact}")
    if done_data.get("ready_sha256") != ready_sha:
        raise HashChainError(f".done does not bind .ready {ready}")

    receipt_id = str(uuid.uuid4())
    payload = {
        "receipt_id": receipt_id,
        "artifact_path": _posix_abs(artifact),
        "artifact_sha256": artifact_sha,
        "ready_path": _posix_abs(ready),
        "ready_sha256": ready_sha,
        "done_path": _posix_abs(done),
        "done_sha256": done_sha,
        "producer": producer,
        "reviewer": reviewer,
        "workstream": workstream,
        "ts": _now_iso(),
    }
    receipts_dir = sprint_root / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipts_dir / receipt_filename(workstream, receipt_id=receipt_id)
    atomic_write_text(receipt_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return receipt_path


def verify_hash_chain(artifact: Path, ready: Path, done: Path, receipt: Path) -> None:
    """Recompute all four files; raise :class:`HashChainError` on any disagreement."""
    artifact = artifact.resolve()
    ready = ready.resolve()
    done = done.resolve()
    receipt = receipt.resolve()

    ready_data = _read_json(ready)
    done_data = _read_json(done)
    receipt_data = _read_json(receipt)

    artifact_sha = sha256_file(artifact)
    ready_sha = sha256_file(ready)
    done_sha = sha256_file(done)

    artifact_posix = artifact.as_posix()
    ready_posix = ready.as_posix()
    done_posix = done.as_posix()

    checks: list[tuple[bool, str]] = [
        (
            ready_data.get("artifact_sha256") == artifact_sha,
            f".ready does not bind artifact {artifact}",
        ),
        (
            done_data.get("artifact_sha256") == artifact_sha,
            f".done does not bind artifact {artifact}",
        ),
        (
            receipt_data.get("artifact_sha256") == artifact_sha,
            f"receipt does not bind artifact {artifact}",
        ),
        (done_data.get("ready_sha256") == ready_sha, f".done does not bind .ready {ready}"),
        (receipt_data.get("ready_sha256") == ready_sha, f"receipt does not bind .ready {ready}"),
        (receipt_data.get("done_sha256") == done_sha, f"receipt does not bind .done {done}"),
        (ready_data.get("artifact_path") == artifact_posix, ".ready artifact_path mismatch"),
        (done_data.get("artifact_path") == artifact_posix, ".done artifact_path mismatch"),
        (done_data.get("ready_path") == ready_posix, ".done ready_path mismatch"),
        (receipt_data.get("artifact_path") == artifact_posix, "receipt artifact_path mismatch"),
        (receipt_data.get("ready_path") == ready_posix, "receipt ready_path mismatch"),
        (receipt_data.get("done_path") == done_posix, "receipt done_path mismatch"),
        (
            ready_data.get("producer") == receipt_data.get("producer"),
            "receipt producer disagrees with .ready",
        ),
        (
            done_data.get("reviewer") == receipt_data.get("reviewer"),
            "receipt reviewer disagrees with .done",
        ),
    ]
    for ok, message in checks:
        if not ok:
            raise HashChainError(message)

    # Receipt filename must embed workstream + receipt_id (Windows-safe naming).
    stem_parts = receipt.stem.rsplit("_", 2)
    if len(stem_parts) != 3:
        raise HashChainError(f"receipt filename is not <workstream>_<ts>_<id>: {receipt.name}")
    stem_workstream, _stem_ts, stem_receipt_id = stem_parts
    if stem_workstream != receipt_data.get("workstream"):
        raise HashChainError("receipt filename workstream disagrees with content")
    if stem_receipt_id != receipt_data.get("receipt_id"):
        raise HashChainError("receipt filename receipt_id disagrees with content")
    missing = [key for key in RECEIPT_REQUIRED_FIELDS if key not in receipt_data]
    if missing:
        raise HashChainError(f"receipt missing required fields: {missing}")

"""Candidate capture identities, preexport key generation, and config normalization.

Provides immutable preexport candidate identities, canonical config snapshots,
source video hashing, and collision detection across pipelines.

Capture evidence is content-addressed and immutable: a persisted payload is
never rewritten, a new capture version never overwrites an older payload file,
and database identity rows are append-only (same identity + different content
is a conflict, not an update).
"""
from __future__ import annotations

import decimal
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA256_CACHE: dict[str, tuple[int, int, str]] = {}

CAPTURE_STATUSES = ("CAPTURED", "FAILED", "NEEDS_REPAIR")
ANALYSIS_EVENT_KINDS = (
    "scored",
    "selected",
    "not_selected",
    "quality_rejected",
    "publication_failed",
)
LEGACY_IDENTITY_VERSION = "legacy-v1"


class SourceIdentityUnavailable(ValueError):
    """The source video identity required for capture is unavailable."""


class CaptureConflict(ValueError):
    """The same capture identity was presented with different content."""


@dataclass(frozen=True)
class PersistedCapture:
    record_id: str
    payload_path: str
    payload_sha256: str


def seconds_to_microsecond_int(val: Any) -> int:
    """Convert float/int/str seconds to exact integer microseconds using Decimal half-up.

    Rejects booleans, non-finite values (NaN, Inf), and invalid inputs.
    """
    if isinstance(val, bool):
        raise ValueError("boolean values not allowed for timestamp")
    if isinstance(val, float):
        if not math.isfinite(val):
            raise ValueError(f"timestamp must be finite, got {val}")
        # Convert float via string representation for Decimal
        val_str = f"{val:.9f}".rstrip("0").rstrip(".") if "." in f"{val:.9f}" else str(val)
    elif isinstance(val, (int, decimal.Decimal, str)):
        val_str = str(val).strip()
    else:
        raise ValueError(f"unsupported timestamp type: {type(val)}")

    try:
        dec = decimal.Decimal(val_str)
    except decimal.InvalidOperation as exc:
        raise ValueError(f"invalid timestamp number: {val_str!r}") from exc

    if not dec.is_finite():
        raise ValueError(f"timestamp must be finite, got {val_str}")

    us_dec = dec * decimal.Decimal(1_000_000)
    return int(us_dec.quantize(decimal.Decimal("1"), rounding=decimal.ROUND_HALF_UP))


def preexport_key(
    source_sha256: str,
    start_us: int,
    end_us: int,
    anchor_us: int,
) -> str:
    """Compute the immutable 64-char canonical SHA-256 preexport key for a candidate window.

    Payload format (canonical JSON with sorted keys and no whitespace):
    {"anchor_us": <int>, "end_us": <int>, "source_sha256": "<64-hex>", "start_us": <int>}
    """
    if isinstance(source_sha256, str):
        source_sha256_clean = source_sha256.strip().lower()
    else:
        raise ValueError(f"source_sha256 must be a string, got {type(source_sha256)}")

    if not _SHA256_HEX_RE.match(source_sha256_clean):
        raise ValueError(
            f"source_sha256 must be a 64-character lowercase hex string, got {source_sha256!r}"
        )

    for name, val in [("start_us", start_us), ("end_us", end_us), ("anchor_us", anchor_us)]:
        if isinstance(val, bool):
            raise ValueError(f"{name} cannot be a boolean; must be an integer, got {val!r}")
        if not isinstance(val, int):
            raise ValueError(f"{name} must be an integer, got {val!r} (type {type(val)})")

    if end_us <= start_us:
        raise ValueError(f"end_us must be greater than start_us, got start_us={start_us}, end_us={end_us}")

    if not (start_us <= anchor_us <= end_us):
        raise ValueError(
            f"anchor_us must be between start_us and end_us, got anchor_us={anchor_us}, "
            f"start_us={start_us}, end_us={end_us}"
        )

    payload = {
        "anchor_us": anchor_us,
        "end_us": end_us,
        "source_sha256": source_sha256_clean,
        "start_us": start_us,
    }
    canonical_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


def resolve_anchor_us(clip: dict, start_us: int, end_us: int) -> tuple[int, str]:
    """Resolve the anchor timestamp in microseconds from clip metadata or fallback to midpoint."""
    best = clip.get("best_frame")
    best_ts = None
    if isinstance(best, dict) and best.get("timestamp") is not None:
        best_ts = best.get("timestamp")
    elif clip.get("anchor_ts") is not None:
        best_ts = clip.get("anchor_ts")
    elif clip.get("peak_timestamp") is not None:
        best_ts = clip.get("peak_timestamp")

    if best_ts is not None:
        try:
            cand_anchor_us = seconds_to_microsecond_int(best_ts)
            if start_us <= cand_anchor_us <= end_us:
                return cand_anchor_us, "best_frame" if isinstance(best, dict) else "explicit_anchor"
        except Exception:
            pass

    midpoint_us = (start_us + end_us) // 2
    return midpoint_us, "window_midpoint"


def compute_source_sha256(video_path: str | Path) -> str:
    """Compute and cache the SHA-256 hash of a source video file."""
    path_obj = Path(video_path).resolve()
    if not path_obj.is_file():
        raise FileNotFoundError(f"source video file not found: {path_obj}")

    stat = path_obj.stat()
    cache_key = str(path_obj)
    cached = _SOURCE_SHA256_CACHE.get(cache_key)
    if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
        return cached[2]

    hasher = hashlib.sha256()
    with open(path_obj, "rb") as f:
        while chunk := f.read(1024 * 1024):  # 1MB buffer
            hasher.update(chunk)

    digest = hasher.hexdigest().lower()
    _SOURCE_SHA256_CACHE[cache_key] = (stat.st_size, stat.st_mtime_ns, digest)
    return digest


def assign_candidate_capture_identities(
    clips: list[dict],
    video_path: str,
    source_sha256: str | None = None,
    capture_enabled: bool = False,
    capture_version: str = "capture-v1",
) -> None:
    """Assign stable candidate identities, full preexport keys, and detect 16-char collisions.

    With capture enabled the source identity must be real: an unreadable
    source file or a non-canonical SHA-256 raises ``SourceIdentityUnavailable``
    instead of degrading to a placeholder hash. Already-frozen ``preexport_key``
    identities are re-verified; a bare ``candidate_id`` is only preserved when
    the clip explicitly carries ``candidate_identity_version="legacy-v1"``.
    """
    if not capture_enabled:
        video_name = os.path.splitext(os.path.basename(video_path))[0]
        for index, clip in enumerate(clips):
            candidate_id = clip.get("candidate_id") or clip.get("clip_id")
            if not isinstance(candidate_id, str) or not candidate_id:
                identity = f"{video_name}:{clip.get('start_ts')}:{clip.get('end_ts')}:{index}"
                candidate_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
            clip["candidate_id"] = candidate_id
            clip.setdefault("clip_id", candidate_id)
        return

    # When capture is enabled the source identity must be real.
    if source_sha256 is None:
        try:
            source_sha256 = compute_source_sha256(video_path)
        except Exception as exc:
            raise SourceIdentityUnavailable(
                f"capture enabled but source video is unreadable: {video_path!r}"
            ) from exc
    if (
        not isinstance(source_sha256, str)
        or not _SHA256_HEX_RE.match(source_sha256.strip().lower())
    ):
        raise SourceIdentityUnavailable(
            f"capture enabled requires a 64-character lowercase hex source "
            f"SHA-256, got {source_sha256!r}"
        )
    source_sha256 = source_sha256.strip().lower()

    seen_prefixes: dict[str, str] = {}
    for index, clip in enumerate(clips):
        existing_key = clip.get("preexport_key")

        if existing_key:
            # Resume: the identity is frozen; recompute deterministically and
            # verify the window still hashes to the same preexport key.
            start_us, end_us, anchor_us = _frozen_window_us(clip)
            key = preexport_key(source_sha256, start_us, end_us, anchor_us)
            if key != existing_key:
                raise ValueError(
                    f"preexport_key mismatch on resume: stored {existing_key}, "
                    f"recomputed {key}"
                )
            existing_cand_id = clip.get("candidate_id") or clip.get("clip_id")
            prefix = (existing_cand_id or key)[:16]
            if prefix in seen_prefixes and seen_prefixes[prefix] != existing_key:
                raise ValueError(
                    f"candidate_id collision detected between {existing_key} "
                    f"and {seen_prefixes[prefix]}"
                )
            seen_prefixes[prefix] = existing_key
            if existing_cand_id:
                clip["candidate_id"] = existing_cand_id
            else:
                clip["candidate_id"] = key[:16]
            clip.setdefault("clip_id", clip["candidate_id"])
            clip.setdefault("candidate_capture_version", capture_version)
            clip.setdefault("source_video_sha256", source_sha256)
            continue

        legacy_id = clip.get("candidate_id") or clip.get("clip_id")
        if (
            isinstance(legacy_id, str)
            and legacy_id
            and clip.get("candidate_identity_version") == LEGACY_IDENTITY_VERSION
        ):
            # Only an explicitly marked legacy identity is preserved as-is.
            prefix = legacy_id[:16]
            if prefix in seen_prefixes and seen_prefixes[prefix] != legacy_id:
                raise ValueError(
                    f"candidate_id collision detected between {legacy_id} "
                    f"and {seen_prefixes[prefix]}"
                )
            seen_prefixes[prefix] = legacy_id
            clip["candidate_id"] = legacy_id
            clip.setdefault("clip_id", legacy_id)
            continue

        start_val = clip.get("start_ts") if clip.get("start_ts") is not None else clip.get("start_s")
        end_val = clip.get("end_ts") if clip.get("end_ts") is not None else clip.get("end_s")
        if start_val is None or end_val is None:
            raise ValueError(
                f"capture enabled requires window bounds for every candidate, "
                f"clip index {index} is missing start/end"
            )

        start_us = seconds_to_microsecond_int(start_val)
        end_us = seconds_to_microsecond_int(end_val)
        anchor_us, anchor_src = resolve_anchor_us(clip, start_us, end_us)

        key = preexport_key(source_sha256, start_us, end_us, anchor_us)
        cand_id = key[:16]

        if cand_id in seen_prefixes and seen_prefixes[cand_id] != key:
            raise ValueError(
                f"candidate_id collision detected between {key} and {seen_prefixes[cand_id]}"
            )
        seen_prefixes[cand_id] = key

        clip["preexport_key"] = key
        clip["candidate_id"] = cand_id
        clip.setdefault("clip_id", cand_id)
        clip["candidate_capture_version"] = capture_version
        clip["start_us"] = start_us
        clip["end_us"] = end_us
        clip["anchor_us"] = anchor_us
        clip["anchor_source"] = anchor_src
        clip["source_video_sha256"] = source_sha256


def _frozen_window_us(clip: dict) -> tuple[int, int, int]:
    """Return the frozen (start_us, end_us, anchor_us) for a clip that already
    carries a preexport key, converting from seconds when needed."""
    start_us = clip.get("start_us")
    end_us = clip.get("end_us")
    anchor_us = clip.get("anchor_us")
    if start_us is None:
        start_val = clip.get("start_ts") if clip.get("start_ts") is not None else clip.get("start_s")
        end_val = clip.get("end_ts") if clip.get("end_ts") is not None else clip.get("end_s")
        if start_val is None or end_val is None:
            raise ValueError("frozen candidate is missing window bounds")
        start_us = seconds_to_microsecond_int(start_val)
        end_us = seconds_to_microsecond_int(end_val)
    if anchor_us is None:
        anchor_us, _src = resolve_anchor_us(clip, int(start_us), int(end_us))
    for name, val in (("start_us", start_us), ("end_us", end_us), ("anchor_us", anchor_us)):
        if isinstance(val, bool) or not isinstance(val, int):
            raise ValueError(f"frozen {name} must be an integer, got {val!r}")
    return int(start_us), int(end_us), int(anchor_us)


def canonical_capture_config_hash(cfg: dict) -> str:
    """Compute SHA-256 hash of canonical capture config JSON."""
    canonical_bytes = json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


def capture_config(snapshot: dict) -> dict:
    """Normalize candidate_capture configuration from snapshot and compute its canonical hash."""
    raw = snapshot.get("candidate_capture", {}) or {}
    normalized = {
        "enabled": bool(raw.get("enabled", False)),
        "failure_policy": str(raw.get("failure_policy", "record_and_retry")),
        "version": str(raw.get("version", "capture-v1")),
    }
    normalized["config_hash"] = canonical_capture_config_hash(normalized)
    return normalized


def validate_candidate_capture_config(
    candidate_capture: dict,
    continuity_gate: dict,
    curve_preference: dict,
) -> None:
    """Validate that continuity enforce or curve active modes have candidate_capture enabled."""
    capture_enabled = bool(candidate_capture.get("enabled", False))
    cont_mode = str(continuity_gate.get("mode", "shadow")).lower()
    cont_enabled = bool(continuity_gate.get("enabled", False))
    curve_mode = str(curve_preference.get("mode", "shadow")).lower()
    curve_enabled = bool(curve_preference.get("enabled", False))

    if cont_enabled and cont_mode == "enforce" and not capture_enabled:
        raise ValueError("continuity enforce mode requires candidate_capture.enabled=true")

    if curve_enabled and curve_mode == "active" and not capture_enabled:
        raise ValueError("curve active mode requires candidate_capture.enabled=true")


def validate_capture(payload: dict) -> list[str]:
    """Validate completeness and integrity of a candidate analysis record payload."""
    reasons: list[str] = []

    src_sha = payload.get("source_video_sha256") or payload.get("source_identity")
    if not isinstance(src_sha, str) or not _SHA256_HEX_RE.match(src_sha):
        reasons.append("source_identity_missing")

    baseline = payload.get("baseline_score")
    if baseline is None:
        baseline = payload.get("base_quality_score")
    if (
        isinstance(baseline, bool)
        or not isinstance(baseline, (int, float))
        or not math.isfinite(baseline)
    ):
        reasons.append("baseline_missing")

    key = payload.get("preexport_key")
    if not isinstance(key, str) or not _SHA256_HEX_RE.match(key):
        reasons.append("preexport_key_missing")

    start_us = payload.get("start_us")
    end_us = payload.get("end_us")
    if (
        start_us is None
        or end_us is None
        or isinstance(start_us, bool)
        or isinstance(end_us, bool)
        or not isinstance(start_us, int)
        or not isinstance(end_us, int)
        or end_us <= start_us
    ):
        reasons.append("window_invalid")

    anchor_us = payload.get("anchor_us")
    if (
        anchor_us is None
        or isinstance(anchor_us, bool)
        or not isinstance(anchor_us, int)
    ):
        reasons.append("anchor_missing")
    elif (
        isinstance(start_us, int)
        and not isinstance(start_us, bool)
        and isinstance(end_us, int)
        and not isinstance(end_us, bool)
        and end_us > start_us
        and not (start_us <= anchor_us <= end_us)
    ):
        reasons.append("anchor_out_of_window")

    run_id = payload.get("run_id")
    if not run_id or not isinstance(run_id, str):
        reasons.append("run_id_missing")

    cap_ver = payload.get("capture_version")
    if not cap_ver or not isinstance(cap_ver, str):
        reasons.append("capture_version_missing")

    cap_status = payload.get("capture_status", "CAPTURED")
    if cap_status not in CAPTURE_STATUSES:
        reasons.append("capture_status_invalid")

    return reasons


def _canonical_payload_bytes(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _safe_path_component(value: str, field: str) -> str:
    """Reject evidence path components that could traverse out of the root."""
    if ".." in value or not _SAFE_COMPONENT_RE.match(value):
        raise ValueError(f"unsafe {field} for evidence path: {value!r}")
    return value


def _contained_evidence_path(evidence_root: Path, *components: str) -> Path:
    """Resolve a content-addressed evidence path and verify it stays inside
    the evidence root (defense in depth against traversal)."""
    root = Path(evidence_root).resolve()
    for component in components:
        _safe_path_component(component, "evidence path component")
    resolved = root.joinpath(*components).resolve()
    root_str = str(root)
    resolved_str = str(resolved)
    if resolved_str != root_str and not resolved_str.startswith(root_str + os.sep):
        raise ValueError(f"evidence path escapes evidence root: {resolved_str!r}")
    return resolved


def _write_content_addressed_file(target_file: Path, payload_bytes: bytes) -> None:
    """Write payload bytes atomically; reuse an identical existing file."""
    if target_file.is_file():
        return
    target_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = target_file.parent / f".tmp.{target_file.name}.{os.getpid()}"
    with open(temp_file, "wb") as handle:
        handle.write(payload_bytes)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_file, target_file)


def _file_sha256_matches(path: Path, expected_sha256: str) -> bool:
    if not path.is_file():
        return False
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest() == expected_sha256


def persist_capture(
    conn: Any,
    payload: dict,
    evidence_root: Path,
) -> PersistedCapture:
    """Persist an immutable capture payload and record it.

    The payload file is content-addressed at
    ``evidence_root/run_id/capture_version/preexport_key/payload_sha256.json``.
    Persisting identical content again is an idempotent no-op; presenting the
    same ``(run_id, preexport_key, capture_version)`` identity with different
    content raises :class:`CaptureConflict` and never updates the old row.
    """
    reasons = validate_capture(payload)
    status = str(payload.get("capture_status", "CAPTURED"))
    if reasons and status not in ("FAILED", "NEEDS_REPAIR"):
        raise ValueError(f"invalid candidate capture payload: {reasons}")

    run_id = _safe_path_component(
        str(payload.get("run_id") or "unknown_run"), "run_id"
    )
    capture_version = str(payload.get("capture_version") or "capture-v1")
    key = payload.get("preexport_key")
    if isinstance(key, str) and _SHA256_HEX_RE.match(key):
        identity_key = key
    elif status in ("FAILED", "NEEDS_REPAIR"):
        # A failure payload without a usable candidate key is content-addressed
        # into its own versioned failure identity instead of a fake key.
        identity_key = hashlib.sha256(_canonical_payload_bytes(payload)).hexdigest()
        capture_version = f"{capture_version}-failure"
    else:
        raise ValueError("invalid candidate capture payload: ['preexport_key_missing']")
    capture_version = _safe_path_component(capture_version, "capture_version")

    payload_bytes = _canonical_payload_bytes(payload)
    payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()

    identity_bytes = f"{run_id}:{identity_key}:{capture_version}".encode("utf-8")
    record_id = hashlib.sha256(identity_bytes).hexdigest()[:32]

    previous_factory = conn.row_factory
    conn.row_factory = None
    try:
        existing = conn.execute(
            """
            SELECT record_id, payload_path, payload_sha256
            FROM candidate_analysis_records
            WHERE run_id = ? AND preexport_key = ? AND capture_version = ?
            """,
            (run_id, identity_key, capture_version),
        ).fetchone()
    finally:
        conn.row_factory = previous_factory

    if existing is not None:
        existing_id, existing_path, existing_sha = existing
        if existing_sha != payload_sha256:
            raise CaptureConflict(
                f"capture identity conflict for run={run_id} "
                f"key={identity_key} version={capture_version}: "
                f"existing payload {existing_sha} != incoming {payload_sha256}"
            )
        if not _file_sha256_matches(Path(existing_path), existing_sha):
            conn.execute(
                "UPDATE candidate_analysis_records SET capture_status = 'NEEDS_REPAIR' "
                "WHERE record_id = ?",
                (existing_id,),
            )
        return PersistedCapture(
            record_id=existing_id,
            payload_path=existing_path,
            payload_sha256=existing_sha,
        )

    target_file = _contained_evidence_path(
        evidence_root, run_id, capture_version, identity_key, f"{payload_sha256}.json"
    )
    _write_content_addressed_file(target_file, payload_bytes)

    anchor_us = payload.get("anchor_us")
    conn.execute(
        """
        INSERT INTO candidate_analysis_records (
            record_id, run_id, preexport_key, capture_version, source_video_sha256,
            start_us, end_us, anchor_us, anchor_source, capture_status,
            failure_reason, payload_path, payload_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_id,
            run_id,
            identity_key,
            capture_version,
            str(payload.get("source_video_sha256") or ""),
            int(payload.get("start_us") or 0),
            int(payload.get("end_us") or 0),
            int(anchor_us) if isinstance(anchor_us, int) and not isinstance(anchor_us, bool) else 0,
            str(payload.get("anchor_source") or "window_midpoint"),
            status if status in CAPTURE_STATUSES else "FAILED",
            json.dumps(reasons) if reasons else payload.get("failure_reason"),
            str(target_file.resolve()),
            payload_sha256,
        ),
    )
    return PersistedCapture(
        record_id=record_id,
        payload_path=str(target_file.resolve()),
        payload_sha256=payload_sha256,
    )


def append_candidate_analysis_event(
    conn: Any,
    record_id: str,
    event_kind: str,
    event_payload,
    evidence_root: Path,
) -> str:
    """Append one analysis event with a content-addressed payload file.

    Identical ``(record_id, event_kind, payload content)`` is idempotent;
    different content produces a NEW event. Historical events are never
    updated or deleted.
    """
    if event_kind not in ANALYSIS_EVENT_KINDS:
        raise ValueError(
            f"invalid candidate analysis event kind: {event_kind!r}, "
            f"expected one of {ANALYSIS_EVENT_KINDS}"
        )

    payload_bytes = _canonical_payload_bytes(event_payload)
    payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    event_id = hashlib.sha256(
        f"{record_id}:{event_kind}:{payload_sha256}".encode("utf-8")
    ).hexdigest()[:32]

    previous_factory = conn.row_factory
    conn.row_factory = None
    try:
        existing = conn.execute(
            """
            SELECT event_id FROM candidate_analysis_events
            WHERE record_id = ? AND event_kind = ? AND payload_sha256 = ?
            """,
            (record_id, event_kind, payload_sha256),
        ).fetchone()
    finally:
        conn.row_factory = previous_factory
    if existing is not None:
        return existing[0]

    target_file = _contained_evidence_path(
        evidence_root, "events", record_id, f"{event_id}.json"
    )
    _write_content_addressed_file(target_file, payload_bytes)

    conn.execute(
        """
        INSERT INTO candidate_analysis_events (
            event_id, record_id, event_kind, payload_path, payload_sha256
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (event_id, record_id, event_kind, str(target_file.resolve()), payload_sha256),
    )
    return event_id


def load_candidate_analysis_events(
    conn: Any,
    record_id: str,
    event_kind=None,
):
    """Load append-only analysis events for one capture record."""
    if event_kind is None:
        rows = conn.execute(
            """
            SELECT event_id, record_id, event_kind, payload_path, payload_sha256, created_at
            FROM candidate_analysis_events WHERE record_id = ?
            ORDER BY created_at ASC, event_id ASC
            """,
            (record_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT event_id, record_id, event_kind, payload_path, payload_sha256, created_at
            FROM candidate_analysis_events
            WHERE record_id = ? AND event_kind = ?
            ORDER BY created_at ASC, event_id ASC
            """,
            (record_id, event_kind),
        ).fetchall()
    return [
        {
            "event_id": row[0],
            "record_id": row[1],
            "event_kind": row[2],
            "payload_path": row[3],
            "payload_sha256": row[4],
            "created_at": row[5],
        }
        for row in rows
    ]


def link_publication(
    conn: Any,
    record_id: str,
    candidate_id: str,
    gif_sha256: str,
) -> None:
    """Link a formal candidate_gifs entry with its preexport analysis record."""
    pub_id = hashlib.sha256(
        f"{record_id}:{candidate_id}:{gif_sha256}".encode("utf-8")
    ).hexdigest()[:32]
    conn.execute(
        """
        INSERT OR IGNORE INTO candidate_analysis_publications (
            publication_id, record_id, candidate_id, gif_sha256
        ) VALUES (?, ?, ?, ?)
        """,
        (pub_id, record_id, candidate_id, gif_sha256),
    )

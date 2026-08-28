#!/usr/bin/env python3
"""W3 historical data audit: pure rule-based scan over T-1 immutable input copies.

Design (docs/superpowers/plans/2026-08-28-token-sprint-w3-data-audit-implementation.md):
- reads ``inputs_manifest.json`` from the sprint root and only opens files whose
  status is ``copied`` after re-verifying their pinned SHA-256 (fail closed, exit 2);
- ``absent`` optional sources (e.g. ``export_results``) are reported under
  ``missing_optional`` and the scan continues -- worktree-relative ``data/exports``
  paths are never read;
- ``--use-snapshot`` verifies ``library_snapshot.db`` against the canonical W1
  artifact recorded in ``sprint_manifest.json`` and queries it read-only
  (``mode=ro`` + ``query_only``); only text columns are interpreted;
- media files are never opened: artifact paths get a pure ``Path.exists`` stat;
- rules: missing_field / score_oob / broken_artifact / mojibake / ts_contradiction;
- low-confidence mojibake texts are packed into ``shards/w3-hard.*.{attempt_id}.jsonl``
  (max 100 lines per shard) for a LATER model triage -- this script never calls any
  model API;
- with ``--publish-ready`` the findings JSON and shard files are published with
  ``protocol.write_ready`` (producer ``w3-executor``); an artifact that already has a
  ``.ready`` sidecar is never modified in place (exit 2 instead).

Exit codes: 0 = scan completed, 2 = fail-closed validation problem.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

# Add project root to path so we can import the coordinator package (same
# pattern as scripts/token_sprint/coordinator/prepare_inputs.py).
_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from scripts.token_sprint.coordinator.common import (  # noqa: E402
    add_sprint_root_argument,
    atomic_write_text,
    resolve_sprint_root,
    sha256_file,
)
from scripts.token_sprint.coordinator.protocol import write_ready  # noqa: E402

INPUTS_SCHEMA = "gifagent.token_sprint.inputs.v1"
INPUTS_MANIFEST_NAME = "inputs_manifest.json"
SPRINT_MANIFEST_NAME = "sprint_manifest.json"
SNAPSHOT_BASENAME = "library_snapshot.db"
PRODUCER = "w3-executor"

DEFAULT_FINDINGS_NAME = "w3-audit-findings.json"
FINDINGS_SUBDIR = "w3"
SHARDS_DIRNAME = "shards"
SHARD_CAP = 100
SHARD_FLOOR = 50
EXCERPT_LIMIT = 240

MEDIA_SUFFIXES = {
    ".gif", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff",
    ".mp4", ".webm", ".mov", ".mkv", ".avi", ".wmv",
}

# Score-like JSON keys the plan pins to [0, 1] (gif_worthiness / final_score /
# sex_act named in the plan; the rest are the same family in this data).
JSON_SCORE_KEYS = {
    "gif_worthiness",
    "final_score",
    "sex_act",
    "profile_score",
    "confidence",
    "transition_risk",
    "action_completeness_score",
    "loop_quality_score",
    "action_boundary_confidence",
    "max_sim",
}

# Mojibake signatures: UTF-8 bytes decoded as Latin-1 / cp1252.
# Strong = cannot occur in clean text (U+0080-U+009F C1 controls, or the
# canonical 0xC3/0xC2/0xE2-lead + continuation-char pairs).
_MOJIBAKE_STRONG = (
    re.compile(r"[\u0080-\u009f]"),
    re.compile(r"[\u00c2\u00c3][\u0080-\u00bf\u00c0-\u00ff]"),
    re.compile(r"\u00e2\u20ac[\u0080-\u00bf\u2018\u2019\u201c\u201d\u201e\u0130]"),
    re.compile(r"[\u00e0-\u00ef][\u0080-\u00bf]"),
)
# Weak = single Latin-1 supplement characters that may be legit accented prose
# (cafe, naive) or the residue of a corruption; low confidence -> hard shard.
_MOJIBAKE_WEAK = re.compile(r"[\u00c0-\u00ff]")

ISO_TS_KEYS = {
    "created_at", "updated_at", "started_at", "finished_at", "indexed_at",
    "undone_at", "completed_at", "published_at", "reviewed_at",
}

# Numeric-second field names eligible for end<start pair checks.
NUMERIC_TS_KEYS = {
    "start_ts", "end_ts", "timestamp", "start_sec", "end_sec", "start", "end",
    "action_start_ts", "action_end_ts", "action_peak_ts", "duration",
}

PATH_FIELD_RE = re.compile(
    r"^(artifact_path|preview_path|export_path|file_path|frame_path|full_path|"
    r"exported_path|source_video_path|path)$"
)
SCORE_COLUMN_RE = re.compile(r"(score|confidence|similarity)$")

# ---------------------------------------------------------------------------
# Snapshot table specs
# ---------------------------------------------------------------------------


class TableSpec:
    """Declarative rule targets for one snapshot table."""

    def __init__(
        self,
        *,
        id_fields,
        required=(),
        path_fields=(),
        score_fields=(),
        ts_pairs=(),
        text_fields=(),
        ref_fields=(),
    ):
        self.id_fields = list(id_fields)
        self.required = list(required)
        self.path_fields = list(path_fields)
        self.score_fields = list(score_fields)
        self.ts_pairs = [tuple(p) for p in ts_pairs]
        self.text_fields = list(text_fields)
        self.ref_fields = list(ref_fields)


SNAPSHOT_TABLE_SPECS = {
    "candidate_gifs": TableSpec(
        id_fields=["candidate_id"],
        required=["candidate_id", "created_at", "updated_at", "start_sec", "end_sec"],
        path_fields=["artifact_path", "preview_path"],
        score_fields=["final_score", "profile_score", "base_rag_similarity"],
        ts_pairs=[("updated_at", "created_at"), ("end_sec", "start_sec")],
    ),
    "media": TableSpec(
        id_fields=["media_id"],
        required=["media_id", "file_path", "created_at"],
        path_fields=["file_path"],
        ts_pairs=[("indexed_at", "created_at")],
    ),
    "frames": TableSpec(
        id_fields=["frame_id"],
        required=["frame_id", "media_id", "frame_path", "timestamp"],
        path_fields=["frame_path"],
        text_fields=["vlm_error"],
    ),
    "annotations": TableSpec(
        id_fields=["annotation_id"],
        required=["annotation_id", "media_id", "created_at"],
        text_fields=["summary", "emotional_core", "why_i_like_it"],
    ),
    "frame_annotations": TableSpec(
        id_fields=["annotation_id"],
        required=["annotation_id", "frame_id", "media_id", "created_at"],
        text_fields=["caption", "emotional_core", "why_i_like_it"],
    ),
    "preference_events": TableSpec(
        id_fields=["event_id"],
        required=["event_id", "created_at"],
        ts_pairs=[("undone_at", "created_at")],
        text_fields=["note"],
    ),
    "favorite_gifs": TableSpec(
        id_fields=["favorite_id"],
        required=["favorite_id", "candidate_id", "created_at"],
        path_fields=["full_path"],
    ),
    "candidate_vectors": TableSpec(
        id_fields=["candidate_id"],
        ref_fields=["candidate_id", "vector_type", "embedding_model"],
        required=["candidate_id", "created_at"],
    ),
    "vector_refs": TableSpec(
        id_fields=["vector_id"],
        required=["vector_id", "created_at"],
    ),
    "preference_profiles": TableSpec(
        id_fields=["profile_id"],
        required=["profile_id", "profile_version", "created_at"],
        score_fields=["confidence"],
    ),
    "preference_profile_builds": TableSpec(
        id_fields=["profile_version"],
        required=["profile_version", "created_at"],
        ts_pairs=[("completed_at", "created_at")],
    ),
    "preference_profile_publications": TableSpec(
        id_fields=["publication_id"],
        required=["publication_id", "profile_version", "published_at"],
    ),
    "preference_profile_current": TableSpec(
        id_fields=["slot"],
        required=["slot", "profile_version", "published_at"],
    ),
    "processing_checkpoint": TableSpec(
        id_fields=["id"],
        required=["id", "phase", "updated_at"],
    ),
    "video_clips": TableSpec(
        id_fields=["clip_id"],
        required=["clip_id", "created_at"],
        path_fields=["exported_path"],
        ts_pairs=[("end", "start")],
    ),
    "collections": TableSpec(
        id_fields=["collection_id"],
        required=["collection_id", "name", "created_at"],
        ts_pairs=[("updated_at", "created_at")],
    ),
    "collection_versions": TableSpec(
        id_fields=["collection_id"],
        ref_fields=["collection_id", "version"],
        required=["collection_id", "version", "created_at"],
    ),
    "collection_items": TableSpec(
        id_fields=["candidate_id"],
        ref_fields=["collection_id", "version", "candidate_id"],
        required=["collection_id", "candidate_id", "created_at"],
        score_fields=["score"],
    ),
    "feedback": TableSpec(
        id_fields=["feedback_id"],
        required=["feedback_id", "media_id"],
        text_fields=["favorite_reason"],
    ),
    "candidate_vector_exclusions": TableSpec(
        id_fields=["candidate_id"],
        ref_fields=["candidate_id", "embedding_model"],
        required=["candidate_id", "created_at"],
        text_fields=["reason", "error_class"],
    ),
}

# Internal/bookkeeping tables that mirror other tables or hold no auditable
# record: skipped with an explicit reason recorded in coverage.
SNAPSHOT_SKIP_TABLES = {
    "search_index_state": "internal incremental-index bookkeeping (single row)",
    "sqlite_sequence": "sqlite internal",
}
SNAPSHOT_SKIP_PREFIXES = ("sqlite_", "candidate_search_fts")


class AuditError(RuntimeError):
    """Fail-closed validation problem; surfaces as exit code 2."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _new_finding(rule, source, record_type, record_ref, *, candidate_id=None,
                 field=None, detail="", confidence="high", hard_case=False,
                 excerpt=None):
    return {
        "rule": rule,
        "source": source,
        "record_type": record_type,
        "record_ref": record_ref,
        "candidate_id": candidate_id,
        "field": field,
        "detail": detail,
        "confidence": confidence,
        "hard_case": hard_case,
        "excerpt": excerpt,
    }


def _looks_absolute(raw: str) -> bool:
    if PureWindowsPath(raw).is_absolute() or PurePosixPath(raw).is_absolute():
        return True
    return bool(re.match(r"^[A-Za-z]:[\\/]", raw)) or raw.startswith(("/", "\\"))


def resolve_artifact_path(raw, data_root: Path | None):
    """Map a stored path to an absolute candidate. Returns (path|None, note).

    Pure path arithmetic + existence stat only; media files are never opened.
    """
    if raw is None:
        return None, "null"
    text = str(raw).strip()
    if not text:
        return None, "empty"
    if _looks_absolute(text):
        return Path(text), "absolute"
    if data_root is None:
        return None, "relative_unchecked"
    return (data_root / text), "relative_to_data_root"


def parse_iso_ts(value):
    """Parse an ISO-ish timestamp; returns (datetime|None, reason)."""
    if not isinstance(value, str) or not value.strip():
        return None, "not_string"
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None, "unparseable"
    return parsed, ("aware" if parsed.tzinfo is not None else "naive")


def _check_pair(end_value, start_value):
    """Compare an (end, start) pair. Returns 'contradiction'|'ok'|'skip'|'mixed_tz'."""
    if isinstance(end_value, (int, float)) and not isinstance(end_value, bool) \
            and isinstance(start_value, (int, float)) and not isinstance(start_value, bool):
        if math.isnan(end_value) or math.isnan(start_value):
            return "skip"
        return "contradiction" if end_value < start_value else "ok"
    end_dt, end_kind = parse_iso_ts(end_value)
    start_dt, start_kind = parse_iso_ts(start_value)
    if end_dt is None or start_dt is None:
        return "skip"
    if end_kind == "aware" and start_kind == "naive":
        return "mixed_tz"
    if end_kind == "naive" and start_kind == "aware":
        return "mixed_tz"
    return "contradiction" if end_dt < start_dt else "ok"


def scan_text_for_mojibake(text: str) -> tuple[str, int]:
    """Classify one text value. Returns ('clean'|'low'|'high', suspicious_count)."""
    if not text:
        return "clean", 0
    for pattern in _MOJIBAKE_STRONG:
        if pattern.search(text):
            return "high", 1
    weak_hits = len(_MOJIBAKE_WEAK.findall(text))
    if weak_hits == 0:
        return "clean", 0
    return "low", weak_hits


def shard_sizes(total: int, cap: int = SHARD_CAP, floor: int = SHARD_FLOOR) -> list[int]:
    """Pack ``total`` lines into shards of <=cap, aiming for >=floor per shard."""
    if total <= 0:
        return []
    if total <= cap:
        return [total]
    n = math.ceil(total / cap)
    while n > 1 and total // n < floor:
        n -= 1
    base, rem = divmod(total, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


def shard_filename(index: int, count: int, attempt_id: str) -> str:
    """w3-hard.{attempt_id}.jsonl for a single shard; numbered when several."""
    if count == 1:
        return f"w3-hard.{attempt_id}.jsonl"
    return f"w3-hard.{index:03d}.{attempt_id}.jsonl"


def write_hard_shards(findings, shards_dir: Path) -> list[dict]:
    """Write hard-case findings into immutable jsonl shards (list-only triage)."""
    hard = [f for f in findings if f.get("hard_case")]
    sizes = shard_sizes(len(hard))
    shards_dir.mkdir(parents=True, exist_ok=True)
    published = []
    cursor = 0
    for index, size in enumerate(sizes, start=1):
        attempt_id = str(uuid.uuid4())
        chunk = hard[cursor:cursor + size]
        cursor += size
        name = shard_filename(index, len(sizes), attempt_id)
        target = shards_dir / name
        if target.exists():
            raise AuditError(f"refusing to overwrite existing shard artifact: {target}")
        lines = []
        for finding in chunk:
            lines.append(json.dumps(
                {
                    "attempt_id": attempt_id,
                    "shard_index": index,
                    "record_ref": finding["record_ref"],
                    "source": finding["source"],
                    "record_type": finding["record_type"],
                    "candidate_id": finding.get("candidate_id"),
                    "rules": [finding["rule"]],
                    "confidence": finding.get("confidence", "low"),
                    "text_excerpt": (finding.get("excerpt") or "")[:EXCERPT_LIMIT],
                    "purpose": "rule-hit-low-confidence; model triage deferred (W3 Task 3)",
                },
                ensure_ascii=False,
            ))
        atomic_write_text(target, "\n".join(lines) + ("\n" if lines else ""))
        published.append(
            {
                "path": target,
                "name": name,
                "attempt_id": attempt_id,
                "lines": len(lines),
                "sha256": sha256_file(target),
            }
        )
    return published


# ---------------------------------------------------------------------------
# JSON sources
# ---------------------------------------------------------------------------


def _walk_strings(node, path="$"):
    """Yield (json_pointer_path, value) for every string in a JSON tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}"
            if isinstance(value, str):
                yield child, value
            else:
                yield from _walk_strings(value, child)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            child = f"{path}[{index}]"
            if isinstance(value, str):
                yield child, value
            else:
                yield from _walk_strings(value, child)


def _walk_scores(node, path="$"):
    """Yield (json_pointer_path, key, numeric value) for score-like keys."""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}"
            if key in JSON_SCORE_KEYS and isinstance(value, (int, float)) \
                    and not isinstance(value, bool):
                yield child, key, value
            else:
                yield from _walk_scores(value, child)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_scores(value, f"{path}[{index}]")


def _json_record_findings(records, source, record_type, *, candidate_via,
                          required, path_fields, ts_pairs, data_root,
                          observations):
    findings = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            findings.append(_new_finding(
                "missing_field", source, record_type, f"{record_type}[{index}]",
                field="<record>", detail=f"record is {type(record).__name__}, not an object",
            ))
            continue
        candidate_id = None
        if candidate_via:
            node = record
            for key in candidate_via.split("."):
                node = node.get(key) if isinstance(node, dict) else None
            candidate_id = node if isinstance(node, str) else None
        ref = f"{record_type}[{index}]"
        for field in required:
            value = record.get(field)
            if value is None or value == "":
                findings.append(_new_finding(
                    "missing_field", source, record_type, ref,
                    candidate_id=candidate_id, field=field,
                    detail=f"agreed key '{field}' is missing or null",
                ))
        for path_field in path_fields:
            raw = record.get(path_field)
            if raw is None or str(raw).strip() == "":
                continue
            resolved, note = resolve_artifact_path(raw, data_root)
            if resolved is None:
                if note == "relative_unchecked":
                    observations.append(
                        {"kind": "relative_path_unchecked", "source": source,
                         "record_ref": ref, "field": path_field})
                continue
            if not resolved.exists():
                findings.append(_new_finding(
                    "broken_artifact", source, record_type, ref,
                    candidate_id=candidate_id, field=path_field,
                    detail=f"path does not exist (stat only, {note}): {raw}",
                ))
        for end_field, start_field in ts_pairs:
            end_value = record.get(end_field)
            start_value = record.get(start_field)
            if end_value is None or start_value is None:
                continue
            verdict = _check_pair(end_value, start_value)
            if verdict == "contradiction":
                findings.append(_new_finding(
                    "ts_contradiction", source, record_type, ref,
                    candidate_id=candidate_id, field=f"{end_field}<{start_field}",
                    detail=f"{end_field}={end_value!r} is before {start_field}={start_value!r}",
                ))
            elif verdict == "mixed_tz":
                observations.append(
                    {"kind": "ts_pair_mixed_tz_uncomparable", "source": source,
                     "record_ref": ref, "field": f"{end_field}<{start_field}"})
        # mojibake over the record's own string values
        for path, value in _walk_strings(record):
            verdict, hits = scan_text_for_mojibake(value)
            if verdict == "clean":
                continue
            findings.append(_new_finding(
                "mojibake", source, record_type, ref,
                candidate_id=candidate_id, field=path.rsplit(".", 1)[-1],
                detail=f"mojibake signature ({verdict} confidence), {hits} suspicious chars",
                confidence=("high" if verdict == "high" else "low"),
                hard_case=(verdict != "high"),
                excerpt=value[:EXCERPT_LIMIT],
            ))
    return findings


def scan_adaptive_result(data, data_root, observations):
    """Scan the adaptive test result JSON (run report + top_clips + gif_exports)."""
    findings = []
    source = "adaptive_test_result"
    if not isinstance(data, dict):
        return [_new_finding("missing_field", source, "run", "$",
                             detail="top-level JSON is not an object")]

    top_clips = data.get("top_clips") or []
    findings += _json_record_findings(
        top_clips, source, "top_clips",
        candidate_via=None,
        required=["candidate_id", "rank", "timestamp", "start_ts", "end_ts"],
        path_fields=["export_path"],
        ts_pairs=[("end_ts", "start_ts"), ("action_end_ts", "action_start_ts")],
        data_root=data_root, observations=observations,
    )
    gif_exports = data.get("gif_exports") or []
    findings += _json_record_findings(
        gif_exports, source, "gif_exports",
        candidate_via="quality_assessment.candidate_id",
        required=["index", "path", "status", "start_ts", "end_ts"],
        path_fields=["path"],
        ts_pairs=[("end_ts", "start_ts")],
        data_root=data_root, observations=observations,
    )

    # Score-like values anywhere in the tree must lie in [0, 1].
    for path, key, value in _walk_scores(data):
        if isinstance(value, float) and math.isnan(value) or not (0.0 <= value <= 1.0):
            findings.append(_new_finding(
                "score_oob", source, "run", path,
                field=key, detail=f"value {value!r} outside [0,1]",
            ))

    # Run-level observation: is the source video still present? (stat only)
    video = data.get("video")
    if isinstance(video, str) and video.strip():
        resolved, note = resolve_artifact_path(video, data_root)
        if resolved is not None and not resolved.exists():
            observations.append(
                {"kind": "source_video_missing", "source": source,
                 "record_ref": "$.video", "field": "video", "path": video})
    return findings


def scan_batch_checkpoint(data, data_root, observations):
    """Scan the batch processing checkpoint JSON."""
    findings = []
    source = "batch_checkpoint"
    if not isinstance(data, dict):
        return [_new_finding("missing_field", source, "checkpoint", "$",
                             detail="top-level JSON is not an object")]

    for field in ("started_at", "updated_at"):
        if data.get(field) in (None, ""):
            findings.append(_new_finding(
                "missing_field", source, "checkpoint", "$", field=field,
                detail=f"agreed key '{field}' is missing or null",
            ))
    end_value, start_value = data.get("updated_at"), data.get("started_at")
    if end_value is not None and start_value is not None:
        verdict = _check_pair(end_value, start_value)
        if verdict == "contradiction":
            findings.append(_new_finding(
                "ts_contradiction", source, "checkpoint", "$",
                field="updated_at<started_at",
                detail=f"updated_at={end_value!r} before started_at={start_value!r}",
            ))
        elif verdict == "mixed_tz":
            observations.append(
                {"kind": "ts_pair_mixed_tz_uncomparable", "source": source,
                 "record_ref": "$", "field": "updated_at<started_at"})

    last_run = data.get("last_run")
    if isinstance(last_run, dict):
        findings += _json_record_findings(
            [last_run], source, "last_run",
            candidate_via=None,
            required=["started_at", "status"],
            path_fields=["dir"],
            ts_pairs=[("updated_at", "started_at"), ("finished_at", "started_at")],
            data_root=data_root, observations=observations,
        )
    else:
        findings.append(_new_finding(
            "missing_field", source, "last_run", "$.last_run", field="last_run",
            detail="agreed key 'last_run' is missing or not an object",
        ))

    completed = data.get("completed")
    if isinstance(completed, dict):
        records = []
        for path_key, entry in completed.items():
            record = dict(entry) if isinstance(entry, dict) else {}
            record["completed_path"] = path_key
            records.append(record)
        findings += _json_record_findings(
            records, source, "completed_entry",
            candidate_via=None,
            required=["status", "finished_at"],
            path_fields=["completed_path"],
            ts_pairs=[],
            data_root=data_root, observations=observations,
        )
    else:
        findings.append(_new_finding(
            "missing_field", source, "completed", "$.completed", field="completed",
            detail="agreed key 'completed' is missing or not an object",
        ))

    for path, value in _walk_strings(data):
        verdict, hits = scan_text_for_mojibake(value)
        if verdict == "clean":
            continue
        findings.append(_new_finding(
            "mojibake", source, "checkpoint", path,
            field=path.rsplit(".", 1)[-1],
            detail=f"mojibake signature ({verdict} confidence), {hits} suspicious chars",
            confidence=("high" if verdict == "high" else "low"),
            hard_case=(verdict != "high"),
            excerpt=value[:EXCERPT_LIMIT],
        ))
    return findings


# ---------------------------------------------------------------------------
# Snapshot (sqlite, read-only)
# ---------------------------------------------------------------------------


def _table_columns(cursor, table):
    return [row[1] for row in cursor.execute(f'PRAGMA table_info("{table}")')]


def _generic_spec(columns) -> TableSpec:
    path_fields = [c for c in columns if PATH_FIELD_RE.match(c)]
    score_fields = [c for c in columns
                    if SCORE_COLUMN_RE.search(c) and c not in path_fields]
    ts_single = [c for c in columns if c in ISO_TS_KEYS]
    pairs = [("updated_at", "created_at")] if {"updated_at", "created_at"} <= set(columns) else []
    text_fields = [c for c in columns
                   if not c.endswith(("_json", "_blob"))
                   and c not in path_fields and c not in score_fields
                   and c not in ts_single and not c.endswith(("_ts", "_at", "_sec"))]
    id_fields = [c for c in ("candidate_id", "media_id", "annotation_id", "frame_id",
                             "event_id", "feedback_id", "vector_id", "clip_id",
                             "collection_id", "profile_id", "slot")
                 if c in columns][:1] or columns[:1]
    required = list(id_fields)
    if "created_at" in columns:
        required.append("created_at")
    return TableSpec(
        id_fields=id_fields, required=required, path_fields=path_fields,
        score_fields=score_fields, ts_pairs=pairs, text_fields=text_fields,
    )


def scan_snapshot(db_path: Path, data_root, observations):
    """Read-only rule scan over the W1 snapshot's text/numeric columns."""
    findings = []
    source = "library_snapshot"
    uri = f"file:{db_path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        cursor = connection.cursor()
        tables = [
            row[0] for row in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        scanned, skipped = [], []
        for table in tables:
            if table in SNAPSHOT_SKIP_TABLES:
                skipped.append({"table": table, "reason": SNAPSHOT_SKIP_TABLES[table]})
                continue
            if table.startswith(SNAPSHOT_SKIP_PREFIXES):
                skipped.append({
                    "table": table,
                    "reason": "FTS shadow/internal mirror of candidate_gifs",
                })
                continue
            columns = _table_columns(cursor, table)
            spec = SNAPSHOT_TABLE_SPECS.get(table) or _generic_spec(columns)
            rows_scanned = _scan_table(
                cursor, table, columns, spec, source, data_root, findings, observations,
            )
            scanned.append({"table": table, "rows": rows_scanned, "spec": (
                "explicit" if table in SNAPSHOT_TABLE_SPECS else "generic")})
        observations.append({"kind": "snapshot_tables_scanned", "tables": scanned})
        observations.append({"kind": "snapshot_tables_skipped", "tables": skipped})
    finally:
        connection.close()
    return findings


def _scan_table(cursor, table, columns, spec, source, data_root, findings,
                observations):
    select_cols = ", ".join(f'"{c}"' for c in columns)
    score_columns = [c for c in spec.score_fields if c in columns]
    try:
        cursor.execute(f"SELECT rowid, {select_cols} FROM \"{table}\"")
    except sqlite3.OperationalError:
        cursor.execute(f"SELECT -1 AS __rowid, {select_cols} FROM \"{table}\"")
    names = ["__rowid"] + columns
    rows_scanned = 0
    score_null_counts = {c: 0 for c in score_columns}
    while True:
        batch = cursor.fetchmany(2000)
        if not batch:
            break
        for row in batch:
            rows_scanned += 1
            record = dict(zip(names, row))
            rowid = record.pop("__rowid")
            ref_values = [str(record.get(f)) for f in (spec.ref_fields or spec.id_fields)
                          if record.get(f) is not None]
            ref = f"{table}#{rowid}" + (f" ({'/'.join(ref_values)})" if ref_values else "")
            candidate_id = next(
                (record.get(f) for f in ("candidate_id", "media_id", "event_id",
                                         "favorite_id", "annotation_id", "frame_id",
                                         "vector_id", "clip_id", "feedback_id")
                 if record.get(f) is not None),
                None,
            )
            for field in spec.required:
                if field not in columns:
                    continue
                value = record.get(field)
                if value is None or (isinstance(value, str) and value.strip() == ""):
                    findings.append(_new_finding(
                        "missing_field", source, table, ref,
                        candidate_id=candidate_id, field=field,
                        detail=f"agreed column '{field}' is missing or null",
                    ))
            for field in score_columns:
                value = record.get(field)
                if value is None:
                    score_null_counts[field] += 1
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                if math.isnan(value) or not (0.0 <= value <= 1.0):
                    findings.append(_new_finding(
                        "score_oob", source, table, ref,
                        candidate_id=candidate_id, field=field,
                        detail=f"value {value!r} outside [0,1]",
                    ))
            for field in spec.path_fields:
                if field not in columns:
                    continue
                raw = record.get(field)
                if raw is None or str(raw).strip() == "":
                    continue
                resolved, note = resolve_artifact_path(raw, data_root)
                if resolved is None:
                    if note == "relative_unchecked":
                        observations.append(
                            {"kind": "relative_path_unchecked", "source": source,
                             "record_ref": ref, "field": field})
                    continue
                if not resolved.exists():
                    findings.append(_new_finding(
                        "broken_artifact", source, table, ref,
                        candidate_id=candidate_id, field=field,
                        detail=f"path does not exist (stat only, {note}): {raw}",
                    ))
            for end_field, start_field in spec.ts_pairs:
                if end_field not in columns or start_field not in columns:
                    continue
                end_value, start_value = record.get(end_field), record.get(start_field)
                if end_value is None or start_value is None:
                    continue
                verdict = _check_pair(end_value, start_value)
                if verdict == "contradiction":
                    findings.append(_new_finding(
                        "ts_contradiction", source, table, ref,
                        candidate_id=candidate_id,
                        field=f"{end_field}<{start_field}",
                        detail=(f"{end_field}={end_value!r} is before "
                                f"{start_field}={start_value!r}"),
                    ))
                elif verdict == "mixed_tz":
                    observations.append(
                        {"kind": "ts_pair_mixed_tz_uncomparable", "source": source,
                         "record_ref": ref, "field": f"{end_field}<{start_field}"})
            for field in spec.text_fields:
                if field not in columns:
                    continue
                value = record.get(field)
                if not isinstance(value, str) or not value:
                    continue
                verdict, hits = scan_text_for_mojibake(value)
                if verdict == "clean":
                    continue
                findings.append(_new_finding(
                    "mojibake", source, table, ref,
                    candidate_id=candidate_id, field=field,
                    detail=(f"mojibake signature ({verdict} confidence), "
                            f"{hits} suspicious chars"),
                    confidence=("high" if verdict == "high" else "low"),
                    hard_case=(verdict != "high"),
                    excerpt=value[:EXCERPT_LIMIT],
                ))
    nulls = {field: count for field, count in score_null_counts.items() if count}
    if nulls:
        observations.append({"kind": "score_column_nulls", "source": source,
                             "table": table, "counts": nulls})
    return rows_scanned


# ---------------------------------------------------------------------------
# Mojibake text sample (repo-root version2_*.md, read as text only)
# ---------------------------------------------------------------------------


def scan_mojibake_sample(path: Path):
    findings = []
    source = "version2_doc"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return [_new_finding("mojibake", source, "doc", path.name,
                             detail=f"sample unreadable: {exc}")]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        findings.append(_new_finding(
            "mojibake", source, "doc", path.name, field="<file>",
            detail="file bytes are not valid UTF-8 (decode('utf-8') fails)",
            confidence="high", excerpt="",
        ))
        text = raw.decode("utf-8", errors="replace")
    for number, line in enumerate(text.splitlines(), start=1):
        verdict, hits = scan_text_for_mojibake(line)
        if verdict == "clean":
            continue
        findings.append(_new_finding(
            "mojibake", source, "doc", f"{path.name}:{number}", field="line",
            detail=f"mojibake signature ({verdict} confidence), {hits} suspicious chars",
            confidence=("high" if verdict == "high" else "low"),
            hard_case=(verdict != "high"),
            excerpt=line[:EXCERPT_LIMIT],
        ))
    return findings


# ---------------------------------------------------------------------------
# Manifest validation (fail closed)
# ---------------------------------------------------------------------------


def validate_inputs_manifest(sprint_root: Path) -> tuple[dict, list[dict]]:
    manifest_path = sprint_root / INPUTS_MANIFEST_NAME
    if not manifest_path.is_file():
        raise AuditError(f"inputs manifest not found: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"inputs manifest unreadable: {exc}") from exc
    if manifest.get("schema") != INPUTS_SCHEMA:
        raise AuditError(
            f"unexpected inputs manifest schema: {manifest.get('schema')!r}"
        )
    missing_optional = []
    files = manifest.get("files") or []
    if not isinstance(files, list):
        raise AuditError("inputs manifest 'files' is not a list")
    for entry in files:
        key = entry.get("key", "?")
        status = entry.get("status")
        if entry.get("required") and status != "copied":
            raise AuditError(
                f"required input '{key}' is not copied (status={status!r})"
            )
        if status == "copied":
            copy_rel = entry.get("copy_rel")
            if not copy_rel:
                raise AuditError(f"copied input '{key}' has no copy_rel")
            copy_path = sprint_root / copy_rel
            if not copy_path.is_file():
                raise AuditError(f"copied input '{key}' missing on disk: {copy_path}")
            actual = sha256_file(copy_path)
            expected = entry.get("sha256")
            if actual != expected:
                raise AuditError(
                    f"copied input '{key}' sha256 mismatch "
                    f"(manifest={expected}, actual={actual}) -- fail closed"
                )
        elif status == "absent":
            missing_optional.append(entry)
        else:
            raise AuditError(f"input '{key}' has unknown status {status!r}")
    return manifest, missing_optional


def validate_snapshot(sprint_root: Path) -> Path:
    snapshot_path = sprint_root / SNAPSHOT_BASENAME
    if not snapshot_path.is_file():
        raise AuditError(f"snapshot db not found: {snapshot_path}")
    sprint_manifest_path = sprint_root / SPRINT_MANIFEST_NAME
    if not sprint_manifest_path.is_file():
        raise AuditError(f"canonical sprint manifest not found: {sprint_manifest_path}")
    try:
        sprint_manifest = json.loads(sprint_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"canonical sprint manifest unreadable: {exc}") from exc
    artifacts = sprint_manifest.get("artifacts") or {}
    canonical = [
        entry for entry in artifacts.values()
        if isinstance(entry, dict)
        and Path(entry.get("artifact_path", "")).name == SNAPSHOT_BASENAME
        and entry.get("workstream") == "w1"
    ]
    if len(canonical) != 1:
        raise AuditError(
            f"expected exactly one canonical W1 snapshot artifact, found {len(canonical)}"
        )
    expected = canonical[0].get("artifact_sha256")
    actual = sha256_file(snapshot_path)
    if actual != expected:
        raise AuditError(
            f"snapshot sha256 mismatch (canonical={expected}, actual={actual}) -- fail closed"
        )
    return snapshot_path


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


_AGGREGATABLE_OBSERVATIONS = (
    "relative_path_unchecked", "ts_pair_mixed_tz_uncomparable", "source_video_missing",
)


def _aggregate_observations(observations):
    """Collapse repeated per-record observations into counted entries."""
    aggregated, counts = [], {}
    for observation in observations:
        if observation.get("kind") in _AGGREGATABLE_OBSERVATIONS:
            key = json.dumps(observation, sort_keys=True, ensure_ascii=False)
            counts[key] = counts.get(key, 0) + 1
        else:
            aggregated.append(observation)
    for key, count in counts.items():
        merged = json.loads(key)
        merged["count"] = count
        aggregated.append(merged)
    return aggregated


def _counts_by_rule(findings):
    counts = {}
    for finding in findings:
        counts[finding["rule"]] = counts.get(finding["rule"], 0) + 1
    return dict(sorted(counts.items()))


def _counts_by_source_rule(findings):
    counts = {}
    for finding in findings:
        counts.setdefault(finding["source"], {}).setdefault(finding["rule"], 0)
        counts[finding["source"]][finding["rule"]] += 1
    return counts


def render_report(result: dict) -> str:
    """Render the Chinese-language markdown audit report."""
    lines = [
        "# W3 历史数据体检报告（纯规则扫描）",
        "",
        f"- 生成时间：{result['generated_at']}",
        f"- 共享根：`{result['sprint_root']}`",
        f"- 扫描器：`scripts/token_sprint/audit_data.py`（零模型调用）",
        f"- 数据根（存在性 stat 基准，不读内容）：`{result.get('data_root')}`",
        "",
        "## 一、输入覆盖率（inputs_manifest 逐项）",
        "",
        "| key | 状态 | SHA 校验 | 记录数 | 命中 |",
        "|-----|------|----------|--------|------|",
    ]
    for item in result["coverage"]:
        lines.append(
            f"| {item['key']} | {item['status']} | {item['sha_verified']} | "
            f"{item['records_scanned']} | {item['findings']} |"
        )
    absent = result.get("missing_optional") or []
    for entry in absent:
        lines.append(
            f"\n**absent 项 fallback 说明**：`{entry['key']}`（`{entry.get('source_abs')}`）"
            "在 T-1 收集时不存在，inputs_manifest 标记为 `absent`；按计划以 "
            "`missing_optional` 记录并继续。本轮未读取任何 worktree 相对路径 "
            "`data/exports/`，export_results 维度的规则命中数为 0 仅表示\"无输入\"，"
            "不代表该数据源健康。"
        )
    lines += [
        "",
        "## 二、规则命中汇总",
        "",
        "| 规则 | 命中数 | 其中疑难片（hard_case） |",
        "|------|--------|--------------------------|",
    ]
    for rule in ("missing_field", "score_oob", "broken_artifact", "mojibake",
                 "ts_contradiction"):
        total = result["rule_counts"].get(rule, 0)
        hard = sum(1 for f in result["findings"] if f["rule"] == rule and f.get("hard_case"))
        lines.append(f"| `{rule}` | {total} | {hard} |")
    lines += ["", "### 按数据源细分", "", "| 数据源 | 规则 | 命中 |", "|--------|------|------|"]
    for source, rules in sorted(result["counts_by_source_rule"].items()):
        for rule, count in sorted(rules.items()):
            lines.append(f"| {source} | `{rule}` | {count} |")
    lines += [
        "",
        "## 三、命中样例（每规则最多 10 条 record_ref）",
        "",
    ]
    for rule in sorted(result["rule_counts"]):
        lines.append(f"### `{rule}`")
        lines.append("")
        samples = [f for f in result["findings"] if f["rule"] == rule][:10]
        for sample in samples:
            excerpt = (sample.get("excerpt") or "").replace("\n", " ")[:80]
            suffix = f" — {excerpt}" if excerpt else ""
            lines.append(f"- `{sample['record_ref']}` 字段 `{sample.get('field')}`"
                         f"（{sample['source']}）：{sample['detail']}{suffix}")
        lines.append("")
    lines += ["## 四、疑难样本分片清单（本轮仅列片，不派模型）", ""]
    shards = result.get("shards") or []
    if not shards:
        lines.append("无疑难片（低置信命中为 0）。")
    else:
        lines += ["| 分片文件 | 行数 | attempt_id | sha256 |", "|----------|------|------------|--------|"]
        for shard in shards:
            lines.append(
                f"| `{shard['name']}` | {shard['lines']} | `{shard['attempt_id']}` | "
                f"`{shard['sha256']}` |"
            )
        lines.append("")
        lines.append("分片纪律：每片 ≤100 行；`attempt_id` 为 UUID，禁止覆盖旧产物；"
                     "分片只含文本摘录与记录引用，不含任何媒体内容。")
    lines += ["", "## 五、结构性观察（非规则命中，供修复决策参考）", ""]
    for observation in result.get("observations", []):
        kind = observation.get("kind")
        if kind == "score_column_nulls":
            lines.append(
                f"- 全表分数列为 null：`{observation['table']}` {observation['counts']}"
                "（按规则定义 null 分数不计 `score_oob`/`missing_field`，单独列观察）"
            )
        elif kind == "snapshot_tables_scanned":
            explicit = sum(1 for t in observation["tables"] if t["spec"] == "explicit")
            total_rows = sum(t["rows"] for t in observation["tables"])
            lines.append(
                f"- 快照表覆盖：{len(observation['tables'])} 张表（显式规则 {explicit} 张、"
                f"通用规则 {len(observation['tables']) - explicit} 张），共 {total_rows} 行"
            )
        elif kind == "snapshot_tables_skipped":
            names = ", ".join(f"`{t['table']}`" for t in observation["tables"])
            lines.append(f"- 快照跳过表（镜像/内部）：{names}")
        elif kind == "ts_pair_mixed_tz_uncomparable":
            lines.append(
                f"- 混合时区时间戳对（naive vs aware，无法可靠比较，未计命中，"
                f"共 {observation.get('count', 1)} 处）：{observation['record_ref']} "
                f"{observation['field']}"
            )
        elif kind == "source_video_missing":
            lines.append(
                f"- 源视频已不存在（仅 stat 探测，未读内容，共 "
                f"{observation.get('count', 1)} 处）：`{observation.get('path')}`"
            )
        elif kind == "relative_path_unchecked":
            lines.append(
                f"- 相对路径无 stat 基准未检查（共 {observation.get('count', 1)} 处）："
                f"`{observation['record_ref']}` {observation['field']}"
            )
    lines += [
        "",
        "## 六、方法与限制",
        "",
        "- 本轮为纯规则扫描，零模型调用；疑难片仅产出清单，模型定级留待 Task 3。",
        "- 全部输入为 T-1 不可变副本，SHA-256 逐项复核一致后才扫描（fail closed）。",
        "- `artifact_path` 类字段只做 `Path.exists` 存在性 stat，未 open 任何 "
        "GIF/JPG/视频；相对路径以生产检出根为基准解析，未使用 worktree 相对路径。",
        "- LLM 抽查（20 条无误报门）：本轮 N/A（未派模型），规则结果照常交付。",
        f"- 产物 JSON：`{result.get('findings_path')}`",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _publish(artifact: Path, qc_summary: dict) -> Path:
    return write_ready(artifact, producer=PRODUCER, qc_summary=qc_summary)


def run_audit(args: argparse.Namespace) -> tuple[int, dict]:
    sprint_root = resolve_sprint_root(args.sprint_root)
    data_root = Path(args.data_root).resolve() if args.data_root else None
    observations: list[dict] = []

    findings_path = Path(args.out_json) if args.out_json else (
        sprint_root / FINDINGS_SUBDIR / DEFAULT_FINDINGS_NAME
    )
    ready_path = findings_path.with_name(findings_path.name + ".ready")
    if ready_path.exists():
        raise AuditError(
            f"findings artifact already published with .ready; refusing in-place "
            f"modification (design global constraint 12): {findings_path}"
        )

    manifest, missing_optional = validate_inputs_manifest(sprint_root)

    findings: list[dict] = []
    coverage: list[dict] = []
    snapshot_scanned = False
    snapshot_path = None
    if args.use_snapshot:
        snapshot_path = validate_snapshot(sprint_root)
        snapshot_scanned = True

    source_status = {entry["key"]: entry.get("status") for entry in manifest["files"]}
    records_per_key: dict[str, int] = {}

    for entry in manifest["files"]:
        key = entry["key"]
        status = entry.get("status")
        if status != "copied":
            coverage.append({
                "key": key, "status": status, "sha_verified": "n/a",
                "records_scanned": 0, "findings": 0,
            })
            continue
        copy_path = sprint_root / entry["copy_rel"]
        text = copy_path.read_bytes()
        try:
            data = json.loads(text.decode("utf-8"))
        except UnicodeDecodeError:
            findings.append(_new_finding(
                "mojibake", key, "file", entry["copy_rel"], field="<file>",
                detail="input copy is not valid UTF-8", confidence="high",
            ))
            data = json.loads(text.decode("utf-8", errors="replace"))
        before = len(findings)
        if key == "adaptive_test_result":
            findings += scan_adaptive_result(data, data_root, observations)
            records_per_key[key] = len(data.get("top_clips") or []) + len(data.get("gif_exports") or [])
        elif key == "batch_checkpoint":
            findings += scan_batch_checkpoint(data, data_root, observations)
            records_per_key[key] = len(data.get("completed") or {})
        else:
            observations.append({"kind": "unknown_input_key", "key": key})
            records_per_key[key] = 0
        coverage.append({
            "key": key, "status": status, "sha_verified": "ok",
            "records_scanned": records_per_key.get(key, 0),
            "findings": len(findings) - before,
        })

    if snapshot_scanned:
        before = len(findings)
        findings += scan_snapshot(snapshot_path, data_root, observations)
        rows = 0
        for observation in observations:
            if observation.get("kind") == "snapshot_tables_scanned":
                rows = sum(t["rows"] for t in observation["tables"])
        coverage.append({
            "key": "library_snapshot", "status": "copied", "sha_verified": "ok",
            "records_scanned": rows, "findings": len(findings) - before,
        })
    else:
        coverage.append({
            "key": "library_snapshot", "status": "not_requested", "sha_verified": "n/a",
            "records_scanned": 0, "findings": 0,
        })
    # export_results absent -> explicit fallback marker in coverage
    for entry in missing_optional:
        coverage.append({
            "key": entry["key"], "status": "absent_fallback",
            "sha_verified": "n/a", "records_scanned": 0, "findings": 0,
        })
        source_status.setdefault(entry["key"], "absent")

    sample_scanned = False
    sample_path = None
    if args.mojibake_sample:
        sample_path = Path(args.mojibake_sample)
    elif args.scan_mojibake_sample:
        candidates = sorted(_project_root.glob("version2_*.md"))
        sample_path = candidates[0] if candidates else None
    if sample_path is not None and sample_path.is_file():
        before = len(findings)
        findings += scan_mojibake_sample(sample_path)
        coverage.append({
            "key": "version2_doc", "status": "copied", "sha_verified": "n/a",
            "records_scanned": 1, "findings": len(findings) - before,
        })
        sample_scanned = True

    shards = write_hard_shards(findings, sprint_root / SHARDS_DIRNAME)

    result = {
        "schema": "gifagent.token_sprint.w3_audit.v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sprint_root": sprint_root.as_posix(),
        "data_root": data_root.as_posix() if data_root else None,
        "use_snapshot": snapshot_scanned,
        "mojibake_sample": (sample_path.as_posix() if sample_scanned else None),
        "missing_optional": missing_optional,
        "coverage": coverage,
        "rule_counts": _counts_by_rule(findings),
        "counts_by_source_rule": _counts_by_source_rule(findings),
        "hard_case_count": sum(1 for f in findings if f.get("hard_case")),
        "shards": [
            {"name": s["name"], "path": s["path"].as_posix(), "lines": s["lines"],
             "attempt_id": s["attempt_id"], "sha256": s["sha256"]}
            for s in shards
        ],
        "observations": _aggregate_observations(observations),
        "findings": findings,
    }

    findings_path.parent.mkdir(parents=True, exist_ok=True)
    qc_summary = {
        "rule_counts": result["rule_counts"],
        "hard_case_count": result["hard_case_count"],
        "records_scanned": sum(item["records_scanned"] for item in coverage),
        "shards": [
            {"name": s["name"], "sha256": s["sha256"], "lines": s["lines"]}
            for s in result["shards"]
        ],
    }
    atomic_write_text(
        findings_path,
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
    )
    result["findings_path"] = findings_path.as_posix()

    if args.publish_ready:
        ready = _publish(findings_path, qc_summary)
        for shard in shards:
            _publish(shard["path"], {
                "lines": shard["lines"],
                "hard_case_rule": "mojibake-low-confidence",
                "model_triage": "deferred to W3 Task 3",
            })
        result["ready_paths"] = [ready.as_posix()] + [
            (shard["path"].with_name(shard["path"].name + ".ready")).as_posix()
            for shard in shards
        ]

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(report_path, render_report(result))
        result["report_path"] = report_path.as_posix()

    return 0, result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_sprint_root_argument(parser)
    parser.add_argument(
        "--use-snapshot", action="store_true",
        help="also scan library_snapshot.db after verifying it against the "
             "canonical W1 artifact SHA",
    )
    parser.add_argument(
        "--data-root", default=None,
        help="base directory for relative artifact paths (existence stat only); "
             "typically the production checkout root",
    )
    parser.add_argument("--out-json", default=None,
                        help="findings JSON path (default <sprint-root>/w3/w3-audit-findings.json)")
    parser.add_argument("--report", default=None,
                        help="optional markdown report output path")
    parser.add_argument("--mojibake-sample", default=None,
                        help="explicit text sample scanned with the mojibake rule only")
    parser.add_argument(
        "--scan-mojibake-sample", action="store_true",
        help="scan the repo-root version2_*.md as a mojibake text sample (read as "
             "text only, per plan; never any media file)",
    )
    parser.add_argument(
        "--publish-ready", action="store_true",
        help="publish .ready sidecars (producer=w3-executor) for the findings "
             "JSON and each hard-case shard; never modifies published artifacts",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code, result = run_audit(args)
    except AuditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "rule_counts": result["rule_counts"],
        "hard_case_count": result["hard_case_count"],
        "findings_path": result["findings_path"],
        "report_path": result.get("report_path"),
        "ready_paths": result.get("ready_paths", []),
    }, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())

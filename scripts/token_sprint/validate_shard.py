#!/usr/bin/env python3
"""Per-shard QC for W1 enrichment outputs: schema, refusals, fidelity heuristic.

W1 deterministic tool (zero model tokens). Validates one model-produced JSONL
shard against its input shard and writes a QC summary JSON to stdout and
``<shard>.qc.json`` (atomic write)::

    {
      "n": 0,
      "schema_ok_rate": 0.0,
      "refuse_rate": 0.0,
      "fidelity_rate_high_stratum": null,
      "suspected_sanitized": [],
      "errors": []
    }

Counting semantics (locked):
- *n* is the number of output lines.
- Refusal rows ``{"candidate_id": "...", "refused": true}`` count towards
  ``refuse_rate`` and are **not** schema failures.
- ``schema_ok_rate`` = schema-valid rows / non-refusal rows (1.0 when there are
  no non-refusal rows), so pure refusals never drag it down.
- Fidelity heuristic (high-explicit layer): an output row is in the fidelity
  denominator when its input row has ``stratum == "high"`` or its input text
  contains an ``EXPLICIT_MARKERS`` entry. The serialized output text must
  retain at least one marker or synonymous stem; zero retention records the
  candidate in ``suspected_sanitized`` and counts as a fidelity failure.
  Legitimate JSON with sanitized semantics is a failure, not a success.

Pass schemas (v1):
- ``p3``: ``keywords`` is a list of 3-12 lowercase English tokens
  (``^[a-z0-9_]+$``).
- ``p1b``: ``tags`` is a list[str] subset of ``<sprint-root>/tag_vocab_v1.json``
  (when the vocab file does not exist yet the subset check is skipped and the
  QC marks ``"vocab": "missing"``).
- ``p2``: ``scene`` is an object with at least the keys ``setting``, ``act``,
  ``finish`` (values may be null).
- ``p1a``: word-aggregate output (no per-candidate schema); every aggregated
  word must match ``^[a-z0-9_]+$``.

T0 gates (probe): refuse rate <= 15%, schema ok rate >= 95%, high-explicit
fidelity >= 90%. The script only computes the heuristics; with ``--t0-gate`` a
failed gate turns into exit code 3 (human review stays mandatory regardless).

Usage:
    python scripts/token_sprint/validate_shard.py --sprint-root <path> \
        --shard <path> --pass-id p3 --prompt-version v1 --input-shard <path>

Exit codes: 0 QC computed, 2 fatal input error, 3 (--t0-gate only) gate failure.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from scripts.token_sprint.coordinator.common import (  # noqa: E402
    add_sprint_root_argument,
    atomic_write_text,
    resolve_sprint_root,
)
from scripts.token_sprint.make_shards import (  # noqa: E402
    EXPLICIT_MARKERS,
    text_has_explicit_marker,
)

VOCAB_FILENAME = "tag_vocab_v1.json"
WORD_PATTERN = r"^[a-z0-9_]+$"

T0_REFUSE_MAX = 0.15
T0_SCHEMA_MIN = 0.95
T0_FIDELITY_MIN = 0.90

#: Lowercase synonym stems that also count as explicit-marker retention in the
#: output text (code constant, not model output). Checked as plain substrings
#: so snake_case compounds such as ``nude_pose`` or ``unclothed_figure`` match.
SYNONYM_STEMS: tuple[str, ...] = (
    "unclothed",
    "undressed",
    "sensual",
    "erotic",
    "18+",
    "r18",
)

_KNOWN_PASSES = ("p1a", "p1b", "p2", "p3")


class ValidationError(RuntimeError):
    """Fatal shard-validation problem; surfaces as exit code 2."""


def _import_prompt_sha256():
    """Best-effort import of the Task 6 ``prompts.prompt_sha256`` helper."""
    try:
        from scripts.token_sprint.prompts import prompt_sha256
    except ImportError:
        return None
    return prompt_sha256


def retains_explicit_marker(text: str | None) -> bool:
    """True when the output text keeps a marker or a synonymous stem."""
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in EXPLICIT_MARKERS) or any(
        stem in lowered for stem in SYNONYM_STEMS
    )


def _read_jsonl(path: Path) -> list[str]:
    if not path.is_file():
        raise ValidationError(f"文件不存在: {path}")
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _load_input_rows(input_shard: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for line in _read_jsonl(input_shard):
        row = json.loads(line)
        if isinstance(row, dict) and row.get("candidate_id") is not None:
            rows[str(row["candidate_id"])] = row
    return rows


def _input_row_is_high_explicit(row: dict) -> bool:
    if row.get("stratum") == "high":
        return True
    haystack = "\n".join(
        str(row.get(field) or "")
        for field in ("vlm_summary_json", "tags_json", "scenario_keys_json")
    )
    return text_has_explicit_marker(haystack)


def _is_refusal(row: object) -> bool:
    return isinstance(row, dict) and row.get("refused") is True


def _output_text(row: dict) -> str:
    """Serialize the content-bearing parts of an output row for the heuristic."""
    parts: list[str] = []
    for key in ("keywords", "tags"):
        value = row.get(key)
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
    scene = row.get("scene")
    if isinstance(scene, dict):
        parts.extend(str(v) for v in scene.values())
    if isinstance(row.get("words"), list):
        parts.extend(str(item) for item in row["words"])
    return " ".join(parts)


def _check_schema(
    pass_id: str,
    row: dict,
    *,
    vocab: set[str] | None,
) -> tuple[bool, str | None]:
    """Validate one non-refusal output row; return (ok, reason)."""
    word_re = re.compile(WORD_PATTERN)

    if pass_id == "p3":
        keywords = row.get("keywords")
        if not isinstance(keywords, list):
            return False, "keywords 不是 list"
        if not (3 <= len(keywords) <= 12):
            return False, f"keywords 数量 {len(keywords)} 不在 3-12"
        for item in keywords:
            if not (isinstance(item, str) and word_re.fullmatch(item)):
                return False, f"keyword 词形非法: {item!r}"
        return True, None

    if pass_id == "p1b":
        tags = row.get("tags")
        if not (isinstance(tags, list) and all(isinstance(t, str) for t in tags)):
            return False, "tags 不是 list[str]"
        if vocab is None:
            return True, None  # vocab file missing: subset check skipped
        unknown = [t for t in tags if t not in vocab]
        if unknown:
            return False, f"tags 不在词表内: {unknown}"
        return True, None

    if pass_id == "p2":
        scene = row.get("scene")
        if not isinstance(scene, dict):
            return False, "scene 不是 object"
        missing = [k for k in ("setting", "act", "finish") if k not in scene]
        if missing:
            return False, f"scene 缺少键: {missing}"
        return True, None

    if pass_id == "p1a":
        for key in ("words", "vocab", "tags", "keywords"):
            value = row.get(key)
            if isinstance(value, list):
                bad = [w for w in value if not (isinstance(w, str) and word_re.fullmatch(w))]
                if bad:
                    return False, f"词形非法: {bad}"
                return True, None
        return False, "未找到词表聚合字段 (words/vocab/tags/keywords)"

    return False, f"未定义的 pass schema: {pass_id}"


def _load_vocab(sprint_root: Path) -> tuple[set[str] | None, str | None]:
    """Load ``tag_vocab_v1.json``; return (vocab, None) or (None, "missing")."""
    vocab_path = sprint_root / VOCAB_FILENAME
    if not vocab_path.is_file():
        return None, "missing"
    data = json.loads(vocab_path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        for key in ("tags", "words", "vocab"):
            if isinstance(data.get(key), list):
                return {str(t) for t in data[key]}, None
        # Frozen W1 vocabulary format: {"entries": [{"term": "...", ...}, ...]}.
        # Fail closed: when entries exist but no term strings can be read, the
        # fallback below (top-level keys) rejects every real tag.
        entries = data.get("entries")
        if isinstance(entries, list):
            terms = {
                str(entry["term"])
                for entry in entries
                if isinstance(entry, dict) and isinstance(entry.get("term"), str)
            }
            if terms:
                return terms, None
        return set(data.keys()), None
    if isinstance(data, list):
        return {str(t) for t in data}, None
    return None, "missing"


def validate_shard(
    *,
    shard: Path,
    input_shard: Path,
    pass_id: str,
    prompt_version: str,
    sprint_root: Path,
) -> dict:
    """Compute the QC summary for one output shard."""
    if pass_id not in _KNOWN_PASSES:
        raise ValidationError(
            f"未定义的 pass schema: {pass_id}（已知: {', '.join(_KNOWN_PASSES)}）"
        )

    input_rows = _load_input_rows(input_shard)
    vocab, vocab_status = (None, None)
    if pass_id == "p1b":
        vocab, vocab_status = _load_vocab(sprint_root)

    errors: list[str] = []
    suspected_sanitized: list[str] = []
    matched_ids: set[str] = set()
    n = 0
    refusals = 0
    schema_ok = 0
    fidelity_denominator = 0
    fidelity_retained = 0

    for line_number, line in enumerate(_read_jsonl(shard), start=1):
        n += 1
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {line_number}: 不是合法 JSON ({exc})")
            continue
        if not isinstance(row, dict):
            errors.append(f"line {line_number}: 不是 JSON object")
            continue

        if _is_refusal(row):
            refusals += 1
            if row.get("candidate_id") is None:
                errors.append(f"line {line_number}: refusal 行缺少 candidate_id")
            continue

        if pass_id == "p1a":
            # p1a output is a vocabulary aggregate; there is no per-candidate
            # schema and no per-candidate fidelity.
            ok, reason = _check_schema(pass_id, row, vocab=None)
            if ok:
                schema_ok += 1
            else:
                errors.append(f"line {line_number}: {reason}")
            continue

        candidate_id = row.get("candidate_id")
        if candidate_id is None:
            errors.append(f"line {line_number}: 缺少 candidate_id")
            continue
        candidate_id = str(candidate_id)

        input_row = input_rows.get(candidate_id)
        if input_row is None:
            errors.append(
                f"line {line_number}: candidate_id 不在 input 分片中: {candidate_id}"
            )
            continue
        matched_ids.add(candidate_id)

        ok, reason = _check_schema(pass_id, row, vocab=vocab)
        if ok:
            schema_ok += 1
        else:
            errors.append(f"line {line_number} ({candidate_id}): {reason}")

        if _input_row_is_high_explicit(input_row):
            fidelity_denominator += 1
            if retains_explicit_marker(_output_text(row)):
                fidelity_retained += 1
            else:
                suspected_sanitized.append(candidate_id)

    non_refusal = n - refusals
    schema_ok_rate = schema_ok / non_refusal if non_refusal > 0 else 1.0
    refuse_rate = refusals / n if n > 0 else 0.0
    fidelity_rate = (
        fidelity_retained / fidelity_denominator if fidelity_denominator > 0 else None
    )
    coverage_rate = len(matched_ids) / len(input_rows) if input_rows else None

    prompt_sha256 = None
    prompt_fn = _import_prompt_sha256()
    if prompt_fn is not None:
        try:
            prompt_sha256 = prompt_fn(pass_id, prompt_version)
        except (OSError, KeyError):
            prompt_sha256 = None

    return {
        "n": n,
        "schema_ok_rate": schema_ok_rate,
        "refuse_rate": refuse_rate,
        "fidelity_rate_high_stratum": fidelity_rate,
        "suspected_sanitized": suspected_sanitized,
        "errors": errors,
        "pass_id": pass_id,
        "prompt_version": prompt_version,
        "prompt_sha256": prompt_sha256,
        "vocab": vocab_status,
        "coverage_rate": coverage_rate,
        "t0_gates": {
            "refuse_rate_le_15pct": refuse_rate <= T0_REFUSE_MAX,
            "schema_ok_rate_ge_95pct": schema_ok_rate >= T0_SCHEMA_MIN,
            "fidelity_rate_high_stratum_ge_90pct": (
                fidelity_rate is None or fidelity_rate >= T0_FIDELITY_MIN
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="QC one enrichment output shard (schema, refusals, fidelity heuristic)."
    )
    add_sprint_root_argument(parser)
    parser.add_argument("--shard", required=True, help="output shard JSONL path")
    parser.add_argument("--pass-id", required=True, choices=_KNOWN_PASSES)
    parser.add_argument("--prompt-version", required=True)
    parser.add_argument("--input-shard", required=True, help="matching input shard JSONL")
    parser.add_argument(
        "--t0-gate",
        action="store_true",
        help="exit 3 when any T0 gate fails (otherwise gates are reported only)",
    )
    args = parser.parse_args(argv)
    sprint_root = resolve_sprint_root(args.sprint_root)

    shard = Path(args.shard)
    input_shard = Path(args.input_shard)
    try:
        qc = validate_shard(
            shard=shard,
            input_shard=input_shard,
            pass_id=args.pass_id,
            prompt_version=args.prompt_version,
            sprint_root=sprint_root,
        )
    except (ValidationError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    qc_path = shard.parent / (shard.name + ".qc.json")
    atomic_write_text(qc_path, json.dumps(qc, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(json.dumps(qc, ensure_ascii=False, indent=2, sort_keys=True))

    if args.t0_gate and not all(qc["t0_gates"].values()):
        print("error: T0 门未全部通过（人工复核仍需进行）", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

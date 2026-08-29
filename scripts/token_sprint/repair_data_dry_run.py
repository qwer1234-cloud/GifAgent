#!/usr/bin/env python3
"""W3 dry-run repair suggestions for historical-data audit findings.

Design (docs/superpowers/plans/2026-08-28-token-sprint-w3-data-audit-implementation.md,
Task 2): for each audit finding, print the JSON-patch pointer or SQL ``UPDATE``
text that a human could apply later. The script **refuses to execute anything**:

- there is no ``--apply`` switch and passing ``--apply`` is rejected with exit 2;
- the script only *reads* the findings JSON and the referenced audit inputs --
  every input file's size/mtime is left untouched (covered by tests).

Usage:
    python scripts/token_sprint/repair_data_dry_run.py --sprint-root <path> \
        [--findings <json>] [--rule missing_field ...] [--max-per-rule 20]
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
    resolve_sprint_root,
)

DEFAULT_FINDINGS_SUBPATH = "w3/w3-audit-findings.json"

_DB_TABLES = {
    "candidate_gifs", "media", "frames", "annotations", "frame_annotations",
    "preference_events", "favorite_gifs", "candidate_vectors", "vector_refs",
    "preference_profiles", "preference_profile_builds",
    "preference_profile_publications", "preference_profile_current",
    "processing_checkpoint", "video_clips", "collections", "collection_versions",
    "collection_items", "feedback", "candidate_vector_exclusions",
}

_ROWID_RE = re.compile(r"^(?P<table>[A-Za-z_]+)#(?P<rowid>\d+)")


class RepairRefusal(RuntimeError):
    """Raised when the invocation asks for something the tool must not do."""


def parse_record_ref(record_ref: str):
    """Split ``table#rowid (...)`` refs; returns (table, rowid) or (None, None)."""
    match = _ROWID_RE.match(record_ref or "")
    if not match:
        return None, None
    return match.group("table"), int(match.group("rowid"))


def suggest_patch(finding: dict) -> str:
    """Return a printable, non-executed patch suggestion for one finding."""
    rule = finding["rule"]
    ref = finding["record_ref"]
    field = finding.get("field") or "<field>"
    detail = finding.get("detail", "")
    table, rowid = parse_record_ref(ref)

    if rule == "missing_field":
        if table and rowid is not None:
            return (
                f"SQL suggestion (NOT executed): UPDATE {table} SET {field} = "
                f"<reviewed value> WHERE rowid = {rowid};  -- verify value manually"
            )
        if finding["record_type"] == "top_clips" and field == "candidate_id":
            return (
                'JSON patch suggestion (NOT executed): {"op": "add", "path": '
                f'"{_pointer_for_record(ref)}/candidate_id", "value": "<from '
                'gif_exports[k].quality_assessment.candidate_id, k = rank-1>"} '
                "-- cross-check export filename window before applying"
            )
        pointer = _pointer_for_record(ref) + f"/{field}"
        return (
            f'JSON patch suggestion (NOT executed): {{"op": "add", "path": '
            f'"{pointer}", "value": "<reviewed value>"}}'
        )

    if rule == "score_oob":
        if table and rowid is not None:
            return (
                f"SQL suggestion (NOT executed; needs manual decision: clamp vs "
                f"re-score vs null): UPDATE {table} SET {field} = NULL WHERE "
                f"rowid = {rowid};  -- original out-of-range value: {detail}"
            )
        return (
            "JSON patch suggestion (NOT executed; needs manual decision): "
            f'{{"op": "replace", "path": "{_pointer_for_record(ref)}", '
            f'"value": null}}  -- original out-of-range value: {detail}'
        )

    if rule == "broken_artifact":
        path_text = detail.split(": ", 1)[-1]
        basename = Path(path_text.replace("\\", "/")).name
        if table and rowid is not None:
            return (
                f"Option A (relink, NOT executed): locate '{basename}' under the "
                f"data root and run UPDATE {table} SET {field} = '<new path>' "
                f"WHERE rowid = {rowid};\n"
                f"Option B (retire, NOT executed): UPDATE {table} SET {field} = "
                f"NULL WHERE rowid = {rowid};"
            )
        return (
            f"Manual decision (NOT executed): path '{path_text}' no longer "
            f"exists; either restore the artifact ('{basename}') or mark the "
            f"export_status/error fields accordingly in the run report."
        )

    if rule == "mojibake":
        excerpt = finding.get("excerpt") or ""
        round_trip = _latin1_round_trip(excerpt)
        target = f"{table}(rowid={rowid}).{field}" if table else ref
        if round_trip is not None:
            return (
                f"Re-encode preview for {target} (NOT executed): "
                f"latin-1 -> utf-8 round trip yields: {round_trip!r}"
            )
        return (
            f"Manual review for {target}: automatic latin-1 -> utf-8 round trip "
            f"failed; decide per text (do NOT delete explicit adult wording as "
            f"if it were corruption)."
        )

    if rule == "ts_contradiction":
        if table and rowid is not None:
            return (
                f"Manual review (NOT executed): {table}(rowid={rowid}) "
                f"{field} -- inspect source pipeline ordering; do not blanket-"
                f"swap timestamps."
            )
        return (
            f"Manual review (NOT executed): {ref} {field} -- inspect source "
            f"pipeline ordering; do not blanket-swap timestamps."
        )

    return f"Manual review (NOT executed): {rule} on {ref} ({field})."


def _pointer_for_record(record_ref: str) -> str:
    """Map ``top_clips[3]`` / ``gif_exports[7]`` style refs to a JSON pointer."""
    match = re.match(r"^(top_clips|gif_exports|last_run|completed_entry)\[(\d+)\]$",
                     record_ref or "")
    if not match:
        return "/" + (record_ref or "").replace(".", "/")
    kind, index = match.groups()
    if kind == "completed_entry":
        return f"/completed/{index}"  # positional placeholder; key is a path
    if kind == "last_run":
        return "/last_run"
    return f"/{kind}/{index}"


def _latin1_round_trip(text: str):
    """Return the text obtained by undoing a UTF-8-as-Latin-1 mojibake, if clean."""
    try:
        repaired = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    return repaired


def render_suggestions(findings: list[dict], rules: set[str] | None,
                       max_per_rule: int) -> str:
    lines = [
        "W3 historical data repair suggestions -- DRY RUN ONLY",
        "=====================================================",
        "This tool never executes SQL or JSON patches and has no --apply mode.",
        "",
    ]
    selected = [f for f in findings if not rules or f["rule"] in rules]
    by_rule: dict[str, list[dict]] = {}
    for finding in selected:
        by_rule.setdefault(finding["rule"], []).append(finding)
    if not by_rule:
        lines.append("No findings matched the selection.")
        return "\n".join(lines)
    for rule in sorted(by_rule):
        group = by_rule[rule]
        lines.append(f"## {rule} ({len(group)} finding(s))")
        for finding in group[:max_per_rule]:
            lines.append(f"- [{finding['record_ref']}] {suggest_patch(finding)}")
        if len(group) > max_per_rule:
            lines.append(f"- ... {len(group) - max_per_rule} more (raise "
                         f"--max-per-rule to see them)")
        lines.append("")
    lines.append("No file, database, or manifest was modified (dry run).")
    return "\n".join(lines)


def run_repair_dry_run(args: argparse.Namespace) -> str:
    sprint_root = resolve_sprint_root(args.sprint_root)
    findings_path = (Path(args.findings) if args.findings
                     else sprint_root / DEFAULT_FINDINGS_SUBPATH)
    if not findings_path.is_file():
        raise RepairRefusal(
            f"findings JSON not found: {findings_path} -- run audit_data.py first"
        )
    findings = json.loads(findings_path.read_text(encoding="utf-8"))["findings"]
    rules = set(args.rule) if args.rule else None
    return render_suggestions(findings, rules, args.max_per_rule)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_sprint_root_argument(parser)
    parser.add_argument("--findings", default=None,
                        help="audit findings JSON (default "
                             "<sprint-root>/w3/w3-audit-findings.json)")
    parser.add_argument("--rule", action="append", default=None,
                        help="restrict to one rule id (repeatable)")
    parser.add_argument("--max-per-rule", type=int, default=20,
                        help="maximum printed suggestions per rule (default 20)")
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = argv if argv is not None else sys.argv[1:]
    if "--apply" in raw_argv:
        # Explicitly rejected before argparse can even parse it (plan Task 2:
        # the tool has no apply mode at all).
        print("error: this tool is dry-run only by design; there is no --apply mode",
              file=sys.stderr)
        return 2
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    try:
        print(run_repair_dry_run(args))
    except RepairRefusal as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

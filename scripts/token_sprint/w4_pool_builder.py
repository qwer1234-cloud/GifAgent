"""W4 pool builder / qrels assembler for the token-sprint search baseline.

Role (W4 搜索基线): produce the FTS-v1 candidate pools behind
``pool_version=pool_v2_snapshot_20260830`` and assemble fully-judged qrels
rows that conform to the W2 ``eval_harness`` schema.  The harness itself is
NOT modified; this tool only produces its *input*.

Subcommands
-----------
``dump-pool``   Open the v2 snapshot READ-ONLY, run the exact fts_v1 live
                ranking path used by ``eval_harness.live_rank_ids``
                (``LibrarySearchService`` with ``embedder=None``) for every
                query-bank row, and dump the top-K pool with the candidate
                text fields (caption/tags/scenario) used for judging.
                No media bytes are ever opened; text only.
``build-qrels`` Merge judge-grade JSON files into a qrels JSONL that
                satisfies the W2 schema and the fully-judged contract
                (every pool candidate of every query graded 0-3).

Fail-closed: any pool/grade coverage gap exits non-zero without writing
output.  The tool never writes to the snapshot and never reads
``base_rag_similarity``.

ranker_set_hash (traceability, W2 contract "sha256:<64hex>"): sha256 over the
canonical descriptor string ``RANKER_SET_DESCRIPTOR`` below; identical in
dump-pool output and build-qrels rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

POOL_VERSION = "pool_v2_snapshot_20260830"
RANKER_SET_DESCRIPTOR = (
    "w4-search-baseline|pool_version=pool_v2_snapshot_20260830"
    "|rankers=fts_v1|pool_size=24|snapshot=library_snapshot_v2"
    "|judge=single-model-silver"
)
POOL_SIZE = 24
GRADE_MIN, GRADE_MAX = 0, 3
GRADE_LEGEND = {
    0: "irrelevant: no caption/tag/scenario support for the query intent",
    1: "marginal: only weak or incidental textual support",
    2: "partially relevant: matches a major facet of the intent",
    3: "highly relevant: matches the intent squarely on caption/tags",
}


def ranker_set_hash() -> str:
    return "sha256:" + hashlib.sha256(
        RANKER_SET_DESCRIPTOR.encode("utf-8")
    ).hexdigest()


def _fail(message: str) -> "SystemExit":
    print(f"error: {message}", file=sys.stderr)
    return SystemExit(2)


def load_query_bank(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            row = json.loads(stripped)
            if not isinstance(row.get("query_id"), str) or not row.get("query_id"):
                raise _fail(f"query bank line {line_no}: missing query_id")
            if not isinstance(row.get("text"), str) or not row["text"].strip():
                raise _fail(f"query bank line {line_no}: missing text")
            rows.append(row)
    if not rows:
        raise _fail(f"query bank is empty: {path}")
    return rows


def cmd_dump_pool(args: argparse.Namespace) -> int:
    if os.environ.get("GIFAGENT_SEARCH_FTS_V2") == "1":
        raise _fail(
            "GIFAGENT_SEARCH_FTS_V2=1 is set; W4 pools must be built with the "
            "default fts_v1 path (unset the flag)"
        )
    from app.services.library_search import LibrarySearchService
    from app.services.workbench_schema import SearchQuery

    snapshot = Path(args.snapshot)
    if not snapshot.is_file():
        raise _fail(f"snapshot not found: {snapshot}")
    import sqlite3

    conn = sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        service = LibrarySearchService(conn, embedder=None)
        bank = load_query_bank(Path(args.query_bank))
        out_rows: list[dict[str, Any]] = []
        for row in bank:
            page = service.search(
                SearchQuery(text=row["text"]), limit=args.pool_size
            )
            pool: list[dict[str, Any]] = []
            for rank, item in enumerate(page.items, start=1):
                crows = conn.execute(
                    """SELECT vlm_summary_json, tags_json, scenario_keys_json
                       FROM candidate_gifs WHERE candidate_id=?""",
                    (item.candidate_id,),
                ).fetchall()
                if len(crows) != 1:
                    raise _fail(
                        f"candidate {item.candidate_id}: expected 1 row, "
                        f"got {len(crows)}"
                    )
                vlm = json.loads(crows[0]["vlm_summary_json"] or "{}")
                caption = ""
                if isinstance(vlm, dict):
                    caption = str(vlm.get("caption") or vlm.get("summary") or "")
                try:
                    tags = json.loads(crows[0]["tags_json"] or "[]")
                except json.JSONDecodeError:
                    tags = []
                try:
                    scenario = json.loads(crows[0]["scenario_keys_json"] or "[]")
                except json.JSONDecodeError:
                    scenario = []
                pool.append(
                    {
                        "candidate_id": item.candidate_id,
                        "fts_v1_rank": rank,
                        "caption": caption,
                        "tags": tags if isinstance(tags, list) else [],
                        "scenario_keys": scenario
                        if isinstance(scenario, list)
                        else [],
                    }
                )
            out_rows.append(
                {
                    "query_id": row["query_id"],
                    "query_text": row["text"],
                    "notes": row.get("notes", ""),
                    "category": row.get("category", ""),
                    "n_fts_matches_total": page.total,
                    "pool_size": len(pool),
                    "pool": pool,
                }
            )
    finally:
        conn.close()

    payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out_rows)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(output)
    print(
        json.dumps(
            {
                "queries": len(out_rows),
                "pool_size": args.pool_size,
                "pool_version": POOL_VERSION,
                "ranker_set_hash": ranker_set_hash(),
                "output": output.as_posix(),
            },
            indent=2,
        )
    )
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    """Render a compact judge-readable view of the pool dump (text only)."""
    rows = [
        json.loads(line)
        for line in Path(args.pool).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    lines: list[str] = []
    for row in rows:
        lines.append(
            f"### {row['query_id']} | text={row['query_text']!r} "
            f"| fts_matches={row['n_fts_matches_total']} | pool={row['pool_size']}"
        )
        for cand in row["pool"]:
            tags = ",".join(str(t) for t in cand["tags"][:12])
            scen = ",".join(str(s) for s in cand["scenario_keys"][:4])
            caption = cand["caption"].replace("\n", " ")[:180]
            lines.append(
                f"r{cand['fts_v1_rank']:>2} {cand['candidate_id']} "
                f"| cap: {caption}"
            )
            lines.append(f"   tags: {tags} | scen: {scen}")
        lines.append("")
    Path(args.output).write_text("\n".join(lines), encoding="utf-8")
    print(f"rendered {len(rows)} queries -> {args.output}")
    return 0


def cmd_build_qrels(args: argparse.Namespace) -> int:
    pool_rows = [
        json.loads(line)
        for line in Path(args.pool).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    grades: dict[str, dict[str, dict[str, Any]]] = {}
    for grades_path in args.grades:
        data = json.loads(
            Path(grades_path).read_text(encoding="utf-8")
        )
        for query_id, entry in data.items():
            if query_id in grades:
                raise _fail(f"duplicate query_id across grade files: {query_id}")
            if not isinstance(entry, dict) or "intent" not in entry:
                raise _fail(f"{grades_path}: {query_id} missing 'intent'")
            grades[query_id] = entry

    qrels: list[dict[str, Any]] = []
    problems: list[str] = []
    judged_queries = 0
    for row in pool_rows:
        query_id = row["query_id"]
        if row["pool_size"] == 0:
            # Zero-hit control: no pool candidates -> no qrels rows.  The
            # harness counts it via the query bank (coverage denominator).
            continue
        entry = grades.get(query_id)
        if entry is None:
            problems.append(f"{query_id}: no grades provided")
            continue
        grade_map = entry.get("grades")
        if not isinstance(grade_map, dict):
            problems.append(f"{query_id}: missing 'grades' object")
            continue
        intent = str(entry["intent"]).strip()
        expected = {c["candidate_id"] for c in row["pool"]}
        graded = set(grade_map)
        if graded != expected:
            missing = sorted(expected - graded)
            extra = sorted(graded - expected)
            problems.append(
                f"{query_id}: coverage mismatch (missing={missing[:5]} "
                f"extra={extra[:5]})"
            )
            continue
        for cand in sorted(row["pool"], key=lambda c: c["fts_v1_rank"]):
            cid = cand["candidate_id"]
            value = grade_map[cid]
            note = ""
            if isinstance(value, list) and len(value) == 2:
                grade, note = int(value[0]), str(value[1])
            else:
                grade = int(value)
            if not GRADE_MIN <= grade <= GRADE_MAX or isinstance(value, bool):
                problems.append(f"{query_id}/{cid}: grade out of range: {value!r}")
                continue
            rationale = (
                f"intent={intent} | {GRADE_LEGEND[grade]}"
                + (f" | note: {note}" if note else "")
            )
            qrels.append(
                {
                    "query_id": query_id,
                    "pool_version": POOL_VERSION,
                    "ranker_set_hash": ranker_set_hash(),
                    "candidate_id": cid,
                    "grade": grade,
                    "rationale": rationale,
                    "pool_sources": {
                        "fts_v1": cand["fts_v1_rank"],
                        "hybrid": None,
                        "fts_v2": None,
                    },
                    "label_tier": "silver",
                    "status": "judged",
                }
            )
        judged_queries += 1

    missing_queries = sorted(set(grades) - {r["query_id"] for r in pool_rows})
    if missing_queries:
        problems.append(f"grades for queries without a pool: {missing_queries}")
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 2

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in qrels),
        encoding="utf-8",
    )
    tmp.replace(output)
    print(
        json.dumps(
            {
                "qrels_rows": len(qrels),
                "judged_pool_queries": judged_queries,
                "zero_hit_queries_skipped": len(pool_rows) - judged_queries,
                "pool_version": POOL_VERSION,
                "ranker_set_hash": ranker_set_hash(),
                "output": output.as_posix(),
            },
            indent=2,
        )
    )
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    dump = sub.add_parser("dump-pool", help="Dump fts_v1 top-K pools (read-only).")
    dump.add_argument("--snapshot", required=True)
    dump.add_argument("--query-bank", required=True)
    dump.add_argument("--pool-size", type=int, default=POOL_SIZE)
    dump.add_argument("--output", required=True)
    dump.set_defaults(func=cmd_dump_pool)

    render = sub.add_parser("render", help="Compact judge-readable pool view.")
    render.add_argument("--pool", required=True)
    render.add_argument("--output", required=True)
    render.set_defaults(func=cmd_render)

    qrels = sub.add_parser("build-qrels", help="Assemble qrels from grade files.")
    qrels.add_argument("--pool", required=True)
    qrels.add_argument("--grades", nargs="+", required=True)
    qrels.add_argument("--output", required=True)
    qrels.set_defaults(func=cmd_build_qrels)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

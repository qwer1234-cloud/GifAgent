"""W2 deterministic retrieval evaluation harness (token sprint).

Scope (implementation plan 2026-08-28-token-sprint-w2-eval-harness, Task 1;
the Task 2 ``build-pools`` subcommand is out of scope this sprint per the
canonical manifest ``scope_rulings``: ``w2_harness_tools_task_1_3`` active,
``w2_model_annotation_and_evalsets_labeling`` cancelled by the T0 fallback):

- Consumes qrels JSONL locked to the sprint schema::

      {query_id, pool_version, ranker_set_hash, candidate_id, grade(0-3),
       rationale, pool_sources: {"fts_v1": rank|null, "hybrid": ..., "fts_v2": ...},
       label_tier: "silver"|"gold", status: "judged"|"disputed"|"adjudicated"}

- NDCG@10 / NDCG@24 (reusing ``app.quality_lab.metrics.ndcg_at_k``) are computed
  ONLY over queries that are fully judged under the target ``pool_version``
  (every pool candidate has ``status in {judged, adjudicated}``).  A single
  ``disputed`` pair excludes the whole query until adjudication; disputed is
  never implicitly treated as grade 0.
- Deterministic: identical inputs produce byte-identical reports (stable
  ordering + fixed float rounding).
- ``candidate_gifs.base_rag_similarity`` (a.k.a. ``gif_worthiness``) is NEVER
  read; it is not a retrieval relevance baseline.
- ``fts_v2`` live ranking arrives with W8; without it the ranker is reported as
  ``skipped`` and no fabricated scores are emitted.
- Hybrid live reranking needs a query vector: the existing
  ``app.services.embedding.compute_text_embedding`` is used when reachable,
  otherwise the optional cache ``evalsets/query_vectors.json``; with neither,
  the hybrid column is reported as ``skipped`` (never silently degraded to
  another scorer, never substituted with worthiness).

Exit codes: 0 success (including empty/skipped inputs reported gracefully),
2 fail-closed harness error (bad sprint root, snapshot/manifest mismatch,
invalid qrels, inconsistent ranker_set_hash).
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

# Add project root to path so direct script execution works (same pattern as
# scripts/token_sprint/coordinator/preflight.py).
_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from app.quality_lab.metrics import ndcg_at_k  # noqa: E402
from scripts.token_sprint.coordinator.common import (  # noqa: E402
    add_sprint_root_argument,
    atomic_write_text,
    resolve_sprint_root,
    sha256_file,
)

RANKERS = ("fts_v1", "hybrid", "fts_v2")
NDCG_K_VALUES = (10, 24)

GRADE_VALUES = frozenset({0, 1, 2, 3})
LABEL_TIERS = frozenset({"silver", "gold"})
QREL_STATUSES = frozenset({"judged", "disputed", "adjudicated"})
# Statuses that count toward "fully judged" (disputed never counts, per plan).
JUDGED_STATUSES = frozenset({"judged", "adjudicated"})

REQUIRED_QRELS_FIELDS = (
    "query_id",
    "pool_version",
    "ranker_set_hash",
    "candidate_id",
    "grade",
    "pool_sources",
    "label_tier",
    "status",
)
POOL_SOURCE_KEYS = ("fts_v1", "hybrid", "fts_v2")

FLOAT_ROUNDING = 6

# Status strings for rankers that cannot be run honestly (never fake scores).
SKIP_FTS_V2_LIVE = "skipped:fts_v2_live_ranking_arrives_with_w8"
SKIP_NO_QUERY_VECTOR = "skipped:no_query_vector_available"
SKIP_NO_CANDIDATE_VECTORS = "skipped:snapshot_has_no_candidate_vectors"

# rank_fn(query_id, grades, pool_candidate_ids) -> ranked candidate ids
RankFn = Callable[[str, dict[str, int], list[str]], list[str]]


class EvalHarnessError(RuntimeError):
    """Fail-closed harness error (CLI maps this to exit code 2)."""


# ---------------------------------------------------------------------------
# Input loading + schema validation
# ---------------------------------------------------------------------------


def _fail(line_no: int | None, message: str) -> EvalHarnessError:
    where = f" (line {line_no})" if line_no is not None else ""
    return EvalHarnessError(f"invalid qrels{where}: {message}")


def _validate_ranker_set_hash(value: Any, line_no: int | None) -> str:
    if not isinstance(value, str) or not value:
        raise _fail(line_no, "ranker_set_hash must be a non-empty string")
    hex_part = value[len("sha256:"):] if value.startswith("sha256:") else ""
    if len(hex_part) != 64 or any(c not in "0123456789abcdef" for c in hex_part):
        raise _fail(
            line_no,
            'ranker_set_hash must look like "sha256:<64 lowercase hex chars>", '
            f"got {value!r}",
        )
    return value


def _validate_pool_sources(value: Any, line_no: int | None) -> dict[str, int | None]:
    if not isinstance(value, dict):
        raise _fail(line_no, "pool_sources must be an object")
    unknown = sorted(set(value) - set(POOL_SOURCE_KEYS))
    if unknown:
        raise _fail(line_no, f"pool_sources has unknown ranker keys: {unknown}")
    validated: dict[str, int | None] = {}
    for key in POOL_SOURCE_KEYS:
        rank = value.get(key)
        if rank is None:
            validated[key] = None
            continue
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
            raise _fail(
                line_no,
                f"pool_sources[{key!r}] must be a 1-based integer rank or null, "
                f"got {rank!r}",
            )
        validated[key] = rank
    return validated


def _validate_qrel_row(row: Any, line_no: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise _fail(line_no, "each qrels line must be a JSON object")
    missing = [field for field in REQUIRED_QRELS_FIELDS if field not in row]
    if missing:
        raise _fail(line_no, f"missing required fields: {missing}")
    if not isinstance(row["query_id"], str) or not row["query_id"]:
        raise _fail(line_no, "query_id must be a non-empty string")
    if not isinstance(row["pool_version"], str) or not row["pool_version"]:
        raise _fail(line_no, "pool_version must be a non-empty string")
    if not isinstance(row["candidate_id"], str) or not row["candidate_id"]:
        raise _fail(line_no, "candidate_id must be a non-empty string")
    grade = row["grade"]
    if isinstance(grade, bool) or not isinstance(grade, int) or grade not in GRADE_VALUES:
        raise _fail(
            line_no, f"grade must be an integer in {sorted(GRADE_VALUES)}, got {grade!r}"
        )
    if row["label_tier"] not in LABEL_TIERS:
        raise _fail(
            line_no,
            f"label_tier must be one of {sorted(LABEL_TIERS)}, got {row['label_tier']!r}",
        )
    if row["status"] not in QREL_STATUSES:
        raise _fail(
            line_no,
            f"status must be one of {sorted(QREL_STATUSES)}, got {row['status']!r}",
        )
    _validate_ranker_set_hash(row["ranker_set_hash"], line_no)
    _validate_pool_sources(row["pool_sources"], line_no)
    return row


def load_qrels(path: Path) -> list[dict[str, Any]]:
    """Load and validate a qrels JSONL file.

    A missing file is *not* an error: an empty evalsets directory is the
    expected state this sprint and the harness must report gracefully.
    Schema violations inside an existing file fail closed (exit 2).
    """
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise _fail(line_no, f"not valid JSON: {exc}") from exc
            rows.append(dict(_validate_qrel_row(parsed, line_no)))
    return rows


def load_query_bank(path: Path | None) -> tuple[list[dict[str, Any]], list[str]]:
    """Load query-bank rows (``query_id`` + optional ``text``).

    Returns ``(rows, notes)``; a missing file yields an empty list plus a note
    (the query bank is intentionally not populated this sprint).
    """
    if path is None or not path.is_file():
        target = path.as_posix() if path is not None else "<unset>"
        return [], [f"query bank file not found (treated as empty): {target}"]
    rows: list[dict[str, Any]] = []
    notes: list[str] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                notes.append(f"query bank line {line_no} skipped: not valid JSON")
                continue
            if isinstance(row, dict) and isinstance(row.get("query_id"), str) and row["query_id"]:
                rows.append(row)
            else:
                notes.append(f"query bank line {line_no} skipped: missing query_id")
    return rows, notes


# ---------------------------------------------------------------------------
# Fully-judged selection (pool_version scoped)
# ---------------------------------------------------------------------------


def select_fully_judged(
    qrels_rows: list[dict[str, Any]],
    *,
    pool_version: str,
) -> tuple[dict[str, dict[str, int]], dict[str, str], str | None, int]:
    """Group qrels for *pool_version* and pick fully judged queries.

    Returns ``(grades_by_query, excluded_queries, ranker_set_hash, n_rows)``
    where ``grades_by_query`` maps query_id -> {candidate_id: grade} for
    queries whose *entire* pool candidate set has
    ``status in {judged, adjudicated}``, and ``excluded_queries`` maps
    query_id -> exclusion reason.
    """
    by_query: dict[str, list[dict[str, Any]]] = {}
    hashes: set[str] = set()
    n_rows = 0
    for row in qrels_rows:
        if row["pool_version"] != pool_version:
            continue
        n_rows += 1
        hashes.add(row["ranker_set_hash"])
        by_query.setdefault(row["query_id"], []).append(row)

    if len(hashes) > 1:
        raise EvalHarnessError(
            "qrels traceability violation: multiple ranker_set_hash values for "
            f"pool_version={pool_version!r}: {sorted(hashes)}"
        )
    ranker_set_hash = next(iter(hashes)) if hashes else None

    grades_by_query: dict[str, dict[str, int]] = {}
    excluded: dict[str, str] = {}
    for query_id in sorted(by_query):
        rows = by_query[query_id]
        seen: dict[str, dict[str, Any]] = {}
        for row in rows:
            cid = row["candidate_id"]
            if cid in seen:
                raise EvalHarnessError(
                    "invalid qrels: duplicate (query_id, candidate_id) pair "
                    f"({query_id!r}, {cid!r}) under pool_version={pool_version!r}"
                )
            seen[cid] = row
        disputed = sorted(
            cid for cid, row in seen.items() if row["status"] == "disputed"
        )
        if disputed:
            excluded[query_id] = (
                f"disputed pairs not adjudicated: {disputed} "
                f"({len(disputed)}/{len(seen)} pool candidates)"
            )
            continue
        not_judged = sorted(
            cid for cid, row in seen.items() if row["status"] not in JUDGED_STATUSES
        )
        if not_judged:  # defensive: QREL_STATUSES is a closed set, kept for safety
            excluded[query_id] = f"pairs without judged/adjudicated status: {not_judged}"
            continue
        grades_by_query[query_id] = {cid: row["grade"] for cid, row in seen.items()}
    return grades_by_query, excluded, ranker_set_hash, n_rows


# ---------------------------------------------------------------------------
# Ranking construction (stored pool_sources ranks, or live rerun)
# ---------------------------------------------------------------------------


def rank_by_pool_sources(
    grades: dict[str, int],
    pool_sources_by_candidate: dict[str, dict[str, int | None]],
    ranker: str,
) -> list[str]:
    """Order pool candidates by the stored 1-based rank of *ranker*.

    Unranked candidates (``null``) go last, ties broken by candidate_id, so
    the list is deterministic.  Unranked-but-judged candidates stay in the
    list: they contribute 0 DCG where they sit and raise the ideal DCG, i.e. a
    ranker that failed to retrieve a relevant pool candidate is penalised.
    """

    def sort_key(cid: str) -> tuple[int, int, str]:
        rank = pool_sources_by_candidate.get(cid, {}).get(ranker)
        return (0, int(rank), cid) if rank is not None else (1, 0, cid)

    return sorted(grades, key=sort_key)


def relevance_list(grades: dict[str, int], ranked_ids: list[str]) -> list[float]:
    return [float(grades[cid]) for cid in ranked_ids]


def _stored_rank_sources(
    qrels_rows: list[dict[str, Any]], *, pool_version: str, query_id: str
) -> dict[str, dict[str, int | None]]:
    return {
        row["candidate_id"]: row["pool_sources"]
        for row in qrels_rows
        if row["pool_version"] == pool_version and row["query_id"] == query_id
    }


# ---------------------------------------------------------------------------
# Snapshot provenance (canonical manifest SHA verification)
# ---------------------------------------------------------------------------


def verify_snapshot_provenance(
    sprint_root: Path,
    snapshot: Path,
    *,
    allow_uncanonical: bool,
) -> dict[str, Any]:
    """Verify the snapshot against the canonical manifest W1 artifact.

    Fail closed (exit 2) on: missing snapshot file, SHA mismatch against the
    manifest entry for the same path, or a snapshot that is not registered in
    the manifest at all unless ``allow_uncanonical`` is set (tmp/test fixtures).
    """
    snapshot = snapshot.resolve()
    if not snapshot.is_file():
        raise EvalHarnessError(f"snapshot database not found: {snapshot.as_posix()}")
    manifest_path = sprint_root / "sprint_manifest.json"
    if not manifest_path.is_file():
        if allow_uncanonical:
            return {
                "snapshot_path": snapshot.as_posix(),
                "snapshot_sha256": sha256_file(snapshot),
                "manifest_check": "skipped:no_manifest_and_uncanonical_allowed",
            }
        raise EvalHarnessError(
            f"canonical manifest not found: {manifest_path.as_posix()} "
            "(pass --allow-uncanonical-snapshot only for tmp/test fixtures)"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalHarnessError(f"cannot read canonical manifest: {exc}") from exc
    snapshot_posix = snapshot.as_posix().lower()
    matches = [
        entry
        for entry in manifest.get("artifacts", {}).values()
        if str(entry.get("artifact_path", "")).lower() == snapshot_posix
    ]
    actual_sha = sha256_file(snapshot)
    if not matches:
        if allow_uncanonical:
            return {
                "snapshot_path": snapshot.as_posix(),
                "snapshot_sha256": actual_sha,
                "manifest_check": "skipped:artifact_not_in_manifest_and_uncanonical_allowed",
            }
        raise EvalHarnessError(
            f"snapshot {snapshot.as_posix()} is not registered in the canonical "
            "manifest; refusing to consume an uncanonical snapshot without "
            "--allow-uncanonical-snapshot"
        )
    expected_sha = matches[0].get("artifact_sha256")
    if actual_sha != expected_sha:
        raise EvalHarnessError(
            "snapshot SHA-256 mismatch against canonical manifest "
            f"(expected {expected_sha}, actual {actual_sha}); refusing to "
            "evaluate against an unverified W1 snapshot"
        )
    return {
        "snapshot_path": snapshot.as_posix(),
        "snapshot_sha256": actual_sha,
        "manifest_check": "ok",
    }


def connect_readonly(snapshot: Path) -> sqlite3.Connection:
    snapshot = snapshot.resolve()
    conn = sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Live rerun (read-only against the snapshot)
# ---------------------------------------------------------------------------

EmbedFn = Callable[[str], list[float]]


def _default_embed_fn() -> EmbedFn:
    """Return the existing query-text embedding function (no new API client)."""
    from app.services.embedding import compute_text_embedding

    return compute_text_embedding


def resolve_query_vector(
    query_text: str,
    *,
    cache_path: Path | None,
    embed_fn: EmbedFn | None = None,
) -> tuple[list[float] | None, str]:
    """Resolve a query vector: live embedder first, then the optional cache.

    Returns ``(vector, status)``; ``vector is None`` means the caller must skip
    hybrid instead of silently degrading to another scorer.
    """
    if embed_fn is None:
        try:
            embed_fn = _default_embed_fn()
        except Exception:  # pragma: no cover - import failure is environmental
            embed_fn = None
    if embed_fn is not None:
        try:
            vec = embed_fn(query_text)
            if isinstance(vec, list) and vec:
                return vec, "ok:live_embedder"
        except Exception:
            pass  # fall through to the cache; never raise out of ranker resolution
    if cache_path is not None and cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}
        cached = cache.get(query_text) if isinstance(cache, dict) else None
        if isinstance(cached, list) and cached:
            return [float(x) for x in cached], "ok:query_vector_cache"
    return None, SKIP_NO_QUERY_VECTOR


def _snapshot_has_vectors(conn: sqlite3.Connection) -> bool:
    from app.services.preference_memory import (
        REQUIRED_EMBEDDING_DIM,
        REQUIRED_EMBEDDING_MODEL,
    )

    try:
        count = conn.execute(
            """SELECT COUNT(*) FROM candidate_vectors
               WHERE embedding_model=? AND embedding_dim=?""",
            (REQUIRED_EMBEDDING_MODEL, REQUIRED_EMBEDDING_DIM),
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return False
    return count > 0


def live_rank_ids(
    conn: sqlite3.Connection,
    query_text: str,
    *,
    ranker: str,
    limit: int,
    query_vector: list[float] | None = None,
) -> tuple[list[str], str]:
    """Re-run a ranker live against the read-only snapshot.

    Returns ``(candidate_ids_in_rank_order, status)``.  ``status`` starts with
    ``skipped:`` when the ranker cannot be run honestly (then ids are empty
    and the report carries no scores for it).
    """
    from app.services.library_search import LibrarySearchService
    from app.services.workbench_schema import SearchQuery

    if ranker == "fts_v1":
        service = LibrarySearchService(conn, embedder=None)
    elif ranker == "fts_v2":
        import os

        old_env = os.environ.get("GIFAGENT_SEARCH_FTS_V2")
        try:
            os.environ["GIFAGENT_SEARCH_FTS_V2"] = "1"
            service = LibrarySearchService(conn, embedder=None)
            page = service.search(SearchQuery(text=query_text), limit=limit)
            if page.degraded and page.diagnosis and "FTS v2 missing" in page.diagnosis:
                return [], SKIP_FTS_V2_LIVE
            return [item.candidate_id for item in page.items], "ok:live_rerun"
        finally:
            if old_env is None:
                os.environ.pop("GIFAGENT_SEARCH_FTS_V2", None)
            else:
                os.environ["GIFAGENT_SEARCH_FTS_V2"] = old_env
    else:  # hybrid: caller must have resolved a query vector
        if query_vector is None:
            return [], SKIP_NO_QUERY_VECTOR
        fixed_vector = list(query_vector)

        def embedder(_text: str) -> list[float]:
            return fixed_vector

        service = LibrarySearchService(conn, embedder=embedder)
    page = service.search(SearchQuery(text=query_text), limit=limit)
    return [item.candidate_id for item in page.items], "ok:live_rerun"


def prepare_live_rerank(
    conn: sqlite3.Connection,
    qrels_rows: list[dict[str, Any]],
    *,
    pool_version: str,
    ranker: str,
    query_texts: dict[str, str],
    cache_path: Path | None,
    embed_fn: EmbedFn | None = None,
) -> dict[str, str]:
    """Pre-flight the live rerun so a ranker is either fully honest or skipped.

    For ``hybrid``, every fully judged query must get a query vector (live
    embedder or cache); if any is missing the whole hybrid column is skipped.
    """
    if ranker == "fts_v2":
        try:
            count = conn.execute("SELECT COUNT(*) FROM candidate_search_fts_v2").fetchone()[0]
            if count == 0:
                return {"status": SKIP_FTS_V2_LIVE}
        except sqlite3.OperationalError:
            return {"status": SKIP_FTS_V2_LIVE}
    if ranker == "hybrid" and not _snapshot_has_vectors(conn):
        return {"status": SKIP_NO_CANDIDATE_VECTORS}
    grades_by_query, _excluded, _hash, _n = select_fully_judged(
        qrels_rows, pool_version=pool_version
    )
    if ranker == "hybrid":
        for query_id in sorted(grades_by_query):
            text = query_texts.get(query_id) or query_id
            vector, _status = resolve_query_vector(
                text, cache_path=cache_path, embed_fn=embed_fn
            )
            if vector is None:
                return {"status": SKIP_NO_QUERY_VECTOR}
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Report construction (pure + deterministic)
# ---------------------------------------------------------------------------


def _round(value: float) -> float:
    return round(float(value), FLOAT_ROUNDING)


def evaluate_pool_version(
    qrels_rows: list[dict[str, Any]],
    *,
    pool_version: str,
    ranker: str,
    rank_fn: RankFn | None = None,
    n_bank_queries: int = 0,
    extra_notes: list[str] | None = None,
    ranker_status: str = "ok:stored_pool_sources",
) -> dict[str, Any]:
    """Build the deterministic NDCG report for one pool version.

    ``rank_fn`` switches to live reranking; ``None`` uses the stored
    ``pool_sources`` ranks.  A ``ranker_status`` starting with ``skipped:``
    suppresses all score fields (no fabricated numbers).
    """
    if ranker not in RANKERS:
        raise EvalHarnessError(f"unknown ranker {ranker!r}; expected one of {RANKERS}")
    skipped = ranker_status.startswith("skipped:")

    grades_by_query, excluded, ranker_set_hash, n_rows = select_fully_judged(
        qrels_rows, pool_version=pool_version
    )

    notes: list[str] = list(extra_notes or [])
    per_query: list[dict[str, Any]] = []
    ndcg_at_10_values: list[float] = []
    ndcg_at_24_values: list[float] = []

    for query_id in sorted(grades_by_query):
        grades = grades_by_query[query_id]
        n_pool = len(grades)
        if skipped:
            per_query.append(
                {
                    "query_id": query_id,
                    "n_pool_candidates": n_pool,
                    "ndcg_at_10": None,
                    "ndcg_at_24": None,
                    "status": ranker_status,
                }
            )
            continue
        if rank_fn is not None:
            ranked_ids = rank_fn(query_id, grades, sorted(grades))
            stored_ranks_used = 0
        else:
            sources = _stored_rank_sources(
                qrels_rows, pool_version=pool_version, query_id=query_id
            )
            ranked_ids = rank_by_pool_sources(grades, sources, ranker)
            stored_ranks_used = sum(
                1 for src in sources.values() if src.get(ranker) is not None
            )
            if stored_ranks_used == 0:
                notes.append(
                    f"query {query_id}: no stored rank for ranker {ranker!r}; "
                    "candidates ordered by candidate_id (unranked-tail contract)"
                )
        relevances = relevance_list(grades, ranked_ids)
        ndcg10 = _round(ndcg_at_k(relevances, 10))
        ndcg24 = _round(ndcg_at_k(relevances, 24))
        ndcg_at_10_values.append(ndcg10)
        ndcg_at_24_values.append(ndcg24)
        per_query.append(
            {
                "query_id": query_id,
                "n_pool_candidates": n_pool,
                "stored_ranks_used": stored_ranks_used,
                "ndcg_at_10": ndcg10,
                "ndcg_at_24": ndcg24,
                "status": "judged",
            }
        )

    n_pool_queries = len(grades_by_query) + len(excluded)
    n_judged = len(grades_by_query)
    mean_at_10: float | None = None
    mean_at_24: float | None = None
    if ndcg_at_10_values:
        mean_at_10 = _round(math.fsum(ndcg_at_10_values) / len(ndcg_at_10_values))
        mean_at_24 = _round(math.fsum(ndcg_at_24_values) / len(ndcg_at_24_values))

    if n_rows == 0:
        notes.append(
            f"no qrels rows for pool_version={pool_version!r}: evalsets are empty "
            "(expected this sprint after the T0 fallback); harness reports zeros"
        )
    elif n_judged == 0 and not skipped:
        notes.append(
            "no fully judged queries under the target pool_version; NDCG is "
            "reported as null instead of a fabricated score"
        )

    coverage = _round(n_judged / n_bank_queries) if n_bank_queries > 0 else 0.0

    return {
        "schema": "gifagent.token_sprint.eval_harness.report.v1",
        "pool_version": pool_version,
        "ranker": ranker,
        "ranker_mode": "live_rerun" if rank_fn is not None else "stored_pool_sources",
        "ranker_status": ranker_status,
        "ranker_set_hash": ranker_set_hash,
        "n_qrels_rows_total": len(qrels_rows),
        "n_qrels_rows_pool_version": n_rows,
        "n_bank_queries": n_bank_queries,
        "n_pool_queries": n_pool_queries,
        "n_judged_queries": n_judged,
        "n_excluded_queries": len(excluded),
        "excluded_queries": [
            {"query_id": query_id, "reason": excluded[query_id]}
            for query_id in sorted(excluded)
        ],
        "coverage": coverage,
        "ndcg_at_10": mean_at_10,
        "ndcg_at_24": mean_at_24,
        "per_query": per_query,
        "ranking_contract": (
            "pool candidates ordered by stored 1-based rank (nulls last, "
            "candidate_id asc); NDCG@10/@24 via app.quality_lab.metrics.ndcg_at_k "
            "over grades 0-3; disputed queries excluded whole, never grade 0"
        ),
        "determinism": {
            "query_ordering": "query_id asc",
            "float_rounding_decimals": FLOAT_ROUNDING,
            "mean_aggregation": "math.fsum / n over per-query rounded values",
        },
        "notes": notes,
    }


def report_to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _resolve_arg_path(
    value: str | None, sprint_root: Path, default: Path | None
) -> Path | None:
    raw = value if value not in (None, "") else (default.as_posix() if default else None)
    if raw is None:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = sprint_root / path
    return path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "W2 deterministic retrieval eval harness: NDCG@10/@24 only over "
            "fully judged queries of the target pool_version."
        )
    )
    add_sprint_root_argument(parser)
    parser.add_argument(
        "--pool-version", required=True, help="Target pool_version to evaluate."
    )
    parser.add_argument(
        "--qrels",
        required=True,
        help="Qrels JSONL path (relative paths resolve against --sprint-root; "
        "a missing file is reported as empty input, not an error).",
    )
    parser.add_argument(
        "--snapshot",
        default=None,
        help="W1 snapshot db path (default: <sprint-root>/library_snapshot.db; "
        "SHA is verified against the canonical manifest).",
    )
    parser.add_argument(
        "--queries",
        default=None,
        help="Query bank JSONL for coverage (default: "
        "<sprint-root>/evalsets/query_bank_v1.jsonl; missing means coverage 0).",
    )
    parser.add_argument(
        "--ranker",
        choices=list(RANKERS),
        default="fts_v1",
        help="Ranker to evaluate (stored pool_sources ranks, or live rerun).",
    )
    parser.add_argument(
        "--rerun",
        action="store_true",
        help="Re-run the ranker live against the read-only snapshot instead of "
        "using stored pool_sources ranks.",
    )
    parser.add_argument(
        "--query-vectors",
        default=None,
        help="Optional query-vector cache JSON for hybrid rerun (default: "
        "<sprint-root>/evalsets/query_vectors.json).",
    )
    parser.add_argument(
        "--allow-uncanonical-snapshot",
        action="store_true",
        help="Allow a snapshot that is not registered in the canonical manifest "
        "(tmp/test fixtures only).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional report JSON output path (atomically written; relative "
        "paths resolve against --sprint-root).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    sprint_root = resolve_sprint_root(args.sprint_root)

    snapshot = _resolve_arg_path(
        args.snapshot, sprint_root, sprint_root / "library_snapshot.db"
    )
    assert snapshot is not None
    qrels_path = _resolve_arg_path(args.qrels, sprint_root, None)
    assert qrels_path is not None
    bank_path = _resolve_arg_path(
        args.queries, sprint_root, sprint_root / "evalsets" / "query_bank_v1.jsonl"
    )
    cache_path = _resolve_arg_path(
        args.query_vectors,
        sprint_root,
        sprint_root / "evalsets" / "query_vectors.json",
    )

    try:
        provenance = verify_snapshot_provenance(
            sprint_root,
            snapshot,
            allow_uncanonical=args.allow_uncanonical_snapshot,
        )
        qrels_rows = load_qrels(qrels_path)
        bank_rows, bank_notes = load_query_bank(bank_path)
    except EvalHarnessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    bank_ids = [row["query_id"] for row in bank_rows]
    query_texts = {
        row["query_id"]: row["text"] if isinstance(row.get("text"), str) else ""
        for row in bank_rows
    }
    notes: list[str] = list(bank_notes)

    conn: sqlite3.Connection | None = None
    rank_fn: RankFn | None = None
    ranker_status = "ok:stored_pool_sources"
    if args.rerun:
        try:
            conn = connect_readonly(Path(provenance["snapshot_path"]))
        except sqlite3.Error as exc:
            print(f"error: cannot open snapshot read-only: {exc}", file=sys.stderr)
            return 2
        prep = prepare_live_rerank(
            conn,
            qrels_rows,
            pool_version=args.pool_version,
            ranker=args.ranker,
            query_texts=query_texts,
            cache_path=cache_path,
        )
        if prep["status"] != "ok":
            ranker_status = prep["status"]
            notes.append(f"live rerun unavailable: {ranker_status}")
        else:
            ranker_status = "ok:live_rerun"

            def rank_fn(  # noqa: F811 - reassigned closure for live rerun
                query_id: str,
                grades: dict[str, int],
                pool_candidate_ids: list[str],
            ) -> list[str]:
                text = query_texts.get(query_id) or query_id
                limit = max(24, len(pool_candidate_ids))
                ids, _status = live_rank_ids(
                    conn,  # type: ignore[arg-type]
                    text,
                    ranker=args.ranker,
                    limit=limit,
                )
                pool_set = set(pool_candidate_ids)
                retrieved = [cid for cid in ids if cid in pool_set]
                missing_tail = sorted(pool_set - set(retrieved))
                return retrieved + missing_tail

    try:
        report = evaluate_pool_version(
            qrels_rows,
            pool_version=args.pool_version,
            ranker=args.ranker,
            rank_fn=rank_fn,
            n_bank_queries=len(bank_ids),
            extra_notes=notes,
            ranker_status=ranker_status,
        )
    except EvalHarnessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()
    report["snapshot"] = provenance

    payload = report_to_json(report)
    if args.output:
        output = _resolve_arg_path(args.output, sprint_root, None)
        assert output is not None
        atomic_write_text(output, payload)
    sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

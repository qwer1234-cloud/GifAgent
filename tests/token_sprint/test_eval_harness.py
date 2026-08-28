"""Tests for the W2 deterministic retrieval eval harness (token sprint Task 1).

Contract under test (implementation plan 2026-08-28-token-sprint-w2-eval-harness):

- NDCG@10/@24 only over fully judged queries of the target ``pool_version``;
  a single ``disputed`` pair excludes the whole query (never grade 0).
- Same input twice -> byte-identical report (determinism locked).
- ``base_rag_similarity`` (gif_worthiness) is never read, even when present.
- Empty evalsets (missing qrels / zero fully judged queries) produce a
  graceful zero report instead of a crash.
- Snapshot provenance: canonical-manifest SHA verification fails closed.
- ``fts_v2`` live rerun and hybrid without query vectors are reported as
  ``skipped`` instead of fabricating scores.

All fixtures use in-memory / tmp_path SQLite; no real annotation data.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from app.quality_lab.metrics import ndcg_at_k
from scripts.token_sprint import eval_harness as eh

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
PV = "pv-test-1"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def make_qrel(
    query_id: str,
    candidate_id: str,
    grade: int,
    *,
    status: str = "judged",
    pool_version: str = PV,
    ranker_set_hash: str = HASH_A,
    ranks: dict[str, int | None] | None = None,
    label_tier: str = "silver",
) -> dict:
    pool_sources = {"fts_v1": None, "hybrid": None, "fts_v2": None}
    if ranks:
        pool_sources.update(ranks)
    return {
        "query_id": query_id,
        "pool_version": pool_version,
        "ranker_set_hash": ranker_set_hash,
        "candidate_id": candidate_id,
        "grade": grade,
        "rationale": "text",
        "pool_sources": pool_sources,
        "label_tier": label_tier,
        "status": status,
    }


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    return path


def make_bare_snapshot(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    snapshot = root / "library_snapshot.db"
    snapshot.write_bytes(b"")
    return snapshot


def build_library_db(
    path: Path,
    candidates: list[tuple[str, str, list[str], float | None]],
    *,
    with_vectors: bool = False,
) -> Path:
    """Create a real snapshot-like library db (text only, no media)."""
    from app.services.preference_schema import apply_preference_schema
    from app.services.workbench_schema import apply_search_schema

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    apply_search_schema(conn)
    for candidate_id, caption, tags, base_rag in candidates:
        conn.execute(
            """INSERT INTO candidate_gifs
               (candidate_id, source_run_id, source_run_candidate_id,
                source_video_sha256, source_video_path, start_sec, end_sec,
                artifact_path, preview_path, vlm_summary_json, tags_json,
                scenario_keys_json, base_rag_similarity, final_score, status,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                candidate_id,
                "run-1",
                f"rc-{candidate_id}",
                "video-default",
                "/videos/sample.mp4",
                0.0,
                5.0,
                "data/exports/full.gif",
                "data/thumbs/preview.jpg",
                json.dumps({"caption": caption}),
                json.dumps(tags),
                "[]",
                base_rag,
                None,
                "candidate",
                "2026-07-18T00:00:00+00:00",
                "2026-07-18T00:00:00+00:00",
            ),
        )
        conn.execute(
            """INSERT OR REPLACE INTO candidate_search_fts
               (candidate_id, summary, tags, source_path) VALUES (?, ?, ?, ?)""",
            (candidate_id, caption, " ".join(tags), "/videos/sample.mp4"),
        )
        if with_vectors:
            rng = np.random.default_rng(abs(hash(candidate_id)) % (2**32))
            vec = rng.normal(0, 1, 768).astype(np.float32)
            vec = vec / np.linalg.norm(vec)
            conn.execute(
                """INSERT OR REPLACE INTO candidate_vectors
                   (candidate_id, vector_type, embedding_model, embedding_dim, vector_blob)
                   VALUES (?,?,?,?,?)""",
                (
                    candidate_id,
                    "clip",
                    "nomic-embed-text:latest",
                    768,
                    vec.tobytes(),
                ),
            )
    conn.commit()
    conn.close()
    return path


def run_cli(argv: list[str], capsys) -> tuple[int, str, str]:
    code = eh.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def parse_report(out: str) -> dict:
    return json.loads(out)


def _broken_default_embed_fn() -> object:
    """Stand-in for the module embedder hook: raises on use (no online calls)."""
    raise RuntimeError("no embedder in tests")


class SpyConn:
    """Delegating connection proxy recording every SQL statement executed."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.executed: list[str] = []

    def execute(self, sql: str, params=()):
        self.executed.append(" ".join(sql.split()))
        return self._real.execute(sql, params)

    def executescript(self, script: str) -> None:
        self.executed.append(" ".join(script.split()))
        self._real.executescript(script)

    def commit(self) -> None:
        self._real.commit()

    def rollback(self) -> None:
        self._real.rollback()

    def close(self) -> None:
        self._real.close()

    def __getattr__(self, name: str):
        return getattr(self._real, name)


# ---------------------------------------------------------------------------
# Fully-judged filtering + disputed exclusion
# ---------------------------------------------------------------------------


def test_report_contains_only_fully_judged_queries():
    rows = [
        # fully judged query: 4 pool candidates with stored fts_v1 ranks
        make_qrel("q_ok", "c1", 3, ranks={"fts_v1": 1}),
        make_qrel("q_ok", "c2", 2, ranks={"fts_v1": 2}),
        make_qrel("q_ok", "c3", 1, ranks={"fts_v1": 3}),
        make_qrel("q_ok", "c4", 0, ranks={"fts_v1": 4}),
        # disputed query: one disputed pair excludes the WHOLE query
        make_qrel("q_bad", "d1", 3, ranks={"fts_v1": 1}),
        make_qrel("q_bad", "d2", 0, status="disputed", ranks={"fts_v1": 2}),
    ]
    report = eh.evaluate_pool_version(rows, pool_version=PV, ranker="fts_v1")

    assert report["n_pool_queries"] == 2
    assert report["n_judged_queries"] == 1
    assert report["n_excluded_queries"] == 1
    assert [entry["query_id"] for entry in report["excluded_queries"]] == ["q_bad"]
    assert "disputed" in report["excluded_queries"][0]["reason"]
    # The mean must equal q_ok's per-query value exactly: q_bad contributed
    # nothing (and its disputed pair was NOT folded in as grade 0).
    assert report["ndcg_at_10"] == report["per_query"][0]["ndcg_at_10"]
    assert report["ndcg_at_10"] == round(ndcg_at_k([3.0, 2.0, 1.0, 0.0], 10), 6)
    assert report["ranker_set_hash"] == HASH_A


def test_disputed_never_counts_as_grade_zero():
    # If the disputed pair were silently graded 0, the judged set would grow
    # and the mean would change; the contract excludes it entirely.
    rows = [
        make_qrel("q1", "a1", 3, ranks={"fts_v1": 1}),
        make_qrel("q1", "a2", 0, status="disputed", ranks={"fts_v1": 2}),
    ]
    report = eh.evaluate_pool_version(rows, pool_version=PV, ranker="fts_v1")
    assert report["n_judged_queries"] == 0
    assert report["ndcg_at_10"] is None
    assert report["ndcg_at_24"] is None
    assert report["per_query"] == []
    assert report["n_excluded_queries"] == 1


def test_two_runs_identical_pure_function():
    rows = [
        make_qrel("q1", "a1", 3, ranks={"fts_v1": 2}),
        make_qrel("q1", "a2", 1, ranks={"fts_v1": 1}),
        make_qrel("q2", "b1", 2, ranks={"fts_v1": 1}),
    ]
    first = eh.evaluate_pool_version(rows, pool_version=PV, ranker="fts_v1")
    second = eh.evaluate_pool_version(rows, pool_version=PV, ranker="fts_v1")
    assert first == second
    assert eh.report_to_json(first) == eh.report_to_json(second)


def test_report_independent_of_qrels_row_order():
    rows = [
        make_qrel("q2", "b1", 2, ranks={"fts_v1": 1}),
        make_qrel("q1", "a1", 3, ranks={"fts_v1": 2}),
        make_qrel("q1", "a2", 1, ranks={"fts_v1": 1}),
    ]
    shuffled = [rows[2], rows[0], rows[1]]
    assert eh.evaluate_pool_version(rows, pool_version=PV, ranker="fts_v1") == (
        eh.evaluate_pool_version(shuffled, pool_version=PV, ranker="fts_v1")
    )


def test_stored_rank_ordering_and_unranked_tail():
    grades = {"c1": 3, "c2": 2, "c3": 0}
    sources = {
        "c1": {"fts_v1": 2, "hybrid": None, "fts_v2": None},
        "c2": {"fts_v1": 1, "hybrid": None, "fts_v2": None},
        "c3": {"fts_v1": None, "hybrid": None, "fts_v2": None},
    }
    ranked = eh.rank_by_pool_sources(grades, sources, "fts_v1")
    assert ranked == ["c2", "c1", "c3"]  # unranked candidate goes last
    relevances = eh.relevance_list(grades, ranked)
    assert relevances == [2.0, 3.0, 0.0]


def test_missing_stored_ranks_gets_note_but_still_scores():
    rows = [
        make_qrel("q1", "a1", 3, ranks={"hybrid": 1}),  # no fts_v1 rank at all
        make_qrel("q1", "a2", 1, ranks={"hybrid": 2}),
    ]
    report = eh.evaluate_pool_version(rows, pool_version=PV, ranker="fts_v1")
    assert report["ndcg_at_10"] is not None
    assert any("no stored rank" in note for note in report["notes"])


def test_pool_version_filtering():
    rows = [
        make_qrel("q1", "a1", 3, pool_version="pv-a", ranks={"fts_v1": 1}),
        make_qrel("q1", "a2", 1, pool_version="pv-a", ranks={"fts_v1": 2}),
        # Same query under another pool version is disputed: must not leak in.
        make_qrel("q1", "a1", 3, pool_version="pv-b", ranks={"fts_v1": 1}),
        make_qrel("q1", "a2", 0, pool_version="pv-b", status="disputed"),
    ]
    report = eh.evaluate_pool_version(rows, pool_version="pv-a", ranker="fts_v1")
    assert report["n_qrels_rows_total"] == 4
    assert report["n_qrels_rows_pool_version"] == 2
    assert report["n_judged_queries"] == 1
    assert report["n_excluded_queries"] == 0
    assert report["ndcg_at_10"] == 1.0


def test_bank_coverage():
    rows = [
        make_qrel("q1", "a1", 3, ranks={"fts_v1": 1}),
        make_qrel("q1", "a2", 1, ranks={"fts_v1": 2}),
        make_qrel("q2", "b1", 2, status="disputed", ranks={"fts_v1": 1}),
    ]
    report = eh.evaluate_pool_version(
        rows, pool_version=PV, ranker="fts_v1", n_bank_queries=3
    )
    assert report["n_bank_queries"] == 3
    assert report["coverage"] == round(1 / 3, 6)


# ---------------------------------------------------------------------------
# base_rag_similarity (gif_worthiness) must never be read
# ---------------------------------------------------------------------------


def test_no_sql_statement_reads_base_rag_similarity(tmp_path):
    path = build_library_db(
        tmp_path / "lib.db",
        [
            ("c_cat", "cat jumps over the fence", ["cat"], 0.01),
            ("c_dog", "dog runs in the park", ["dog"], 0.99),
        ],
    )
    real_conn = eh.connect_readonly(path)
    spy = SpyConn(real_conn)
    ids, status = eh.live_rank_ids(spy, "cat", ranker="fts_v1", limit=24)
    real_conn.close()
    assert status == "ok:live_rerun"
    assert ids == ["c_cat"]  # FTS-only ranking, not worthiness ordering
    assert spy.executed, "spy must observe the SQL executed by the ranker"
    assert all("base_rag_similarity" not in sql for sql in spy.executed)


def test_live_rerun_ignores_worthiness_values(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    monkeypatch.setattr(eh, "_default_embed_fn", _broken_default_embed_fn)
    # Two libraries identical except base_rag_similarity (worthiness) values
    # are inverted; the harness report must be byte-identical on both.  The
    # worthiness ordering also contradicts both the true relevance order and
    # the alphabetical order, so any use of it would change the ranking.
    candidates_low = [
        ("c_cat", "cat jumps over the fence", ["cat"], 0.01),
        ("c_dog", "dog runs in the park", ["dog"], 0.99),
    ]
    candidates_high = [
        ("c_cat", "cat jumps over the fence", ["cat"], 0.99),
        ("c_dog", "dog runs in the park", ["dog"], 0.01),
    ]
    reports = []
    for index, candidates in enumerate((candidates_low, candidates_high)):
        root = tmp_path / f"root{index}"
        (root / "evalsets" / "qrels").mkdir(parents=True)
        snapshot = build_library_db(root / "library_snapshot.db", candidates)
        bank = write_jsonl(
            root / "evalsets" / "query_bank_v1.jsonl",
            [{"query_id": "q1", "text": "cat", "origin": "human"}],
        )
        qrels = write_jsonl(
            root / "evalsets" / "qrels" / "pv-live.jsonl",
            [
                make_qrel("q1", "c_cat", 3, pool_version="pv-live", ranks={"fts_v1": 1}),
                make_qrel("q1", "c_dog", 0, pool_version="pv-live", ranks={"fts_v1": 2}),
            ],
        )
        code, out, err = run_cli(
            [
                "--sprint-root", str(root),
                "--pool-version", "pv-live",
                "--qrels", qrels.as_posix(),
                "--snapshot", snapshot.as_posix(),
                "--queries", bank.as_posix(),
                "--allow-uncanonical-snapshot",
                "--rerun",
                "--ranker", "fts_v1",
            ],
            capsys,
        )
        assert code == 0, err
        report = parse_report(out)
        del report["snapshot"]  # paths / hashes legitimately differ
        reports.append(report)
    assert reports[0] == reports[1]
    # True relevance order (c_cat grade 3 first) must yield a perfect NDCG;
    # worthiness ordering would rank c_dog (0.99) first and break this.
    assert reports[0]["ndcg_at_10"] == 1.0


# ---------------------------------------------------------------------------
# Empty input behaviour (evalsets/ empty is the expected state this sprint)
# ---------------------------------------------------------------------------


def test_empty_input_reports_gracefully(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    root = tmp_path / "root"
    root.mkdir()
    make_bare_snapshot(root)
    # No evalsets/ directory at all: qrels + query bank missing.
    code, out, err = run_cli(
        [
            "--sprint-root", str(root),
            "--pool-version", "pv-x",
            "--qrels", "evalsets/qrels/pv-x.jsonl",
            "--allow-uncanonical-snapshot",
        ],
        capsys,
    )
    assert code == 0, err
    report = parse_report(out)
    assert report["n_qrels_rows_total"] == 0
    assert report["n_qrels_rows_pool_version"] == 0
    assert report["n_bank_queries"] == 0
    assert report["n_pool_queries"] == 0
    assert report["n_judged_queries"] == 0
    assert report["coverage"] == 0.0
    assert report["ndcg_at_10"] is None
    assert report["ndcg_at_24"] is None
    assert report["per_query"] == []
    assert report["ranker_set_hash"] is None
    assert any("evalsets are empty" in note for note in report["notes"])


def test_all_disputed_report(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    root = tmp_path / "root"
    make_bare_snapshot(root)
    qrels = write_jsonl(
        root / "evalsets" / "qrels" / "pv-d.jsonl",
        [make_qrel("q1", "a1", 2, status="disputed", pool_version="pv-d")],
    )
    code, out, err = run_cli(
        [
            "--sprint-root", str(root),
            "--pool-version", "pv-d",
            "--qrels", qrels.as_posix(),
            "--allow-uncanonical-snapshot",
        ],
        capsys,
    )
    assert code == 0, err
    report = parse_report(out)
    assert report["n_judged_queries"] == 0
    assert report["ndcg_at_10"] is None
    assert any("no fully judged queries" in note for note in report["notes"])


# ---------------------------------------------------------------------------
# Fail-closed inputs
# ---------------------------------------------------------------------------


def test_missing_sprint_root_exits_2(capsys, monkeypatch):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        eh.main(["--pool-version", "pv", "--qrels", "q.jsonl"])
    assert excinfo.value.code == 2


def test_snapshot_missing_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    root = tmp_path / "root"
    root.mkdir()
    code, out, err = run_cli(
        [
            "--sprint-root", str(root),
            "--pool-version", "pv",
            "--qrels", "evalsets/qrels/pv.jsonl",
        ],
        capsys,
    )
    assert code == 2
    assert "snapshot database not found" in err


def test_snapshot_sha_mismatch_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    root = tmp_path / "root"
    root.mkdir()
    snapshot = make_bare_snapshot(root)
    (root / "sprint_manifest.json").write_text(
        json.dumps(
            {
                "artifacts": {
                    "w1": {
                        "artifact_path": snapshot.resolve().as_posix(),
                        "artifact_sha256": "0" * 64,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    code, out, err = run_cli(
        [
            "--sprint-root", str(root),
            "--pool-version", "pv",
            "--qrels", "evalsets/qrels/pv.jsonl",
        ],
        capsys,
    )
    assert code == 2
    assert "SHA-256 mismatch" in err


def test_uncanonical_snapshot_requires_flag(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    root = tmp_path / "root"
    root.mkdir()
    snapshot = make_bare_snapshot(root)
    (root / "sprint_manifest.json").write_text(
        json.dumps({"artifacts": {}}), encoding="utf-8"
    )
    argv = [
        "--sprint-root", str(root),
        "--pool-version", "pv",
        "--qrels", "evalsets/qrels/pv.jsonl",
    ]
    code, out, err = run_cli(argv, capsys)
    assert code == 2
    assert "uncanonical" in err
    code, out, err = run_cli(argv + ["--allow-uncanonical-snapshot"], capsys)
    assert code == 0, err


def test_qrels_schema_violations_fail_closed():
    valid = make_qrel("q1", "a1", 1)
    cases = [
        {**valid, "grade": 5},
        {**valid, "grade": "2"},
        {**valid, "status": "pending"},
        {**valid, "label_tier": "bronze"},
        {**valid, "ranker_set_hash": "deadbeef"},
        {**valid, "pool_sources": {"fts_v1": 0}},
        {**valid, "pool_sources": {"bogus": 1}},
        {key: value for key, value in valid.items() if key != "pool_sources"},
    ]
    for case in cases:
        with pytest.raises(eh.EvalHarnessError) as excinfo:
            eh._validate_qrel_row(case, 1)
        assert "invalid qrels (line 1)" in str(excinfo.value)


def test_duplicate_qrel_pair_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    root = tmp_path / "root"
    make_bare_snapshot(root)
    qrels = write_jsonl(
        root / "evalsets" / "qrels" / "pv-dup.jsonl",
        [
            make_qrel("q1", "a1", 3, pool_version="pv-dup", ranks={"fts_v1": 1}),
            make_qrel("q1", "a1", 2, pool_version="pv-dup", ranks={"fts_v1": 1}),
        ],
    )
    code, out, err = run_cli(
        [
            "--sprint-root", str(root),
            "--pool-version", "pv-dup",
            "--qrels", qrels.as_posix(),
            "--allow-uncanonical-snapshot",
        ],
        capsys,
    )
    assert code == 2
    assert "duplicate" in err


def test_ranker_set_hash_inconsistency_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    root = tmp_path / "root"
    make_bare_snapshot(root)
    qrels = write_jsonl(
        root / "evalsets" / "qrels" / "pv-hash.jsonl",
        [
            make_qrel("q1", "a1", 3, pool_version="pv-hash", ranker_set_hash=HASH_A),
            make_qrel("q1", "a2", 1, pool_version="pv-hash", ranker_set_hash=HASH_B),
        ],
    )
    code, out, err = run_cli(
        [
            "--sprint-root", str(root),
            "--pool-version", "pv-hash",
            "--qrels", qrels.as_posix(),
            "--allow-uncanonical-snapshot",
        ],
        capsys,
    )
    assert code == 2
    assert "ranker_set_hash" in err


# ---------------------------------------------------------------------------
# Skipped rankers: no fabricated scores
# ---------------------------------------------------------------------------


def test_stored_fts_v2_ranks_score_without_v2_table(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    root = tmp_path / "root"
    make_bare_snapshot(root)
    qrels = write_jsonl(
        root / "evalsets" / "qrels" / "pv-v2.jsonl",
        [
            make_qrel("q1", "a1", 3, pool_version="pv-v2", ranks={"fts_v2": 1}),
            make_qrel("q1", "a2", 0, pool_version="pv-v2", ranks={"fts_v2": 2}),
        ],
    )
    code, out, err = run_cli(
        [
            "--sprint-root", str(root),
            "--pool-version", "pv-v2",
            "--qrels", qrels.as_posix(),
            "--allow-uncanonical-snapshot",
            "--ranker", "fts_v2",
        ],
        capsys,
    )
    assert code == 0, err
    report = parse_report(out)
    assert report["ranker_status"] == "ok:stored_pool_sources"
    assert report["ndcg_at_10"] == 1.0  # stored ranks: grade 3 at rank 1


def test_live_fts_v2_skipped_without_fake_scores(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    root = tmp_path / "root"
    make_bare_snapshot(root)
    qrels = write_jsonl(
        root / "evalsets" / "qrels" / "pv-v2.jsonl",
        [
            make_qrel("q1", "a1", 3, pool_version="pv-v2", ranks={"fts_v2": 1}),
            make_qrel("q1", "a2", 0, pool_version="pv-v2", ranks={"fts_v2": 2}),
        ],
    )
    code, out, err = run_cli(
        [
            "--sprint-root", str(root),
            "--pool-version", "pv-v2",
            "--qrels", qrels.as_posix(),
            "--allow-uncanonical-snapshot",
            "--ranker", "fts_v2",
            "--rerun",
        ],
        capsys,
    )
    assert code == 0, err
    report = parse_report(out)
    assert report["ranker_status"].startswith("skipped:")
    assert report["ndcg_at_10"] is None
    assert report["ndcg_at_24"] is None
    assert all(
        entry["status"].startswith("skipped:") for entry in report["per_query"]
    )


def test_hybrid_live_skipped_without_candidate_vectors(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    root = tmp_path / "root"
    snapshot = build_library_db(
        root / "library_snapshot.db",
        [("c1", "cat jumps", ["cat"], None)],
    )
    qrels = write_jsonl(
        root / "evalsets" / "qrels" / "pv-h.jsonl",
        [
            make_qrel("q1", "c1", 3, pool_version="pv-h", ranks={"hybrid": 1}),
        ],
    )
    monkeypatch.setattr(eh, "_default_embed_fn", _broken_default_embed_fn)
    code, out, err = run_cli(
        [
            "--sprint-root", str(root),
            "--pool-version", "pv-h",
            "--qrels", qrels.as_posix(),
            "--snapshot", snapshot.as_posix(),
            "--allow-uncanonical-snapshot",
            "--ranker", "hybrid",
            "--rerun",
        ],
        capsys,
    )
    assert code == 0, err
    report = parse_report(out)
    assert report["ranker_status"] == eh.SKIP_NO_CANDIDATE_VECTORS
    assert report["ndcg_at_10"] is None


def test_hybrid_live_skipped_without_query_vector_or_cache(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    root = tmp_path / "root"
    snapshot = build_library_db(
        root / "library_snapshot.db",
        [("c1", "cat jumps", ["cat"], None)],
        with_vectors=True,
    )
    qrels = write_jsonl(
        root / "evalsets" / "qrels" / "pv-h.jsonl",
        [
            make_qrel("q1", "c1", 3, pool_version="pv-h", ranks={"hybrid": 1}),
        ],
    )
    monkeypatch.setattr(eh, "_default_embed_fn", _broken_default_embed_fn)
    code, out, err = run_cli(
        [
            "--sprint-root", str(root),
            "--pool-version", "pv-h",
            "--qrels", qrels.as_posix(),
            "--snapshot", snapshot.as_posix(),
            "--allow-uncanonical-snapshot",
            "--ranker", "hybrid",
            "--rerun",
        ],
        capsys,
    )
    assert code == 0, err
    report = parse_report(out)
    assert report["ranker_status"] == eh.SKIP_NO_QUERY_VECTOR
    assert report["ndcg_at_10"] is None


def test_hybrid_live_with_cache_runs_and_is_deterministic(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    monkeypatch.setattr(eh, "_default_embed_fn", _broken_default_embed_fn)
    root = tmp_path / "root"
    snapshot = build_library_db(
        root / "library_snapshot.db",
        [
            ("c_cat", "cat jumps over the fence", ["cat"], None),
            ("c_dog", "dog runs in the park", ["dog"], None),
        ],
        with_vectors=True,
    )
    # Cache maps the query text to a vector aligned with c_cat's vector so the
    # hybrid ordering is deterministic without any online embedder.
    real_conn = sqlite3.connect(snapshot)
    cat_vec = np.frombuffer(
        real_conn.execute(
            "SELECT vector_blob FROM candidate_vectors WHERE candidate_id='c_cat'"
        ).fetchone()[0],
        dtype=np.float32,
    )
    real_conn.close()
    cache_path = root / "evalsets" / "query_vectors.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"cat": [float(x) for x in cat_vec]}), encoding="utf-8"
    )
    bank = write_jsonl(
        root / "evalsets" / "query_bank_v1.jsonl",
        [{"query_id": "q1", "text": "cat", "origin": "human"}],
    )
    qrels = write_jsonl(
        root / "evalsets" / "qrels" / "pv-h.jsonl",
        [
            make_qrel("q1", "c_cat", 3, pool_version="pv-h", ranks={"hybrid": 1}),
            make_qrel("q1", "c_dog", 0, pool_version="pv-h", ranks={"hybrid": 2}),
        ],
    )
    argv = [
        "--sprint-root", str(root),
        "--pool-version", "pv-h",
        "--qrels", qrels.as_posix(),
        "--snapshot", snapshot.as_posix(),
        "--queries", bank.as_posix(),
        "--allow-uncanonical-snapshot",
        "--ranker", "hybrid",
        "--rerun",
        "--query-vectors", cache_path.as_posix(),
    ]
    code1, out1, err1 = run_cli(argv, capsys)
    code2, out2, _err2 = run_cli(argv, capsys)
    assert code1 == 0, err1
    assert code2 == 0
    assert out1 == out2  # byte-identical: deterministic live rerun
    report = parse_report(out1)
    assert report["ranker_status"] == "ok:live_rerun"
    assert report["ndcg_at_10"] == 1.0  # c_cat (grade 3) must rank first


# ---------------------------------------------------------------------------
# CLI-level determinism over live FTS rerun
# ---------------------------------------------------------------------------


def test_live_fts_v1_two_cli_runs_byte_identical(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    root = tmp_path / "root"
    snapshot = build_library_db(
        root / "library_snapshot.db",
        [
            ("c_cat", "cat jumps over the fence", ["cat"], 0.1),
            ("c_dog", "dog runs in the park", ["dog"], 0.2),
            ("c_bird", "bird flies over the tree", ["bird"], 0.3),
        ],
    )
    bank = write_jsonl(
        root / "evalsets" / "query_bank_v1.jsonl",
        [
            {"query_id": "q1", "text": "cat", "origin": "human"},
            {"query_id": "q2", "text": "dog", "origin": "human"},
            {"query_id": "q3", "text": "fish", "origin": "human"},
        ],
    )
    qrels = write_jsonl(
        root / "evalsets" / "qrels" / "pv-live.jsonl",
        [
            make_qrel("q1", "c_cat", 3, pool_version="pv-live", ranks={"fts_v1": 1}),
            make_qrel("q1", "c_dog", 0, pool_version="pv-live", ranks={"fts_v1": 2}),
            make_qrel("q1", "c_bird", 0, pool_version="pv-live"),
            make_qrel("q2", "c_dog", 2, pool_version="pv-live", ranks={"fts_v1": 1}),
            make_qrel("q2", "c_cat", 0, pool_version="pv-live", ranks={"fts_v1": 2}),
            make_qrel("q2", "c_bird", 0, pool_version="pv-live"),
            make_qrel("q3", "c_bird", 0, pool_version="pv-live", status="disputed"),
        ],
    )
    argv = [
        "--sprint-root", str(root),
        "--pool-version", "pv-live",
        "--qrels", qrels.as_posix(),
        "--snapshot", snapshot.as_posix(),
        "--queries", bank.as_posix(),
        "--allow-uncanonical-snapshot",
        "--rerun",
        "--ranker", "fts_v1",
    ]
    code1, out1, err1 = run_cli(argv, capsys)
    code2, out2, _err2 = run_cli(argv, capsys)
    assert code1 == 0, err1
    assert code2 == 0
    assert out1 == out2  # same pool_version: two runs, identical numbers
    report = parse_report(out1)
    assert report["n_bank_queries"] == 3
    assert report["n_judged_queries"] == 2
    assert report["n_excluded_queries"] == 1  # q3 disputed -> excluded whole
    assert report["coverage"] == round(2 / 3, 6)
    assert report["ranker_mode"] == "live_rerun"
    per_query = {entry["query_id"]: entry for entry in report["per_query"]}
    assert per_query["q1"]["ndcg_at_10"] == 1.0  # "cat" -> c_cat first
    assert per_query["q2"]["ndcg_at_10"] == 1.0  # "dog" -> c_dog first


def test_output_flag_writes_report_file(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    root = tmp_path / "root"
    make_bare_snapshot(root)
    code, out, err = run_cli(
        [
            "--sprint-root", str(root),
            "--pool-version", "pv-o",
            "--qrels", "evalsets/qrels/pv-o.jsonl",
            "--allow-uncanonical-snapshot",
            "--output", "w2/report.json",
        ],
        capsys,
    )
    assert code == 0, err
    written = (root / "w2" / "report.json").read_text(encoding="utf-8")
    assert written == out

"""Tests for W1 ``merge_shards.py``: only .done shards enter the cache.

Walks the full ready/done/receipt four-party hash chain (coordinator protocol)
inside ``tmp_path``. Locked behaviours: shards without ``.done``, with hash
drift after ``.done``, without a binding receipt, or with malformed names are
skipped; refusal and sanitized rows land in ``enrichment_refusals``; the cache
is rebuildable from the approved JSONL alone (delete the db, re-merge, the
identity-keyed row set is unchanged).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from scripts.token_sprint.coordinator import protocol
from scripts.token_sprint.merge_shards import main

# ── helpers ──────────────────────────────────────────────────────────────────


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    return path


def _p3_row(candidate_id: str) -> dict:
    return {"candidate_id": candidate_id, "keywords": ["alpha", "beta", "gamma"]}


def _p2_row(candidate_id: str) -> dict:
    return {
        "candidate_id": candidate_id,
        "scene": {"setting": "studio", "act": "pose", "finish": None},
    }


def _shard_name(pass_id: str) -> str:
    return f"shard-x.{pass_id}.v1.{uuid.uuid4()}.jsonl"


def _approve(sprint_root: Path, shard: Path) -> None:
    """Full executor->reviewer chain for one shard (protocol functions)."""
    ready = protocol.write_ready(shard, producer="w1-executor", qc_summary={})
    done = protocol.write_done(
        shard, ready, reviewer="w1-reviewer", qc_evidence={"checks": "ok"}
    )
    protocol.write_receipt(
        sprint_root=sprint_root,
        workstream="w1",
        producer="w1-executor",
        reviewer="w1-reviewer",
        artifact=shard,
        ready=ready,
        done=done,
    )


def _db_rows(db_path: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# ── merge gating ─────────────────────────────────────────────────────────────


class TestMergeGating:
    def test_only_done_shards_with_receipt_are_merged(self, tmp_path, capsys):
        sprint_root = tmp_path / "sprint"
        shards = sprint_root / "shards"
        shard_done = _write_jsonl(
            shards / _shard_name("p3"),
            [_p3_row("c1"), _p3_row("c2"), {"candidate_id": "c3", "refused": True}],
        )
        shard_ready_only = _write_jsonl(shards / _shard_name("p3"), [_p3_row("c4")])
        shard_done_2 = _write_jsonl(shards / _shard_name("p2"), [_p2_row("c5")])
        _approve(sprint_root, shard_done)
        protocol.write_ready(
            shard_ready_only, producer="w1-executor", qc_summary={}
        )  # .ready but never .done
        _approve(sprint_root, shard_done_2)

        rc = main(["--sprint-root", str(sprint_root)])

        assert rc == 0
        err = capsys.readouterr().err
        assert "缺少 .done" in err

        db = sprint_root / "enrichment.db"
        rows = _db_rows(db, "SELECT candidate_id, pass_id FROM candidate_enrichment ORDER BY candidate_id")
        assert rows == [("c1", "p3"), ("c2", "p3"), ("c5", "p2")]
        refusals = _db_rows(
            db, "SELECT candidate_id, reason FROM enrichment_refusals"
        )
        assert refusals == [("c3", "refused")]

        # every inserted row carries the shard SHA it came from
        sha_rows = _db_rows(
            db,
            """SELECT DISTINCT shard_sha256 FROM candidate_enrichment
               WHERE candidate_id IN ('c1', 'c2')""",
        )
        from scripts.token_sprint.coordinator.common import sha256_file

        assert sha_rows == [(sha256_file(shard_done),)]

    def test_skip_on_hash_drift_after_done(self, tmp_path, capsys):
        sprint_root = tmp_path / "sprint"
        shard = _write_jsonl(sprint_root / "shards" / _shard_name("p3"), [_p3_row("c1")])
        _approve(sprint_root, shard)
        # tamper after the chain was written
        with shard.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_p3_row("c9")) + "\n")

        rc = main(["--sprint-root", str(sprint_root)])

        assert rc == 0
        assert "不一致" in capsys.readouterr().err
        rows = _db_rows(
            sprint_root / "enrichment.db", "SELECT candidate_id FROM candidate_enrichment"
        )
        assert rows == []

    def test_done_without_receipt_skipped(self, tmp_path, capsys):
        sprint_root = tmp_path / "sprint"
        shard = _write_jsonl(sprint_root / "shards" / _shard_name("p3"), [_p3_row("c1")])
        ready = protocol.write_ready(shard, producer="w1-executor", qc_summary={})
        protocol.write_done(shard, ready, reviewer="w1-reviewer", qc_evidence={})

        rc = main(["--sprint-root", str(sprint_root)])

        assert rc == 0
        assert "receipt" in capsys.readouterr().err
        rows = _db_rows(
            sprint_root / "enrichment.db", "SELECT candidate_id FROM candidate_enrichment"
        )
        assert rows == []

    def test_receipt_in_processed_dir_counts(self, tmp_path):
        sprint_root = tmp_path / "sprint"
        shard = _write_jsonl(sprint_root / "shards" / _shard_name("p3"), [_p3_row("c1")])
        ready = protocol.write_ready(shard, producer="w1-executor", qc_summary={})
        done = protocol.write_done(shard, ready, reviewer="w1-reviewer", qc_evidence={})
        receipt = protocol.write_receipt(
            sprint_root=sprint_root,
            workstream="w1",
            producer="w1-executor",
            reviewer="w1-reviewer",
            artifact=shard,
            ready=ready,
            done=done,
        )
        processed = sprint_root / "receipts" / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        receipt.replace(processed / receipt.name)

        assert main(["--sprint-root", str(sprint_root)]) == 0

        rows = _db_rows(
            sprint_root / "enrichment.db", "SELECT candidate_id FROM candidate_enrichment"
        )
        assert rows == [("c1",)]

    def test_malformed_shard_name_skipped(self, tmp_path, capsys):
        sprint_root = tmp_path / "sprint"
        _write_jsonl(sprint_root / "shards" / "shard-no-version.jsonl", [_p3_row("c1")])

        rc = main(["--sprint-root", str(sprint_root)])

        assert rc == 0
        assert "文件名" in capsys.readouterr().err


# ── sanitized rows + rebuildability ──────────────────────────────────────────


class TestSanitizedAndRebuild:
    def test_sanitized_rows_go_to_refusals(self, tmp_path):
        sprint_root = tmp_path / "sprint"
        shard = _write_jsonl(
            sprint_root / "shards" / _shard_name("p3"),
            [_p3_row("c1"), _p3_row("c2")],
        )
        qc = {
            "suspected_sanitized": ["c2"],
        }
        (sprint_root / "shards" / (shard.name + ".qc.json")).write_text(
            json.dumps(qc), encoding="utf-8"
        )
        _approve(sprint_root, shard)

        assert main(["--sprint-root", str(sprint_root)]) == 0

        db = sprint_root / "enrichment.db"
        content = _db_rows(db, "SELECT candidate_id FROM candidate_enrichment")
        refusals = _db_rows(db, "SELECT candidate_id, reason FROM enrichment_refusals")
        assert content == [("c1",)]
        assert refusals == [("c2", "sanitized")]

    def test_cache_rebuildable_from_jsonl_alone(self, tmp_path):
        sprint_root = tmp_path / "sprint"
        shard_a = _write_jsonl(
            sprint_root / "shards" / _shard_name("p3"),
            [_p3_row("c1"), {"candidate_id": "c3", "refused": True}],
        )
        shard_b = _write_jsonl(sprint_root / "shards" / _shard_name("p2"), [_p2_row("c5")])
        _approve(sprint_root, shard_a)
        _approve(sprint_root, shard_b)

        assert main(["--sprint-root", str(sprint_root)]) == 0
        db = sprint_root / "enrichment.db"
        first_rows = _db_rows(
            db,
            """SELECT candidate_id, pass_id, prompt_version, shard_sha256
               FROM candidate_enrichment ORDER BY candidate_id""",
        )
        first_refusals = _db_rows(
            db,
            """SELECT candidate_id, pass_id, prompt_version, shard_sha256
               FROM enrichment_refusals ORDER BY candidate_id""",
        )

        # drop the cache entirely and rebuild from the approved JSONL
        db.unlink()
        assert main(["--sprint-root", str(sprint_root)]) == 0

        assert (
            _db_rows(
                db,
                """SELECT candidate_id, pass_id, prompt_version, shard_sha256
                   FROM candidate_enrichment ORDER BY candidate_id""",
            )
            == first_rows
        )
        assert (
            _db_rows(
                db,
                """SELECT candidate_id, pass_id, prompt_version, shard_sha256
                   FROM enrichment_refusals ORDER BY candidate_id""",
            )
            == first_refusals
        )
        assert {row[1] for row in first_rows} == {"p3", "p2"}

    def test_merge_is_idempotent(self, tmp_path):
        sprint_root = tmp_path / "sprint"
        shard = _write_jsonl(sprint_root / "shards" / _shard_name("p3"), [_p3_row("c1")])
        _approve(sprint_root, shard)

        assert main(["--sprint-root", str(sprint_root)]) == 0
        assert main(["--sprint-root", str(sprint_root)]) == 0

        rows = _db_rows(
            sprint_root / "enrichment.db", "SELECT candidate_id FROM candidate_enrichment"
        )
        assert rows == [("c1",)]

    def test_conflicting_shards_last_sorted_wins(self, tmp_path):
        sprint_root = tmp_path / "sprint"
        shard_old = _write_jsonl(
            sprint_root / "shards" / "shard-a.p3.v1.00000000-0000-0000-0000-000000000000.jsonl",
            [_p3_row("c1")],
        )
        shard_new = _write_jsonl(
            sprint_root / "shards" / "shard-b.p3.v1.00000000-0000-0000-0000-000000000001.jsonl",
            [_p3_row("c1")],
        )
        _approve(sprint_root, shard_old)
        _approve(sprint_root, shard_new)

        assert main(["--sprint-root", str(sprint_root)]) == 0

        rows = _db_rows(
            sprint_root / "enrichment.db",
            "SELECT shard_id FROM candidate_enrichment WHERE candidate_id='c1'",
        )
        assert rows == [(shard_new.stem,)]

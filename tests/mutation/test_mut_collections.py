"""W4 kill tests for collections mutation survivors.

Targets survivors from the cosmic-ray run recorded in
``<sprint-root>/w4/mutation_scores/cosmic-ray/collections.json``:

- ``_cosine_distance`` clamping ([-1, 1] bounds, ``1.0 - sim``).
- ``_farthest_first_select``: combined-score blend weights and operator
  variants, min-distance default for vector-less candidates, first-pick
  indices, sort direction, ``len(pool) <= target`` short-circuit ordering,
  greedy tie handling, ``None`` score normalisation.
- ``_write_pbf`` score/source/timestamp fallbacks.
- ``freeze`` / ``export`` version guards, export directory creation,
  manifest encoding and indentation.
- ``refresh`` search-limit formula (``max(target_count * 5, 100)``).

Registered as equivalent/practical in w4-surviving.md: annotation-free
module but several blend mutants preserve ranking for non-negative scores,
the ``min()``-over-empty crash for a vector-less selected champion
(latent bug, W7), and the ``max(x, 101/99)`` limit constants shadowed by
``target_count * 5``.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.services.collections import CollectionService, _cosine_distance, _write_pbf
from app.services.preference_memory import REQUIRED_EMBEDDING_DIM, REQUIRED_EMBEDDING_MODEL
from app.services.workbench_schema import SearchPage, SearchResultItem


# ── helpers ─────────────────────────────────────────────────────────────────


def _conn() -> sqlite3.Connection:
    from app.services.preference_schema import apply_preference_schema
    from app.services.workbench_schema import (
        apply_collections_schema,
        apply_search_schema,
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    apply_search_schema(conn)
    apply_collections_schema(conn)
    return conn


def _unit(v: list[float]) -> np.ndarray:
    arr = np.array(v, dtype=np.float32)
    return arr / np.linalg.norm(arr)


def _insert_vector(conn: sqlite3.Connection, candidate_id: str, vec: np.ndarray) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO candidate_vectors"
        " (candidate_id, vector_type, embedding_model, embedding_dim, vector_blob)"
        " VALUES (?, 'clip', ?, ?, ?)",
        (candidate_id, REQUIRED_EMBEDDING_MODEL, REQUIRED_EMBEDDING_DIM, vec.astype(np.float32).tobytes()),
    )
    conn.commit()


def _item(cid: str, score) -> SearchResultItem:
    return SearchResultItem(
        candidate_id=cid,
        preview_path=None,
        source_video_path="/videos/a.mp4",
        start_sec=0.0,
        end_sec=5.0,
        duration=5.0,
        summary="s",
        tags=[],
        status="candidate",
        score=score,
        created_at="2026-07-18T00:00:00+00:00",
    )


class _StubSearch:
    """Records the last search() call and returns canned items."""

    def __init__(self, items):
        self.items = items
        self.last_limit = None

    def search(self, query, *, limit, offset):
        self.last_limit = limit
        return SearchPage(items=self.items[:limit], total=len(self.items), limit=limit, offset=offset)


def _service(conn):
    return CollectionService(conn, _StubSearch([]))


# ── _cosine_distance ────────────────────────────────────────────────────────


def test_cosine_distance_basics_and_clamping():
    e1 = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    e2 = np.array([0.0, 1.0, 0.0], dtype=np.float32)
    assert _cosine_distance(e1, e1) == pytest.approx(0.0)
    assert _cosine_distance(e1, e2) == pytest.approx(1.0)
    assert _cosine_distance(e1, -e1) == pytest.approx(2.0)
    # dot products outside [-1, 1] must clamp: 2 -> 0, -2 -> 2
    assert _cosine_distance(np.array([2.0, 0.0]), np.array([1.0, 0.0])) == pytest.approx(0.0)
    assert _cosine_distance(np.array([-2.0, 0.0]), np.array([1.0, 0.0])) == pytest.approx(2.0)


# ── _farthest_first_select ──────────────────────────────────────────────────

DIM = 8


def _vec(index: int) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    v[index] = 1.0
    return v


def _near(index: int) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    v[index] = 0.995
    v[(index + 1) % DIM] = 0.1
    return v / np.linalg.norm(v)


def test_farthest_first_short_circuit_returns_insertion_order():
    conn = _conn()
    svc = _service(conn)
    items = [_item("a", 0.5), _item("b", 0.9)]
    selected = svc._farthest_first_select(items, 3, 0.5)
    # pool (2) <= target (3): returned as-is, NOT reordered by score
    assert [cid for cid, _ in selected] == ["a", "b"]


def test_farthest_first_prefers_diverse_second_pick():
    conn = _conn()
    _insert_vector(conn, "a", _vec(0))
    _insert_vector(conn, "b", _vec(1))
    _insert_vector(conn, "c", _near(0))
    svc = _service(conn)
    items = [_item("a", 0.9), _item("b", 0.8), _item("c", 0.7)]
    selected = svc._farthest_first_select(items, 2, 0.5)
    assert [cid for cid, _ in selected] == ["a", "b"]


def test_farthest_first_score_dominance_with_equal_distance():
    # b and c share the same vector -> identical distances; the higher score
    # must win ((1-dw)*sc term). Score-subtraction / power mutants flip it.
    conn = _conn()
    _insert_vector(conn, "a", _vec(0))
    _insert_vector(conn, "b", _vec(1))
    _insert_vector(conn, "c", _vec(1))
    svc = _service(conn)
    items = [_item("a", 0.9), _item("b", 0.8), _item("c", 0.7)]
    selected = svc._farthest_first_select(items, 2, 0.5)
    assert [cid for cid, _ in selected] == ["a", "b"]


def test_farthest_first_distance_dominance_with_equal_scores():
    # Equal scores: the candidate farthest from the champion wins via
    # diversity_weight * min_dist. Subtraction/division/power mutants of the
    # distance term flip the pick toward the near duplicate.
    conn = _conn()
    _insert_vector(conn, "a", _vec(0))
    _insert_vector(conn, "b", _vec(1))
    _insert_vector(conn, "c", _near(0))
    svc = _service(conn)
    items = [_item("a", 0.6), _item("b", 0.6), _item("c", 0.6)]
    selected = svc._farthest_first_select(items, 2, 0.5)
    assert [cid for cid, _ in selected] == ["a", "b"]


def test_farthest_first_high_score_champion_with_close_and_far_rivals():
    # Champion a; b duplicates a (score 8.0), c is far (score 7.5).
    # Original: distance term (0.5) outweighs the 0.5 score gap -> c.
    # A (1.0 -> 2.0) score-weight mutant keeps b.
    conn = _conn()
    _insert_vector(conn, "a", _vec(0))
    _insert_vector(conn, "b", _vec(0))
    _insert_vector(conn, "c", _vec(1))
    svc = _service(conn)
    items = [_item("a", 9.0), _item("b", 8.0), _item("c", 7.5)]
    selected = svc._farthest_first_select(items, 2, 0.5)
    assert [cid for cid, _ in selected] == ["a", "c"]


def test_farthest_first_vectorless_candidates_rank_by_score_only():
    # d has no vector row: min_dist defaults to 0.0 (not 1.0 / -1.0), so it
    # cannot outrank a vector-bearing candidate on diversity.
    conn = _conn()
    _insert_vector(conn, "a", _vec(0))
    _insert_vector(conn, "b", _vec(1))
    svc = _service(conn)
    items = [_item("a", 0.9), _item("b", 0.5), _item("d", 0.7)]
    selected = svc._farthest_first_select(items, 2, 0.5)
    assert [cid for cid, _ in selected] == ["a", "b"]


def test_farthest_first_diversity_scan_needs_the_vector_map():
    # ZeroIteration mutant empties the vector map; then min_dist is 0.0 for
    # everyone and the near duplicate wins on raw score.
    conn = _conn()
    _insert_vector(conn, "a", _vec(0))
    _insert_vector(conn, "b", _near(0))
    _insert_vector(conn, "c", _vec(1))
    svc = _service(conn)
    items = [_item("a", 0.9), _item("b", 0.8), _item("c", 0.7)]
    selected = svc._farthest_first_select(items, 2, 0.5)
    assert [cid for cid, _ in selected] == ["a", "c"]


def test_farthest_first_exact_tie_keeps_insertion_order():
    conn = _conn()
    _insert_vector(conn, "a", _vec(0))
    _insert_vector(conn, "b", _vec(0))
    _insert_vector(conn, "c", _vec(1))
    svc = _service(conn)
    # b and c have identical vectors and identical scores relative to a...
    # they tie on combined; the strict `>` keeps the FIRST (b).
    items = [_item("a", 0.9), _item("b", 0.5), _item("c", 0.5)]
    selected = svc._farthest_first_select(items, 3, 0.5)
    assert [cid for cid, _ in selected] == ["a", "b", "c"]


# ── _write_pbf ──────────────────────────────────────────────────────────────


def _read_pbf_bytes(data: bytes) -> list[dict]:
    assert data[:8] == b"GIFPBF01"
    count = struct.unpack("<I", data[8:12])[0]
    out = []
    pos = 12
    for _ in range(count):
        id_len = struct.unpack("<H", data[pos : pos + 2])[0]
        pos += 2
        cid = data[pos : pos + id_len].decode("utf-8")
        pos += id_len
        score = struct.unpack("<d", data[pos : pos + 8])[0]
        pos += 8
        src_len = struct.unpack("<H", data[pos : pos + 2])[0]
        pos += 2
        src = data[pos : pos + src_len].decode("utf-8")
        pos += src_len
        ts_len = struct.unpack("<H", data[pos : pos + 2])[0]
        pos += 2
        ts = data[pos : pos + ts_len].decode("utf-8")
        pos += ts_len
        out.append({"candidate_id": cid, "score": score, "source_video_path": src, "created_at": ts})
    return out


def test_write_pbf_falls_back_for_missing_and_falsy_fields(tmp_path: Path):
    target = tmp_path / "out.pbf"
    _write_pbf(
        target,
        [
            {"candidate_id": "x"},  # everything missing -> zeros/empties
            {
                "candidate_id": "y",
                "score": 0.0,  # falsy score must stay 0.0, not ±1
                "source_video_path": "/videos/y.mp4",
                "created_at": "2026-07-18T00:00:00+00:00",
            },
        ],
    )
    parsed = _read_pbf_bytes(target.read_bytes())
    assert parsed[0] == {"candidate_id": "x", "score": 0.0, "source_video_path": "", "created_at": ""}
    assert parsed[1]["score"] == 0.0
    assert parsed[1]["source_video_path"] == "/videos/y.mp4"
    assert parsed[1]["created_at"] == "2026-07-18T00:00:00+00:00"


# ── freeze / export / refresh ───────────────────────────────────────────────


def _make_collection(conn, svc, *, name="collection", target=2):
    from app.services.collections import CollectionSpec
    from app.services.workbench_schema import SearchQuery

    spec = CollectionSpec(
        name=name,
        query=SearchQuery(text="demo"),
        target_count=target,
        diversity_weight=0.5,
        profile_version="v1",
    )
    return svc.create(spec).collection_id


def test_export_without_versions_raises_value_error():
    conn = _conn()
    svc = _service(conn)
    collection_id = _make_collection(conn, svc)
    with pytest.raises(ValueError, match="no versions to export"):
        svc.export(collection_id, Path("."))


def test_refresh_with_empty_search_saves_empty_version_and_freeze_placeholder():
    conn = _conn()
    svc = _service(conn)
    collection_id = _make_collection(conn, svc)
    version = svc.refresh(collection_id)
    # An empty search still persists an immutable (empty) version.
    assert version.version == 1
    assert version.candidate_ids == ()
    frozen = svc.freeze(collection_id)
    assert frozen.version == 1
    assert frozen.candidate_ids == ()


def test_freeze_returns_latest_version_number():
    conn = _conn()
    stub = _StubSearch([])
    svc = CollectionService(conn, stub)
    collection_id = _make_collection(conn, svc)
    conn.execute(
        "UPDATE collections SET current_version=1 WHERE collection_id=?", (collection_id,)
    )
    conn.execute(
        "INSERT INTO collection_versions (collection_id, version, candidate_ids_json,"
        " scores_json, manifest_hash, created_at) VALUES (?, 1, '[]', '{}', 'h', 't')",
        (collection_id,),
    )
    conn.commit()
    frozen = svc.freeze(collection_id)
    assert frozen.version == 1
    assert frozen.manifest_hash == "h"


def test_export_with_version_hole_raises_value_error():
    conn = _conn()
    svc = _service(conn)
    collection_id = _make_collection(conn, svc)
    # current_version points at a missing version row (data hole)
    conn.execute(
        "UPDATE collections SET current_version=2 WHERE collection_id=?", (collection_id,)
    )
    conn.commit()
    with pytest.raises(ValueError, match="no versions to export"):
        svc.export(collection_id, Path("."))


def test_export_creates_nested_dirs_utf8_manifest_and_indent(tmp_path: Path):
    conn = _conn()
    conn.execute(
        "INSERT INTO candidate_gifs (candidate_id, source_run_id, source_run_candidate_id,"
        " source_video_sha256, source_video_path, start_sec, end_sec, artifact_path,"
        " preview_path, status, created_at, updated_at)"
        " VALUES ('c1', 'run', 'rc', 'sha', '/videos/场景.mp4', 0, 1, '/a.gif', '/p.jpg',"
        " 'candidate', 't', 't')"
    )
    conn.commit()
    stub = _StubSearch([])
    svc = CollectionService(conn, stub)
    collection_id = _make_collection(conn, svc, name="场景精选")
    conn.execute(
        "UPDATE collections SET current_version=1 WHERE collection_id=?", (collection_id,)
    )
    conn.execute(
        "INSERT INTO collection_versions (collection_id, version, candidate_ids_json,"
        " scores_json, manifest_hash, created_at) VALUES (?, 1, '[\"c1\"]', '{\"c1\": 0.5}', 'h', 't')",
        (collection_id,),
    )
    conn.commit()

    out_dir = tmp_path / "nested" / "deeper"  # parents must be created
    report = svc.export(collection_id, out_dir)
    manifest_raw = Path(report.manifest_path).read_text(encoding="utf-8")
    assert "场景精选" in manifest_raw  # ensure_ascii=False
    assert '\n  "name"' in manifest_raw  # indent=2
    manifest = json.loads(manifest_raw)
    assert manifest["candidates"][0]["rank"] == 0
    assert manifest["candidates"][0]["source_video_path"] == "/videos/场景.mp4"
    pbf_entries = _read_pbf_bytes(Path(report.pbf_path).read_bytes())
    assert pbf_entries[0]["score"] == 0.5
    assert pbf_entries[0]["source_video_path"] == "/videos/场景.mp4"


def test_refresh_passes_multiplicative_search_limit():
    conn = _conn()
    stub = _StubSearch([])
    svc = CollectionService(conn, stub)
    collection_id = _make_collection(conn, svc, target=25)
    svc.refresh(collection_id)
    # search_limit = max(25 * 5, 100) = 125
    assert stub.last_limit == 125

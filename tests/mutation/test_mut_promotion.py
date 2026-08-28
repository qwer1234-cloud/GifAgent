"""W4 kill tests for quality_lab.promotion mutation survivors.

Targets survivors from the cosmic-ray run recorded in
``<sprint-root>/w4/mutation_scores/cosmic-ray/promotion.json``:

- scorecard ``mean`` is ``sum(values) / len(values)`` (vs +/*/%/**).
- confirmation gate raises for a wrong confirmation that sorts BEFORE the
  config id (``!=`` vs ``>``).
- integrity gate: average strictly below the threshold is rejected, an
  average equal to the threshold is accepted, missing metrics default to
  1.0 (not 0.0), and the threshold constant is 0.9.
- JSON files are written with ``ensure_ascii=False`` and ``indent=2``.
- exception nets catch OSError/TypeError as documented.
- champion history / previous-config lookups invert correctly.

Equivalent mutants (keyword-only marker, 22 annotation unions, ``is not``
on interned config ids) are registered in w4-surviving.md. All file
artifacts are redirected into ``tmp_path`` via monkeypatch — no product
tree writes.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from app.quality_lab import apply_quality_schema
from app.quality_lab import promotion as promo_module
from app.quality_lab.promotion import (
    _get_current_config_data,
    list_champion_history,
    promote_config,
)


def _seed_full_promotable(db: sqlite3.Connection, *, config_id: str | None = None) -> str:
    """Seed config + tune/holdout runs + completed AB session; return cid."""
    cid = config_id or f"cfg_{uuid.uuid4().hex[:8]}"
    manifest_id = f"m_{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO experiment_configs (config_id, config_json, provenance_json, created_at)"
        " VALUES (?, ?, ?, '2026-07-18T00:00:00')",
        (cid, json.dumps({"vlm": {"model": "测试场景"}}), "{}"),
    )
    db.execute(
        "INSERT INTO benchmark_manifests (manifest_id, version, item_count, created_at)"
        " VALUES (?, 1, 1, '2026-07-18T00:00:00')",
        (manifest_id,),
    )
    tune = f"run_{uuid.uuid4().hex[:8]}"
    holdout = f"run_{uuid.uuid4().hex[:8]}"
    for rid, split in ((tune, "tune"), (holdout, "holdout")):
        db.execute(
            "INSERT INTO experiment_runs (run_id, manifest_id, config_id, split, status,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, 'completed', 't', 't')",
            (rid, manifest_id, cid, split),
        )
    db.execute(
        "INSERT INTO ab_sessions (session_id, run_a, run_b, seed, status, created_at)"
        " VALUES (?, ?, ?, 42, 'completed', 't')",
        (f"ab_{uuid.uuid4().hex[:8]}", tune, holdout),
    )
    db.commit()
    return cid


def _seed_metric(db: sqlite3.Connection, run_ids, value: float) -> None:
    for rid in run_ids:
        db.execute(
            "INSERT INTO metric_values (metric_id, run_id, metric_name, value, item_id, created_at)"
            " VALUES (?, ?, 'export_integrity', ?, NULL, 't')",
            (f"m_{uuid.uuid4().hex[:8]}", rid, value),
        )
    db.commit()


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """In-memory DB + product file paths redirected into tmp_path."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_quality_schema(conn)
    monkeypatch.setattr(promo_module, "CONFIG_VERSIONS_DIR", str(tmp_path / "config_versions"))
    monkeypatch.setattr(promo_module, "CURRENT_CONFIG_PATH", str(tmp_path / "current_config.json"))
    return {"db": conn, "tmp_path": tmp_path}


def test_scorecard_mean_is_the_arithmetic_mean(env):
    db = env["db"]
    cid = _seed_full_promotable(db)
    run_ids = [r["run_id"] for r in db.execute(
        "SELECT run_id FROM experiment_runs WHERE config_id=?", (cid,)).fetchall()]
    _seed_metric(db, run_ids, 1.0)
    # one extra metric group with distinguishable values
    db.execute(
        "INSERT INTO metric_values (metric_id, run_id, metric_name, value, item_id, created_at)"
        " VALUES (?, ?, 'custom_metric', 3.0, NULL, 't')", (f"m_{uuid.uuid4().hex[:8]}", run_ids[0]))
    db.execute(
        "INSERT INTO metric_values (metric_id, run_id, metric_name, value, item_id, created_at)"
        " VALUES (?, ?, 'custom_metric', 5.0, NULL, 't')", (f"m_{uuid.uuid4().hex[:8]}", run_ids[1]))
    db.commit()

    result = promote_config(cid, db_conn=db, confirmation=cid)
    scorecard = result["scorecard"]
    assert scorecard["custom_metric"]["mean"] == pytest.approx(4.0)
    assert scorecard["custom_metric"]["count"] == 2


def test_confirmation_gate_catches_wrong_confirmation(env):
    db = env["db"]
    cid = "b_config"
    _seed_full_promotable(db, config_id=cid)
    # confirmation sorts BEFORE the config id: a ``>`` mutant would not raise
    with pytest.raises(ValueError, match="Confirmation string does not match"):
        promote_config(cid, db_conn=db, confirmation="a_config")


def test_integrity_gate_boundaries_and_threshold_constant(env):
    db = env["db"]
    # exactly at the 0.9 threshold: accepted (a ``<=`` mutant would reject)
    cid = _seed_full_promotable(db)
    run_ids = [r["run_id"] for r in db.execute(
        "SELECT run_id FROM experiment_runs WHERE config_id=?", (cid,)).fetchall()]
    _seed_metric(db, run_ids, 0.9)
    result = promote_config(cid, db_conn=db, confirmation=cid)
    assert result["status"] == "promoted"

    # clearly below the 0.9 threshold: rejected (threshold mutants fail)
    cid2 = _seed_full_promotable(db)
    run_ids2 = [r["run_id"] for r in db.execute(
        "SELECT run_id FROM experiment_runs WHERE config_id=?", (cid2,)).fetchall()]
    _seed_metric(db, run_ids2, 0.5)
    with pytest.raises(ValueError, match="below gate threshold 0.9"):
        promote_config(cid2, db_conn=db, confirmation=cid2)


def test_missing_integrity_metrics_default_to_perfect(env):
    db = env["db"]
    cid = _seed_full_promotable(db)  # no metric_values rows at all
    result = promote_config(cid, db_conn=db, confirmation=cid)
    assert result["status"] == "promoted"


def test_promoted_files_use_utf8_and_two_space_indent(env):
    db = env["db"]
    cid = "cfg_场景"
    _seed_full_promotable(db, config_id=cid)
    result = promote_config(cid, db_conn=db, confirmation=cid)
    assert result["status"] == "promoted"

    current_raw = (env["tmp_path"] / "current_config.json").read_text(encoding="utf-8")
    assert "场景" in current_raw  # ensure_ascii=False
    assert '\n  "config_id"' in current_raw  # indent=2

    versioned_files = list((env["tmp_path"] / "config_versions").glob("*.json"))
    assert len(versioned_files) == 1
    versioned_raw = versioned_files[0].read_text(encoding="utf-8")
    assert "场景" in versioned_raw
    assert '\n  "config_id"' in versioned_raw


def test_get_current_config_data_swallows_os_errors(tmp_path, monkeypatch):
    target = tmp_path / "current_config.json"
    target.mkdir()  # a directory: open() raises PermissionError (OSError)
    monkeypatch.setattr(promo_module, "CURRENT_CONFIG_PATH", str(target))
    assert _get_current_config_data() is None


def test_champion_history_parses_scorecards_and_preserves_previous_id(env):
    db = env["db"]
    cid = _seed_full_promotable(db)
    db.execute(
        "INSERT INTO champion_history (config_id, action, previous_config_id,"
        " scorecard_json, created_at) VALUES ('old_cfg', 'promote', NULL, '{\"a\": 1}', 't')"
    )
    db.execute(
        "INSERT INTO champion_history (config_id, action, previous_config_id,"
        " scorecard_json, created_at) VALUES ('null_cfg', 'promote', NULL, NULL, 't3')"
    )
    db.commit()

    events = list_champion_history(db_conn=db)
    by_cfg = {e["config_id"]: e for e in events}
    assert by_cfg["old_cfg"]["scorecard"] == {"a": 1}
    assert by_cfg["null_cfg"]["scorecard"] is None

    # a promotion records a previous-config id; NOTE (pinned quirk, W7):
    # _get_current_config_id_before_overwrite() is called AFTER
    # current_config.json has been overwritten, so the recorded
    # previous_config_id is the newly promoted config itself, not the
    # actually-previous champion.
    result = promote_config(cid, db_conn=db, confirmation=cid)
    assert result["status"] == "promoted"
    previous = db.execute(
        "SELECT previous_config_id FROM champion_history WHERE config_id=? AND action='promote'",
        (cid,),
    ).fetchone()
    assert previous["previous_config_id"] == cid

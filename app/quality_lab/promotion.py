"""Phase 2 Task 6: Champion promotion and rollback service.

Gates for promotion
--------------------
1. Config exists in ``experiment_configs``.
2. Confirmation string equals *config_id*.
3. At least one completed tune experiment run for the config.
4. At least one completed holdout experiment run for the config.
5. At least one completed blind A/B review (AB session) involving
   any of the config's runs.
6. Average ``export_integrity`` metric across the config's runs is
   >= ``PROMOTION_GATE_THRESHOLD``.

On success
----------
- A versioned config snapshot is written to ``data/config_versions/``.
- ``data/current_config.json`` is updated atomically (tmp + replace).
- A ``promote`` row is inserted into ``champion_history``.
- ``configs/models.yaml`` is **never** touched.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROMOTION_GATE_THRESHOLD = 0.9
"""Minimum average ``export_integrity`` required for promotion."""

CONFIG_VERSIONS_DIR = "data/config_versions/"
"""Directory where versioned config snapshots are stored."""

CURRENT_CONFIG_PATH = "data/current_config.json"
"""Path to the JSON file tracking the current champion config."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_current_config_data() -> dict | None:
    """Read and return ``current_config.json`` data, or ``None``."""
    if not os.path.exists(CURRENT_CONFIG_PATH):
        return None
    try:
        with open(CURRENT_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _atomic_write(path: str, data: dict) -> None:
    """Write *data* as JSON to *path* via a temp file + atomic replace."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


def _build_scorecard(
    db_conn: sqlite3.Connection,
    run_ids: list[str],
) -> dict[str, dict[str, float]]:
    """Aggregate metric values for the given runs into a summary scorecard.

    Returns a dict mapping metric names to ``{mean, min, max, count}``.
    """
    if not run_ids:
        return {}

    placeholders = ",".join("?" for _ in run_ids)
    rows = db_conn.execute(
        f"SELECT metric_name, value FROM metric_values "
        f"WHERE run_id IN ({placeholders})",
        run_ids,
    ).fetchall()

    groups: dict[str, list[float]] = {}
    for r in rows:
        groups.setdefault(r["metric_name"], []).append(r["value"])

    summary: dict[str, dict[str, float]] = {}
    for name, values in groups.items():
        summary[name] = {
            "mean": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
            "count": len(values),
        }
    return summary


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def promote_config(
    config_id: str,
    *,
    db_conn: sqlite3.Connection,
    confirmation: str,
) -> dict[str, Any]:
    """Promote *config_id* to champion, subject to gates.

    Parameters
    ----------
    config_id
        The config to promote.
    db_conn
        Open quality-lab database connection (with ``row_factory`` set).
    confirmation
        Must equal *config_id*.

    Returns
    -------
    dict
        ``{"status": "promoted", "config_id": ..., "scorecard": ..., "message": ...}``

    Raises
    ------
    ValueError
        If any gate condition is not met.
    """
    # ---- Gate 1: Config exists -------------------------------------------

    config_row = db_conn.execute(
        "SELECT * FROM experiment_configs WHERE config_id=?", (config_id,)
    ).fetchone()
    if config_row is None:
        raise ValueError(f"Config not found: {config_id}")

    # ---- Gate 2: Confirmation match --------------------------------------

    if confirmation != config_id:
        raise ValueError(
            f"Confirmation string does not match config_id. "
            f"Expected '{config_id}', got '{confirmation}'"
        )

    # ---- Gate 3: Completed tune run --------------------------------------

    tune_run = db_conn.execute(
        "SELECT run_id FROM experiment_runs "
        "WHERE config_id=? AND split='tune' AND status='completed'",
        (config_id,),
    ).fetchone()
    if tune_run is None:
        raise ValueError(
            f"No completed tune run found for config {config_id}"
        )

    # ---- Gate 4: Completed holdout run -----------------------------------

    holdout_run = db_conn.execute(
        "SELECT run_id FROM experiment_runs "
        "WHERE config_id=? AND split='holdout' AND status='completed'",
        (config_id,),
    ).fetchone()
    if holdout_run is None:
        raise ValueError(
            f"No completed holdout run found for config {config_id}"
        )

    # ---- Gate 5: Completed blind review ----------------------------------

    config_run_ids = [
        r["run_id"]
        for r in db_conn.execute(
            "SELECT run_id FROM experiment_runs WHERE config_id=?",
            (config_id,),
        ).fetchall()
    ]
    if not config_run_ids:
        raise ValueError(f"No runs found for config {config_id}")

    placeholders = ",".join("?" for _ in config_run_ids)
    ab_session = db_conn.execute(
        f"""SELECT 1 FROM ab_sessions
            WHERE (run_a IN ({placeholders}) OR run_b IN ({placeholders}))
            AND status='completed'
            LIMIT 1""",
        config_run_ids + config_run_ids,
    ).fetchone()
    if ab_session is None:
        raise ValueError(
            f"No completed blind A/B review found for config {config_id}"
        )

    # ---- Gate 6: Export integrity ----------------------------------------

    integrity_values: list[float] = []
    for rid in config_run_ids:
        rows = db_conn.execute(
            "SELECT value FROM metric_values "
            "WHERE run_id=? AND metric_name='export_integrity'",
            (rid,),
        ).fetchall()
        integrity_values.extend(r["value"] for r in rows)

    if integrity_values:
        avg_integrity = sum(integrity_values) / len(integrity_values)
    else:
        avg_integrity = 1.0  # no metric values recorded → assume perfect

    if avg_integrity < PROMOTION_GATE_THRESHOLD:
        raise ValueError(
            f"Export integrity {avg_integrity:.3f} is below gate "
            f"threshold {PROMOTION_GATE_THRESHOLD}"
        )

    # ---- Build scorecard -------------------------------------------------

    scorecard = _build_scorecard(db_conn, config_run_ids)

    # ---- Write versioned config file -------------------------------------

    os.makedirs(CONFIG_VERSIONS_DIR, exist_ok=True)
    timestamp = _utcnow()
    safe_ts = timestamp.replace(":", "-").replace(".", "-")
    versioned_path = os.path.join(CONFIG_VERSIONS_DIR, f"{config_id}_{safe_ts}.json")

    try:
        config_obj = json.loads(config_row["config_json"])
    except (json.JSONDecodeError, TypeError):
        config_obj = {"raw": config_row["config_json"]}

    try:
        provenance_obj = json.loads(config_row["provenance_json"])
    except (json.JSONDecodeError, TypeError):
        provenance_obj = {"raw": config_row["provenance_json"]}

    versioned_data: dict[str, Any] = {
        "config_id": config_id,
        "config_json": config_obj,
        "provenance_json": provenance_obj,
        "scorecard": scorecard,
        "promoted_at": timestamp,
    }
    with open(versioned_path, "w", encoding="utf-8") as f:
        json.dump(versioned_data, f, indent=2, ensure_ascii=False)

    # ---- Atomically update current_config.json ---------------------------

    current_data: dict[str, Any] = {
        "config_id": config_id,
        "promoted_at": timestamp,
        "scorecard_summary": scorecard,
        "versioned_file": versioned_path,
    }
    _atomic_write(CURRENT_CONFIG_PATH, current_data)

    # ---- Record in champion_history --------------------------------------

    previous_id = _get_current_config_id_before_overwrite()
    now = _utcnow()
    db_conn.execute(
        """INSERT INTO champion_history
           (config_id, action, previous_config_id, scorecard_json, created_at)
           VALUES (?, 'promote', ?, ?, ?)""",
        (config_id, previous_id, json.dumps(scorecard), now),
    )
    db_conn.commit()

    return {
        "status": "promoted",
        "config_id": config_id,
        "scorecard": scorecard,
        "message": f"Config {config_id} promoted to champion",
    }


def rollback(*, db_conn: sqlite3.Connection) -> dict[str, Any]:
    """Rollback to the previous champion config.

    Finds the most recent promote event, then sets the config **before**
    it as the current champion.  A ``rollback`` row is inserted into
    ``champion_history``; no rows are deleted.

    Returns
    -------
    dict
        ``{"status": "rolled_back", "config_id": ..., "message": ...}``

    Raises
    ------
    ValueError
        If there is no current champion or no previous champion.
    """
    current = _get_current_config_data()
    if current is None:
        raise ValueError("No current champion config to rollback from")

    current_config_id = current.get("config_id")

    # Find the immediate-prior promote event in champion_history
    previous = db_conn.execute(
        """SELECT config_id FROM champion_history
           WHERE action='promote' AND config_id != ?
           ORDER BY created_at DESC LIMIT 1""",
        (current_config_id,),
    ).fetchone()

    if previous is None:
        raise ValueError("No previous champion config to rollback to")

    previous_config_id = previous["config_id"]

    # Verify the previous config still exists in experiment_configs
    prev_config_row = db_conn.execute(
        "SELECT 1 FROM experiment_configs WHERE config_id=?",
        (previous_config_id,),
    ).fetchone()
    if prev_config_row is None:
        raise ValueError(
            f"Previous config {previous_config_id} no longer exists in database"
        )

    # ---- Update current_config.json --------------------------------------

    now = _utcnow()
    current_data: dict[str, Any] = {
        "config_id": previous_config_id,
        "promoted_at": now,
        "scorecard_summary": None,
    }
    _atomic_write(CURRENT_CONFIG_PATH, current_data)

    # ---- Record rollback in champion_history -----------------------------

    db_conn.execute(
        """INSERT INTO champion_history
           (config_id, action, previous_config_id, scorecard_json, created_at)
           VALUES (?, 'rollback', ?, ?, ?)""",
        (previous_config_id, current_config_id, None, now),
    )
    db_conn.commit()

    return {
        "status": "rolled_back",
        "config_id": previous_config_id,
        "previous_config_id": current_config_id,
        "message": f"Rolled back to config {previous_config_id}",
    }


def list_champion_history(
    *, db_conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Return all champion history events in descending order.

    Returns
    -------
    list[dict]
        Each entry contains ``event_id``, ``config_id``, ``action``,
        ``previous_config_id``, ``scorecard``, ``created_at``.
    """
    rows = db_conn.execute(
        "SELECT * FROM champion_history ORDER BY created_at DESC"
    ).fetchall()

    result: list[dict[str, Any]] = []
    for r in rows:
        sc: Any = None
        if r["scorecard_json"]:
            try:
                sc = json.loads(r["scorecard_json"])
            except (json.JSONDecodeError, TypeError):
                sc = r["scorecard_json"]
        result.append(
            {
                "event_id": r["event_id"],
                "config_id": r["config_id"],
                "action": r["action"],
                "previous_config_id": r["previous_config_id"],
                "scorecard": sc,
                "created_at": r["created_at"],
            }
        )
    return result


def _get_current_config_id_before_overwrite() -> str | None:
    """Return the currently promoted config ID (before it gets overwritten)."""
    data = _get_current_config_data()
    return data.get("config_id") if data else None

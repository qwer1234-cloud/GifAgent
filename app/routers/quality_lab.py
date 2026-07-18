"""Quality Lab API router — experiment runs, scorecards, AB sessions, champions.

Endpoints
---------
- ``GET    /api/quality/runs``                         — list all runs
- ``GET    /api/quality/runs/{run_id}``                 — get a single run
- ``GET    /api/quality/runs/{run_id}/scorecard``       — run scorecard
- ``POST   /api/quality/ab-sessions``                   — create blind A/B session
- ``POST   /api/quality/ab-sessions/{session_id}/judgments`` — record judgment
- ``POST   /api/quality/champions/{config_id}/promote`` — promote config
- ``POST   /api/quality/champions/rollback``            — rollback champion
- ``GET    /api/quality/champions/history``             — champion history
- ``GET    /api/quality/champions/current``             — current champion
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.quality_lab import (
    BlindReviewService,
    connect_quality_db,
)
from app.quality_lab.models import Choice
from app.quality_lab.promotion import (
    list_champion_history as _list_champion_history,
    promote_config as _promote_config,
    rollback as _rollback,
)

router = APIRouter(prefix="/api/quality", tags=["quality"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class PromoteRequest(BaseModel):
    confirmation: str


class PromoteResponse(BaseModel):
    status: str
    config_id: str
    scorecard: dict | None = None
    message: str


class RollbackResponse(BaseModel):
    status: str
    config_id: str
    previous_config_id: str | None = None
    message: str


class RunResponse(BaseModel):
    run_id: str
    manifest_id: str
    config_id: str
    split: str
    status: str
    created_at: str
    updated_at: str


class ScorecardResponse(BaseModel):
    run_id: str
    scorecard: dict[str, Any]


class ABSessionRequest(BaseModel):
    run_a: str
    run_b: str
    seed: int


class ABSessionResponse(BaseModel):
    session_id: str
    run_a: str
    run_b: str
    status: str


class JudgmentRequest(BaseModel):
    pair_index: str
    choice: Choice


class ChampionHistoryItem(BaseModel):
    event_id: int
    config_id: str
    action: str
    previous_config_id: str | None = None
    scorecard: dict | None = None
    created_at: str


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------


def get_quality_db():
    """Yield a short-lived quality-lab database connection."""
    conn = connect_quality_db()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Experiment run endpoints
# ---------------------------------------------------------------------------


@router.get("/runs", response_model=list[RunResponse])
def list_runs(db: sqlite3.Connection = Depends(get_quality_db)):
    """List all experiment runs."""
    rows = db.execute(
        "SELECT * FROM experiment_runs ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: str, db: sqlite3.Connection = Depends(get_quality_db)):
    """Get a single experiment run."""
    row = db.execute(
        "SELECT * FROM experiment_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return dict(row)


@router.get("/runs/{run_id}/scorecard", response_model=ScorecardResponse)
def get_run_scorecard(
    run_id: str, db: sqlite3.Connection = Depends(get_quality_db),
):
    """Return aggregated metric scorecard for a run."""
    row = db.execute(
        "SELECT 1 FROM experiment_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")

    metric_rows = db.execute(
        "SELECT metric_name, value FROM metric_values WHERE run_id=?",
        (run_id,),
    ).fetchall()

    groups: dict[str, list[float]] = {}
    for r in metric_rows:
        groups.setdefault(r["metric_name"], []).append(r["value"])

    scorecard: dict[str, dict[str, float]] = {}
    for name, values in groups.items():
        scorecard[name] = {
            "mean": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
            "count": len(values),
        }

    return ScorecardResponse(run_id=run_id, scorecard=scorecard)


# ---------------------------------------------------------------------------
# Blind A/B session endpoints
# ---------------------------------------------------------------------------


@router.post("/ab-sessions", response_model=ABSessionResponse, status_code=201)
def create_ab_session(
    body: ABSessionRequest,
    db: sqlite3.Connection = Depends(get_quality_db),
):
    """Create a blind A/B review session between two runs."""
    service = BlindReviewService(db)
    try:
        session = service.create_session(
            run_a=body.run_a, run_b=body.run_b, seed=body.seed
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return ABSessionResponse(
        session_id=session.session_id,
        run_a=session.run_a,
        run_b=session.run_b,
        status=session.status,
    )


@router.post("/ab-sessions/{session_id}/judgments")
def record_ab_judgment(
    session_id: str,
    body: JudgmentRequest,
    db: sqlite3.Connection = Depends(get_quality_db),
):
    """Record a judgment for a blind A/B pair."""
    service = BlindReviewService(db)
    try:
        service.record(session_id, body.pair_index, body.choice)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"status": "recorded"}


# ---------------------------------------------------------------------------
# Champion endpoints
# ---------------------------------------------------------------------------


@router.post("/champions/{config_id}/promote", response_model=PromoteResponse)
def promote_config(
    config_id: str,
    body: PromoteRequest,
    db: sqlite3.Connection = Depends(get_quality_db),
):
    """Promote a config to champion (subject to gates)."""
    try:
        result = _promote_config(
            config_id, db_conn=db, confirmation=body.confirmation,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return result


@router.post("/champions/rollback", response_model=RollbackResponse)
def rollback_champion(db: sqlite3.Connection = Depends(get_quality_db)):
    """Rollback to the previous champion config."""
    try:
        result = _rollback(db_conn=db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return result


@router.get("/champions/history", response_model=list[ChampionHistoryItem])
def get_champion_history(db: sqlite3.Connection = Depends(get_quality_db)):
    """Return champion history events."""
    return _list_champion_history(db_conn=db)


@router.get("/champions/current")
def get_current_champion():
    """Return the current champion config data."""
    from app.quality_lab.promotion import _get_current_config_data
    data = _get_current_config_data()
    if data is None:
        raise HTTPException(status_code=404, detail="No current champion")
    return data

"""API tests for quality lab endpoints (Phase 2 Task 6).

Uses ``TestClient`` and patching to avoid file-system side effects.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


# ===================================================================
# Fixtures
# ===================================================================


@pytest.fixture
def client():
    """Yield a ``TestClient`` for the FastAPI app."""
    with TestClient(app) as c:
        yield c


# ===================================================================
# Champion promote / rollback endpoint tests
# ===================================================================


class TestChampionPromoteAPI:
    """``POST /api/quality/champions/{config_id}/promote``."""

    @patch("app.routers.quality_lab._promote_config")
    def test_promote_success(
        self, mock_promote, client: TestClient,
    ):
        """Returns 200 with promote result when all gates pass."""
        mock_promote.return_value = {
            "status": "promoted",
            "config_id": "cfg_test",
            "scorecard": {"export_integrity": {"mean": 0.95, "count": 2}},
            "message": "Config cfg_test promoted to champion",
        }

        resp = client.post(
            "/api/quality/champions/cfg_test/promote",
            json={"confirmation": "cfg_test"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "promoted"
        assert data["config_id"] == "cfg_test"

    @patch("app.routers.quality_lab._promote_config")
    def test_promote_rejects_wrong_confirmation(
        self, mock_promote, client: TestClient,
    ):
        """Returns 400 when confirmation does not match."""
        mock_promote.side_effect = ValueError("Confirmation string does not match")

        resp = client.post(
            "/api/quality/champions/cfg_test/promote",
            json={"confirmation": "wrong"},
        )
        assert resp.status_code == 400

    @patch("app.routers.quality_lab._promote_config")
    def test_promote_rejects_missing_tune_run(
        self, mock_promote, client: TestClient,
    ):
        """Returns 400 when no completed tune run exists."""
        mock_promote.side_effect = ValueError("No completed tune run found")

        resp = client.post(
            "/api/quality/champions/cfg_test/promote",
            json={"confirmation": "cfg_test"},
        )
        assert resp.status_code == 400
        assert "tune" in resp.text.lower()

    @patch("app.routers.quality_lab._promote_config")
    def test_promote_rejects_low_integrity(
        self, mock_promote, client: TestClient,
    ):
        """Returns 400 when export integrity is below threshold."""
        mock_promote.side_effect = ValueError("below gate threshold")

        resp = client.post(
            "/api/quality/champions/cfg_test/promote",
            json={"confirmation": "cfg_test"},
        )
        assert resp.status_code == 400


class TestChampionRollbackAPI:
    """``POST /api/quality/champions/rollback``."""

    @patch("app.routers.quality_lab._rollback")
    def test_rollback_success(self, mock_rollback, client: TestClient):
        """Returns 200 with rollback result."""
        mock_rollback.return_value = {
            "status": "rolled_back",
            "config_id": "cfg_prev",
            "previous_config_id": "cfg_cur",
            "message": "Rolled back to config cfg_prev",
        }

        resp = client.post("/api/quality/champions/rollback")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "rolled_back"
        assert data["config_id"] == "cfg_prev"

    @patch("app.routers.quality_lab._rollback")
    def test_rollback_no_previous(self, mock_rollback, client: TestClient):
        """Returns 400 when there is no previous champion."""
        mock_rollback.side_effect = ValueError("No previous champion")

        resp = client.post("/api/quality/champions/rollback")
        assert resp.status_code == 400


class TestChampionHistoryAPI:
    """``GET /api/quality/champions/history``."""

    @patch("app.routers.quality_lab._list_champion_history")
    def test_history_empty(self, mock_history, client: TestClient):
        """Returns empty list when no history exists."""
        mock_history.return_value = []

        resp = client.get("/api/quality/champions/history")
        assert resp.status_code == 200
        assert resp.json() == []

    @patch("app.routers.quality_lab._list_champion_history")
    def test_history_with_events(self, mock_history, client: TestClient):
        """Returns list of history events."""
        mock_history.return_value = [
            {
                "event_id": 1,
                "config_id": "cfg_test",
                "action": "promote",
                "previous_config_id": None,
                "scorecard": {},
                "created_at": "2026-07-18T00:00:00",
            },
        ]

        resp = client.get("/api/quality/champions/history")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["action"] == "promote"


class TestCurrentChampionAPI:
    """``GET /api/quality/champions/current``."""

    @patch("app.quality_lab.promotion._get_current_config_data")
    def test_current_exists(self, mock_current, client: TestClient):
        """Returns current champion data."""
        mock_current.return_value = {
            "config_id": "cfg_test",
            "promoted_at": "2026-07-18T00:00:00",
        }

        resp = client.get("/api/quality/champions/current")
        assert resp.status_code == 200
        assert resp.json()["config_id"] == "cfg_test"

    @patch("app.quality_lab.promotion._get_current_config_data")
    def test_current_not_found(self, mock_current, client: TestClient):
        """Returns 404 when no current champion exists."""
        mock_current.return_value = None

        resp = client.get("/api/quality/champions/current")
        assert resp.status_code == 404


# ===================================================================
# Experiment run endpoint tests
# ===================================================================


class TestRunsAPI:
    """Experiment run endpoints."""

    @patch("app.routers.quality_lab.connect_quality_db")
    def test_list_runs_empty(self, mock_connect, client: TestClient):
        """``GET /api/quality/runs`` returns empty list."""
        import sqlite3

        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        from app.quality_lab import apply_quality_schema
        apply_quality_schema(conn)
        mock_connect.return_value = conn

        resp = client.get("/api/quality/runs")
        assert resp.status_code == 200
        assert resp.json() == []

    @patch("app.routers.quality_lab.connect_quality_db")
    def test_get_run_not_found(self, mock_connect, client: TestClient):
        """``GET /api/quality/runs/{run_id}`` returns 404."""
        import sqlite3

        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        from app.quality_lab import apply_quality_schema
        apply_quality_schema(conn)
        mock_connect.return_value = conn

        resp = client.get("/api/quality/runs/nonexistent")
        assert resp.status_code == 404

    @patch("app.routers.quality_lab.connect_quality_db")
    def test_scorecard_not_found(self, mock_connect, client: TestClient):
        """``GET /api/quality/runs/{run_id}/scorecard`` returns 404."""
        import sqlite3

        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        from app.quality_lab import apply_quality_schema
        apply_quality_schema(conn)
        mock_connect.return_value = conn

        resp = client.get("/api/quality/runs/nonexistent/scorecard")
        assert resp.status_code == 404

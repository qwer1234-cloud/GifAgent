from __future__ import annotations

import json


def test_profile_backfill_fails_before_database_work_when_embedding_service_is_down(monkeypatch):
    from app.services.embedding import EmbeddingServiceUnavailable
    from app.ui.tabs import profile

    def fail_check():
        raise EmbeddingServiceUnavailable("embedding service unavailable")

    monkeypatch.setattr(profile, "check_embedding_service", fail_check)

    result = json.loads(profile.backfill_profile_vectors())

    assert result["status"] == "paused"
    assert "unavailable" in result["error"]


def test_backfill_status_is_json_and_reports_idle(monkeypatch):
    from app.ui.tabs import profile

    monkeypatch.setattr(
        profile,
        "_BACKFILL_STATE",
        {"status": "idle", "processed": 0, "total": 0},
    )

    result = json.loads(profile.get_backfill_status())

    assert result["status"] == "idle"
    assert result["processed"] == 0

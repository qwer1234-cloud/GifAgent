import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _profile_conn() -> sqlite3.Connection:
    from app.services.preference_schema import apply_preference_schema

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    return conn


def test_build_profile_accepts_empty_body_with_default_dry_run(monkeypatch):
    from app.routers import preference as preference_router

    conn = _profile_conn()
    monkeypatch.setattr(preference_router, "get_connection", lambda: conn)

    app = FastAPI()
    app.include_router(preference_router.router)
    client = TestClient(app)

    response = client.post("/api/preference/profiles/build")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["effective_feedback_count"] == 0


def test_publish_profile_endpoint_sets_current_profile(monkeypatch, tmp_path):
    from app.routers import preference as preference_router
    from app.services.preference_schema import apply_preference_schema

    db_path = tmp_path / "library.db"

    def connect():
        db_conn = sqlite3.connect(str(db_path), check_same_thread=False)
        db_conn.row_factory = sqlite3.Row
        return db_conn

    conn = connect()
    apply_preference_schema(conn)
    conn.execute(
        """INSERT INTO preference_profile_builds
           (profile_version, event_watermark, embedding_model, embedding_dim,
            effective_feedback_count, source_video_count, config_json, status,
            completed_at)
           VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
        (
            "profile-test",
            "2026-07-09T00:00:00+00:00",
            "nomic-embed-text:latest",
            768,
            40,
            4,
            "{}",
            "completed",
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(preference_router, "get_connection", connect)

    app = FastAPI()
    app.include_router(preference_router.router)
    client = TestClient(app)

    response = client.post("/api/preference/profiles/profile-test/publish")

    assert response.status_code == 200
    assert response.json() == {"status": "published", "profile_version": "profile-test"}
    check_conn = connect()
    current = check_conn.execute(
        "SELECT profile_version FROM preference_profile_current WHERE slot='current'"
    ).fetchone()
    assert current["profile_version"] == "profile-test"
    check_conn.close()

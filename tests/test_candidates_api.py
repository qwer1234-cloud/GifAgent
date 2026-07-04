import sqlite3


def _setup_conn() -> sqlite3.Connection:
    from app.services.preference_schema import apply_preference_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    return conn


def _insert_candidate(
    conn: sqlite3.Connection,
    candidate_id: str,
    *,
    status: str = "candidate",
    artifact_path: str = "data/exports/full.gif",
    preview_path: str | None = "data/thumbs/preview.jpg",
    created_at: str = "2026-07-04T00:00:00+00:00",
) -> None:
    conn.execute(
        """INSERT INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path, start_sec, end_sec,
            artifact_path, preview_path, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            candidate_id,
            "run-1",
            f"rc-{candidate_id}",
            "video-sha",
            "/videos/sample.mp4",
            0.0,
            5.0,
            artifact_path,
            preview_path,
            status,
            created_at,
            created_at,
        ),
    )
    conn.commit()


def test_list_candidates_is_paginated_and_filtered(monkeypatch):
    from app.routers import candidates as candidates_router

    conn = _setup_conn()
    _insert_candidate(conn, "cand-old", created_at="2026-07-04T00:00:00+00:00")
    _insert_candidate(conn, "cand-new", created_at="2026-07-04T00:01:00+00:00")
    _insert_candidate(conn, "cand-liked", status="liked", created_at="2026-07-04T00:02:00+00:00")
    monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

    payload = candidates_router.list_candidates(status="candidate", limit=1, offset=0)

    assert payload["total"] == 2
    assert payload["limit"] == 1
    assert payload["offset"] == 0
    assert payload["has_more"] is True
    assert payload["status_counts"] == {"candidate": 2, "liked": 1}
    assert [c["candidate_id"] for c in payload["candidates"]] == ["cand-new"]


def test_list_candidates_allows_all_statuses_and_prefers_preview_path(monkeypatch):
    from app.routers import candidates as candidates_router

    conn = _setup_conn()
    _insert_candidate(
        conn,
        "cand-preview",
        artifact_path="data/exports/full.gif",
        preview_path="data/thumbs/preview.jpg",
    )
    _insert_candidate(
        conn,
        "cand-artifact",
        artifact_path="data/exports/fallback.gif",
        preview_path=None,
    )
    monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

    payload = candidates_router.list_candidates(status="all", limit=10, offset=0)
    by_id = {c["candidate_id"]: c for c in payload["candidates"]}

    assert payload["total"] == 2
    assert by_id["cand-preview"]["display_path"] == "data/thumbs/preview.jpg"
    assert by_id["cand-artifact"]["display_path"] == "data/exports/fallback.gif"

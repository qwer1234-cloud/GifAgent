import sqlite3

import pytest
from fastapi import HTTPException


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


def test_favorite_candidate_records_path_and_hides_it_from_unrated_list(monkeypatch, tmp_path):
    from app.routers import candidates as candidates_router

    conn = _setup_conn()
    gif_path = tmp_path / "favorite.gif"
    gif_path.write_bytes(b"gif")
    _insert_candidate(conn, "cand-favorite", artifact_path=str(gif_path), preview_path=str(gif_path))
    monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

    response = candidates_router.favorite_candidate(
        "cand-favorite", candidates_router.FavoriteRequest(expected_artifact_path=str(gif_path))
    )
    payload = candidates_router.list_candidates(status="candidate", limit=10, offset=0)

    assert response.status == "favorited"
    assert response.full_path == str(gif_path)
    assert payload["total"] == 0


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


def test_candidate_folders_are_discovered_recursively(monkeypatch, tmp_path):
    from app.routers import candidates as candidates_router

    root = tmp_path / "adaptive_test"
    jur = root / "JUR-639"
    nested = root / "A" / "B"
    jur.mkdir(parents=True)
    nested.mkdir(parents=True)
    jur_gif = jur / "one.gif"
    nested_gif = nested / "two.gif"
    jur_gif.write_bytes(b"gif")
    nested_gif.write_bytes(b"gif")

    conn = _setup_conn()
    _insert_candidate(conn, "cand-jur", artifact_path=str(jur_gif), preview_path=str(jur_gif))
    _insert_candidate(conn, "cand-nested", artifact_path=str(nested_gif), preview_path=str(nested_gif))
    monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

    payload = candidates_router.list_candidate_folders(root=str(root), status="all")
    rels = {folder["relative_folder"]: folder["count"] for folder in payload["folders"]}

    assert rels == {"JUR-639": 1, "A/B": 1}


def test_candidate_folders_include_unmaterialized_gif_folders(monkeypatch, tmp_path):
    from app.routers import candidates as candidates_router

    root = tmp_path / "adaptive_test"
    nested = root / "LapkaLu" / "SceneA"
    nested.mkdir(parents=True)
    (nested / "SceneA@@@001_10s-15s.gif").write_bytes(b"gif")

    conn = _setup_conn()
    monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

    payload = candidates_router.list_candidate_folders(root=str(root), status="all")
    folders = {folder["relative_folder"]: folder for folder in payload["folders"]}

    assert folders["LapkaLu/SceneA"]["count"] == 1
    assert folders["LapkaLu/SceneA"]["unmaterialized_count"] == 1
    assert folders["LapkaLu/SceneA"]["status_counts"] == {"candidate": 1}


def test_list_candidates_filters_to_exact_selected_folder(monkeypatch, tmp_path):
    from app.routers import candidates as candidates_router

    root = tmp_path / "adaptive_test"
    jur = root / "JUR-639"
    child = jur / "child"
    jur.mkdir(parents=True)
    child.mkdir()
    jur_gif = jur / "one.gif"
    child_gif = child / "nested.gif"
    jur_gif.write_bytes(b"gif")
    child_gif.write_bytes(b"gif")

    conn = _setup_conn()
    _insert_candidate(conn, "cand-jur", artifact_path=str(jur_gif), preview_path=str(jur_gif))
    _insert_candidate(conn, "cand-child", artifact_path=str(child_gif), preview_path=str(child_gif))
    monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

    payload = candidates_router.list_candidates(
        status="all",
        limit=10,
        offset=0,
        folder=str(jur),
    )

    assert payload["total"] == 1
    assert payload["candidates"][0]["candidate_id"] == "cand-jur"


def test_list_candidates_materializes_untracked_gifs_for_selected_folder(monkeypatch, tmp_path):
    from app.routers import candidates as candidates_router

    folder = tmp_path / "LapkaLu" / "SceneA"
    folder.mkdir(parents=True)
    gif_path = folder / "SceneA@@@001_10s-15s.gif"
    gif_path.write_bytes(b"gif")
    child = folder / "child"
    child.mkdir()
    (child / "nested@@@001_20s-25s.gif").write_bytes(b"gif")

    conn = _setup_conn()
    monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

    payload = candidates_router.list_candidates(
        status="all",
        limit=10,
        offset=0,
        folder=str(folder),
    )

    assert payload["total"] == 1
    candidate = payload["candidates"][0]
    assert candidate["status"] == "candidate"
    assert candidate["start_sec"] == 10.0
    assert candidate["end_sec"] == 15.0
    assert candidates_router._resolve_artifact_path(candidate["artifact_path"]) == gif_path
    assert conn.execute("SELECT COUNT(*) FROM candidate_gifs").fetchone()[0] == 1

    candidates_router.list_candidates(status="all", limit=10, offset=0, folder=str(folder))
    assert conn.execute("SELECT COUNT(*) FROM candidate_gifs").fetchone()[0] == 1


def test_list_candidates_errors_when_selected_folder_file_is_missing(monkeypatch, tmp_path):
    from app.routers import candidates as candidates_router

    folder = tmp_path / "JUR-639"
    folder.mkdir()
    missing_gif = folder / "missing.gif"

    conn = _setup_conn()
    _insert_candidate(conn, "cand-missing", artifact_path=str(missing_gif), preview_path=str(missing_gif))
    monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

    with pytest.raises(HTTPException) as exc:
        candidates_router.list_candidates(
            status="all",
            limit=10,
            offset=0,
            folder=str(folder),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "candidate_path_changed_or_missing"


def test_feedback_errors_when_candidate_path_changed_after_load(monkeypatch, tmp_path):
    from app.routers import candidates as candidates_router

    folder = tmp_path / "JUR-639"
    folder.mkdir()
    gif_path = folder / "one.gif"
    other_path = folder / "other.gif"
    gif_path.write_bytes(b"gif")
    other_path.write_bytes(b"gif")

    conn = _setup_conn()
    _insert_candidate(conn, "cand-one", artifact_path=str(gif_path), preview_path=str(gif_path))
    monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

    with pytest.raises(HTTPException) as exc:
        candidates_router.submit_feedback(
            "cand-one",
            candidates_router.FeedbackRequest(
                rating="like",
                expected_artifact_path=str(other_path),
            ),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "candidate_path_changed"

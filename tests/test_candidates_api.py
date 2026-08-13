import os
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
    event = conn.execute(
        "SELECT rating FROM preference_events WHERE target_id=? ORDER BY created_at DESC LIMIT 1",
        ("cand-favorite",),
    ).fetchone()
    candidate_status = conn.execute(
        "SELECT status FROM candidate_gifs WHERE candidate_id=?", ("cand-favorite",)
    ).fetchone()[0]
    assert event["rating"] == "like"
    assert candidate_status == "candidate"


def test_undo_last_action_restores_candidate_and_removes_favorite(monkeypatch, tmp_path):
    from app.routers import candidates as candidates_router

    conn = _setup_conn()
    gif_path = tmp_path / "undo-favorite.gif"
    gif_path.write_bytes(b"gif")
    _insert_candidate(conn, "cand-undo-favorite", artifact_path=str(gif_path), preview_path=str(gif_path))
    monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)
    candidates_router.favorite_candidate(
        "cand-undo-favorite",
        candidates_router.FavoriteRequest(expected_artifact_path=str(gif_path)),
    )

    response = candidates_router.undo_last_action()

    assert response["status"] == "undone"
    assert conn.execute("SELECT COUNT(*) FROM favorite_gifs").fetchone()[0] == 0
    assert conn.execute(
        "SELECT status FROM candidate_gifs WHERE candidate_id='cand-undo-favorite'"
    ).fetchone()[0] == "candidate"


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


def test_candidate_rows_can_narrow_database_scan_to_selected_folder(tmp_path):
    from app.routers import candidates as candidates_router

    root = tmp_path / "adaptive_test"
    jur = root / "JUR-639"
    other = root / "Other"
    jur.mkdir(parents=True)
    other.mkdir()
    jur_gif = jur / "one.gif"
    other_gif = other / "two.gif"
    jur_gif.write_bytes(b"gif")
    other_gif.write_bytes(b"gif")

    conn = _setup_conn()
    _insert_candidate(conn, "cand-jur", artifact_path=str(jur_gif), preview_path=str(jur_gif))
    _insert_candidate(conn, "cand-other", artifact_path=str(other_gif), preview_path=str(other_gif))

    rows = candidates_router._candidate_rows(conn, status="all", folder=jur)

    assert [row["candidate_id"] for row in rows] == ["cand-jur"]


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


def test_list_candidates_folder_orders_by_file_mtime_descending(monkeypatch, tmp_path):
    from app.routers import candidates as candidates_router

    folder = tmp_path / "JUR-639"
    folder.mkdir()
    older_gif = folder / "older.gif"
    newer_gif = folder / "newer.gif"
    older_gif.write_bytes(b"gif")
    newer_gif.write_bytes(b"gif")
    base_ns = 1_700_000_000_000_000_000
    os.utime(older_gif, ns=(base_ns, base_ns))
    os.utime(newer_gif, ns=(base_ns, base_ns + 5_000_000_000))

    conn = _setup_conn()
    # DB created_at order disagrees with the file mtime order.
    _insert_candidate(
        conn,
        "cand-older-file",
        artifact_path=str(older_gif),
        created_at="2026-07-05T00:00:00+00:00",
    )
    _insert_candidate(
        conn,
        "cand-newer-file",
        artifact_path=str(newer_gif),
        created_at="2026-07-04T00:00:00+00:00",
    )
    monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

    payload = candidates_router.list_candidates(
        status="all", limit=10, offset=0, folder=str(folder)
    )

    assert payload["total"] == 2
    assert [c["candidate_id"] for c in payload["candidates"]] == [
        "cand-newer-file",
        "cand-older-file",
    ]


def test_list_candidates_folder_sorts_before_pagination_with_deterministic_ties(
    monkeypatch, tmp_path
):
    from app.routers import candidates as candidates_router

    folder = tmp_path / "JUR-639"
    folder.mkdir()
    newer_gif = folder / "Newer.gif"
    gif_a = folder / "a.gif"
    gif_b = folder / "B.gif"
    for path in (newer_gif, gif_a, gif_b):
        path.write_bytes(b"gif")
    base_ns = 1_700_000_000_000_000_000
    os.utime(newer_gif, ns=(base_ns, base_ns + 10_000_000_000))
    os.utime(gif_a, ns=(base_ns, base_ns))
    os.utime(gif_b, ns=(base_ns, base_ns))

    conn = _setup_conn()
    _insert_candidate(
        conn,
        "cand-newer",
        artifact_path=str(newer_gif),
        created_at="2026-07-03T00:00:00+00:00",
    )
    _insert_candidate(
        conn,
        "cand-b",
        artifact_path=str(gif_b),
        created_at="2026-07-05T00:00:00+00:00",
    )
    _insert_candidate(
        conn,
        "cand-a",
        artifact_path=str(gif_a),
        created_at="2026-07-04T00:00:00+00:00",
    )
    monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

    page0 = candidates_router.list_candidates(
        status="all", limit=2, offset=0, folder=str(folder)
    )
    page1 = candidates_router.list_candidates(
        status="all", limit=2, offset=2, folder=str(folder)
    )

    assert page0["total"] == 3
    assert page1["total"] == 3
    assert [c["candidate_id"] for c in page0["candidates"]] == ["cand-newer", "cand-a"]
    assert [c["candidate_id"] for c in page1["candidates"]] == ["cand-b"]


def test_list_candidates_folder_tie_breaks_equal_mtimes_by_candidate_id(
    monkeypatch, tmp_path
):
    from app.routers import candidates as candidates_router

    folder = tmp_path / "JUR-639"
    folder.mkdir()
    gif_path = folder / "same.gif"
    gif_path.write_bytes(b"gif")
    base_ns = 1_700_000_000_000_000_000
    os.utime(gif_path, ns=(base_ns, base_ns))

    conn = _setup_conn()
    _insert_candidate(
        conn,
        "cand-z",
        artifact_path=str(gif_path),
        created_at="2026-07-04T00:00:00+00:00",
    )
    _insert_candidate(
        conn,
        "cand-a",
        artifact_path=str(gif_path),
        created_at="2026-07-05T00:00:00+00:00",
    )
    monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

    payload = candidates_router.list_candidates(
        status="all", limit=10, offset=0, folder=str(folder)
    )

    assert [c["candidate_id"] for c in payload["candidates"]] == ["cand-a", "cand-z"]


def test_list_candidates_folder_materialized_gifs_use_actual_mtimes(
    monkeypatch, tmp_path
):
    from app.routers import candidates as candidates_router

    folder = tmp_path / "LapkaLu" / "SceneA"
    folder.mkdir(parents=True)
    tracked_gif = folder / "tracked.gif"
    untracked_gif = folder / "SceneA@@@001_10s-15s.gif"
    tracked_gif.write_bytes(b"gif")
    untracked_gif.write_bytes(b"gif")
    base_ns = 1_700_000_000_000_000_000
    os.utime(tracked_gif, ns=(base_ns, base_ns))
    os.utime(untracked_gif, ns=(base_ns, base_ns + 20_000_000_000))

    conn = _setup_conn()
    _insert_candidate(
        conn,
        "cand-tracked",
        artifact_path=str(tracked_gif),
        preview_path=str(tracked_gif),
        created_at="2026-07-05T00:00:00+00:00",
    )
    monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

    payload = candidates_router.list_candidates(
        status="all", limit=10, offset=0, folder=str(folder)
    )

    assert payload["total"] == 2
    assert [
        candidates_router._resolve_artifact_path(c["artifact_path"])
        for c in payload["candidates"]
    ] == [untracked_gif, tracked_gif]


def test_list_candidates_folder_unreadable_file_returns_409(monkeypatch, tmp_path):
    from app.routers import candidates as candidates_router

    folder = tmp_path / "JUR-639"
    folder.mkdir()
    gif_path = folder / "one.gif"
    gif_path.write_bytes(b"gif")

    conn = _setup_conn()
    _insert_candidate(
        conn,
        "cand-one",
        artifact_path=str(gif_path),
        preview_path=str(gif_path),
    )
    monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

    original_stat = candidates_router.Path.stat

    def fake_stat(self, *args, **kwargs):
        if self == gif_path:
            raise PermissionError("access denied")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(candidates_router.Path, "stat", fake_stat)

    with pytest.raises(HTTPException) as exc:
        candidates_router.list_candidates(
            status="all", limit=10, offset=0, folder=str(folder)
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "candidate_path_changed_or_missing"


def test_list_candidates_without_folder_keeps_created_at_order(monkeypatch, tmp_path):
    from app.routers import candidates as candidates_router

    folder = tmp_path / "JUR-639"
    folder.mkdir()
    old_file = folder / "old-file.gif"
    new_file = folder / "new-file.gif"
    old_file.write_bytes(b"gif")
    new_file.write_bytes(b"gif")
    base_ns = 1_700_000_000_000_000_000
    os.utime(old_file, ns=(base_ns, base_ns))
    os.utime(new_file, ns=(base_ns, base_ns + 10_000_000_000))

    conn = _setup_conn()
    # The later-DB-created candidate points at the older-mtime file and vice
    # versa, so mtime ordering would disagree with created_at ordering.
    _insert_candidate(
        conn,
        "cand-new-db",
        artifact_path=str(old_file),
        created_at="2026-07-05T00:00:00+00:00",
    )
    _insert_candidate(
        conn,
        "cand-old-db",
        artifact_path=str(new_file),
        created_at="2026-07-04T00:00:00+00:00",
    )
    monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

    payload = candidates_router.list_candidates(status="all", limit=10, offset=0)

    assert [c["candidate_id"] for c in payload["candidates"]] == [
        "cand-new-db",
        "cand-old-db",
    ]

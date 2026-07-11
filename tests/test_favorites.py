import sqlite3


def test_favorite_service_records_full_path_and_marks_candidate():
    from app.services.favorites import FavoriteService
    from app.services.preference_schema import apply_preference_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    conn.execute(
        """INSERT INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path, start_sec, end_sec, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("cand-1", "run-1", "clip-1", "sha", "D:/video.mp4", 1.0, 2.0, "candidate"),
    )
    conn.commit()

    result = FavoriteService(conn).favorite("cand-1", "D:/exports/movie.gif")

    row = conn.execute(
        "SELECT candidate_id, full_path FROM favorite_gifs WHERE candidate_id=?",
        ("cand-1",),
    ).fetchone()
    assert result["status"] == "favorited"
    assert row["full_path"] == "D:/exports/movie.gif"

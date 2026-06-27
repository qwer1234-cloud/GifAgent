import sqlite3


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def test_preference_schema_creates_required_tables():
    from app.services.preference_schema import apply_preference_schema

    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)

    assert {
        "candidate_gifs",
        "candidate_vectors",
        "preference_events",
        "preference_profile_builds",
        "preference_profiles",
        "preference_profile_current",
    }.issubset(table_names(conn))


def test_preference_schema_is_idempotent():
    from app.services.preference_schema import apply_preference_schema

    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)
    apply_preference_schema(conn)

    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

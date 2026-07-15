"""P1-4: Append-only feedback events for candidate GIFs."""

import sqlite3


def test_feedback_events_are_append_only_and_latest_effective():
    from app.services.preference_schema import apply_preference_schema
    from app.services.preference_events import PreferenceEventService

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    service = PreferenceEventService(conn)

    first = service.record_feedback(
        target_type="candidate_gif",
        target_id="cand-1",
        rating="like",
        source_video_sha256="video-1",
        scenario_keys=["tag:smile"],
    )
    second = service.record_feedback(
        target_type="candidate_gif",
        target_id="cand-1",
        rating="dislike",
        source_video_sha256="video-1",
        scenario_keys=["tag:smile"],
    )

    latest = service.latest_effective_ratings()

    assert first.event_id != second.event_id
    assert conn.execute("SELECT COUNT(*) FROM preference_events").fetchone()[0] == 2
    assert latest["candidate_gif:cand-1"].rating == "dislike"


def test_feedback_updates_candidate_status():
    """Recording feedback should update the candidate_gifs.status column."""
    from app.services.preference_schema import apply_preference_schema
    from app.services.preference_events import PreferenceEventService

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)

    # First insert a candidate row so FK / target lookup works
    conn.execute(
        """INSERT INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path, start_sec, end_sec,
            status)
           VALUES (?,?,?,?,?,?,?,?)""",
        ("cand-1", "run-1", "rc-1", "video-1", "/v/sample.mp4", 0.0, 5.0, "candidate"),
    )
    conn.commit()

    service = PreferenceEventService(conn)

    service.record_feedback(
        target_type="candidate_gif",
        target_id="cand-1",
        rating="like",
        source_video_sha256="video-1",
        scenario_keys=["tag:smile"],
    )

    status = conn.execute(
        "SELECT status FROM candidate_gifs WHERE candidate_id=?", ("cand-1",)
    ).fetchone()["status"]
    assert status == "liked"

    service.record_feedback(
        target_type="candidate_gif",
        target_id="cand-1",
        rating="dislike",
        source_video_sha256="video-1",
        scenario_keys=["tag:smile"],
    )

    status = conn.execute(
        "SELECT status FROM candidate_gifs WHERE candidate_id=?", ("cand-1",)
    ).fetchone()["status"]
    assert status == "disliked"


def test_quality_reject_sets_rejected_status():
    """quality_reject should set status to 'rejected'."""
    from app.services.preference_schema import apply_preference_schema
    from app.services.preference_events import PreferenceEventService

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)

    conn.execute(
        """INSERT INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path, start_sec, end_sec,
            status)
           VALUES (?,?,?,?,?,?,?,?)""",
        ("cand-qr", "run-1", "rc-qr", "video-1", "/v/sample.mp4", 0.0, 5.0, "candidate"),
    )
    conn.commit()

    service = PreferenceEventService(conn)

    service.record_feedback(
        target_type="candidate_gif",
        target_id="cand-qr",
        rating="quality_reject",
        source_video_sha256="video-1",
        scenario_keys=[],
    )

    status = conn.execute(
        "SELECT status FROM candidate_gifs WHERE candidate_id=?", ("cand-qr",)
    ).fetchone()["status"]
    assert status == "rejected"


def test_skip_does_not_change_status():
    """skip feedback should NOT update candidate_gifs.status."""
    from app.services.preference_schema import apply_preference_schema
    from app.services.preference_events import PreferenceEventService

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)

    conn.execute(
        """INSERT INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path, start_sec, end_sec,
            status)
           VALUES (?,?,?,?,?,?,?,?)""",
        ("cand-sk", "run-1", "rc-sk", "video-1", "/v/sample.mp4", 0.0, 5.0, "candidate"),
    )
    conn.commit()

    service = PreferenceEventService(conn)

    service.record_feedback(
        target_type="candidate_gif",
        target_id="cand-sk",
        rating="skip",
        source_video_sha256="video-1",
        scenario_keys=[],
    )

    status = conn.execute(
        "SELECT status FROM candidate_gifs WHERE candidate_id=?", ("cand-sk",)
    ).fetchone()["status"]
    assert status == "candidate"  # unchanged


def test_undo_last_candidate_action_marks_event_undone_and_restores_status():
    from app.services.preference_schema import apply_preference_schema
    from app.services.preference_events import PreferenceEventService

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    conn.execute(
        """INSERT INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path, start_sec, end_sec, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("cand-undo", "run", "clip", "video", "/v.mp4", 0.0, 1.0, "candidate"),
    )
    conn.commit()
    service = PreferenceEventService(conn)
    event = service.record_feedback(
        target_type="candidate_gif", target_id="cand-undo", rating="like",
        source_video_sha256="video", scenario_keys=[],
    )

    result = service.undo_last_candidate_action()

    undone = conn.execute(
        "SELECT undone_at, previous_status FROM preference_events WHERE event_id=?",
        (event.event_id,),
    ).fetchone()
    status = conn.execute(
        "SELECT status FROM candidate_gifs WHERE candidate_id='cand-undo'"
    ).fetchone()[0]
    assert result["status"] == "undone"
    assert undone["undone_at"] is not None
    assert undone["previous_status"] == "candidate"
    assert status == "candidate"


def test_undo_with_no_active_candidate_action_is_noop():
    from app.services.preference_schema import apply_preference_schema
    from app.services.preference_events import PreferenceEventService

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)

    assert PreferenceEventService(conn).undo_last_candidate_action()["status"] == "nothing_to_undo"


def test_invalid_rating_raises():
    import pytest
    from app.services.preference_schema import apply_preference_schema
    from app.services.preference_events import PreferenceEventService

    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)
    service = PreferenceEventService(conn)

    with pytest.raises(ValueError, match="Invalid rating"):
        service.record_feedback(
            target_type="candidate_gif",
            target_id="cand-1",
            rating="bogus",  # type: ignore[arg-type]
            source_video_sha256="video-1",
            scenario_keys=[],
        )


def test_event_id_format():
    """Event IDs have the prefevt_ prefix and are unique."""
    import re
    from app.services.preference_schema import apply_preference_schema
    from app.services.preference_events import PreferenceEventService

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    service = PreferenceEventService(conn)

    event = service.record_feedback(
        target_type="candidate_gif",
        target_id="cand-1",
        rating="like",
        source_video_sha256="video-1",
        scenario_keys=["tag:smile"],
    )

    assert event.event_id.startswith("prefevt_")
    assert re.match(r"^prefevt_[0-9a-f]{32}$", event.event_id)


def test_latest_effective_ratings_multiple_targets():
    """latest_effective_ratings returns one event per target, the most recent."""
    from app.services.preference_schema import apply_preference_schema
    from app.services.preference_events import PreferenceEventService

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    service = PreferenceEventService(conn)

    service.record_feedback(
        target_type="candidate_gif", target_id="cand-a", rating="like",
        source_video_sha256="video-1", scenario_keys=[],
    )
    service.record_feedback(
        target_type="candidate_gif", target_id="cand-b", rating="dislike",
        source_video_sha256="video-1", scenario_keys=[],
    )
    service.record_feedback(
        target_type="candidate_gif", target_id="cand-a", rating="neutral",
        source_video_sha256="video-1", scenario_keys=[],
    )

    latest = service.latest_effective_ratings()

    assert len(latest) == 2
    assert latest["candidate_gif:cand-a"].rating == "neutral"
    assert latest["candidate_gif:cand-b"].rating == "dislike"


def test_scenario_keys_stored_in_event():
    import json
    from app.services.preference_schema import apply_preference_schema
    from app.services.preference_events import PreferenceEventService

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    service = PreferenceEventService(conn)

    event = service.record_feedback(
        target_type="candidate_gif",
        target_id="cand-1",
        rating="like",
        source_video_sha256="video-1",
        scenario_keys=["emotion:joy", "tag:smile"],
        note="Great clip!",
    )

    row = conn.execute(
        "SELECT scenario_keys_json, note FROM preference_events WHERE event_id=?",
        (event.event_id,),
    ).fetchone()

    keys = json.loads(row["scenario_keys_json"])
    assert "emotion:joy" in keys
    assert "tag:smile" in keys
    assert row["note"] == "Great clip!"

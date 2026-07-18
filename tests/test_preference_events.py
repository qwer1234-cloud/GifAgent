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


def test_correct_feedback_creates_correction_event():
    """correct_feedback must create a correction event linked to the original."""
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
        ("cand-corr", "run-1", "rc-corr", "video-1", "/v/sample.mp4", 0.0, 5.0, "candidate"),
    )
    conn.commit()

    svc = PreferenceEventService(conn)
    original = svc.record_feedback(
        target_type="candidate_gif",
        target_id="cand-corr",
        rating="like",
        source_video_sha256="video-1",
        scenario_keys=["tag:smile"],
    )

    correction = svc.correct_feedback(
        event_id=original.event_id, replacement="dislike", reason="fat-finger"
    )

    assert correction.event_kind == "correction"
    assert correction.supersedes_event_id == original.event_id
    assert correction.rating == "dislike"
    assert correction.target_id == "cand-corr"
    assert correction.event_id != original.event_id

    # Original row must still be "like".
    original_row = conn.execute(
        "SELECT rating FROM preference_events WHERE event_id=?",
        (original.event_id,),
    ).fetchone()
    assert original_row["rating"] == "like", "Original row was mutated!"


def test_correct_feedback_updates_candidate_status():
    """Correction must update candidate_gifs.status to the new rating."""
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
        ("cand-corr2", "run-1", "rc-corr2", "video-1", "/v/sample.mp4", 0.0, 5.0, "candidate"),
    )
    conn.commit()

    svc = PreferenceEventService(conn)
    original = svc.record_feedback(
        target_type="candidate_gif",
        target_id="cand-corr2",
        rating="like",
        source_video_sha256="video-1",
        scenario_keys=[],
    )

    svc.correct_feedback(
        event_id=original.event_id, replacement="dislike", reason="changed mind"
    )

    status = conn.execute(
        "SELECT status FROM candidate_gifs WHERE candidate_id=?", ("cand-corr2",)
    ).fetchone()["status"]
    assert status == "disliked"


def test_effective_feedback_excludes_undone():
    """effective_feedback must not return undone events."""
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
        ("cand-eff1", "run-1", "rc-eff1", "video-1", "/v/sample.mp4", 0.0, 5.0, "candidate"),
    )
    conn.commit()

    svc = PreferenceEventService(conn)
    evt = svc.record_feedback(
        target_type="candidate_gif",
        target_id="cand-eff1",
        rating="like",
        source_video_sha256="video-1",
        scenario_keys=[],
    )

    effective_before_undo = svc.effective_feedback()
    assert any(e.event_id == evt.event_id for e in effective_before_undo)

    svc.undo_last_candidate_action()

    effective_after_undo = svc.effective_feedback()
    assert not any(e.event_id == evt.event_id for e in effective_after_undo)


def test_effective_feedback_excludes_superseded():
    """effective_feedback must not return events that have been superseded."""
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
        ("cand-eff2", "run-1", "rc-eff2", "video-1", "/v/sample.mp4", 0.0, 5.0, "candidate"),
    )
    conn.commit()

    svc = PreferenceEventService(conn)
    original = svc.record_feedback(
        target_type="candidate_gif",
        target_id="cand-eff2",
        rating="like",
        source_video_sha256="video-1",
        scenario_keys=[],
    )

    correction = svc.correct_feedback(
        event_id=original.event_id, replacement="dislike", reason="mistake"
    )

    effective = svc.effective_feedback()

    # Original must NOT be in effective.
    assert not any(e.event_id == original.event_id for e in effective)
    # Correction must be in effective.
    assert any(e.event_id == correction.event_id for e in effective)


def test_correct_feedback_invalid_rating_raises():
    """correct_feedback with invalid replacement rating must raise."""
    import pytest
    from app.services.preference_schema import apply_preference_schema
    from app.services.preference_events import PreferenceEventService

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)

    svc = PreferenceEventService(conn)

    with pytest.raises(ValueError, match="Invalid replacement rating"):
        svc.correct_feedback(
            event_id="nonexistent", replacement="bogus",  # type: ignore[arg-type]
            reason="test",
        )


def test_correct_feedback_nonexistent_event_raises():
    """correct_feedback with a non-existent event_id must raise."""
    import pytest
    from app.services.preference_schema import apply_preference_schema
    from app.services.preference_events import PreferenceEventService

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)

    svc = PreferenceEventService(conn)

    with pytest.raises(ValueError, match="No preference event found"):
        svc.correct_feedback(
            event_id="prefevt_nonexistent", replacement="dislike", reason="test",
        )


def test_record_feedback_favorite_rating():
    """record_feedback with rating='favorite' must succeed and not change status."""
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
        ("cand-fav", "run-1", "rc-fav", "video-1", "/v/sample.mp4", 0.0, 5.0, "candidate"),
    )
    conn.commit()

    svc = PreferenceEventService(conn)
    evt = svc.record_feedback(
        target_type="candidate_gif",
        target_id="cand-fav",
        rating="favorite",
        source_video_sha256="video-1",
        scenario_keys=[],
    )

    assert evt.rating == "favorite"
    assert evt.event_kind == "feedback"
    assert evt.supersedes_event_id is None

    # Status should remain "candidate" (favorite does not change status).
    status = conn.execute(
        "SELECT status FROM candidate_gifs WHERE candidate_id=?", ("cand-fav",)
    ).fetchone()["status"]
    assert status == "candidate"


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

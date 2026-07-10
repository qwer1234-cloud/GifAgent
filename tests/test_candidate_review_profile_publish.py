from types import SimpleNamespace
import json


def test_profile_publish_choices_prefers_latest_completed_profile():
    from app.ui.candidate_review import profile_publish_choices

    payload = {
        "profiles": [
            {"profile_version": "profile_latest", "status": "completed"},
            {"profile_version": "profile_blocked", "status": "blocked"},
            {"profile_version": "profile_old", "status": "completed"},
        ],
        "current": {"profile_version": "profile_old", "published_at": "2026-07-09"},
    }

    choices, value = profile_publish_choices(payload)

    assert choices == ["profile_latest", "profile_old"]
    assert value == "profile_latest"


def test_publish_profile_version_posts_selected_profile(monkeypatch):
    from app.ui import candidate_review

    calls = []

    def fake_post(url, timeout):
        calls.append((url, timeout))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"status": "published", "profile_version": "profile_ok"},
        )

    monkeypatch.setattr(candidate_review.httpx, "post", fake_post)

    result = candidate_review.publish_profile_version("profile_ok")

    assert calls == [
        (
            f"{candidate_review.API_BASE}/api/preference/profiles/profile_ok/publish",
            30,
        )
    ]
    assert "published" in result
    assert "profile_ok" in result


def test_publish_profile_version_requires_selection():
    from app.ui.candidate_review import publish_profile_version

    assert publish_profile_version("") == "Select a completed profile_version first."


def test_backfill_profile_vectors_only_embeds_feedback_targets(monkeypatch):
    from app.ui import candidate_review

    class FakeConnection:
        closed = False

        def close(self):
            self.closed = True

    conn = FakeConnection()
    calls = []

    def fake_backfill(connection, *, embed_fn, only_feedback):
        calls.append((connection, embed_fn, only_feedback))
        return {"scanned": 12, "missing": 5, "inserted": 5, "failed": 0}

    monkeypatch.setattr(candidate_review, "get_connection", lambda: conn)
    monkeypatch.setattr(candidate_review, "backfill_candidate_vectors", fake_backfill)
    monkeypatch.setattr(candidate_review, "compute_text_embedding", lambda text: [0.0] * 768)

    result = json.loads(candidate_review.backfill_profile_vectors())

    assert result["inserted"] == 5
    assert calls == [(conn, candidate_review.compute_text_embedding, True)]
    assert conn.closed is True

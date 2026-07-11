from types import SimpleNamespace


def test_ui_undo_posts_to_undo_endpoint(monkeypatch):
    from app.ui import candidate_review

    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return SimpleNamespace(status_code=200, json=lambda: {"status": "undone"})

    monkeypatch.setattr(candidate_review.httpx, "post", fake_post)

    assert candidate_review.undo_last_action() == "Undo: undone"
    assert calls[0][0].endswith("/api/candidates/undo-last")

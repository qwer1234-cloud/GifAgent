from types import SimpleNamespace


def test_ui_favorite_posts_to_favorite_endpoint(monkeypatch):
    from app.ui import candidate_review

    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return SimpleNamespace(status_code=200, json=lambda: {"status": "favorited"})

    monkeypatch.setattr(candidate_review.httpx, "post", fake_post)

    assert candidate_review.favorite_candidate("cand-1", "D:/exports/movie.gif") == "Rated: favorited"
    assert calls[0][0].endswith("/api/candidates/cand-1/favorite")

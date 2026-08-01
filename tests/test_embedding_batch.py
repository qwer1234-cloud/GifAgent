"""Tests for batch text embedding via Ollama /api/embed."""

from types import SimpleNamespace

import httpx

from app.services import embedding


def _fake_response(payload):
    return SimpleNamespace(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: payload,
    )


def test_batch_empty_input_returns_empty_list(monkeypatch):
    called = []

    def fake_post(*args, **kwargs):
        called.append((args, kwargs))
        raise AssertionError("empty input must not call HTTP")

    monkeypatch.setattr(embedding.httpx, "post", fake_post)

    assert embedding.compute_text_embeddings_batch([]) == []
    assert called == []


def test_batch_request_shape_and_timeouts(monkeypatch):
    captured = {}

    def fake_post(url, *, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _fake_response({"embeddings": [[0.1, 0.2], [0.3, 0.4]]})

    monkeypatch.setattr(embedding.httpx, "post", fake_post)

    result = embedding.compute_text_embeddings_batch(["first test", "second test"])

    assert captured["url"] == f"{embedding.EMBED_BASE}/api/embed"
    assert captured["json"] == {
        "model": embedding.EMBED_TEXT_MODEL,
        "input": ["first test", "second test"],
    }
    assert isinstance(captured["timeout"], httpx.Timeout)
    assert captured["timeout"].connect == 5.0
    assert captured["timeout"].read == 60.0
    assert result == [[0.1, 0.2], [0.3, 0.4]]


def test_batch_maps_output_in_order(monkeypatch):
    vectors = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]

    def fake_post(url, *, json=None, timeout=None):
        return _fake_response({"embeddings": vectors})

    monkeypatch.setattr(embedding.httpx, "post", fake_post)

    result = embedding.compute_text_embeddings_batch(["a", "b"])
    assert result == vectors


def test_batch_rejects_missing_embeddings_key(monkeypatch):
    def fake_post(url, *, json=None, timeout=None):
        return _fake_response({})

    monkeypatch.setattr(embedding.httpx, "post", fake_post)

    try:
        embedding.compute_text_embeddings_batch(["a"])
    except ValueError as exc:
        assert "expected 1 embeddings" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_batch_rejects_wrong_response_count(monkeypatch):
    def fake_post(url, *, json=None, timeout=None):
        return _fake_response({"embeddings": [[0.1]]})

    monkeypatch.setattr(embedding.httpx, "post", fake_post)

    try:
        embedding.compute_text_embeddings_batch(["a", "b"])
    except ValueError as exc:
        assert "expected 2 embeddings, got 1" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_batch_rejects_empty_vector(monkeypatch):
    def fake_post(url, *, json=None, timeout=None):
        return _fake_response({"embeddings": [[]]})

    monkeypatch.setattr(embedding.httpx, "post", fake_post)

    try:
        embedding.compute_text_embeddings_batch(["a"])
    except ValueError as exc:
        assert "non-empty" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_batch_rejects_non_list_vector(monkeypatch):
    def fake_post(url, *, json=None, timeout=None):
        return _fake_response({"embeddings": ["oops"]})

    monkeypatch.setattr(embedding.httpx, "post", fake_post)

    try:
        embedding.compute_text_embeddings_batch(["a"])
    except ValueError as exc:
        assert "non-empty" in str(exc)
    else:
        raise AssertionError("expected ValueError")

from __future__ import annotations

import httpx
import pytest


def test_check_embedding_service_fails_fast_when_endpoint_is_unreachable(monkeypatch):
    from app.services import embedding

    def fail_request(*_args, **_kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(embedding.httpx, "get", fail_request)

    with pytest.raises(embedding.EmbeddingServiceUnavailable, match="unavailable"):
        embedding.check_embedding_service()


def test_check_embedding_service_rejects_missing_configured_model(monkeypatch):
    from app.services import embedding

    monkeypatch.setattr(
        embedding.httpx,
        "get",
        lambda *_args, **_kwargs: httpx.Response(
            200,
            json={"models": [{"name": "other-model:latest"}]},
        ),
    )

    with pytest.raises(embedding.EmbeddingServiceUnavailable, match="not found"):
        embedding.check_embedding_service()


def test_ollama_request_error_is_classified_as_service_unavailable(monkeypatch):
    from app.services import embedding

    def fail_request(*_args, **_kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(embedding.httpx, "post", fail_request)

    with pytest.raises(embedding.EmbeddingServiceUnavailable):
        embedding.compute_text_embedding("diagnostic")

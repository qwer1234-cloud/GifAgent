from types import SimpleNamespace

import httpx
import pytest

from app.services import embedding, ollama_runtime


def _runtime_config(**overrides):
    values = dict(
        base_url="http://stub:11434",
        manage_lifecycle=False,
        launch_mode="none",
        wsl_distro="Ubuntu-20.04",
        startup_timeout_s=1.0,
        request_timeout_s=60.0,
        retry_attempts=1,
        retry_backoff_s=0.0,
        keep_alive="",
        embedding_model="nomic-embed-text:latest",
        embedding_dim=768,
    )
    values.update(overrides)
    return ollama_runtime.EmbeddingRuntimeConfig(**values)


def test_check_embedding_service_fails_fast_when_endpoint_is_unreachable(monkeypatch):
    from app.services import embedding

    monkeypatch.setattr(
        embedding.ollama_runtime, "resolve_base_url", lambda: "http://127.0.0.1:11434"
    )

    def fail_request(*_args, **_kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(embedding.httpx, "get", fail_request)

    with pytest.raises(embedding.EmbeddingServiceUnavailable, match="unavailable"):
        embedding.check_embedding_service()


def test_check_embedding_service_rejects_missing_configured_model(monkeypatch):
    from app.services import embedding

    monkeypatch.setattr(
        embedding.ollama_runtime, "resolve_base_url", lambda: "http://127.0.0.1:11434"
    )
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


def test_ollama_request_error_is_classified_as_runtime_error(monkeypatch):
    from app.services import embedding

    def fail_request(*_args, **_kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(embedding.httpx, "post", fail_request)
    monkeypatch.setattr(
        embedding.ollama_runtime,
        "ensure_runtime_ready",
        lambda: SimpleNamespace(base_url="http://stub:11434"),
    )
    monkeypatch.setattr(embedding.ollama_runtime, "invalidate_runtime", lambda: None)
    monkeypatch.setattr(
        embedding.ollama_runtime,
        "get_runtime_config",
        lambda: _runtime_config(),
    )

    with pytest.raises(ollama_runtime.EmbeddingRuntimeError):
        embedding.compute_text_embedding("diagnostic")

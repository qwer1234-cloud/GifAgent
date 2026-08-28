"""Tests for batch/single text embedding via Ollama through the runtime."""

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


def _fake_response(payload, status_code=200, json_error=None):
    def _json():
        if json_error is not None:
            raise json_error
        return payload

    resp = SimpleNamespace(status_code=status_code, json=_json)
    if status_code >= 400:
        def raise_for_status():
            raise httpx.HTTPStatusError(
                f"HTTP {status_code}",
                request=httpx.Request("POST", "http://stub:11434/api/embed"),
                response=resp,
            )
    else:
        def raise_for_status():
            return None

    resp.raise_for_status = raise_for_status
    return resp


def _patch_runtime(monkeypatch, config=None, base_url="http://stub:11434"):
    cfg = config or _runtime_config()
    ensure_calls = {"count": 0}

    def ensure():
        ensure_calls["count"] += 1
        return SimpleNamespace(base_url=base_url)

    monkeypatch.setattr(
        embedding.ollama_runtime, "ensure_runtime_ready", ensure
    )
    monkeypatch.setattr(
        embedding.ollama_runtime, "invalidate_runtime", lambda: None
    )
    monkeypatch.setattr(
        embedding.ollama_runtime, "get_runtime_config", lambda: cfg
    )
    return ensure_calls


def _vectors(dim=768, count=1, fill=0.1):
    return [[fill] * dim for _ in range(count)]


def test_batch_empty_input_returns_empty_list(monkeypatch):
    called = []
    ensure_calls = _patch_runtime(monkeypatch)

    def fake_post(*args, **kwargs):
        called.append((args, kwargs))
        raise AssertionError("empty input must not call HTTP")

    monkeypatch.setattr(embedding.httpx, "post", fake_post)

    assert embedding.compute_text_embeddings_batch([]) == []
    assert called == []
    assert ensure_calls["count"] == 0


def test_batch_request_shape_and_timeouts(monkeypatch):
    captured = {}

    def fake_post(url, *, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _fake_response({"embeddings": _vectors(count=2)})

    monkeypatch.setattr(embedding.httpx, "post", fake_post)
    ensure_calls = _patch_runtime(
        monkeypatch, _runtime_config(request_timeout_s=60.0)
    )

    result = embedding.compute_text_embeddings_batch(["first test", "second test"])

    assert captured["url"] == "http://stub:11434/api/embed"
    assert captured["json"] == {
        "model": embedding.EMBED_TEXT_MODEL,
        "input": ["first test", "second test"],
    }
    assert isinstance(captured["timeout"], httpx.Timeout)
    assert captured["timeout"].connect == 5.0
    assert captured["timeout"].read == 60.0
    assert result == _vectors(count=2)
    assert ensure_calls["count"] == 1


def test_batch_keep_alive_included_when_configured(monkeypatch):
    captured = {}

    def fake_post(url, *, json=None, timeout=None):
        captured["json"] = json
        return _fake_response({"embeddings": _vectors()})

    monkeypatch.setattr(embedding.httpx, "post", fake_post)
    _patch_runtime(monkeypatch, _runtime_config(keep_alive="30m"))

    embedding.compute_text_embeddings_batch(["a"])

    assert captured["json"]["keep_alive"] == "30m"


def test_batch_maps_output_in_order(monkeypatch):
    vectors = _vectors(count=2, fill=0.0)
    vectors[1] = [0.9] * 768

    def fake_post(url, *, json=None, timeout=None):
        return _fake_response({"embeddings": vectors})

    monkeypatch.setattr(embedding.httpx, "post", fake_post)
    _patch_runtime(monkeypatch)

    result = embedding.compute_text_embeddings_batch(["a", "b"])
    assert result == vectors


def test_batch_retries_two_transport_failures_then_succeeds_in_order(monkeypatch):
    failures = [
        httpx.ConnectError("down", request=httpx.Request("POST", "http://stub")),
        httpx.ReadTimeout("slow", request=httpx.Request("POST", "http://stub")),
    ]
    post_calls = []

    def fake_post(url, *, json=None, timeout=None):
        post_calls.append(json["input"])
        if post_calls and failures:
            raise failures.pop(0)
        return _fake_response({"embeddings": _vectors(count=2)})

    monkeypatch.setattr(embedding.httpx, "post", fake_post)
    ensure_calls = _patch_runtime(
        monkeypatch,
        _runtime_config(retry_attempts=3, retry_backoff_s=0.0),
    )
    invalidations = []
    monkeypatch.setattr(
        embedding.ollama_runtime,
        "invalidate_runtime",
        lambda: invalidations.append(1),
    )

    result = embedding.compute_text_embeddings_batch(["a", "b"])

    assert result == _vectors(count=2)
    assert post_calls == [["a", "b"], ["a", "b"], ["a", "b"]]
    assert invalidations == [1, 1]
    assert ensure_calls["count"] == 3


def test_batch_retries_503_then_succeeds(monkeypatch):
    statuses = [503, 200]

    def fake_post(url, *, json=None, timeout=None):
        status = statuses.pop(0)
        if status == 503:
            return _fake_response({}, status_code=503)
        return _fake_response({"embeddings": _vectors()})

    monkeypatch.setattr(embedding.httpx, "post", fake_post)
    _patch_runtime(monkeypatch, _runtime_config(retry_attempts=3))
    invalidations = []
    monkeypatch.setattr(
        embedding.ollama_runtime,
        "invalidate_runtime",
        lambda: invalidations.append(1),
    )

    result = embedding.compute_text_embeddings_batch(["a"])

    assert result == _vectors()
    assert invalidations == [1]


def test_batch_does_not_retry_permanent_4xx(monkeypatch):
    post_calls = []

    def fake_post(url, *, json=None, timeout=None):
        post_calls.append(url)
        return _fake_response({}, status_code=400)

    monkeypatch.setattr(embedding.httpx, "post", fake_post)
    _patch_runtime(monkeypatch, _runtime_config(retry_attempts=3))

    with pytest.raises(ollama_runtime.EmbeddingRuntimeError) as excinfo:
        embedding.compute_text_embeddings_batch(["a"])

    err = excinfo.value
    assert err.phase == "embed"
    assert err.retryable is False
    assert err.attempts == 1
    assert err.base_url == "http://stub:11434"
    assert "HTTP 400" in str(err)
    assert len(post_calls) == 1


def test_batch_does_not_retry_wrong_dimension(monkeypatch):
    post_calls = []

    def fake_post(url, *, json=None, timeout=None):
        post_calls.append(url)
        return _fake_response({"embeddings": [[0.0] * 4]})

    monkeypatch.setattr(embedding.httpx, "post", fake_post)
    _patch_runtime(monkeypatch, _runtime_config(retry_attempts=3))

    with pytest.raises(ollama_runtime.EmbeddingRuntimeError) as excinfo:
        embedding.compute_text_embeddings_batch(["a"])

    err = excinfo.value
    assert err.retryable is False
    assert "embedding_dim mismatch" in str(err)
    assert len(post_calls) == 1


def test_batch_does_not_retry_invalid_json(monkeypatch):
    post_calls = []

    def fake_post(url, *, json=None, timeout=None):
        post_calls.append(url)
        return _fake_response(
            {},
            json_error=ValueError("invalid json payload"),
        )

    monkeypatch.setattr(embedding.httpx, "post", fake_post)
    _patch_runtime(monkeypatch, _runtime_config(retry_attempts=3))

    with pytest.raises(ollama_runtime.EmbeddingRuntimeError) as excinfo:
        embedding.compute_text_embeddings_batch(["a"])

    err = excinfo.value
    assert err.retryable is False
    assert "invalid json payload" in str(err)
    assert len(post_calls) == 1


def test_batch_does_not_retry_non_object_json(monkeypatch):
    post_calls = []

    def fake_post(url, *, json=None, timeout=None):
        post_calls.append(url)
        return _fake_response(["not", "an", "object"])

    monkeypatch.setattr(embedding.httpx, "post", fake_post)
    _patch_runtime(monkeypatch, _runtime_config(retry_attempts=3))

    with pytest.raises(ollama_runtime.EmbeddingRuntimeError) as excinfo:
        embedding.compute_text_embeddings_batch(["a"])

    err = excinfo.value
    assert err.retryable is False
    assert err.attempts == 1
    assert "JSON object" in str(err)
    assert len(post_calls) == 1


def test_batch_retry_exhaustion_raises_structured_error(monkeypatch):
    def fake_post(url, *, json=None, timeout=None):
        raise httpx.ConnectError(
            "unreachable",
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(embedding.httpx, "post", fake_post)
    _patch_runtime(monkeypatch, _runtime_config(retry_attempts=3))

    with pytest.raises(ollama_runtime.EmbeddingRuntimeError) as excinfo:
        embedding.compute_text_embeddings_batch(["a"])

    err = excinfo.value
    assert err.phase == "embed"
    assert err.retryable is True
    assert err.attempts == 3
    assert err.base_url == "http://stub:11434"
    assert "unreachable" in str(err)


def test_batch_rejects_missing_embeddings_key(monkeypatch):
    def fake_post(url, *, json=None, timeout=None):
        return _fake_response({})

    monkeypatch.setattr(embedding.httpx, "post", fake_post)
    _patch_runtime(monkeypatch)

    with pytest.raises(ollama_runtime.EmbeddingRuntimeError) as excinfo:
        embedding.compute_text_embeddings_batch(["a"])
    assert "expected 1 embeddings" in str(excinfo.value)
    assert excinfo.value.retryable is False


def test_batch_rejects_wrong_response_count(monkeypatch):
    def fake_post(url, *, json=None, timeout=None):
        return _fake_response({"embeddings": [[0.1] * 768]})

    monkeypatch.setattr(embedding.httpx, "post", fake_post)
    _patch_runtime(monkeypatch)

    with pytest.raises(ollama_runtime.EmbeddingRuntimeError) as excinfo:
        embedding.compute_text_embeddings_batch(["a", "b"])
    assert "expected 2 embeddings, got 1" in str(excinfo.value)
    assert excinfo.value.retryable is False


def test_batch_rejects_empty_vector(monkeypatch):
    def fake_post(url, *, json=None, timeout=None):
        return _fake_response({"embeddings": [[]]})

    monkeypatch.setattr(embedding.httpx, "post", fake_post)
    _patch_runtime(monkeypatch)

    with pytest.raises(ollama_runtime.EmbeddingRuntimeError) as excinfo:
        embedding.compute_text_embeddings_batch(["a"])
    assert "non-empty" in str(excinfo.value)
    assert excinfo.value.retryable is False


def test_batch_rejects_non_list_vector(monkeypatch):
    def fake_post(url, *, json=None, timeout=None):
        return _fake_response({"embeddings": ["oops"]})

    monkeypatch.setattr(embedding.httpx, "post", fake_post)
    _patch_runtime(monkeypatch)

    with pytest.raises(ollama_runtime.EmbeddingRuntimeError) as excinfo:
        embedding.compute_text_embeddings_batch(["a"])
    assert "non-empty" in str(excinfo.value)
    assert excinfo.value.retryable is False


def test_single_embedding_routes_through_runtime(monkeypatch):
    captured = {}

    def fake_post(url, *, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _fake_response({"embeddings": [[0.0] * 768]})

    monkeypatch.setattr(embedding.httpx, "post", fake_post)
    _patch_runtime(monkeypatch, _runtime_config(keep_alive="30m"))

    result = embedding.compute_text_embedding("hello")

    assert captured["url"] == "http://stub:11434/api/embed"
    assert captured["json"]["model"] == embedding.EMBED_TEXT_MODEL
    assert captured["json"]["input"] == ["hello"]
    assert captured["json"]["keep_alive"] == "30m"
    assert result == [0.0] * 768

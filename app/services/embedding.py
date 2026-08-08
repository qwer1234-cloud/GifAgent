"""
Embedding service for GifAgent.

Uses Ollama to generate vector embeddings for text (annotation summaries, tags,
emotional core) and images (via VLM description of frames, then text embedding).

The base URL is resolved through :mod:`app.services.ollama_runtime` at call
time (environment override, explicit config, or automatic WSL discovery), so
a rebooted machine whose WSL address changed is handled without editing
``configs/models.yaml``.
"""

import base64
import json
import time
from typing import Optional, List

import numpy as np
import httpx

from app.db import get_connection
from app.config import get
from app.services import ollama_runtime

EMBED_TEXT_MODEL = get("embedding.text_model")
EMBED_IMAGE_MODEL = get("embedding.image_model")


class _RetryableStatus(RuntimeError):
    """Internal marker for transient HTTP status codes."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"transient HTTP status {status_code}")
        self.status_code = status_code


def _post_with_retries(
    path: str,
    payload: dict,
    *,
    phase: str,
    validate,
) -> object:
    """POST to the runtime-resolved Ollama endpoint with transient retries.

    Each attempt re-ensures runtime readiness so a changed WSL IP is
    rediscovered after a transport failure.  Only timeouts, transport
    errors, and HTTP 408/429/500/502/503/504 are retried.  Permanent 4xx
    responses, invalid JSON, and malformed vectors raise
    :class:`~app.services.ollama_runtime.EmbeddingRuntimeError` with
    ``retryable=False``.
    """
    config = ollama_runtime.get_runtime_config()
    attempts = max(1, int(config.retry_attempts))
    last_error = None
    last_base_url = None

    for attempt in range(1, attempts + 1):
        state = ollama_runtime.ensure_runtime_ready()
        base_url = state.base_url
        last_base_url = base_url
        try:
            resp = httpx.post(
                f"{base_url}{path}",
                json=payload,
                timeout=httpx.Timeout(config.request_timeout_s, connect=5.0),
            )
            if resp.status_code in ollama_runtime.RETRYABLE_STATUS_CODES:
                raise _RetryableStatus(resp.status_code)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError(
                    f"Ollama response must be a JSON object, "
                    f"got {type(data).__name__}"
                )
            return validate(data)
        except _RetryableStatus as exc:
            last_error = exc
            ollama_runtime.invalidate_runtime()
        except httpx.TimeoutException as exc:
            last_error = exc
            ollama_runtime.invalidate_runtime()
        except httpx.TransportError as exc:
            last_error = exc
            ollama_runtime.invalidate_runtime()
        except httpx.HTTPStatusError as exc:
            raise ollama_runtime.EmbeddingRuntimeError(
                f"Ollama embedding request failed with HTTP "
                f"{exc.response.status_code}",
                phase=phase,
                attempts=attempt,
                base_url=base_url,
                retryable=False,
                cause=exc,
            ) from exc
        except (ValueError, TypeError) as exc:
            raise ollama_runtime.EmbeddingRuntimeError(
                f"Ollama embedding response was invalid: {exc}",
                phase=phase,
                attempts=attempt,
                base_url=base_url,
                retryable=False,
                cause=exc,
            ) from exc

        if attempt < attempts:
            time.sleep(float(config.retry_backoff_s) * (2 ** (attempt - 1)))

    raise ollama_runtime.EmbeddingRuntimeError(
        f"Ollama embedding request failed after {attempts} attempts: "
        f"{last_error}",
        phase=phase,
        attempts=attempts,
        base_url=last_base_url,
        retryable=True,
        cause=last_error,
    ) from last_error


def _ollama_embed(text: str, model: Optional[str] = None) -> List[float]:
    """Call Ollama /api/embeddings. Returns a list of floats."""
    model = model or EMBED_TEXT_MODEL
    config = ollama_runtime.get_runtime_config()
    payload = {"model": model, "prompt": text}
    if config.keep_alive:
        payload["keep_alive"] = config.keep_alive

    def validate(data):
        vector = data.get("embedding")
        if not isinstance(vector, list) or not vector:
            raise ValueError("embedding must be a non-empty list")
        if model == EMBED_TEXT_MODEL and len(vector) != config.embedding_dim:
            raise ValueError(
                f"embedding_dim mismatch: got {len(vector)}, "
                f"expected {config.embedding_dim}"
            )
        return vector

    return _post_with_retries(
        "/api/embeddings", payload, phase="embed", validate=validate
    )


def _ollama_describe_image(image_path: str, model: Optional[str] = None) -> Optional[str]:
    """Use a VLM to describe an image. Returns a text description or None."""
    model = model or EMBED_IMAGE_MODEL
    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()

        base_url = ollama_runtime.resolve_base_url()
        resp = httpx.post(
            f"{base_url}/api/generate",
            json={
                "model": model,
                "prompt": "Describe this image in a few sentences. Focus on the subject, colors, composition, and emotional tone.",
                "images": [base64.b64encode(image_bytes).decode("utf-8")],
                "stream": False,
            },
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception:
        return None


def compute_text_embedding(text: str) -> List[float]:
    """Generate embedding for text using Ollama."""
    return _ollama_embed(text)


def compute_text_embeddings_batch(texts: List[str]) -> List[List[float]]:
    """Generate embeddings for multiple texts using Ollama /api/embed.

    Returns one vector (list of floats) per input text, in the same order.
    An empty input list returns [] without making an HTTP request.
    """
    if not texts:
        return []

    config = ollama_runtime.get_runtime_config()
    payload = {"model": EMBED_TEXT_MODEL, "input": list(texts)}
    if config.keep_alive:
        payload["keep_alive"] = config.keep_alive

    def validate(data):
        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list):
            raise ValueError(
                f"expected {len(texts)} embeddings, "
                f"got {type(embeddings).__name__}"
            )
        if len(embeddings) != len(texts):
            raise ValueError(
                f"expected {len(texts)} embeddings, got {len(embeddings)}"
            )
        for vector in embeddings:
            if not isinstance(vector, list) or not vector:
                raise ValueError("each embedding must be a non-empty list")
            if len(vector) != config.embedding_dim:
                raise ValueError(
                    f"embedding_dim mismatch: got {len(vector)}, "
                    f"expected {config.embedding_dim}"
                )
        return embeddings

    return _post_with_retries(
        "/api/embed", payload, phase="embed", validate=validate
    )


def compute_image_embedding(image_path: str) -> Optional[List[float]]:
    """Generate an embedding for an image.

    Describes the image with a VLM, then embeds the resulting description text.
    Returns None if VLM description fails.
    """
    description = _ollama_describe_image(image_path)
    if not description:
        return None
    return _ollama_embed(description)


def compute_media_embedding(media_id: str) -> Optional[List[float]]:
    """Compute embedding for a media item. Prefers text annotation embedding."""
    # Try text summary embedding first (works with nomic-embed-text)
    emb = compute_text_summary_embedding(media_id)
    if emb:
        return emb

    # Fallback to image embeddings via frame descriptions
    conn = get_connection()
    frame_rows = conn.execute(
        "SELECT frame_path FROM frames WHERE media_id=? ORDER BY frame_index",
        (media_id,),
    ).fetchall()

    embeddings: List[List[float]] = []
    for fr in frame_rows:
        emb = compute_image_embedding(fr["frame_path"])
        if emb:
            embeddings.append(emb)

    if not embeddings:
        return None

    avg_embedding = np.mean(np.array(embeddings), axis=0).tolist()
    return avg_embedding


def compute_text_summary_embedding(media_id: str) -> Optional[List[float]]:
    """Compute text embedding from the annotation summary, emotional_core, and tags."""
    conn = get_connection()
    row = conn.execute(
        "SELECT summary, emotional_core, tags_json, why_i_like_it FROM annotations WHERE media_id=?",
        (media_id,),
    ).fetchone()

    if not row:
        return None

    text_parts: List[str] = []
    if row["summary"]:
        text_parts.append(row["summary"])
    if row["emotional_core"]:
        text_parts.append(row["emotional_core"])
    if row["why_i_like_it"]:
        text_parts.append(row["why_i_like_it"])
    if row["tags_json"]:
        try:
            tags = json.loads(row["tags_json"])
            text_parts.extend(tags)
        except json.JSONDecodeError:
            pass

    text = " ".join(text_parts)
    if not text.strip():
        return None

    return _ollama_embed(text)

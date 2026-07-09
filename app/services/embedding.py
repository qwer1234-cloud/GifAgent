"""
Embedding service for GifAgent.

Uses Ollama to generate vector embeddings for text (annotation summaries, tags,
emotional core) and images (via VLM description of frames, then text embedding).
"""

import base64
import json
from typing import Optional, List

import numpy as np
import httpx

from app.db import get_connection
from app.config import get

EMBED_BASE = get("embedding.base_url", "http://127.0.0.1:11434")
EMBED_TEXT_MODEL = get("embedding.text_model")
EMBED_IMAGE_MODEL = get("embedding.image_model")


def _ollama_embed(text: str, model: Optional[str] = None) -> List[float]:
    """Call Ollama /api/embeddings. Returns a list of floats."""
    model = model or EMBED_TEXT_MODEL
    resp = httpx.post(
        f"{EMBED_BASE}/api/embeddings",
        json={"model": model, "prompt": text},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def _ollama_describe_image(image_path: str, model: Optional[str] = None) -> Optional[str]:
    """Use a VLM to describe an image. Returns a text description or None."""
    model = model or EMBED_IMAGE_MODEL
    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()

        resp = httpx.post(
            f"{EMBED_BASE}/api/generate",
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

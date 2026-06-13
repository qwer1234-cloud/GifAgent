"""
FAISS indexer for GifAgent.

Manages a FAISS IndexFlatIP index (inner product for cosine similarity after
L2-normalisation) persisted under data/faiss/.  The id_map.json sidecar maps
FAISS row numbers back to media_id values.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import faiss

from app.db import get_connection
from app.config import get

FAISS_DIR = get("paths.faiss_dir", "data/faiss")
INDEX_FILE = os.path.join(FAISS_DIR, "media_index.faiss")
ID_MAP_FILE = os.path.join(FAISS_DIR, "id_map.json")


class MediaIndex:
    """Cosine-similarity index over media embeddings.

    Uses FAISS IndexFlatIP with L2-normalised vectors so inner product equals
    cosine similarity.
    """

    def __init__(self, dim: int = 768):
        self.dim = dim
        os.makedirs(FAISS_DIR, exist_ok=True)
        if os.path.exists(INDEX_FILE):
            self.index = faiss.read_index(INDEX_FILE)
        else:
            self.index = faiss.IndexFlatIP(self.dim)

    def _load_id_map(self) -> Dict[int, str]:
        if os.path.exists(ID_MAP_FILE):
            with open(ID_MAP_FILE) as f:
                return {int(k): v for k, v in json.load(f).items()}
        return {}

    def _save_id_map(self, id_map: Dict[int, str]) -> None:
        with open(ID_MAP_FILE, "w") as f:
            json.dump({str(k): v for k, v in id_map.items()}, f)

    def add(self, vector: List[float], media_id: str, vector_type: str = "media_global") -> str:
        """Add a vector to the index and record it in vector_refs.

        Returns the new vector_id.
        """
        vec = np.array([vector], dtype=np.float32)
        faiss.normalize_L2(vec)
        idx = self.index.ntotal
        self.index.add(vec)
        faiss.write_index(self.index, INDEX_FILE)

        id_map = self._load_id_map()
        id_map[idx] = media_id
        self._save_id_map(id_map)

        vector_id = f"vec_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection()
        conn.execute(
            "INSERT INTO vector_refs VALUES (?,?,?,?,?,?)",
            (vector_id, "media", media_id, vector_type, "media_index", now),
        )
        conn.commit()
        return vector_id

    def search(self, vector: List[float], top_k: int = 10) -> List[Dict[str, Any]]:
        """Return the top-k nearest media items (by cosine similarity).

        Each result dict contains media_id, score, film, summary, emotional_core,
        tags, and file_path.
        """
        if self.index.ntotal == 0:
            return []

        vec = np.array([vector], dtype=np.float32)
        faiss.normalize_L2(vec)
        distances, indices = self.index.search(vec, min(top_k, self.index.ntotal))

        id_map = self._load_id_map()
        conn = get_connection()
        results: List[Dict[str, Any]] = []
        for dist, idx in zip(distances[0], indices[0]):
            if idx < 0:
                continue
            media_id = id_map.get(int(idx))
            if not media_id:
                continue
            row = conn.execute(
                """SELECT m.file_path, m.film, a.summary, a.emotional_core, a.tags_json
                   FROM media m LEFT JOIN annotations a ON m.media_id = a.media_id
                   WHERE m.media_id = ?""",
                (media_id,),
            ).fetchone()
            if row:
                results.append({
                    "media_id": media_id,
                    "score": float(dist),
                    "film": row["film"],
                    "summary": row["summary"],
                    "emotional_core": row["emotional_core"],
                    "tags": json.loads(row["tags_json"]) if row["tags_json"] else [],
                    "file_path": row["file_path"],
                })
        return results

    @property
    def count(self) -> int:
        return self.index.ntotal


_media_index: Optional[MediaIndex] = None


def get_index() -> MediaIndex:
    """Return the singleton MediaIndex, creating it on first access."""
    global _media_index
    if _media_index is None:
        _media_index = MediaIndex()
    return _media_index


def index_all_annotated() -> Dict[str, int]:
    """Build FAISS index from all annotated media not already indexed.

    Only processes media that have both annotations and frames, and that are
    not yet represented by a 'media_global' vector_ref.
    """
    from app.services.embedding import compute_media_embedding

    conn = get_connection()
    rows = conn.execute(
        """SELECT DISTINCT m.media_id FROM media m
           INNER JOIN annotations a ON m.media_id = a.media_id
           INNER JOIN frames f ON m.media_id = f.media_id
           WHERE m.media_id NOT IN (
               SELECT owner_id FROM vector_refs WHERE vector_type = 'media_global'
           )"""
    ).fetchall()

    idx = get_index()
    stats = {"total": len(rows), "indexed": 0, "failed": 0}
    for row in rows:
        try:
            emb = compute_media_embedding(row["media_id"])
            if emb:
                idx.add(emb, row["media_id"], "media_global")
                stats["indexed"] += 1
            else:
                stats["failed"] += 1
        except Exception:
            stats["failed"] += 1
    return stats

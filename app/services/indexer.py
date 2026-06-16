"""
FAISS indexer for GifAgent — cosine-similarity index with manifest and atomic writes.

Manages a FAISS IndexFlatIP, persisted under data/faiss/ with:
  - media_index.faiss      : FAISS binary index
  - id_map.json             : row_number → media_id lookups
  - manifest.json           : embedding model, dimension, schema version
"""
import json, os, uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import faiss

from app.db import get_connection
from app.config import get

FAISS_DIR = get("paths.faiss_dir", "data/faiss")
INDEX_FILE = os.path.join(FAISS_DIR, "media_index.faiss")
ID_MAP_FILE = os.path.join(FAISS_DIR, "id_map.json")
MANIFEST_FILE = os.path.join(FAISS_DIR, "manifest.json")

EMBED_MODEL = get("embedding.text_model", "nomic-embed-text:latest")
SCHEMA_VERSION = 1


def _atomic_write(path: str, data: str) -> None:
    """Write to a temp file then rename for atomicity."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)


class MediaIndex:
    """Cosine-similarity index over media text embeddings."""

    def __init__(self):
        os.makedirs(FAISS_DIR, exist_ok=True)
        manifest = self._load_manifest()
        self._dim = manifest.get("dim", 768)

        if os.path.exists(INDEX_FILE):
            # Auto-upgrade legacy indexes without manifest
            if manifest.get("schema_version") is None:
                self.index = faiss.read_index(INDEX_FILE)
                self._dim = self.index.d
                self._save_manifest()
            elif manifest.get("schema_version") != SCHEMA_VERSION:
                raise RuntimeError(
                    f"FAISS manifest schema mismatch: "
                    f"expected {SCHEMA_VERSION}, got {manifest.get('schema_version')}"
                )
            elif manifest.get("embedding_model") != EMBED_MODEL:
                raise RuntimeError(
                    f"FAISS embedding model mismatch: "
                    f"expected {EMBED_MODEL}, got {manifest.get('embedding_model')}"
                )
            else:
                self.index = faiss.read_index(INDEX_FILE)
                self._dim = self.index.d
        else:
            self.index = faiss.IndexFlatIP(self._dim)

    def _load_manifest(self) -> dict:
        if os.path.exists(MANIFEST_FILE):
            with open(MANIFEST_FILE, encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_manifest(self) -> None:
        manifest = {
            "index_name": "media_index",
            "embedding_model": EMBED_MODEL,
            "dim": self._dim,
            "metric": "cosine",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "vector_count": self.index.ntotal,
            "schema_version": SCHEMA_VERSION,
        }
        _atomic_write(MANIFEST_FILE, json.dumps(manifest, indent=2))

    def _load_id_map(self) -> Dict[int, str]:
        if os.path.exists(ID_MAP_FILE):
            with open(ID_MAP_FILE, encoding="utf-8") as f:
                return {int(k): v for k, v in json.load(f).items()}
        return {}

    def _save_id_map(self, id_map: Dict[int, str]) -> None:
        _atomic_write(ID_MAP_FILE, json.dumps({str(k): v for k, v in id_map.items()}))

    def add(self, vector: List[float], media_id: str, vector_type: str = "media_global") -> str:
        vec = np.array([vector], dtype=np.float32)
        faiss.normalize_L2(vec)

        # Auto-detect dimension on first add
        if self.index.ntotal == 0 and self._dim != vec.shape[1]:
            self._dim = vec.shape[1]
            self.index = faiss.IndexFlatIP(self._dim)

        idx_pos = self.index.ntotal
        self.index.add(vec)

        # Atomic write sequence: FAISS → id_map → manifest
        faiss.write_index(self.index, INDEX_FILE + ".tmp")
        os.replace(INDEX_FILE + ".tmp", INDEX_FILE)

        id_map = self._load_id_map()
        id_map[idx_pos] = media_id
        self._save_id_map(id_map)
        self._save_manifest()

        vector_id = f"vec_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection()
        conn.execute(
            "INSERT INTO vector_refs (vector_id, owner_type, owner_id, vector_type, "
            "index_name, created_at, embedding_model, embedding_dim) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (vector_id, "media", media_id, vector_type, "media_index", now,
             EMBED_MODEL, self._dim),
        )
        conn.commit()
        return vector_id

    def search(self, vector: List[float], top_k: int = 10) -> List[Dict[str, Any]]:
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
                    "media_id": media_id, "score": float(dist),
                    "film": row["film"], "summary": row["summary"],
                    "emotional_core": row["emotional_core"],
                    "tags": json.loads(row["tags_json"]) if row["tags_json"] else [],
                    "file_path": row["file_path"],
                })
        return results

    @property
    def count(self) -> int:
        return self.index.ntotal

    @property
    def dim(self) -> int:
        return self._dim


_media_index: Optional[MediaIndex] = None


def get_index() -> MediaIndex:
    global _media_index
    if _media_index is None:
        _media_index = MediaIndex()
    return _media_index


def verify_index() -> Dict[str, Any]:
    """Verify FAISS index, id_map, and SQLite vector_refs are consistent."""
    idx = get_index()
    id_map = idx._load_id_map()
    conn = get_connection()
    sql_count = conn.execute(
        "SELECT COUNT(*) FROM vector_refs WHERE index_name='media_index'"
    ).fetchone()[0]
    faiss_count = idx.index.ntotal
    id_map_count = len(id_map)

    errors = []
    if faiss_count != id_map_count:
        errors.append(f"FAISS ntotal={faiss_count} != id_map size={id_map_count}")
    if faiss_count != sql_count:
        errors.append(f"FAISS ntotal={faiss_count} != SQL vector_refs={sql_count}")

    # Verify all id_map entries have matching vector_refs
    for faiss_idx, media_id in list(id_map.items())[:5]:  # spot check
        vr = conn.execute(
            "SELECT COUNT(*) FROM vector_refs WHERE owner_id=? AND index_name='media_index'",
            (media_id,),
        ).fetchone()[0]
        if vr == 0:
            errors.append(f"id_map entry faiss_idx={faiss_idx} media={media_id} not in vector_refs")

    return {
        "ok": len(errors) == 0,
        "faiss_ntotal": faiss_count,
        "id_map_size": id_map_count,
        "sql_vector_refs": sql_count,
        "embedding_model": EMBED_MODEL,
        "dim": idx.dim,
        "errors": errors,
    }


def index_all_annotated() -> Dict[str, int]:
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

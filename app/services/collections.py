"""Phase 4 Task 6: CollectionService -- reproducible smart collections.

Public API
----------
- ``CollectionService.create(spec)`` -- create a new collection.
- ``CollectionService.refresh(collection_id)`` -- run query + farthest-first
  diversity and store a new immutable version.
- ``CollectionService.freeze(collection_id)`` -- mark a collection as frozen
  so no further implicit refreshes are allowed.
- ``CollectionService.export(collection_id, output_dir)`` -- write a
  deterministic JSON manifest and a binary PBF file to *output_dir*.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple
from uuid import uuid4

import numpy as np

from app.services.library_search import LibrarySearchService
from app.services.preference_memory import REQUIRED_EMBEDDING_DIM, REQUIRED_EMBEDDING_MODEL
from app.services.workbench_schema import (
    Collection,
    CollectionSpec,
    CollectionVersion,
    ExportReport,
    SearchQuery,
    SearchResultItem,
    apply_collections_schema,
)

# ---------------------------------------------------------------------------
# PBF constants
# ---------------------------------------------------------------------------

_PBF_MAGIC = b"GIFPBF01"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    """Return current UTC timestamp as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _query_to_dict(query: SearchQuery) -> dict:
    """Convert a SearchQuery to a plain dict for JSON serialization."""
    return {
        "text": query.text,
        "tags": list(query.tags),
        "folder": query.folder,
        "min_duration": query.min_duration,
        "max_duration": query.max_duration,
        "statuses": list(query.statuses),
        "created_after": query.created_after,
        "created_before": query.created_before,
    }


def _dict_to_query(data: dict) -> SearchQuery:
    """Reconstruct a SearchQuery from a plain dict."""
    return SearchQuery(
        text=data.get("text", ""),
        tags=tuple(data.get("tags", [])),
        folder=data.get("folder"),
        min_duration=data.get("min_duration"),
        max_duration=data.get("max_duration"),
        statuses=tuple(data.get("statuses", [])),
        created_after=data.get("created_after"),
        created_before=data.get("created_before"),
    )


def _cosine_distance(v1: np.ndarray, v2: np.ndarray) -> float:
    """Cosine distance between two normalized vectors in [0, 2] range.

    Returns a value in [0, 2] where 0 = identical direction and 2 = opposite.
    For unit-normalised vectors the result is 1 - dot(v1, v2).
    """
    sim = float(np.dot(v1, v2))
    sim = max(-1.0, min(1.0, sim))
    return 1.0 - sim


def _atomic_write(path: Path, data: bytes) -> None:
    """Atomically write *data* to *path* using a temp file + os.replace."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _compute_manifest_hash(candidate_ids: Tuple[str, ...]) -> str:
    """Deterministic SHA-256 of the sorted candidate IDs."""
    sorted_ids = sorted(candidate_ids)
    return hashlib.sha256(json.dumps(sorted_ids).encode()).hexdigest()


def _write_pbf(path: Path, candidates: List[dict]) -> None:
    """Write a PBF binary file with candidate data and source timestamps.

    Format (little-endian)::

        [0:8]   Magic "GIFPBF01"
        [8:12]  Entry count  (uint32)
        For each entry:
          [0:2]    candidate_id length (uint16)
          [2:..]   candidate_id (UTF-8)
          [+0:8]   score (float64)
          [+0:2]   source_path length (uint16)
          [+2:..]  source_path (UTF-8)
          [+0:2]   created_at length (uint16)
          [+2:..]  created_at (UTF-8)
    """
    buf = bytearray()
    buf.extend(_PBF_MAGIC)
    buf.extend(struct.pack("<I", len(candidates)))

    for c in candidates:
        cid_enc = c["candidate_id"].encode("utf-8")
        score = c.get("score", 0.0) or 0.0
        src_enc = (c.get("source_video_path", "") or "").encode("utf-8")
        ts_enc = (c.get("created_at", "") or "").encode("utf-8")

        buf.extend(struct.pack("<H", len(cid_enc)))
        buf.extend(cid_enc)
        buf.extend(struct.pack("<d", score))
        buf.extend(struct.pack("<H", len(src_enc)))
        buf.extend(src_enc)
        buf.extend(struct.pack("<H", len(ts_enc)))
        buf.extend(ts_enc)

    _atomic_write(path, bytes(buf))


# ===================================================================
# Service
# ===================================================================


class CollectionService:
    """Create, refresh, freeze, and export reproducible smart collections.

    Parameters
    ----------
    conn : sqlite3.Connection
        Read-write connection to the library database.
    search_service : LibrarySearchService
        The search service used to populate collection candidates.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        search_service: LibrarySearchService,
    ):
        self.conn = conn
        self.search_service = search_service
        apply_collections_schema(conn)

    # ── public API ──────────────────────────────────────────────────────────

    def create(self, spec: CollectionSpec) -> Collection:
        """Persist a new collection and return its dataclass."""
        collection_id = uuid4().hex
        now = _now()

        self.conn.execute(
            """INSERT INTO collections
               (collection_id, name, search_query_json, target_count,
                min_duration, max_duration, diversity_weight,
                profile_version, config_id,
                current_version, frozen, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)""",
            (
                collection_id,
                spec.name,
                json.dumps(_query_to_dict(spec.query)),
                spec.target_count,
                spec.min_duration,
                spec.max_duration,
                spec.diversity_weight,
                spec.profile_version,
                spec.config_id,
                now,
                now,
            ),
        )
        self.conn.commit()

        return Collection(
            collection_id=collection_id,
            spec=spec,
            current_version=0,
            frozen=False,
        )

    def refresh(self, collection_id: str) -> CollectionVersion:
        """Run the collection query, apply farthest-first diversity, and
        persist a new immutable version.

        Raises ``ValueError`` if the collection is frozen.
        """
        row = self.conn.execute(
            """SELECT frozen, current_version, search_query_json,
                      target_count, diversity_weight
               FROM collections
               WHERE collection_id=?""",
            (collection_id,),
        ).fetchone()

        if row is None:
            raise ValueError(f"Collection {collection_id} not found")
        if row["frozen"]:
            raise ValueError(f"Collection {collection_id} is frozen")

        query_dict = json.loads(row["search_query_json"])
        query = _dict_to_query(query_dict)
        target_count = row["target_count"]
        diversity_weight = row["diversity_weight"]
        current_version = row["current_version"]

        # Search for candidates (use a generous limit to give diversity a pool)
        search_limit = max(target_count * 5, 100)
        page = self.search_service.search(query, limit=search_limit, offset=0)

        if not page.items:
            return self._save_version(
                collection_id, (), {}, current_version
            )

        # Apply farthest-first diversity selection
        selected = self._farthest_first_select(
            page.items, target_count, diversity_weight
        )

        if not selected:
            return self._save_version(
                collection_id, (), {}, current_version
            )

        candidate_ids = tuple(cid for cid, _ in selected)
        scores = {cid: sc for cid, sc in selected}

        return self._save_version(collection_id, candidate_ids, scores, current_version)

    def freeze(self, collection_id: str) -> CollectionVersion:
        """Mark a collection as frozen.

        Returns the latest ``CollectionVersion`` (or an empty version-0
        placeholder if no versions exist yet).

        Raises ``ValueError`` if the collection does not exist.
        """
        row = self.conn.execute(
            "SELECT current_version FROM collections WHERE collection_id=?",
            (collection_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Collection {collection_id} not found")

        now = _now()
        self.conn.execute(
            "UPDATE collections SET frozen=1, updated_at=? WHERE collection_id=?",
            (now, collection_id),
        )
        self.conn.commit()

        # Return the latest version (or empty v0 placeholder)
        if row["current_version"] > 0:
            vrow = self.conn.execute(
                """SELECT version, candidate_ids_json, manifest_hash
                   FROM collection_versions
                   WHERE collection_id=? AND version=?""",
                (collection_id, row["current_version"]),
            ).fetchone()
            return CollectionVersion(
                collection_id=collection_id,
                version=vrow["version"],
                candidate_ids=tuple(json.loads(vrow["candidate_ids_json"])),
                manifest_hash=vrow["manifest_hash"],
            )

        return CollectionVersion(
            collection_id=collection_id,
            version=0,
            candidate_ids=(),
            manifest_hash="",
        )

    def export(self, collection_id: str, output_dir: Path) -> ExportReport:
        """Export the latest version of a collection.

        Writes a deterministic JSON manifest and a binary PBF file.
        Reports missing (deleted) candidate IDs without silently replacing
        them.  Both files are written atomically via temp-file + ``os.replace``.

        Raises ``ValueError`` if the collection has no versions.
        """
        # Fetch latest-version data with a join
        row = self.conn.execute(
            """SELECT c.name, c.current_version,
                      v.version, v.candidate_ids_json, v.scores_json
               FROM collections c
               LEFT JOIN collection_versions v
                 ON c.collection_id = v.collection_id
                AND c.current_version = v.version
               WHERE c.collection_id=?""",
            (collection_id,),
        ).fetchone()

        if row is None:
            raise ValueError(f"Collection {collection_id} not found")
        if row["current_version"] == 0 or row["version"] is None:
            raise ValueError(
                f"Collection {collection_id} has no versions to export"
            )

        candidate_ids: Tuple[str, ...] = tuple(
            json.loads(row["candidate_ids_json"])
        )
        scores: dict = json.loads(row["scores_json"])
        name = row["name"]
        version = row["version"]

        # Check which candidates still exist
        missing: List[str] = []
        existing: List[dict] = []

        for cid in candidate_ids:
            c_row = self.conn.execute(
                """SELECT candidate_id, preview_path, source_video_path,
                          start_sec, end_sec, created_at, final_score
                   FROM candidate_gifs
                   WHERE candidate_id=?""",
                (cid,),
            ).fetchone()
            if c_row is None:
                missing.append(cid)
            else:
                existing.append(
                    {
                        "candidate_id": cid,
                        "score": scores.get(cid, c_row["final_score"]),
                        "preview_path": c_row["preview_path"],
                        "source_video_path": c_row["source_video_path"],
                        "start_sec": c_row["start_sec"],
                        "end_sec": c_row["end_sec"],
                        "created_at": c_row["created_at"],
                    }
                )

        # Build manifest
        manifest: dict = {
            "collection_id": collection_id,
            "name": name,
            "version": version,
            "created_at": _now(),
            "query": json.loads(
                self.conn.execute(
                    "SELECT search_query_json FROM collections WHERE collection_id=?",
                    (collection_id,),
                ).fetchone()["search_query_json"]
            ),
            "candidates": existing,
            "missing_candidate_ids": missing,
        }

        # Add rank to candidates
        for rank, c in enumerate(existing):
            c["rank"] = rank

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # JSON manifest
        manifest_name = f"collection_{collection_id}_v{version}.json"
        manifest_path = output_dir / manifest_name
        _atomic_write(
            manifest_path,
            json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8"),
        )

        # PBF binary file
        pbf_name = f"collection_{collection_id}_v{version}.pbf"
        pbf_path = output_dir / pbf_name
        _write_pbf(pbf_path, existing)

        return ExportReport(
            manifest_path=str(manifest_path.resolve()),
            pbf_path=str(pbf_path.resolve()),
            exported=len(existing),
            missing_candidate_ids=tuple(missing),
        )

    # ── internal: diversity selection ───────────────────────────────────────

    def _farthest_first_select(
        self,
        items: List[SearchResultItem],
        target_count: int,
        diversity_weight: float,
    ) -> List[Tuple[str, float]]:
        """Farthest-first diversity selection from a pool of candidates.

        Algorithm
        ---------
        1. Sort candidates by search score descending.
        2. Select the highest-scored candidate as the first element.
        3. For each remaining candidate compute:
           ``combined = (1-dw) * score + dw * min_distance_to_selected``
        4. Pick the candidate with the highest combined score.
        5. Repeat until ``target_count`` is reached or the pool is empty.

        Candidates without vectors get ``distance = 0`` so they rank by
        search score alone.
        """
        # Build (candidate_id, score) list from items
        all_candidates: List[Tuple[str, float]] = []
        for item in items:
            score = item.score if item.score is not None else 0.0
            all_candidates.append((item.candidate_id, score))

        if len(all_candidates) <= target_count:
            return all_candidates

        # Load vectors for all candidates in batch
        cid_to_vec: dict[str, np.ndarray] = {}
        for cid, _ in all_candidates:
            row = self.conn.execute(
                """SELECT vector_blob FROM candidate_vectors
                   WHERE candidate_id=?
                     AND vector_type='clip'
                     AND embedding_model=?
                     AND embedding_dim=?""",
                (cid, REQUIRED_EMBEDDING_MODEL, REQUIRED_EMBEDDING_DIM),
            ).fetchone()
            if row is not None:
                cid_to_vec[cid] = np.frombuffer(row["vector_blob"], dtype=np.float32)

        # Initial sort by search score descending
        all_candidates.sort(key=lambda x: -x[1])

        # Select highest-scored first
        selected: List[Tuple[str, float]] = [all_candidates[0]]
        selected_ids: set[str] = {all_candidates[0][0]}
        remaining: List[Tuple[str, float]] = list(all_candidates[1:])

        while len(selected) < target_count and remaining:
            best_idx = -1
            best_combined = -float("inf")

            for i, (cid_i, sc_i) in enumerate(remaining):
                # Compute min cosine distance to any selected candidate
                if cid_i in cid_to_vec and selected_ids:
                    min_dist = min(
                        _cosine_distance(cid_to_vec[cid_i], cid_to_vec[sid])
                        for sid in selected_ids
                        if sid in cid_to_vec
                    )
                else:
                    min_dist = 0.0

                combined = (1.0 - diversity_weight) * sc_i + diversity_weight * min_dist
                if combined > best_combined:
                    best_combined = combined
                    best_idx = i

            if best_idx < 0:
                break

            cid, sc = remaining.pop(best_idx)
            selected.append((cid, sc))
            selected_ids.add(cid)

        return selected

    # ── internal: persistence ───────────────────────────────────────────────

    def _save_version(
        self,
        collection_id: str,
        candidate_ids: Tuple[str, ...],
        scores: dict[str, float],
        current_version: int,
    ) -> CollectionVersion:
        """Persist a new collection version and update the collection row."""
        new_version = current_version + 1
        now = _now()

        # Determine the manifest hash from sorted IDs
        manifest_hash = _compute_manifest_hash(candidate_ids)

        # Persist version row
        self.conn.execute(
            """INSERT INTO collection_versions
               (collection_id, version, candidate_ids_json, scores_json,
                manifest_hash, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                collection_id,
                new_version,
                json.dumps(candidate_ids),
                json.dumps(scores, default=str),
                manifest_hash,
                now,
            ),
        )

        # Persist individual collection_items
        for rank, cid in enumerate(candidate_ids):
            self.conn.execute(
                """INSERT INTO collection_items
                   (collection_id, version, candidate_id, score, rank, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (collection_id, new_version, cid, scores.get(cid), rank, now),
            )

        # Update collection's current_version
        self.conn.execute(
            "UPDATE collections SET current_version=?, updated_at=? WHERE collection_id=?",
            (new_version, now, collection_id),
        )
        self.conn.commit()

        return CollectionVersion(
            collection_id=collection_id,
            version=new_version,
            candidate_ids=candidate_ids,
            manifest_hash=manifest_hash,
        )

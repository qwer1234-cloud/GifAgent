"""Phase 4 Task 3/6: Workbench search + collections schema.

FTS5 virtual table for full-text search across candidate GIFs, plus
frozen dataclasses for ``SearchQuery`` / ``SearchPage`` / ``IndexHealth``
/ ``RebuildReport`` / ``CollectionSpec`` / ``Collection`` /
``CollectionVersion`` / ``ExportReport``.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# FTS5 schema DDL
# ---------------------------------------------------------------------------

FTS5_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS candidate_search_fts USING fts5(
    candidate_id UNINDEXED,
    summary,
    tags,
    source_path,
    tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS search_index_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    last_candidate_id TEXT,
    last_candidate_created_at TEXT,
    indexed_count INTEGER NOT NULL DEFAULT 0,
    total_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


def apply_search_schema(conn: sqlite3.Connection) -> None:
    """Create FTS5 virtual table and state-tracking table if missing."""
    conn.executescript(FTS5_DDL)
    # Migrate older state tables that lack the created_at column
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(search_index_state)").fetchall()
    }
    if "last_candidate_created_at" not in cols:
        conn.execute(
            "ALTER TABLE search_index_state ADD COLUMN last_candidate_created_at TEXT"
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchQuery:
    """Query parameters for candidate search.

    All fields are optional.  When *text* is empty the search returns
    filtered results ordered by ``final_score`` (no vector reranking).
    """

    text: str = ""
    tags: Tuple[str, ...] = ()
    folder: Optional[str] = None
    min_duration: Optional[float] = None
    max_duration: Optional[float] = None
    statuses: Tuple[str, ...] = ()
    created_after: Optional[str] = None
    created_before: Optional[str] = None


@dataclass(frozen=True)
class SearchResultItem:
    """A single search result carrying the static *preview_path*."""

    candidate_id: str
    preview_path: Optional[str]
    source_video_path: str
    start_sec: float
    end_sec: float
    duration: float
    summary: str
    tags: List[str]
    status: str
    score: Optional[float]
    created_at: str


@dataclass(frozen=True)
class SearchPage:
    """Paginated search result page."""

    items: List[SearchResultItem]
    total: int
    limit: int
    offset: int
    degraded: bool = False
    diagnosis: Optional[str] = None


@dataclass(frozen=True)
class IndexHealth:
    """Health of the search index and candidate-vector coverage."""

    total_candidates: int
    indexed_in_fts: int
    vectors_available: int
    vectors_missing: int
    complete: bool
    diagnosis: str


@dataclass(frozen=True)
class RebuildReport:
    """Result from a search-index rebuild operation."""

    scanned: int
    inserted: int
    skipped: int
    errors: int
    error_details: List[str] = field(default_factory=list)
    batch_commits: int = 0
    last_candidate_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Phase 4 Task 6: Collections schema DDL
# ---------------------------------------------------------------------------

COLLECTIONS_DDL = """
CREATE TABLE IF NOT EXISTS collections (
    collection_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    search_query_json TEXT NOT NULL,
    target_count INTEGER NOT NULL,
    min_duration REAL,
    max_duration REAL,
    diversity_weight REAL NOT NULL DEFAULT 0.5,
    profile_version TEXT,
    config_id TEXT,
    current_version INTEGER NOT NULL DEFAULT 0,
    frozen INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_versions (
    collection_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    candidate_ids_json TEXT NOT NULL,
    scores_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (collection_id, version),
    FOREIGN KEY (collection_id) REFERENCES collections(collection_id)
);

CREATE TABLE IF NOT EXISTS collection_items (
    collection_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    candidate_id TEXT NOT NULL,
    score REAL,
    rank INTEGER NOT NULL,
    created_at TEXT,
    PRIMARY KEY (collection_id, version, candidate_id),
    FOREIGN KEY (collection_id) REFERENCES collections(collection_id)
);
"""


def apply_collections_schema(conn: sqlite3.Connection) -> None:
    """Create collections, collection_versions, and collection_items tables."""
    conn.executescript(COLLECTIONS_DDL)
    conn.commit()


# ---------------------------------------------------------------------------
# Phase 4 Task 6: Collection data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CollectionSpec:
    """Specification for a smart collection.

    Parameters
    ----------
    name:
        Human-readable name for the collection.
    query:
        Search query that defines the candidate pool.
    target_count:
        Desired number of candidates in the collection.
    min_duration:
        Minimum GIF duration in seconds (applied as a filter).
    max_duration:
        Maximum GIF duration in seconds.
    diversity_weight:
        Weight for farthest-first diversity (0.0 = pure score, 1.0 = pure diversity).
    profile_version:
        Optional preference profile version to apply.
    config_id:
        Optional config identifier.
    """

    name: str
    query: SearchQuery
    target_count: int
    min_duration: Optional[float] = None
    max_duration: Optional[float] = None
    diversity_weight: float = 0.5
    profile_version: Optional[str] = None
    config_id: Optional[str] = None


@dataclass(frozen=True)
class Collection:
    """A persisted smart collection."""

    collection_id: str
    spec: CollectionSpec
    current_version: int
    frozen: bool


@dataclass(frozen=True)
class CollectionVersion:
    """An immutable snapshot of a collection at a given version."""

    collection_id: str
    version: int
    candidate_ids: Tuple[str, ...]
    manifest_hash: str


@dataclass(frozen=True)
class ExportReport:
    """Result from a collection export operation."""

    manifest_path: str
    pbf_path: str
    exported: int
    missing_candidate_ids: Tuple[str, ...]

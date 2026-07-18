"""Cross-DB attention inbox -- read-only aggregation for the Workbench Today tab.

Data sources
------------
- Task database  (task_state.db)   -> ``task_failure`` items
- Library database (library.db)    -> ``migration_conflict``, ``profile_publish``, ``high_value_review``
- Quality database (quality_lab.db) -> ``champion_promotion`` items

Each source is queried independently with short-lived read connections.
The router catches connection errors per-source so that a single locked
database never causes the entire inbox to fail.
"""

import hashlib
import json as _json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import List, Literal

AttentionKind = Literal[
    "task_failure",
    "migration_conflict",
    "profile_publish",
    "high_value_review",
    "champion_promotion",
]

Severity = Literal["info", "warning", "error"]


@dataclass(frozen=True)
class AttentionItem:
    attention_id: str
    kind: AttentionKind
    severity: Severity
    title: str
    detail: str
    action_label: str
    action_target: str
    created_at: str


@dataclass
class AttentionResponse:
    items: List[AttentionItem]
    source_warnings: List[str]


# --------------------------------------------------------------------------
# Private helpers
# --------------------------------------------------------------------------

_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def _stable_id(kind: AttentionKind, key: str) -> str:
    """Deterministic, stable ID derived from kind and a source key."""
    raw = "{kind}:{key}".format(kind=kind, key=key)
    return "att_{hex}".format(
        hex=hashlib.sha256(raw.encode()).hexdigest()[:16]
    )


def _sort_key(item: AttentionItem):
    """Sort key: severity ascending (error first), then created_at descending."""
    sev = _SEVERITY_ORDER.get(item.severity, 99)
    try:
        if item.created_at:
            ts = datetime.fromisoformat(item.created_at).timestamp()
        else:
            ts = 0
    except (ValueError, TypeError):
        ts = 0
    return (sev, -ts)


def _readable_error(raw):
    """Return a human-readable error snippet from a JSON error blob."""
    if not raw:
        return "No error details recorded."
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            parsed = _json.loads(raw)
            return parsed.get("message", parsed.get("error", raw[:200]))
        except _json.JSONDecodeError:
            pass
    return raw[:200]


# --------------------------------------------------------------------------
# Per-source collectors
# --------------------------------------------------------------------------


def _task_failures(task_repo, limit: int) -> List[AttentionItem]:
    """Collect job-level and stage-level task failures."""
    items: List[AttentionItem] = []
    conn = task_repo.conn

    # Job-level failures (higher severity)
    rows = conn.execute(
        """SELECT job_id, directory, created_at
           FROM task_jobs
           WHERE status = 'needs_attention'
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    for row in rows:
        items.append(
            AttentionItem(
                attention_id=_stable_id("task_failure", "job:{jid}".format(jid=row["job_id"])),
                kind="task_failure",
                severity="error",
                title="Task job needs attention: {dir}".format(dir=row["directory"]),
                detail="Job {jid} is in 'needs_attention' status.".format(jid=row["job_id"]),
                action_label="View Job",
                action_target="/api/tasks/jobs/{jid}".format(jid=row["job_id"]),
                created_at=row["created_at"],
            )
        )

    # Stage-level failures (lower severity than job-level)
    rows = conn.execute(
        """SELECT s.stage_id, s.stage_name, s.last_error_json,
                  s.created_at, v.job_id, j.directory
           FROM task_stages s
           JOIN task_videos v ON v.video_id = s.video_id
           JOIN task_jobs j ON j.job_id = v.job_id
           WHERE s.status = 'needs_attention'
           ORDER BY s.created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    for row in rows:
        items.append(
            AttentionItem(
                attention_id=_stable_id("task_failure", "stage:{sid}".format(sid=row["stage_id"])),
                kind="task_failure",
                severity="warning",
                title="Stage '{name}' needs attention".format(name=row["stage_name"]),
                detail=_readable_error(row["last_error_json"]),
                action_label="View Job",
                action_target="/api/tasks/jobs/{jid}".format(jid=row["job_id"]),
                created_at=row["created_at"],
            )
        )

    return items


def _migration_conflicts(library_conn: sqlite3.Connection, limit: int) -> List[AttentionItem]:
    """Find duplicate SHA256 entries in the media table."""
    items: List[AttentionItem] = []
    try:
        rows = library_conn.execute(
            """SELECT sha256, COUNT(*) as cnt, GROUP_CONCAT(media_id) as media_ids,
                      MIN(created_at) as created_at
               FROM media
               WHERE sha256 IS NOT NULL
               GROUP BY sha256
               HAVING COUNT(*) > 1
               ORDER BY cnt DESC, created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return items

    for row in rows:
        cnt = row["cnt"]
        short_sha = row["sha256"][:16]
        items.append(
            AttentionItem(
                attention_id=_stable_id("migration_conflict", "sha256:{s}".format(s=row["sha256"])),
                kind="migration_conflict",
                severity="warning",
                title="Duplicate media ({cnt} copies)".format(cnt=cnt),
                detail="SHA256 {sha}... appears in {cnt} media records. "
                       "Media IDs: {ids}".format(
                           sha=short_sha, cnt=cnt, ids=row["media_ids"]
                       ),
                action_label="Resolve",
                action_target="/api/workbench/conflicts?sha256={sha}".format(sha=row["sha256"]),
                created_at=row["created_at"],
            )
        )
    return items


def _profile_publishes(library_conn: sqlite3.Connection, limit: int) -> List[AttentionItem]:
    """Collect recent preference profile publications."""
    items: List[AttentionItem] = []
    try:
        rows = library_conn.execute(
            """SELECT publication_id, profile_version, previous_profile_version,
                      published_at
               FROM preference_profile_publications
               ORDER BY published_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return items

    for row in rows:
        prev = row["previous_profile_version"]
        version = row["profile_version"]
        if prev:
            title = "Profile published: {prev} -> {v}".format(prev=prev, v=version)
            detail = "Preference profile was updated from {prev} to {v} and is now active.".format(
                prev=prev, v=version
            )
        else:
            title = "Initial profile published: {v}".format(v=version)
            detail = "The first preference profile has been published and is now active."
        items.append(
            AttentionItem(
                attention_id=_stable_id(
                    "profile_publish", "pub:{pid}".format(pid=row["publication_id"])
                ),
                kind="profile_publish",
                severity="info",
                title=title,
                detail=detail,
                action_label="View Profile",
                action_target="/api/preference/profile/{v}".format(v=version),
                created_at=row["published_at"],
            )
        )
    return items


def _high_value_reviews(library_conn: sqlite3.Connection, limit: int) -> List[AttentionItem]:
    """Collect high-scoring candidates that await review."""
    items: List[AttentionItem] = []
    try:
        rows = library_conn.execute(
            """SELECT candidate_id, source_video_path, final_score, created_at
               FROM candidate_gifs
               WHERE status = 'candidate'
                 AND final_score IS NOT NULL
                 AND final_score >= 0.7
               ORDER BY final_score DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return items

    for row in rows:
        score = row["final_score"]
        items.append(
            AttentionItem(
                attention_id=_stable_id("high_value_review", "cand:{cid}".format(cid=row["candidate_id"])),
                kind="high_value_review",
                severity="info",
                title="High-value candidate: score {s:.2f}".format(s=score),
                detail="Candidate {cid} from {path} scored {s:.2f} and awaits review.".format(
                    cid=row["candidate_id"], path=row["source_video_path"], s=score
                ),
                action_label="Review",
                action_target="/api/candidates/{cid}".format(cid=row["candidate_id"]),
                created_at=row["created_at"],
            )
        )
    return items


def _champion_promotions(quality_conn: sqlite3.Connection, limit: int) -> List[AttentionItem]:
    """Collect recent champion config promotions."""
    items: List[AttentionItem] = []
    try:
        rows = quality_conn.execute(
            """SELECT event_id, config_id, previous_config_id, scorecard_json, created_at
               FROM champion_history
               WHERE action = 'promote'
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return items

    for row in rows:
        items.append(
            AttentionItem(
                attention_id=_stable_id("champion_promotion", "event:{eid}".format(eid=row["event_id"])),
                kind="champion_promotion",
                severity="info",
                title="Champion config promoted: {cfg}".format(cfg=row["config_id"]),
                detail="Configuration {cfg} was promoted to champion.".format(cfg=row["config_id"]),
                action_label="View History",
                action_target="/api/quality/champions/history",
                created_at=row["created_at"],
            )
        )
    return items


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def list_attention_items(
    *,
    task_repo=None,
    library_conn=None,
    quality_conn=None,
    limit: int = 100,
) -> List[AttentionItem]:
    """Collect attention items from all available data sources.

    Parameters
    ----------
    task_repo : TaskRepository or None
        Repository wrapping a task-database connection. ``None`` means
        the task-database source is skipped.
    library_conn : sqlite3.Connection or None
        Connection to the main library database. ``None`` means all
        library-database sources are skipped.
    quality_conn : sqlite3.Connection or None
        Connection to the quality-lab database. ``None`` means the
        champion-promotion source is skipped.
    limit : int
        Maximum number of items to return.

    Returns
    -------
    list[AttentionItem]
        Merged, sorted list of attention items across all available sources.
    """
    all_items: List[AttentionItem] = []

    if task_repo is not None:
        try:
            all_items.extend(_task_failures(task_repo, limit))
        except sqlite3.OperationalError:
            pass

    if library_conn is not None:
        try:
            all_items.extend(_migration_conflicts(library_conn, limit))
        except sqlite3.OperationalError:
            pass
        try:
            all_items.extend(_profile_publishes(library_conn, limit))
        except sqlite3.OperationalError:
            pass
        try:
            all_items.extend(_high_value_reviews(library_conn, limit))
        except sqlite3.OperationalError:
            pass

    if quality_conn is not None:
        try:
            all_items.extend(_champion_promotions(quality_conn, limit))
        except sqlite3.OperationalError:
            pass

    # Stable sort: severity (error first), then newest first within each level
    all_items.sort(key=_sort_key)
    return all_items[:limit]

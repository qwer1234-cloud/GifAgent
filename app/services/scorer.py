"""
Preference scorer for GifAgent.

Scores individual media items against the preference index by blending:
- visual similarity (cosine distance to nearest indexed item)
- text similarity (emotional_core overlap with neighbours)
- emotional match (fraction of neighbours sharing the same emotional core)
- cluster confidence (fraction of neighbours above the review threshold)

Produces a final_score and a human-readable decision.
"""

import json
from typing import Any, Dict, List, Optional

from app.db import get_connection
from app.config import get
from app.services.indexer import get_index
from app.services.embedding import compute_media_embedding

# scoring weights and thresholds
VISUAL_W = get("scoring.visual_similarity_weight", 0.50)
TEXT_W = get("scoring.text_similarity_weight", 0.25)
EMOTION_W = get("scoring.emotional_match_weight", 0.15)
CLUSTER_W = get("scoring.cluster_confidence_weight", 0.10)
HIGH_THRESHOLD = get("scoring.high_match_threshold", 0.80)
REVIEW_THRESHOLD = get("scoring.review_threshold", 0.60)


def score_media(media_id: str) -> Optional[Dict[str, Any]]:
    """Score a single media item against the preference index.

    Returns a dict with similarity_to_saved_items, text_similarity,
    emotional_match, cluster_confidence, final_score, decision, needs_review,
    and nearest_items (top 5).

    Returns None if the media item cannot be embedded.
    """
    idx = get_index()
    if idx.count == 0:
        return {
            "media_id": media_id,
            "final_score": 0.0,
            "decision": "unknown",
            "needs_review": True,
        }

    emb = compute_media_embedding(media_id)
    if not emb:
        return None

    similar = idx.search(emb, top_k=10)

    # visual_similarity -- cosine score of the single nearest item
    visual_similarity = similar[0]["score"] if similar else 0.0

    # text_similarity -- emotional_core overlap with neighbours
    conn = get_connection()
    annotation = conn.execute(
        "SELECT emotional_core, tags_json FROM annotations WHERE media_id = ?",
        (media_id,),
    ).fetchone()

    text_similarity = 0.0
    if annotation and annotation["emotional_core"] and similar:
        query_emo = annotation["emotional_core"]
        for s in similar:
            if s.get("emotional_core") == query_emo:
                text_similarity = max(text_similarity, s["score"])

    # emotional_match -- average score of neighbours with same emotional core
    emotional_match = 0.0
    if annotation and annotation["emotional_core"]:
        emo = annotation["emotional_core"]
        matches = [s for s in similar if s.get("emotional_core") == emo]
        if matches:
            emotional_match = sum(s["score"] for s in matches) / len(matches)

    # cluster_confidence -- fraction of top-k that clear the review threshold
    k = max(len(similar), 1)
    cluster_confidence = sum(1 for s in similar if s["score"] >= REVIEW_THRESHOLD) / k

    final_score = (
        VISUAL_W * visual_similarity
        + TEXT_W * text_similarity
        + EMOTION_W * emotional_match
        + CLUSTER_W * cluster_confidence
    )

    if final_score >= HIGH_THRESHOLD:
        decision = "high_match"
        needs_review = False
    elif final_score >= REVIEW_THRESHOLD:
        decision = "maybe"
        needs_review = False
    elif final_score >= REVIEW_THRESHOLD * 0.5:
        decision = "review"
        needs_review = True
    else:
        decision = "unknown"
        needs_review = True

    return {
        "media_id": media_id,
        "similarity_to_saved_items": visual_similarity,
        "text_similarity": text_similarity,
        "emotional_match": emotional_match,
        "cluster_confidence": cluster_confidence,
        "final_score": round(final_score, 4),
        "decision": decision,
        "needs_review": needs_review,
        "nearest_items": similar[:5],
    }


def score_all_unscored() -> Dict[str, Any]:
    """Score every GIF and image in the database.

    Returns {"scored": [...], "stats": {"total": N, "scored": N, "skipped": N}}.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT media_id FROM media WHERE media_type IN ('gif', 'image')"
    ).fetchall()

    scored: List[Dict[str, Any]] = []
    stats = {"total": len(rows), "scored": 0, "skipped": 0}

    for row in rows:
        s = score_media(row["media_id"])
        if s:
            scored.append(s)
            stats["scored"] += 1
        else:
            stats["skipped"] += 1

    return {"scored": scored, "stats": stats}

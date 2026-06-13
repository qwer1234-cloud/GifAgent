#!/usr/bin/env python3
"""
Phase 4-5: Inherit cluster labels to non-representatives, then build FAISS index.

Phase 4: For each cluster, copy representative annotations to non-representative members.
         Uses averaged tags/emotional_core from all representatives in the cluster.
Phase 5: Build FAISS index from all annotated media.
"""
import sys, json, uuid
from datetime import datetime, timezone

sys.path.insert(0, '.')
from app.db import init_db, get_connection, save_checkpoint, load_checkpoint
from app.config import load_config
from app.services.indexer import get_index, index_all_annotated
from app.services.scorer import score_all_unscored

load_config()
init_db()

# ── Phase 4: Inherit labels ───────────────────────────────────────────
print("=" * 60)
print("Phase 4: Inherit cluster labels")
print("=" * 60)

conn = get_connection()

# Find all clusters that have at least one annotated representative
clusters = conn.execute("""
    SELECT DISTINCT m.cluster_id
    FROM media m
    INNER JOIN annotations a ON m.media_id = a.media_id
    WHERE m.is_representative = 1 AND m.cluster_id IS NOT NULL
""").fetchall()

print(f"  Clusters with annotated representatives: {len(clusters)}")

total_inherited = 0
for (cid,) in clusters:
    # Get all representative annotations for this cluster
    rep_anns = conn.execute("""
        SELECT a.summary, a.emotional_core, a.aesthetic_notes_json, a.why_i_like_it, a.tags_json
        FROM annotations a
        INNER JOIN media m ON a.media_id = m.media_id
        WHERE m.cluster_id = ? AND m.is_representative = 1
    """, (cid,)).fetchall()

    if not rep_anns:
        continue

    # Aggregate representative tags (take union of all tags)
    all_tags = set()
    all_emotions = []
    all_notes = []
    all_summaries = []
    for ra in rep_anns:
        if ra["tags_json"]:
            try:
                all_tags.update(json.loads(ra["tags_json"]))
            except json.JSONDecodeError:
                pass
        if ra["emotional_core"]:
            all_emotions.append(ra["emotional_core"])
        if ra["aesthetic_notes_json"]:
            try:
                all_notes.extend(json.loads(ra["aesthetic_notes_json"]))
            except json.JSONDecodeError:
                pass
        if ra["summary"]:
            all_summaries.append(ra["summary"])

    # Pick dominant emotion
    dominant_emotion = max(set(all_emotions), key=all_emotions.count) if all_emotions else "unknown"
    # Deduplicate notes, keep top 4 by length (more detailed)
    unique_notes = list(dict.fromkeys(all_notes))[:4]
    # Combined summary
    combined_summary = " | ".join(all_summaries[:2]) if all_summaries else f"Cluster {cid}"

    # Get non-representatives in this cluster without annotations
    members = conn.execute("""
        SELECT m.media_id FROM media m
        WHERE m.cluster_id = ? AND m.is_representative = 0
          AND m.media_id NOT IN (SELECT media_id FROM annotations)
    """, (cid,)).fetchall()

    now = datetime.now(timezone.utc).isoformat()
    for (mid,) in members:
        ann_id = f"ann_{uuid.uuid4().hex[:12]}"
        conn.execute("""
            INSERT INTO annotations (annotation_id, media_id, model_name, summary,
                emotional_core, aesthetic_notes_json, why_i_like_it, tags_json, raw_json, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            ann_id, mid, "cluster_inherit",
            f"[Inherited from cluster {cid}] {combined_summary}",
            dominant_emotion,
            json.dumps(unique_notes, ensure_ascii=False),
            f"Inherited from {len(rep_anns)} representative(s) in cluster {cid}",
            json.dumps(list(all_tags), ensure_ascii=False),
            json.dumps({"inherited": True, "source_cluster": cid, "source_reps": len(rep_anns)}),
            now,
        ))
        total_inherited += 1

conn.commit()
print(f"  Labels inherited to: {total_inherited} non-representative GIFs")

# Show distribution
annotated_total = conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0]
media_total = conn.execute("SELECT COUNT(*) FROM media WHERE media_type='gif'").fetchone()[0]
print(f"  Coverage: {annotated_total}/{media_total} GIFs annotated ({annotated_total/media_total*100:.1f}%)")

# ── Phase 5: Build FAISS index ────────────────────────────────────────
print("\n" + "=" * 60)
print("Phase 5: Build FAISS index")
print("=" * 60)

from app.services.embedding import compute_media_embedding, compute_text_summary_embedding

# Index all annotated media that aren't already indexed
idx = get_index()
rows = conn.execute("""
    SELECT DISTINCT m.media_id
    FROM media m
    INNER JOIN annotations a ON m.media_id = a.media_id
    WHERE m.media_id NOT IN (SELECT owner_id FROM vector_refs WHERE vector_type='media_global')
""").fetchall()

print(f"  Media to index: {len(rows)}")

indexed = 0
failed = 0
for i, (mid,) in enumerate(rows):
    try:
        emb = compute_media_embedding(mid)
        if emb is None:
            # Fallback to text embedding
            emb = compute_text_summary_embedding(mid)
        if emb:
            idx.add(emb, mid, "media_global")
            indexed += 1
            if (i + 1) % 100 == 0:
                print(f"  [{i+1}/{len(rows)}] indexed...")
        else:
            failed += 1
    except Exception as e:
        failed += 1
        if failed <= 5:
            print(f"  Index failed for {mid}: {e}")

print(f"  Indexed: {indexed}, Failed: {failed}")
print(f"  FAISS index size: {idx.count} vectors")

# Quick sanity check: search for a random indexed item
if indexed > 0:
    test_mid = rows[0][0]
    emb = compute_media_embedding(test_mid)
    if emb:
        results = idx.search(emb, top_k=3)
        print(f"\n  Sanity check - nearest to {test_mid}:")
        for r in results:
            print(f"    {r['media_id'][:14]} score={r['score']:.3f} film={r.get('film','?')} emotion={r.get('emotional_core','?')}")

print("\n" + "=" * 60)
print("Phase 4-5 complete!")
print(f"  Annotated: {annotated_total} GIFs")
print(f"  Indexed: {idx.count} vectors")
print("=" * 60)

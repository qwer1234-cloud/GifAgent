#!/usr/bin/env python3
"""
Phase 0-2: Complete media scan, pHash clustering, and representative selection.

Steps:
  0. Resume scan_and_register for remaining files
  1. Load all pHash values from media table
  2. Cluster by Hamming distance (greedy clustering)
  3. Select 2-3 representatives per cluster (highest frame_count)
  4. Write cluster_id and is_representative to media table
"""
import sys, uuid
sys.path.insert(0, '.')

from app.db import init_db, get_connection
from app.config import load_config
from app.services.scanner import scan_and_register
import imagehash

load_config()
init_db()

# ── Phase 0: Resume scan ──────────────────────────────────────────────
print("=" * 60)
print("Phase 0: Complete media scan")
print("=" * 60)

def safe_print_progress(i, total, path, status):
    try:
        print(f"  [{i+1}/{total}] {status}: {path}")
    except UnicodeEncodeError:
        print(f"  [{i+1}/{total}] {status}: <unicode filename>")

stats = scan_and_register(
    "E:/data/originals",
    progress_callback=safe_print_progress
)
print(f"  Registered: {stats['registered']}, Skipped SHA256: {stats['skipped_sha256']}, "
      f"Skipped pHash: {stats['skipped_phash']}")

# ── Phase 1: pHash clustering ─────────────────────────────────────────
print("\n" + "=" * 60)
print("Phase 1: pHash clustering")
print("=" * 60)

conn = get_connection()
rows = conn.execute(
    "SELECT media_id, phash, frame_count FROM media WHERE phash IS NOT NULL AND media_type='gif'"
).fetchall()
print(f"  GIFs with pHash: {len(rows)}")

# Greedy clustering: iterate items, assign to nearest cluster or create new one
THRESHOLD = 5  # Hamming distance threshold for same cluster
clusters = {}  # cluster_id -> {"center": phash_obj, "members": [media_id, ...]}

for row in rows:
    phash_obj = imagehash.hex_to_hash(row["phash"])
    assigned = False
    for cid, cdata in clusters.items():
        if abs(phash_obj - cdata["center"]) <= THRESHOLD:
            cdata["members"].append((row["media_id"], row["frame_count"] or 0))
            assigned = True
            break
    if not assigned:
        # New cluster
        cid = f"cluster_{uuid.uuid4().hex[:8]}"
        clusters[cid] = {
            "center": phash_obj,
            "members": [(row["media_id"], row["frame_count"] or 0)],
        }

print(f"  Clusters formed: {len(clusters)}")
# Show cluster size distribution
sizes = sorted([len(c["members"]) for c in clusters.values()], reverse=True)
print(f"  Largest cluster: {sizes[0]}, Smallest: {sizes[-1]}")
print(f"  Clusters with 1 item: {sum(1 for s in sizes if s == 1)}")

# ── Phase 2: Select representatives ───────────────────────────────────
print("\n" + "=" * 60)
print("Phase 2: Select representatives")
print("=" * 60)

total_reps = 0
for cid, cdata in clusters.items():
    # Sort by frame_count descending (more dynamic = better representative)
    members_sorted = sorted(cdata["members"], key=lambda x: x[1], reverse=True)
    n_reps = min(3, max(2, len(members_sorted)))  # 2-3 reps per cluster

    rep_ids = set()
    # Always pick the highest frame_count
    rep_ids.add(members_sorted[0][0])
    # Pick evenly spaced from the rest
    if n_reps > 1 and len(members_sorted) > 1:
        step = max(1, (len(members_sorted) - 1) // (n_reps - 1))
        for j in range(1, min(n_reps, len(members_sorted))):
            idx = min(j * step, len(members_sorted) - 1)
            rep_ids.add(members_sorted[idx][0])

    # Update all members
    for mid, fc in cdata["members"]:
        is_rep = 1 if mid in rep_ids else 0
        conn.execute(
            "UPDATE media SET cluster_id=?, is_representative=? WHERE media_id=?",
            (cid, is_rep, mid),
        )
    total_reps += len(rep_ids)

conn.commit()
print(f"  Total representatives: {total_reps}")
print(f"  Non-representatives (inherit labels): {len(rows) - total_reps}")
print(f"  Clusters: {len(clusters)}")

# Show sample cluster
sample_cid = list(clusters.keys())[0]
sample = conn.execute(
    "SELECT media_id, file_path, is_representative FROM media WHERE cluster_id=?",
    (sample_cid,)
).fetchall()
print(f"\n  Sample cluster {sample_cid}:")
for s in sample:
    tag = "[REP]" if s["is_representative"] else "[MEM]"
    name = s["file_path"].split("/")[-1][:60]
    print(f"    {tag} {s['media_id']}  {name}")

print("\n" + "=" * 60)
print("Phase 0-2 complete! Representatives ready for annotation.")
print("=" * 60)

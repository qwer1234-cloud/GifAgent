#!/usr/bin/env python3
"""Full indexing pipeline: scan -> preprocess -> annotate -> index -> score."""
import sys
sys.path.insert(0, '.')

from app.db import init_db
from app.config import load_config
from app.services.scanner import scan_and_register
from app.services.preprocess import preprocess_all
from app.services.scheduler import ModelScheduler
from app.services.indexer import index_all_annotated, get_index
from app.services.scorer import score_all_unscored


def main():
    load_config()
    init_db()

    print("=== Phase 1: Scan and register ===")
    stats = scan_and_register("E:/data/originals",
        progress_callback=lambda i, t, p, s: print(f"  [{i+1}/{t}] {s}: {p}"))
    print(f"  Registered: {stats['registered']}, Skipped SHA256: {stats['skipped_sha256']}, Skipped pHash: {stats['skipped_phash']}")

    print("\n=== Phase 2: Extract frames ===")
    pp_stats = preprocess_all(
        progress_callback=lambda i, t, mid, s: print(f"  [{i+1}/{t}] {s}: {mid}"))
    print(f"  Processed: {pp_stats['processed']}, Frames: {pp_stats['frames_extracted']}, Failed: {pp_stats['failed']}")

    print("\n=== Phase 3: VLM analysis + LLM synthesis ===")
    scheduler = ModelScheduler()
    proc_stats = scheduler.process_pending_frames(
        progress_callback=lambda i, t, phase: print(f"  [{i}/{t}] {phase}"))
    print(f"  Processed: {proc_stats.get('processed', 0)}, Failed: {proc_stats.get('failed', 0)}")

    print("\n=== Phase 4: Build FAISS index ===")
    idx_stats = index_all_annotated()
    print(f"  Indexed: {idx_stats['indexed']}, Total: {idx_stats['total']}, Failed: {idx_stats['failed']}")

    print("\n=== Phase 5: Score all ===")
    score_result = score_all_unscored()
    print(f"  Scored: {score_result['stats']['scored']}, Skipped: {score_result['stats']['skipped']}")

    print(f"\n=== Pipeline complete. Index has {get_index().count} vectors. ===")


if __name__ == "__main__":
    main()

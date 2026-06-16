#!/usr/bin/env python3
"""
Reset all VLM/LLM-derived quality data, preserving raw media, frames, and feedback.

Usage:
  uv run python scripts/reset_derived_quality_data.py --dry-run
  uv run python scripts/reset_derived_quality_data.py --apply
"""
import sys, os, shutil, argparse
from datetime import datetime

sys.path.insert(0, '.')
from app.db import init_db, get_connection
from app.config import get

BACKUP_DIR = "data/backups"
FAISS_DIR = get("paths.faiss_dir", "data/faiss")

def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    init_db()
    conn = get_connection()

    # Pre-state
    fa_count = conn.execute("SELECT COUNT(*) FROM frame_annotations").fetchone()[0]
    a_count = conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0]
    vr_count = conn.execute("SELECT COUNT(*) FROM vector_refs").fetchone()[0]
    ch_count = conn.execute("SELECT COUNT(*) FROM processing_checkpoint").fetchone()[0]
    fb_count = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    m_count = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
    fr_count = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    fr_done = conn.execute("SELECT COUNT(*) FROM frames WHERE vlm_status!='pending'").fetchone()[0]

    faiss_files = [
        os.path.join(FAISS_DIR, "media_index.faiss"),
        os.path.join(FAISS_DIR, "id_map.json"),
    ]

    print(f"=== Derived Quality Data Reset ===")
    print(f"  frame_annotations:     {fa_count} rows")
    print(f"  annotations:           {a_count} rows")
    print(f"  vector_refs:           {vr_count} rows")
    print(f"  processing_checkpoint: {ch_count} rows")
    print(f"  frames to reset:       {fr_done}/{fr_count} (done/failed → pending)")
    print(f"  FAISS files to delete: {len(faiss_files)}")
    print(f"  --- Preserved ---")
    print(f"  media:                 {m_count} rows (untouched)")
    print(f"  feedback:              {fb_count} rows (untouched)")

    if args.dry_run:
        print(f"\nThis was a DRY RUN. No changes made.")
        print(f"Pass --apply to execute.")
        return

    # --apply: backup first
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    db_path = get("database.path", "data/library.db")
    backup_path = os.path.join(BACKUP_DIR, f"library.before-quality-reset.{ts}.db")
    shutil.copy2(db_path, backup_path)
    print(f"\nBackup saved: {backup_path}")

    # Delete derived data
    conn.execute("DELETE FROM vector_refs")
    conn.execute("DELETE FROM annotations")
    conn.execute("DELETE FROM frame_annotations")
    conn.execute("DELETE FROM processing_checkpoint")
    conn.execute("UPDATE frames SET vlm_status='pending'")
    conn.commit()

    # Delete FAISS files
    for fp in faiss_files:
        if os.path.exists(fp):
            os.remove(fp)

    # Post-state
    print(f"\n=== Post-reset ===")
    print(f"  frame_annotations:     0")
    print(f"  annotations:           0")
    print(f"  vector_refs:           0")
    print(f"  processing_checkpoint: 0")
    print(f"  frames pending:        {conn.execute('SELECT COUNT(*) FROM frames WHERE vlm_status=\"pending\"').fetchone()[0]}")
    print(f"  feedback:              {conn.execute('SELECT COUNT(*) FROM feedback').fetchone()[0]}")
    print(f"  media:                 {conn.execute('SELECT COUNT(*) FROM media').fetchone()[0]}")
    print(f"\nReset complete. Run 'uv run python scripts/pipeline.py annotate-frames --limit 50' to verify.")

if __name__ == "__main__":
    main()

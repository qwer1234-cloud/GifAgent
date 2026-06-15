#!/usr/bin/env python3
"""Continuous VLM batch runner - runs vlm_quick_200.py repeatedly until all frames done."""
import subprocess
import sys
import time
import os

sys.path.insert(0, '.')
from app.db import init_db, get_connection

init_db()

batch = 0
while True:
    conn = get_connection()
    pending = conn.execute("SELECT COUNT(*) FROM frames WHERE vlm_status='pending'").fetchone()[0]
    conn.close()

    if pending == 0:
        print(f"\nAll frames processed! Total batches: {batch}")
        break

    batch += 1
    print(f"\n=== Batch {batch}: {pending} frames remaining ===")
    print(f"Starting at: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    result = subprocess.run(
        [sys.executable, "-u", "scripts/vlm_quick_200.py"],
        capture_output=False,
        timeout=1200,  # 20 min max per batch
    )

    if result.returncode != 0:
        print(f"Batch {batch} failed with code {result.returncode}, waiting 30s...")
        time.sleep(30)
    else:
        print(f"Batch {batch} completed at: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # Small pause between batches
    time.sleep(2)

print("Continuous VLM processing complete!")

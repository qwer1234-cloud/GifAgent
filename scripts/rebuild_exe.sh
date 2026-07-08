#!/usr/bin/env bash
# Rebuild the GifAgentUI exe while preserving dist/GifAgentUI/data/.
#
# Default `rm -rf dist/GifAgentUI` wipes the entire bundle including the
# runtime DB, FAISS index, and exports junction - which can lose data
# (e.g. a real `tushy/` folder under data/exports/adaptive_test/ that
# wasn't a junction target).
#
# This script:
# 1. Kills any running GifAgentUI.exe (file locks)
# 2. Backs up dist/GifAgentUI/data -> dist/data_backup_<timestamp>
# 3. Removes the rest of dist/GifAgentUI
# 4. Runs PyInstaller
# 5. Restores data/ back into the new bundle
# 6. Verifies the rebuild
#
# Usage:
#   bash scripts/rebuild_exe.sh
#   bash scripts/rebuild_exe.sh --no-backup   # skip backup (destructive)

set -euo pipefail

cd "$(dirname "$0")/.."

EXE_DIR="dist/GifAgentUI"
DATA_DIR="$EXE_DIR/data"
SPEC="build_exe.spec"

# ── 1. Kill running exe (file locks prevent clean rebuild) ──────────────
echo "=== Stopping running GifAgentUI.exe ==="
if taskkill //F //IM GifAgentUI.exe 2>/dev/null; then
  sleep 2
  echo "Killed."
else
  echo "Not running."
fi

# ── 2. Backup data/ if it exists and has real content ────────────────────
BACKUP_DIR=""
if [[ -d "$DATA_DIR" ]]; then
  if [[ "${1:-}" == "--no-backup" ]]; then
    echo "=== --no-backup: deleting $DATA_DIR ==="
    rm -rf "$DATA_DIR"
  else
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    BACKUP_DIR="dist/data_backup_$TIMESTAMP"
    echo "=== Backing up $DATA_DIR -> $BACKUP_DIR ==="
    mv "$DATA_DIR" "$BACKUP_DIR"
    echo "Backup saved. Restore manually if rebuild fails: mv $BACKUP_DIR $DATA_DIR"
  fi
fi

# ── 3. Remove old bundle (data/ already moved/deleted above) ────────────
echo "=== Removing $EXE_DIR ==="
rm -rf "$EXE_DIR"

# ── 4. Build ─────────────────────────────────────────────────────────────
echo "=== Running PyInstaller ==="
uv run pyinstaller "$SPEC" --noconfirm

# ── 5. Restore data/ if we backed it up ──────────────────────────────────
if [[ -n "$BACKUP_DIR" && -d "$BACKUP_DIR" ]]; then
  if [[ -d "$DATA_DIR" ]]; then
    # New build created a fresh data/ (e.g. junctions from launcher haven't
    # run yet, but PyInstaller may have written configs). Merge carefully.
    echo "=== New build has data/ - merging backup ==="
    # Copy any non-existing files from backup into new data/
    cp -rn "$BACKUP_DIR/." "$DATA_DIR/" 2>/dev/null || true
    echo "Merged. Backup kept at $BACKUP_DIR (delete manually if all good)."
  else
    echo "=== Restoring data/ from backup ==="
    mv "$BACKUP_DIR" "$DATA_DIR"
    echo "Restored."
  fi
fi

# ── 6. Verify ────────────────────────────────────────────────────────────
echo "=== Verification ==="
if [[ -f "$EXE_DIR/GifAgentUI.exe" ]]; then
  echo "✓ exe exists: $EXE_DIR/GifAgentUI.exe"
else
  echo "✗ FAIL: exe not found"
  exit 1
fi

if [[ -d "$DATA_DIR" ]]; then
  echo "✓ data/ preserved: $DATA_DIR"
  # Show what's in data/
  ls "$DATA_DIR" 2>/dev/null | head -10 | sed 's/^/  /'
else
  echo "✓ data/ will be created on first run (junctions auto-linked)"
fi

echo ""
echo "=== Rebuild complete ==="
echo "Run: ./$EXE_DIR/GifAgentUI.exe"

#!/usr/bin/env bash
# Rebuild the GifAgentUI exe while preserving user-owned runtime state.
#
# Default `rm -rf dist/GifAgentUI` wipes the entire bundle including the
# runtime DB, FAISS index, and exports junction - which can lose data
# (e.g. a real `tushy/` folder under data/exports/adaptive_test/ that
# wasn't a junction target).
#
# This script:
# 1. Kills any running GifAgentUI.exe (file locks)
# 2. Backs up dist/GifAgentUI/data and dist/GifAgentUI/configs
# 3. Removes the rest of dist/GifAgentUI
# 4. Runs PyInstaller
# 5. Restores data/ and the writable configs/ back into the new bundle
# 6. Verifies the rebuild
#
# Usage:
#   bash scripts/rebuild_exe.sh
#   bash scripts/rebuild_exe.sh --no-backup   # skip backup (destructive)

set -euo pipefail

cd "$(dirname "$0")/.."

EXE_DIR="dist/GifAgentUI"
DATA_DIR="$EXE_DIR/data"
CONFIG_DIR="$EXE_DIR/configs"
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
CONFIG_BACKUP_DIR=""
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

# The packaged launcher copies its bundled default to configs/models.yaml only
# when the writable file does not exist.  Preserve that writable directory so
# rebuilding never resets settings edited through the UI.
if [[ -d "$CONFIG_DIR" ]]; then
  if [[ "${1:-}" == "--no-backup" ]]; then
    echo "=== --no-backup: deleting $CONFIG_DIR ==="
    rm -rf "$CONFIG_DIR"
  else
    TIMESTAMP=${TIMESTAMP:-$(date +%Y%m%d_%H%M%S)}
    CONFIG_BACKUP_DIR="dist/config_backup_$TIMESTAMP"
    echo "=== Backing up $CONFIG_DIR -> $CONFIG_BACKUP_DIR ==="
    mv "$CONFIG_DIR" "$CONFIG_BACKUP_DIR"
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

if [[ -n "$CONFIG_BACKUP_DIR" && -d "$CONFIG_BACKUP_DIR" ]]; then
  if [[ -d "$CONFIG_DIR" ]]; then
    echo "=== New build has configs/ - restoring writable config ==="
    cp -rf "$CONFIG_BACKUP_DIR/." "$CONFIG_DIR/"
    rm -rf "$CONFIG_BACKUP_DIR"
  else
    echo "=== Restoring writable configs/ from backup ==="
    mv "$CONFIG_BACKUP_DIR" "$CONFIG_DIR"
  fi
  echo "Writable configuration restored."
fi

# ── 6. Verify ────────────────────────────────────────────────────────────
echo "=== Verification ==="
if [[ -f "$EXE_DIR/GifAgentUI.exe" ]]; then
  echo "✓ exe exists: $EXE_DIR/GifAgentUI.exe"
else
  echo "✗ FAIL: exe not found"
  exit 1
fi

# Verify that the bundled adaptive exporter is from the current source.  An
# EXE-only replacement can leave a stale _internal/scripts copy behind, which
# silently restores the legacy seconds-based filename format.
RUNTIME_ADAPTIVE="$EXE_DIR/_internal/scripts/test_video_adaptive.py"
if [[ -f "$RUNTIME_ADAPTIVE" ]] && grep -Fq "from app.services.gif_naming import build_gif_filename" "$RUNTIME_ADAPTIVE"; then
  echo "✓ bundled adaptive exporter uses millisecond filenames"
else
  echo "✗ FAIL: bundled adaptive exporter is missing millisecond naming code"
  exit 1
fi

if [[ -d "$DATA_DIR" ]]; then
  echo "✓ data/ preserved: $DATA_DIR"
  # Show what's in data/
  ls "$DATA_DIR" 2>/dev/null | head -10 | sed 's/^/  /'
else
  echo "✓ data/ will be created on first run (junctions auto-linked)"
fi

if [[ -f "$CONFIG_DIR/models.yaml" ]]; then
  echo "鉁?user configuration preserved: $CONFIG_DIR/models.yaml"
else
  echo "鉁?bundled defaults will initialize configs/models.yaml on first run"
fi

echo ""
echo "=== Rebuild complete ==="
echo "Run: ./$EXE_DIR/GifAgentUI.exe"

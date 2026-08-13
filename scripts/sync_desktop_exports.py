#!/usr/bin/env python3
"""One-time/manual Favorite-GIF + PBF desktop synchronization.

Usage:
  uv run python scripts/sync_desktop_exports.py
  uv run python scripts/sync_desktop_exports.py --source-root <dir> --json

All roots also respect the GIFAGENT_* environment overrides documented in
README.md / Agent.md.  The command exits nonzero only for top-level fatal
failures; per-file missing/conflict entries are visible in the report.
"""
import sys

sys.path.insert(0, ".")

from app.services.desktop_export_sync import main


if __name__ == "__main__":
    raise SystemExit(main())

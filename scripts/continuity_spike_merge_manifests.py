"""Merge the 0B video and GIF manifests into one spike input manifest."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    parts = [Path(p) for p in sys.argv[1:-1]]
    output = Path(sys.argv[-1])
    candidates: list[dict] = []
    seen: set[str] = set()
    for part in parts:
        payload = json.loads(part.read_text(encoding="utf-8"))
        for candidate in payload["candidates"]:
            if candidate["candidate_id"] in seen:
                raise SystemExit(f"duplicate candidate_id: {candidate['candidate_id']}")
            seen.add(candidate["candidate_id"])
            candidates.append(candidate)
    output.write_text(
        json.dumps(
            {"schema_version": "continuity-spike-input-v1", "candidates": candidates},
            indent=1,
        ),
        encoding="utf-8",
    )
    videos = sum(1 for c in candidates if c["media_kind"] == "source_video")
    gifs = len(candidates) - videos
    print(f"wrote {output}: {videos} source videos + {gifs} historical GIFs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

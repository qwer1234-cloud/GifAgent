from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def collect_status(*, library_db_path: Path, faiss_manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(faiss_manifest_path.read_text(encoding="utf-8"))
    return {
        "library_db_path": str(library_db_path),
        "library_db_exists": library_db_path.exists(),
        "preference_memory_enabled": False,
        "production_write_allowed": False,
        "embedding_model": manifest["embedding_model"],
        "embedding_dim": int(manifest["dim"]),
        "vector_count": int(manifest.get("vector_count", 0)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--library-db", default="data/library.db")
    status.add_argument("--faiss-manifest", default="data/faiss/manifest.json")
    status.add_argument("--json", action="store_true")
    args = parser.parse_args()

    payload = collect_status(
        library_db_path=Path(args.library_db),
        faiss_manifest_path=Path(args.faiss_manifest),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

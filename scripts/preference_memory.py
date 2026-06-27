import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _build_safe_defaults(
    *, library_db_path: Path
) -> dict[str, Any]:
    """Return status payload with safe defaults when the manifest is missing/invalid."""
    return {
        "library_db_path": str(library_db_path),
        "library_db_exists": library_db_path.exists(),
        "wal_file_exists": False,
        "preference_memory_enabled": False,
        "production_write_allowed": False,
        "embedding_model": None,
        "embedding_dim": None,
        "vector_count": 0,
        "manifest_error": None,
    }


def collect_status(*, library_db_path: Path, faiss_manifest_path: Path) -> dict[str, Any]:
    try:
        from app.config import get as config_get
    except ImportError:
        config_get = lambda key, default=None: default

    preference_enabled = bool(config_get("preference_memory.enabled", False))

    wal_path = library_db_path.with_name(library_db_path.name + "-wal")
    wal_file_exists = wal_path.exists()
    production_write_allowed = not wal_file_exists

    manifest_error = None
    embedding_model = None
    embedding_dim = None
    vector_count = 0

    try:
        manifest = json.loads(faiss_manifest_path.read_text(encoding="utf-8"))
        embedding_model = manifest.get("embedding_model")
        embedding_dim = int(manifest.get("dim", 0))
        vector_count = int(manifest.get("vector_count", 0))
    except FileNotFoundError:
        manifest_error = f"manifest not found: {faiss_manifest_path}"
    except json.JSONDecodeError as exc:
        manifest_error = f"invalid manifest JSON: {exc}"
    except Exception as exc:
        manifest_error = f"manifest error: {exc}"

    return {
        "library_db_path": str(library_db_path),
        "library_db_exists": library_db_path.exists(),
        "wal_file_exists": wal_file_exists,
        "preference_memory_enabled": preference_enabled,
        "production_write_allowed": production_write_allowed,
        "embedding_model": embedding_model,
        "embedding_dim": embedding_dim,
        "vector_count": vector_count,
        "manifest_error": manifest_error,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--library-db", default="data/library.db")
    status.add_argument("--faiss-manifest", default="data/faiss/manifest.json")
    status.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    payload = collect_status(
        library_db_path=Path(args.library_db),
        faiss_manifest_path=Path(args.faiss_manifest),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

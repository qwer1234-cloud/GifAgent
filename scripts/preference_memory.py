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


def cmd_build(args: argparse.Namespace) -> int:
    """Trigger a preference profile build."""
    from app.db import get_connection
    from app.services.preference_schema import apply_preference_schema
    from app.services.preference_memory import PreferenceMemoryService

    conn = get_connection()
    apply_preference_schema(conn)

    service = PreferenceMemoryService(conn)
    result = service.build_profile(dry_run=args.dry_run)

    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if result["status"] == "built" else 1


def cmd_publish(args: argparse.Namespace) -> int:
    """Publish a completed profile build as the current active profile."""
    from app.db import get_connection
    from app.services.preference_schema import apply_preference_schema
    from app.services.preference_memory import PreferenceMemoryService

    conn = get_connection()
    apply_preference_schema(conn)

    service = PreferenceMemoryService(conn)
    try:
        service.publish(args.profile_version)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1

    print(
        json.dumps(
            {"status": "published", "profile_version": args.profile_version},
            ensure_ascii=False,
            indent=2 if args.pretty else None,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    # status subcommand (existing)
    status = sub.add_parser("status")
    status.add_argument("--library-db", default="data/library.db")
    status.add_argument("--faiss-manifest", default="data/faiss/manifest.json")
    status.add_argument("--pretty", action="store_true")

    # build subcommand
    build = sub.add_parser("build", help="Build an immutable preference profile")
    build.add_argument("--dry-run", action="store_true", help="Validate gates without persisting")
    build.add_argument("--library-db", default="data/library.db")
    build.add_argument("--pretty", action="store_true")

    # publish subcommand
    publish = sub.add_parser("publish", help="Publish a completed profile as current")
    publish.add_argument("--profile-version", required=True, help="Profile version to publish")
    publish.add_argument("--library-db", default="data/library.db")
    publish.add_argument("--pretty", action="store_true")

    args = parser.parse_args()

    if args.command == "status":
        payload = collect_status(
            library_db_path=Path(args.library_db),
            faiss_manifest_path=Path(args.faiss_manifest),
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))
        return 0
    elif args.command == "build":
        return cmd_build(args)
    elif args.command == "publish":
        return cmd_publish(args)
    else:
        print(json.dumps({"error": f"unknown command: {args.command}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

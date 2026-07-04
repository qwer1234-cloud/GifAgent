"""
Launcher — starts FastAPI backend + Gradio UI in one process.
Use as the PyInstaller entry point, or run directly:

    uv run python app/ui/launcher.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
import shutil

import uvicorn


def _setup_runtime_files(exe_dir):
    """Copy bundled read-only config to a writable location, create data dir."""
    writable_config_dir = os.path.join(exe_dir, "configs")
    writable_config = os.path.join(writable_config_dir, "models.yaml")

    if getattr(sys, "frozen", False):
        bundled = os.path.join(sys._MEIPASS, "configs", "models.yaml")
        if not os.path.exists(writable_config) and os.path.exists(bundled):
            os.makedirs(writable_config_dir, exist_ok=True)
            shutil.copy2(bundled, writable_config)
            print(f"Copied default config to {writable_config}")

    os.makedirs(writable_config_dir, exist_ok=True)
    data_dir = os.path.join(exe_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(os.path.join(data_dir, "faiss"), exist_ok=True)

    # If exe is inside the project (dist/GifAgentUI/), link to project's data
    # so the user sees existing candidates/exports without a 70GB copy.
    project_data = os.path.normpath(os.path.join(exe_dir, "..", "..", "data"))
    if os.path.exists(os.path.join(project_data, "library.db")):
        _link_data_files(exe_dir, data_dir, project_data)


def _link_data_files(exe_dir, data_dir, project_data):
    """Copy small data files (DB, FAISS) and junction large dirs (exports)."""
    import subprocess

    # Copy library.db if missing
    exe_db = os.path.join(data_dir, "library.db")
    src_db = os.path.join(project_data, "library.db")
    if not os.path.exists(exe_db) and os.path.exists(src_db):
        shutil.copy2(src_db, exe_db)
        print(f"Copied library.db ({os.path.getsize(src_db) // 1024 // 1024}MB)")

    # Copy FAISS index if missing
    src_faiss = os.path.join(project_data, "faiss")
    exe_faiss = os.path.join(data_dir, "faiss")
    if os.path.isdir(src_faiss) and not os.listdir(exe_faiss):
        for f in os.listdir(src_faiss):
            shutil.copy2(os.path.join(src_faiss, f), os.path.join(exe_faiss, f))
        print(f"Copied FAISS index ({len(os.listdir(src_faiss))} files)")

    # Junction exports (70GB — too large to copy)
    exe_exports = os.path.join(data_dir, "exports")
    src_exports = os.path.join(project_data, "exports")
    if os.path.isdir(src_exports) and not os.path.exists(exe_exports):
        r = subprocess.run(["cmd", "/c", "mklink", "/J", exe_exports, src_exports],
                           capture_output=True, text=True)
        if r.returncode == 0:
            print(f"Junction: {exe_exports} -> {src_exports}")
        else:
            print(f"WARNING: could not create exports junction: {r.stderr.strip()}")


def _init_database():
    """Load config and initialize DB with all schemas (base + preference memory)."""
    from app.config import load_config
    load_config()

    from app.db import init_db
    init_db(apply_preference=True)

    # Explicitly apply preference schema in case init_db's lazy import was missed
    from app.services.preference_schema import apply_preference_schema
    from app.db import get_connection
    conn = get_connection()
    apply_preference_schema(conn)
    conn.close()
    print("Database initialized with preference schema.")


def start_api_server():
    """Run uvicorn in a background thread (daemon). Import app object directly
    to avoid string-based import which fails in PyInstaller frozen exe."""
    from app.main import app as fastapi_app
    uvicorn.run(
        fastapi_app,
        host="127.0.0.1",
        port=8000,
        log_level="warning",
        access_log=False,
    )


def main():
    # Determine exe/project dir and chdir FIRST so all relative paths resolve correctly
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
    else:
        exe_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.chdir(exe_dir)
    print(f"Working dir: {exe_dir}")

    # Copy config, create data dir
    _setup_runtime_files(exe_dir)

    # Init DB (config must be loadable now that CWD is set)
    try:
        _init_database()
    except Exception as e:
        print(f"WARNING: DB init failed: {e}")

    # Start API server in background thread
    api_thread = threading.Thread(target=start_api_server, daemon=True)
    api_thread.start()
    print("Starting FastAPI on http://127.0.0.1:8000 ...")

    # Wait for API to be ready (max 30s)
    import httpx
    for _ in range(30):
        try:
            r = httpx.get("http://127.0.0.1:8000/api/status", timeout=2)
            if r.status_code == 200:
                print("API ready.")
                break
        except Exception:
            time.sleep(1)
    else:
        print("WARNING: API did not become ready in 30s, UI may not work properly.")

    # Open browser after a short delay (let Gradio start)
    def _open_browser():
        time.sleep(3)
        webbrowser.open("http://127.0.0.1:7861")
    threading.Thread(target=_open_browser, daemon=True).start()

    # Start Gradio UI (blocks)
    from app.ui.candidate_review import app as gradio_app
    gradio_app.launch(
        server_name="127.0.0.1",
        server_port=7861,
        prevent_thread_lock=False,
        allowed_paths=["data/exports", "data/thumbs", "data/frames"],
    )


if __name__ == "__main__":
    main()

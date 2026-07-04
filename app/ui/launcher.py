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


def _wait_for_url(url, label, timeout=30, thread=None):
    """Poll a URL until it returns 200 or timeout. Returns True if ready.

    If `thread` is provided and dies before the URL is ready, exit immediately
    (the backend won't come up if its thread crashed).
    """
    import httpx
    for _ in range(timeout):
        if thread is not None and not thread.is_alive():
            print(f"ERROR: {label} thread died.", flush=True)
            os._exit(1)
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                print(f"{label} ready.")
                return True
        except Exception:
            time.sleep(1)
    print(f"WARNING: {label} did not become ready in {timeout}s.")
    return False


def _run_script_mode():
    """When invoked as `GifAgentUI.exe --run-script <path> [args...]`,
    run the given .py script via runpy instead of starting the GUI.

    This is how the exe spawns batch/adaptive subprocesses — PyInstaller
    exes can't run arbitrary .py files via sys.executable directly.
    """
    if "--run-script" not in sys.argv:
        return False

    idx = sys.argv.index("--run-script")
    script_path = sys.argv[idx + 1]
    # Reconstruct argv for the script: everything after the script path
    script_argv = [script_path] + sys.argv[idx + 2:]
    sys.argv = script_argv

    # Set CWD to exe dir so relative paths (configs/, data/) resolve
    if getattr(sys, "frozen", False):
        os.chdir(os.path.dirname(sys.executable))

    import runpy
    runpy.run_path(script_path, run_name="__main__")
    return True


def main():
    # Script subprocess mode: GifAgentUI.exe --run-script <path> [args...]
    if _run_script_mode():
        return

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

    # Wait for API to be ready (max 30s). Exit fast if the thread dies —
    # uvicorn won't come up if its thread crashed (port in use, missing dep, etc.).
    _wait_for_url(
        "http://127.0.0.1:8000/api/status",
        "API",
        timeout=30,
        thread=api_thread,
    )

    # Start Gradio UI in a background thread (prevent_thread_lock=True makes
    # launch() return immediately; the server runs in Gradio's internal thread).
    from app.ui.candidate_review import GRADIO_ALLOWED_PATHS, app as gradio_app
    try:
        gradio_app.launch(
            server_name="127.0.0.1",
            server_port=7861,
            prevent_thread_lock=True,
            allowed_paths=GRADIO_ALLOWED_PATHS,
        )
    except Exception as e:
        print(f"ERROR: Gradio failed to launch: {e}", flush=True)
        os._exit(1)
    print("Starting Gradio on http://127.0.0.1:7861 ...")

    # Wait for Gradio to be ready before opening the window. If it doesn't come
    # up in 30s, exit instead of opening a window to a dead URL.
    if not _wait_for_url("http://127.0.0.1:7861", "Gradio", timeout=30):
        print("ERROR: Gradio did not become ready, exiting.", flush=True)
        try:
            gradio_app.close()
        except Exception:
            pass
        os._exit(1)

    # Open a pywebview desktop window in the main thread. webview.start() blocks
    # until the user closes the window. On Windows the GUI message loop must run
    # on the main thread, so this has to be the last thing main() does.
    import webview
    webview.create_window("GifAgent", "http://127.0.0.1:7861", width=1400, height=900)
    try:
        webview.start()
    except Exception as e:
        print(f"ERROR: webview failed to start: {e}", flush=True)
    finally:
        # Window closed (or start failed) — shut down Gradio cleanly, then exit.
        # FastAPI runs in a daemon thread and is killed when the process exits.
        # os._exit() avoids hanging on Gradio's non-daemon internal threads.
        print("Window closed, shutting down servers...", flush=True)
        try:
            gradio_app.close()
        except Exception:
            pass
        os._exit(0)


if __name__ == "__main__":
    main()

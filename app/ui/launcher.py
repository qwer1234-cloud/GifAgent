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

import uvicorn


def start_api_server():
    """Run uvicorn in a background thread (daemon)."""
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        log_level="warning",
        access_log=False,
    )


def main():
    # Ensure cwd is project root (important for PyInstaller exe)
    if getattr(sys, "frozen", False):
        os.chdir(os.path.dirname(sys.executable))
    else:
        os.chdir(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

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
    gradio_app.launch(server_name="127.0.0.1", server_port=7861, prevent_thread_lock=False)


if __name__ == "__main__":
    main()

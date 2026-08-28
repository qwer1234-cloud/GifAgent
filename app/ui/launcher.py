"""
Launcher — starts FastAPI backend + Gradio UI in one process.
Use as the PyInstaller entry point, or run directly:

    uv run python app/ui/launcher.py
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
import shutil
from typing import Optional

import uvicorn

from app.services.desktop_export_sync import (
    start_background_sync,
    stop_background_sync,
)
from app.services import ollama_runtime
from app.ui.local_port import choose_local_port, reclaim_owned_listen_port

API_HOST = "127.0.0.1"
API_PORT = 8000
GRADIO_HOST = "127.0.0.1"
GRADIO_PORT = 7861
_api_server: Optional[uvicorn.Server] = None
_api_thread: Optional[threading.Thread] = None


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


def _run_startup_sync():
    """Run one full desktop reconciliation after DB init and before workers.

    Startup must continue with a warning if synchronization fails.
    """
    from app.services.desktop_export_sync import run_reconciliation

    try:
        report = run_reconciliation()
        print(report.log_line(), flush=True)
    except Exception as exc:
        print(
            f"WARNING: desktop export synchronization failed: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


def start_api_server():
    """Run uvicorn in a background thread (daemon). Import app object directly
    to avoid string-based import which fails in PyInstaller frozen exe."""
    global _api_server
    from app.main import app as fastapi_app

    config = uvicorn.Config(
        fastapi_app,
        host=API_HOST,
        port=API_PORT,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=1,
    )
    server = uvicorn.Server(config)
    _api_server = server
    server.run()


def stop_api_server(timeout_s: float = 2.0) -> None:
    """Ask uvicorn to leave port 8000, then wait briefly for the thread."""
    server = _api_server
    if server is not None:
        server.should_exit = True
        server.force_exit = True
        print(f"Stopping API on {API_HOST}:{API_PORT} ...", flush=True)
    thread = _api_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout_s)


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


def launch_gradio_app(gradio_app, *, server_port: int | None = None):
    """Launch Gradio with the UI module's complete visual configuration."""
    from app.ui.candidate_review import launch_kwargs

    kwargs = launch_kwargs()
    if server_port is not None:
        kwargs["server_port"] = server_port
    gradio_app.launch(prevent_thread_lock=True, **kwargs)


def resolve_gradio_port(
    *,
    host: str = GRADIO_HOST,
    preferred: int = GRADIO_PORT,
    reclaim=reclaim_owned_listen_port,
    choose=choose_local_port,
    log=print,
) -> int:
    """Reclaim leftover GifAgent listeners, then pick a bindable Gradio port."""
    reclaim(preferred)
    port = choose(host, preferred)
    if port != preferred:
        log(
            f"WARNING: port {preferred} is busy; using {port} for Gradio. "
            "A foreign app (often Afterlow) may be using the preferred port "
            "as an outbound connection; GifAgent will not kill it."
        )
    return port


def _stage_worker_counts() -> tuple[int, int]:
    """Read gpu/cpu worker counts from models.yaml. Defaults stay at 1."""
    from app.config import load_config

    try:
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    te = cfg.get("task_engine") or {}
    gpu = max(1, int(te.get("gpu_stage_workers", 1)))
    cpu = max(1, int(te.get("cpu_stage_workers", 1)))
    return gpu, cpu


def _start_task_worker():
    """Start GPU-class and CPU-class task worker daemon threads.

    Returns ``(stop_event, threads)`` if successful, or ``(None, None)``
    on failure.  Each thread owns its own SQLite connection.
    """
    import threading as _threading

    from app.task_engine import (
        AdaptivePipelineAdapter,
        CPU_STAGES,
        GPU_STAGES,
        StageName,
        TaskRepository,
        TaskWorker,
        connect_task_db,
    )

    try:
        stop_event = _threading.Event()
        gpu_n, cpu_n = _stage_worker_counts()
        threads: list = []

        all_stages: list[StageName] = [
            "discover",
            "sample",
            "vlm",
            "refine",
            "synthesize",
            "rank_dedup",
            "gif_clip",
            "materialize",
        ]

        def _spawn(worker_id: str, stage_names: tuple[str, ...], name: str):
            def _loop():
                conn = None
                try:
                    conn = connect_task_db()
                    repo = TaskRepository(conn)
                    adapters: dict[StageName, AdaptivePipelineAdapter] = {
                        stage: AdaptivePipelineAdapter(stage) for stage in all_stages
                    }
                    worker = TaskWorker(
                        repo, worker_id, adapters, stage_names=stage_names,
                    )
                    worker.run_forever(poll_seconds=2.0, stop_event=stop_event)
                except Exception as exc:
                    print(
                        f"ERROR: task worker {worker_id} crashed: {exc}",
                        flush=True,
                    )
                finally:
                    if conn is not None:
                        conn.close()

            thread = _threading.Thread(target=_loop, daemon=True, name=name)
            thread.start()
            threads.append(thread)

        pid = os.getpid()
        for index in range(gpu_n):
            _spawn(f"launcher-{pid}-gpu-{index}", GPU_STAGES, f"task-gpu-{index}")
        for index in range(cpu_n):
            _spawn(f"launcher-{pid}-cpu-{index}", CPU_STAGES, f"task-cpu-{index}")
        print(
            f"Task workers started (gpu={gpu_n}, cpu={cpu_n}).",
            flush=True,
        )
        return stop_event, threads
    except Exception as e:
        print(f"WARNING: Could not start task worker: {e}", flush=True)
        return None, None


def _stop_worker(stop_event, threads):
    """Signal every worker thread to stop and join them."""
    if stop_event is not None:
        stop_event.set()
    if threads is None:
        return
    pending = threads if isinstance(threads, (list, tuple)) else [threads]
    for thread in pending:
        if thread is not None:
            thread.join(timeout=3.0)


def _register_window_shutdown(
    window,
    graceful_shutdown,
    *,
    force_timeout=12.0,
    exit_process=None,
):
    """Exit even if pywebview or a server cleanup call never returns.

    pywebview dispatches ``closed`` callbacks independently of its GUI loop.
    Registering here therefore keeps process shutdown reachable when
    ``webview.start()`` does not return. A watchdog also bounds synchronous
    cleanup such as ``gradio_app.close()``.
    """
    if exit_process is None:
        exit_process = os._exit

    state_lock = threading.Lock()
    shutdown_complete = threading.Event()
    shutdown_started = False

    def shutdown(*_args, **_kwargs):
        nonlocal shutdown_started
        with state_lock:
            if shutdown_started:
                return
            shutdown_started = True

        def force_exit_watchdog():
            if not shutdown_complete.wait(force_timeout):
                print("Shutdown timed out; forcing process exit.", flush=True)
                exit_process(0)

        threading.Thread(
            target=force_exit_watchdog,
            daemon=True,
            name="shutdown-watchdog",
        ).start()

        try:
            graceful_shutdown()
        finally:
            shutdown_complete.set()
            exit_process(0)

    events = getattr(window, "events", None)
    for event_name in ("closing", "closed"):
        event = getattr(events, event_name, None)
        if event is None:
            continue
        event += shutdown
    return shutdown


def _install_exit_signals(shutdown) -> None:
    """Map console Ctrl+C / close to the same shutdown path as the window X."""

    def _handle(*_args):
        shutdown()

    signal.signal(signal.SIGINT, _handle)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle)


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
        exe_dir = os.path.dirname(sys.executable)
        os.chdir(exe_dir)
        _setup_runtime_files(exe_dir)

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

    # Initial full desktop reconciliation (after DB init, before task worker).
    # Startup must continue with a warning if synchronization fails.
    try:
        _run_startup_sync()
    except Exception as e:
        print(f"WARNING: desktop export synchronization failed: {e}")

    # Background scheduler for post-completion incremental reconciliations.
    sync_stop_event = threading.Event()
    start_background_sync(sync_stop_event)

    reclaim_owned_listen_port(API_PORT)

    # Start API server in background thread
    global _api_thread
    api_thread = threading.Thread(target=start_api_server, daemon=True, name="api-server")
    _api_thread = api_thread
    api_thread.start()
    print(f"Starting FastAPI on http://{API_HOST}:{API_PORT} ...")

    # Wait for API to be ready (max 30s). Exit fast if the thread dies —
    # uvicorn won't come up if its thread crashed (port in use, missing dep, etc.).
    _wait_for_url(
        f"http://{API_HOST}:{API_PORT}/api/status",
        "API",
        timeout=30,
        thread=api_thread,
    )

    # Start task worker background thread (daemon, drives pipeline stages)
    worker_stop_event, worker_thread = _start_task_worker()

    # Start Gradio UI in a background thread (prevent_thread_lock=True makes
    # launch() return immediately; the server runs in Gradio's internal thread).
    from app.ui.candidate_review import app as gradio_app
    try:
        gradio_port = resolve_gradio_port()
        launch_gradio_app(gradio_app, server_port=gradio_port)
    except Exception as e:
        print(f"ERROR: Gradio failed to launch: {e}", flush=True)
        _stop_worker(worker_stop_event, worker_thread)
        stop_background_sync(sync_stop_event)
        stop_api_server()
        os._exit(1)
    print(f"Starting Gradio on http://{GRADIO_HOST}:{gradio_port} ...")

    # Wait for Gradio to be ready before opening the window. If it doesn't come
    # up in 30s, exit instead of opening a window to a dead URL.
    if not _wait_for_url(f"http://{GRADIO_HOST}:{gradio_port}", "Gradio", timeout=30):
        print("ERROR: Gradio did not become ready, exiting.", flush=True)
        _stop_worker(worker_stop_event, worker_thread)
        stop_background_sync(sync_stop_event)
        try:
            gradio_app.close()
        except Exception:
            pass
        stop_api_server()
        os._exit(1)

    # Open a pywebview desktop window in the main thread. webview.start() blocks
    # until the user closes the window. On Windows the GUI message loop must run
    # on the main thread, so this has to be the last thing main() does.
    import webview
    window = webview.create_window(
        "GifAgent",
        f"http://{GRADIO_HOST}:{gradio_port}",
        width=1400,
        height=900,
        min_size=(1024, 680),
    )

    def graceful_shutdown():
        print("Window closed, shutting down servers...", flush=True)
        _stop_worker(worker_stop_event, worker_thread)
        stop_background_sync(sync_stop_event)
        try:
            stop_api_server()
        except Exception as exc:
            print(f"WARNING: API shutdown failed: {exc}", flush=True)
        try:
            gradio_app.close()
        except Exception:
            pass
        try:
            ollama_runtime.shutdown_runtime()
        except Exception as exc:
            print(
                f"WARNING: Ollama runtime shutdown failed: {exc}",
                flush=True,
            )

    shutdown = _register_window_shutdown(window, graceful_shutdown)
    if getattr(sys, "frozen", False):
        try:
            _install_exit_signals(shutdown)
        except Exception:
            pass
    try:
        webview.start()
    except Exception as e:
        print(f"ERROR: webview failed to start: {e}", flush=True)
    finally:
        # Window closed (or start failed) — stop the API so port 8000 is
        # released, then os._exit() to avoid hanging on Gradio's non-daemon
        # internal threads.
        shutdown()


if __name__ == "__main__":
    main()

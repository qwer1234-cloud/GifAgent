from types import SimpleNamespace

from app.ui import launcher
from app.ui.local_port import (
    choose_local_port,
    reclaim_owned_listen_port,
    _port_from_local_address,
)


def test_port_from_local_address_parses_ipv4_and_ipv6():
    assert _port_from_local_address("127.0.0.1:8000") == 8000
    assert _port_from_local_address("0.0.0.0:8000") == 8000
    assert _port_from_local_address("[::1]:8000") == 8000
    assert _port_from_local_address("not-a-port") is None


def test_reclaim_kills_leftover_exe_but_not_foreign_process():
    killed = []
    logs = []

    def basename(pid):
        return {
            111: "afterlow_core.exe",
            222: "GifAgentUI.exe",
        }[pid]

    reclaimed = reclaim_owned_listen_port(
        8000,
        listen_rows=lambda: [(8000, 111), (8000, 222), (7861, 222)],
        process_basename=basename,
        terminate_pid=killed.append,
        is_frozen=True,
        executable=r"C:\dist\GifAgentUI\GifAgentUI.exe",
        wait_s=0.0,
        log=logs.append,
    )

    assert reclaimed == [222]
    assert killed == [222]
    assert any("afterlow_core.exe" in line for line in logs)
    assert any("PID 222" in line for line in logs)


def test_choose_local_port_skips_busy_preferred():
    busy = {7861}

    def available(host, port):
        assert host == "127.0.0.1"
        return port not in busy

    assert choose_local_port("127.0.0.1", 7861, span=3, available=available) == 7862


def test_choose_local_port_raises_when_range_exhausted():
    try:
        choose_local_port(
            "127.0.0.1",
            7861,
            span=2,
            available=lambda host, port: False,
        )
    except RuntimeError as exc:
        assert "7861-7862" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_resolve_gradio_port_falls_back_and_warns():
    logs = []
    reclaimed = []

    port = launcher.resolve_gradio_port(
        preferred=7861,
        reclaim=lambda p: reclaimed.append(p),
        choose=lambda host, preferred: 7862,
        log=logs.append,
    )

    assert port == 7862
    assert reclaimed == [7861]
    assert any("7862" in line for line in logs)
    assert any("Afterlow" in line for line in logs)


def test_resolve_gradio_port_keeps_preferred_without_warning():
    logs = []
    port = launcher.resolve_gradio_port(
        preferred=7861,
        reclaim=lambda p: None,
        choose=lambda host, preferred: preferred,
        log=logs.append,
    )
    assert port == 7861
    assert logs == []
    killed = []
    reclaimed = reclaim_owned_listen_port(
        8000,
        listen_rows=lambda: [(8000, 222)],
        process_basename=lambda pid: "GifAgentUI.exe",
        terminate_pid=killed.append,
        is_frozen=False,
        executable=r"C:\dist\GifAgentUI\GifAgentUI.exe",
        wait_s=0.0,
        log=lambda *_: None,
    )
    assert reclaimed == []
    assert killed == []


def test_stop_api_server_signals_uvicorn_and_joins_thread():
    class FakeServer:
        should_exit = False
        force_exit = False

    class FakeThread:
        def __init__(self):
            self.joined = False
            self.alive = True

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            self.joined = True
            self.timeout = timeout
            self.alive = False

    server = FakeServer()
    thread = FakeThread()
    launcher._api_server = server
    launcher._api_thread = thread
    try:
        launcher.stop_api_server(timeout_s=1.5)
    finally:
        launcher._api_server = None
        launcher._api_thread = None

    assert server.should_exit is True
    assert server.force_exit is True
    assert thread.joined is True
    assert thread.timeout == 1.5


def test_window_closing_and_closed_only_exit_once():
    class FakeEvent:
        def __init__(self):
            self.callback = None

        def __iadd__(self, callback):
            self.callback = callback
            return self

    window = SimpleNamespace(
        events=SimpleNamespace(closing=FakeEvent(), closed=FakeEvent()),
    )
    cleanup_calls = []
    exit_codes = []

    launcher._register_window_shutdown(
        window,
        lambda: cleanup_calls.append(1),
        force_timeout=1.0,
        exit_process=exit_codes.append,
    )

    window.events.closing.callback()
    window.events.closed.callback()

    assert cleanup_calls == [1]
    assert exit_codes == [0]

"""Find and reclaim local TCP listen ports owned by a leftover GifAgent process.

Windows WSL/Ollama addresses are unrelated; this only inspects host TCP
listeners. Foreign processes (for example another app bound to port 8000)
are never killed.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import Callable, Iterable, Optional

DEFAULT_PORT_SPAN = 10


def local_bind_available(host: str, port: int) -> bool:
    """Return True when *host*:*port* can be bound as a TCP server.

    This matches Gradio's occupancy check. Windows "Bound"/ephemeral client
    ports (for example Afterlow using 7861 as an outbound source port) are
    not LISTEN rows, so ``reclaim_owned_listen_port`` cannot see them.
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def choose_local_port(
    host: str,
    preferred: int,
    *,
    span: int = DEFAULT_PORT_SPAN,
    available: Optional[Callable[[str, int], bool]] = None,
) -> int:
    """Return *preferred* or the next free port in ``[preferred, preferred+span)``.

    Raises ``RuntimeError`` when every candidate fails the bind probe.
    """
    check = available or local_bind_available
    count = max(1, int(span))
    last = preferred + count - 1
    for port in range(preferred, last + 1):
        if check(host, port):
            return port
    raise RuntimeError(f"Cannot find empty port in range: {preferred}-{last}.")


def reclaim_owned_listen_port(
    port: int,
    *,
    listen_rows: Optional[Callable[[], Iterable[tuple[int, int]]]] = None,
    process_basename: Optional[Callable[[int], str]] = None,
    terminate_pid: Optional[Callable[[int], None]] = None,
    is_frozen: Optional[bool] = None,
    executable: Optional[str] = None,
    wait_s: float = 5.0,
    log=print,
) -> list[int]:
    """Kill leftover copies of this EXE that are still listening on *port*.

    Returns the PIDs that were asked to exit. Does nothing when this process
    is not a frozen EXE, and never terminates a PID whose image name does not
    match the current executable.
    """
    frozen = getattr(sys, "frozen", False) if is_frozen is None else is_frozen
    if not frozen:
        _warn_foreign_holders(
            port,
            listen_rows=listen_rows or _ipv4_listen_rows,
            process_basename=process_basename or _process_basename,
            log=log,
        )
        return []

    own_name = os.path.basename(executable or sys.executable).casefold()
    rows = list((listen_rows or _ipv4_listen_rows)())
    basename_of = process_basename or _process_basename
    killer = terminate_pid or _terminate_pid
    current_pid = os.getpid()

    reclaim: list[int] = []
    foreign: list[tuple[int, str]] = []
    for listen_port, pid in rows:
        if listen_port != port or pid <= 0 or pid == current_pid:
            continue
        name = basename_of(pid)
        if name.casefold() == own_name:
            reclaim.append(pid)
        else:
            foreign.append((pid, name or "unknown"))

    seen: set[int] = set()
    unique_reclaim: list[int] = []
    for pid in reclaim:
        if pid not in seen:
            seen.add(pid)
            unique_reclaim.append(pid)

    for pid in unique_reclaim:
        log(f"Reclaiming port {port} from leftover {own_name} PID {pid}.")
        try:
            killer(pid)
        except Exception as exc:
            log(f"WARNING: could not stop leftover PID {pid}: {exc}")

    if unique_reclaim:
        _wait_until_free(
            port,
            listen_rows=listen_rows or _ipv4_listen_rows,
            timeout_s=wait_s,
        )

    for pid, name in foreign:
        log(
            f"WARNING: port {port} is in use by {name} (PID {pid}); "
            "GifAgent will not kill another application."
        )
    return unique_reclaim


def _warn_foreign_holders(
    port: int,
    *,
    listen_rows: Callable[[], Iterable[tuple[int, int]]],
    process_basename: Callable[[int], str],
    log,
) -> None:
    current_pid = os.getpid()
    for listen_port, pid in listen_rows():
        if listen_port != port or pid <= 0 or pid == current_pid:
            continue
        name = process_basename(pid) or "unknown"
        log(
            f"WARNING: port {port} is already in use by {name} (PID {pid})."
        )


def _wait_until_free(
    port: int,
    *,
    listen_rows: Callable[[], Iterable[tuple[int, int]]],
    timeout_s: float,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_s)
    current_pid = os.getpid()
    while True:
        holders = [
            pid
            for listen_port, pid in listen_rows()
            if listen_port == port and pid > 0 and pid != current_pid
        ]
        if not holders:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _ipv4_listen_rows() -> list[tuple[int, int]]:
    if os.name != "nt":
        return []
    try:
        return _windows_ipv4_listen_rows()
    except Exception:
        return _netstat_listen_rows()


def _windows_ipv4_listen_rows() -> list[tuple[int, int]]:
    import ctypes
    from ctypes import wintypes
    import socket

    iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
    af_inet = 2
    tcp_table_owner_pid_all = 5
    mib_tcp_state_listen = 2

    class MIB_TCPROW_OWNER_PID(ctypes.Structure):
        _fields_ = (
            ("dwState", wintypes.DWORD),
            ("dwLocalAddr", wintypes.DWORD),
            ("dwLocalPort", wintypes.DWORD),
            ("dwRemoteAddr", wintypes.DWORD),
            ("dwRemotePort", wintypes.DWORD),
            ("dwOwningPid", wintypes.DWORD),
        )

    size = wintypes.DWORD(0)
    iphlpapi.GetExtendedTcpTable(
        None, ctypes.byref(size), False, af_inet, tcp_table_owner_pid_all, 0
    )
    buf = ctypes.create_string_buffer(size.value)
    ret = iphlpapi.GetExtendedTcpTable(
        buf, ctypes.byref(size), False, af_inet, tcp_table_owner_pid_all, 0
    )
    if ret != 0:
        raise OSError(ret)

    count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value

    class MIB_TCPTABLE_OWNER_PID(ctypes.Structure):
        _fields_ = (
            ("dwNumEntries", wintypes.DWORD),
            ("table", MIB_TCPROW_OWNER_PID * max(count, 1)),
        )

    table = ctypes.cast(buf, ctypes.POINTER(MIB_TCPTABLE_OWNER_PID)).contents
    rows: list[tuple[int, int]] = []
    for index in range(count):
        row = table.table[index]
        if int(row.dwState) != mib_tcp_state_listen:
            continue
        port = socket.ntohs(int(row.dwLocalPort) & 0xFFFF)
        rows.append((port, int(row.dwOwningPid)))
    return rows


def _netstat_listen_rows() -> list[tuple[int, int]]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=flags,
        )
    except Exception:
        return []
    listen_states = {"LISTENING", "侦听"}
    rows: list[tuple[int, int]] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        if parts[0].upper() != "TCP":
            continue
        if parts[3].upper() not in listen_states and parts[3] not in listen_states:
            continue
        local = parts[1]
        pid_text = parts[-1]
        if not pid_text.isdigit():
            continue
        port = _port_from_local_address(local)
        if port is None:
            continue
        rows.append((port, int(pid_text)))
    return rows


def _port_from_local_address(address: str) -> Optional[int]:
    if address.startswith("[") and "]:" in address:
        _, port_text = address.rsplit("]:", 1)
    elif ":" in address:
        _, port_text = address.rsplit(":", 1)
    else:
        return None
    try:
        return int(port_text)
    except ValueError:
        return None


def _process_basename(pid: int) -> str:
    if os.name == "nt":
        name = _windows_process_basename(pid)
        if name:
            return name
    return ""


def _windows_process_basename(pid: int) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    process_query_limited_information = 0x1000
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(32768)
        query = kernel32.QueryFullProcessImageNameW
        query.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        query.restype = wintypes.BOOL
        if not query(handle, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(handle)


def _terminate_pid(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=flags,
        )
        return
    os.kill(pid, signal.SIGTERM)

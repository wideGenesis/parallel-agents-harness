"""Running external commands: their output is text for an agent, never a reason to crash."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

STOP_POLL_S = 0.5  # how soon a set `stop` kills a running command

# A tool's output is not always UTF-8 (a Windows CI runner, a cp1251 console);
# a stray byte becomes U+FFFD instead of a UnicodeDecodeError.
TEXT = {"text": True, "encoding": "utf-8", "errors": "replace"}


def run(argv: Sequence[str], cwd: Path, **kw) -> subprocess.CompletedProcess:
    """`argv` in `cwd`, output captured as text; never raises on a non-zero exit."""
    kw.setdefault("capture_output", "stdout" not in kw)
    return subprocess.run(list(argv), cwd=cwd, check=False, **TEXT, **kw)


def run_logged(
    argv: Sequence[str],
    cwd: Path,
    log: Path,
    header: str = "",
    env: dict[str, str] | None = None,
    timeout_s: float | None = None,
    stop: threading.Event | None = None,
) -> tuple[int | None, str]:
    """`argv` with its output going to `log` as it comes, after `header`: a job
    cancelled mid-step still leaves what the step printed. Past `timeout_s`, or once `stop`
    is set (the tests' database died), the command is killed with every process it
    started. Returns (exit code, None when killed; output)."""
    end = None if timeout_s is None else time.monotonic() + timeout_s
    with log.open("w", encoding="utf-8", newline="\n") as f:
        f.write(header)
        f.flush()
        # stdin closed: a tool waiting for input would otherwise hang until the limit.
        child = subprocess.Popen(list(argv), cwd=cwd, env=env, stdout=f,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)  # fmt: skip
        code: int | None = None
        while True:
            left = None if end is None else end - time.monotonic()
            if stop is not None:
                left = STOP_POLL_S if left is None else min(STOP_POLL_S, left)
            try:
                code = child.wait(timeout=None if left is None else max(0.0, left))
                break
            except subprocess.TimeoutExpired:
                past = end is not None and time.monotonic() >= end
                if past or (stop is not None and stop.is_set()):
                    kill_tree(child.pid)
                    child.wait()
                    code = None
                    break
    raw = log.read_bytes()[len(header.encode("utf-8")) :]
    return code, raw.decode("utf-8", errors="replace")


PID_MAX = 2**31 - 1  # os.kill takes a C int: a bigger number raised OverflowError


def alive(pid: int) -> bool:
    """A process with this PID runs on this machine (a sweep spares the container
    of a live run). A reused PID can only make a dead run look alive — never the reverse."""
    if not 0 < pid <= PID_MAX:  # a number from a name, not a PID: no process has it
        return False
    if sys.platform == "win32":
        return _alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # someone else's process, but alive
    except OSError:
        return False
    return True


def _alive_windows(pid: int) -> bool:
    if sys.platform != "win32":  # for the type checker on Linux: WinDLL is Windows-only
        return False
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # cannot tell: keep it
        return code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def kill_tree(pid: int) -> None:
    """`pid` and everything under it: a test runner's workers outlive their parent."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, check=False)  # fmt: skip
        return
    tree = [pid, *_descendants(pid)]  # before the kill: orphans lose their parent
    for p in tree:
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass  # already gone


def _descendants(pid: int) -> list[int]:
    r = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True,
                       text=True, check=False)  # fmt: skip
    kids: dict[int, list[int]] = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            kids.setdefault(int(parts[1]), []).append(int(parts[0]))
    found, todo = [], [pid]
    while todo:
        for child in kids.get(todo.pop(), []):
            found.append(child)
            todo.append(child)
    return found

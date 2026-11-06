"""Machine-wide semaphore for heavy jobs (full builds, clippy, test suites).

CPU is a machine resource: seven sessions in different clones or projects compete for the
same cores, so the slots live in the user cache, not in one repository. Each slot is a file
held with an OS lock — the OS drops it when the process dies, so a crashed session never
leaves a slot taken.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path


def lock_dir() -> Path:
    if override := os.environ.get("HARNESS_LOCK_DIR"):
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "harness" / "locks"


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def try_acquire(limit: int, name: str = "heavy") -> tuple[int, int] | None:
    """Take a free slot without waiting. Returns (slot, fd) or None when all are busy."""
    d = lock_dir()
    d.mkdir(parents=True, exist_ok=True)
    for slot in range(limit):
        # Never write to the file: Windows locks are mandatory, so writing a byte another
        # thread has just locked raises PermissionError (a real race on first use). Locking
        # a range past EOF is allowed, so an empty file is enough.
        fd = os.open(d / f"{name}-{slot}.lock", os.O_RDWR | os.O_CREAT, 0o644)
        if _try_lock(fd):
            return slot, fd
        os.close(fd)
    return None


def release(fd: int) -> None:
    try:
        _unlock(fd)
    finally:
        os.close(fd)


@contextmanager
def slot(
    limit: int,
    name: str = "heavy",
    on_wait: Callable[[], None] | None = None,
    poll: float = 0.5,
) -> Iterator[int]:
    """Block until one of `limit` slots is free; `on_wait` is called once if we had to wait.
    A whole-machine step waiting for the slots (`machine`) goes first: new heavy steps hold
    back until it is done, or freed slots would be taken again and again under it."""
    waited = False
    while True:
        if (name != "heavy" or _gate_free()) and (
            got := try_acquire(limit, name)
        ) is not None:
            break
        if not waited and on_wait:
            on_wait()
        waited = True
        time.sleep(poll)
    idx, fd = got
    try:
        yield idx
    finally:
        release(fd)


GATE = (
    "exclusive"  # held by the one step that is waiting for, or using, the whole machine
)


def _gate_free() -> bool:
    got = try_acquire(1, GATE)
    if got is None:
        return False
    release(got[1])
    return True


@contextmanager
def machine(
    limit: int, on_wait: Callable[[], None] | None = None, poll: float = 0.5
) -> Iterator[None]:
    """Every heavy slot: for a step that must not share the machine, like timed
    e2e tests. The gate first, so two such steps queue instead of each holding half the
    slots and waiting for the other's; then the slots one by one as running steps finish."""
    waited = False

    def wait() -> None:
        nonlocal waited
        if not waited and on_wait:
            on_wait()
        waited = True
        time.sleep(poll)

    held: list[int] = []
    try:
        while (gate := try_acquire(1, GATE)) is None:
            wait()
        held.append(gate[1])
        d = lock_dir()
        for i in range(limit):
            fd = os.open(d / f"heavy-{i}.lock", os.O_RDWR | os.O_CREAT, 0o644)
            while not _try_lock(fd):
                try:
                    wait()
                except BaseException:
                    os.close(fd)
                    raise
            held.append(fd)
        yield
    finally:
        for fd in reversed(held):
            release(fd)

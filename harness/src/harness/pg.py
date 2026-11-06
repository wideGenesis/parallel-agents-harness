"""A throwaway Postgres per `h check` run, as CI starts one (check.yml).

`sqlx::test` names its test databases from the test path alone, so two runs of the same
tests on one server collide ("database _sqlx_test_… already exists"). Each run gets its own
server: a container from `[ci] postgres-image` on a free port, removed with its volume when
the run ends. A run that was killed leaves its container behind; it holds an OS file lock
while alive (lock.py), so the next run removes containers whose lock is free.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import os
import re
import secrets
import shutil
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from . import lock, proc
from .config import Config

PREFIX = "h-check-pg-"
READY_S = 60.0
PASSWORD = "postgres"


def wanted(cfg: Config, steps: list) -> bool:
    """A project with a Postgres image, not turned off, outside CI (the workflow starts its
    own), and a test or sql step that will run."""
    return bool(
        cfg.ci_postgres_image
        and cfg.check_postgres
        and os.environ.get("CI") != "true"
        and any(s.kind in ("test", "sql") and (s.cmd or s.call) for s in steps)
    )


def _docker() -> str | None:
    exe = shutil.which("docker")
    if exe is None:
        return None
    r = proc.run([exe, "version", "--format", "{{.Server.Version}}"], Path.cwd())
    return exe if r.returncode == 0 and r.stdout.strip() else None


def _lock_name(container: str) -> str:
    return f"pg-{container}"


def _drop_lock_file(container: str) -> None:
    # Windows refuses to delete a file another sweep holds open; the next sweep retries.
    with contextlib.suppress(OSError):
        (lock.lock_dir() / f"{_lock_name(container)}-0.lock").unlink(missing_ok=True)


def namespace() -> str:
    """Whose containers a sweep may judge: runs whose locks it can see. The lock
    dir and the platform: a test's own lock dir, and Windows and WSL on one Docker Desktop
    (each with its own lock dir and PIDs), each get their own and never touch the others'."""
    key = f"{sys.platform}:{lock.lock_dir().resolve()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def _owner(name: str) -> tuple[str | None, int] | None:
    """(namespace, PID) from `<PREFIX><ns>-<pid>-<hex>`; ns None for h ≤ 0.30's
    `<PREFIX><pid>-<hex>`; None for a name h did not make."""
    if not name.startswith(PREFIX):
        return None
    parts = name[len(PREFIX) :].split("-")
    # The tag is h's `secrets.token_hex(3)`: a name like ci.yml's `…-<run id>-<attempt>`
    # is not h's, whatever its prefix (a CI run once parsed one as a PID).
    if not _HEX6.fullmatch(parts[-1]):
        return None
    if len(parts) == 3 and _HEX8.fullmatch(parts[0]) and parts[1].isdigit():
        return parts[0], int(parts[1])
    if len(parts) == 2 and parts[0].isdigit():
        return None, int(parts[0])
    return None


_HEX6 = re.compile(r"[0-9a-f]{6}")
_HEX8 = re.compile(r"[0-9a-f]{8}")


def _ours(name: str) -> bool:
    """A sweep may remove it: its namespace (or h ≤ 0.30's, which had none)."""
    who = _owner(name)
    return who is not None and who[0] in (None, namespace())


def _names(docker: str) -> list[str]:
    r = proc.run([docker, "ps", "-a", "--filter", f"name={PREFIX}", "--format",
                  "{{.Names}}"], Path.cwd())  # fmt: skip
    return [n for n in r.stdout.split() if n.startswith(PREFIX)]


def sweep(docker: str) -> list[str]:
    """Remove containers of runs that are gone: of this namespace, their lock free and the
    process that started them dead (a free lock alone was no proof: a test with
    its own lock dir removed live runs' databases). Returns their names."""
    gone = []
    for name in _names(docker):
        who = _owner(name)
        if who is None or not _ours(name) or proc.alive(who[1]):
            continue
        held = lock.try_acquire(1, _lock_name(name))
        if held is None:  # its run is alive
            continue
        try:
            proc.run([docker, "rm", "-f", "-v", name], Path.cwd())
            gone.append(name)
        finally:
            lock.release(held[1])
            _drop_lock_file(name)
    return gone


ORPHAN_H = 24


def orphans(docker: str, now: datetime.datetime | None = None) -> list[str]:
    """Containers no sweep of this side may remove — another namespace's — older than a
    day: a WSL distro or a lock dir that is gone leaves them for good. `h doctor`
    names them; nobody removes them on a guess."""
    foreign = [n for n in _names(docker) if _owner(n) is not None and not _ours(n)]
    if not foreign:
        return []
    r = proc.run(
        [docker, "inspect", "-f", "{{.Name}} {{.Created}}", *foreign], Path.cwd()
    )
    now = now or datetime.datetime.now(datetime.UTC)
    old = []
    for line in r.stdout.splitlines():
        name, _, created = line.strip().lstrip("/").partition(" ")
        try:  # 2026-11-04T10:31:29.749795024Z: seconds are enough
            at = datetime.datetime.fromisoformat(created[:19]).replace(
                tzinfo=datetime.UTC
            )
        except ValueError:
            continue
        if now - at > datetime.timedelta(hours=ORPHAN_H):
            old.append(name)
    return old


def _port(docker: str, name: str) -> str:
    r = proc.run([docker, "port", name, "5432/tcp"], Path.cwd())
    first = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
    return first.rsplit(":", 1)[-1] if ":" in first else ""


def _ready(docker: str, name: str, deadline: float) -> bool:
    # TCP, not the socket: the image's init server listens on the socket only, so a
    # socket probe says ready too early (check.yml does the same).
    while time.monotonic() < deadline:
        r = proc.run([docker, "exec", name, "pg_isready", "-h", "127.0.0.1", "-U",
                      "postgres", "-q"], Path.cwd())  # fmt: skip
        if r.returncode == 0:
            return True
        time.sleep(1)
    return False


EVIDENCE_LINES = 200


@dataclass
class Server:
    """What `throwaway` gives the run: a line for the output ("" when nothing to say) and
    the container behind DATABASE_URL when it is h's own."""

    line: str = ""
    container: str = ""
    docker: str = ""

    def keep_evidence(self, path: Path) -> bool:
        """The container's state and last log lines, before it is removed with its logs
        (why the database died was gone with it)."""
        if not self.container:
            return False
        state = proc.run([self.docker, "inspect", "-f", "{{json .State}}", self.container],
                         Path.cwd())  # fmt: skip
        logs = proc.run([self.docker, "logs", "--tail", str(EVIDENCE_LINES), self.container],
                        Path.cwd())  # fmt: skip
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"$ docker inspect -f '{{{{json .State}}}}' {self.container}\n"
            f"{state.stdout}{state.stderr}\n"
            f"$ docker logs --tail {EVIDENCE_LINES} {self.container}\n"
            f"{logs.stdout}{logs.stderr}",
            encoding="utf-8",
        )
        return True


@contextmanager
def throwaway(cfg: Config, steps: list) -> Iterator[Server]:
    """Run the block with a Postgres of its own: `cfg.check_env["DATABASE_URL"]` points at
    it inside."""
    if not wanted(cfg, steps):
        yield Server()
        return
    docker = _docker()
    if docker is None:
        yield Server("no Docker: the tests use DATABASE_URL from [check.env] — parallel "
                     "runs share that server")  # fmt: skip
        return
    sweep(docker)
    name = f"{PREFIX}{namespace()}-{os.getpid()}-{secrets.token_hex(3)}"
    held = lock.try_acquire(1, _lock_name(name))
    if held is None:  # without its lock another run's sweep could remove it mid-run
        yield Server(
            "throwaway Postgres not started (its lock is taken): the tests use [check.env]"
        )
        return
    started = False
    old = cfg.check_env.get("DATABASE_URL")
    try:
        r = proc.run([docker, "run", "-d", "--name", name, "-e", f"POSTGRES_PASSWORD={PASSWORD}",
                      "-p", "127.0.0.1::5432", cfg.ci_postgres_image], Path.cwd())  # fmt: skip
        started = r.returncode == 0
        port = _port(docker, name) if started else ""
        if (
            not started
            or not port
            or not _ready(docker, name, time.monotonic() + READY_S)
        ):
            why = (
                (r.stderr or r.stdout).strip().splitlines()[-1:] if not started else []
            )
            yield Server("throwaway Postgres did not start"
                         + (f" ({why[0]})" if why else "")
                         + ": the tests use DATABASE_URL from [check.env]")  # fmt: skip
            return
        cfg.check_env["DATABASE_URL"] = (
            f"postgres://postgres:{PASSWORD}@127.0.0.1:{port}/postgres"
        )
        yield Server(f"throwaway Postgres {cfg.ci_postgres_image} on 127.0.0.1:{port}",
                     name, docker)  # fmt: skip
    finally:
        if old is None:
            cfg.check_env.pop("DATABASE_URL", None)
        else:
            cfg.check_env["DATABASE_URL"] = old
        if started:
            proc.run([docker, "rm", "-f", "-v", name], Path.cwd())
        if held is not None:
            lock.release(held[1])
            _drop_lock_file(name)


# --- the tests' database while the run goes on ---------------------------------

PROBE_EVERY_S = 10.0
PROBE_TIMEOUT_S = 5.0
PROBE_MISSES = 3  # in a row: one slow answer under load is not a dead server
# Postgres's SSLRequest; the server answers one byte, S or N. Closing then logs nothing
# (postmaster: "we might be here after responding to NEGOTIATE_SSL_CODE").
SSL_REQUEST = struct.pack("!II", 8, 80877103)


def answers(host: str, port: int, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """Postgres speaks on host:port. A TCP accept alone is not enough: on a pilot project Docker
    Desktop's forwarder accepted with nothing behind it and answered 0x00."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(SSL_REQUEST)
            return s.recv(1) in (b"S", b"N")
    except OSError:
        return False


def target(url: str | None) -> tuple[str, int] | None:
    """host and port of a postgres:// URL over TCP; None for a socket path or no URL."""
    if not url:
        return None
    try:
        u = urlsplit(url)
        port = u.port or 5432
    except ValueError:
        return None
    if u.scheme not in ("postgres", "postgresql") or not u.hostname:
        return None
    return u.hostname, port


class Watch:
    """Probes the tests' Postgres in a thread while the run goes on. Armed by the first
    answer — a server that never answered is not one that died, and a project whose tests
    do not need its DATABASE_URL must not lose them — or from the start for h's own
    throwaway, which answered before the run: a forwarder broken from the first second is
    caught too. Then PROBE_MISSES misses in a row set `dead`, with `cause` saying when it
    went silent and which steps were running."""

    def __init__(self, where: tuple[str, int] | None, label: str = "Postgres",
                 during: Callable[[], str] = lambda: "", every: float = PROBE_EVERY_S,
                 misses: int = PROBE_MISSES, timeout: float = PROBE_TIMEOUT_S,
                 armed: bool = False) -> None:  # fmt: skip
        # armed from the start: h's own throwaway, which answered pg_isready before the run
        self.where, self.label, self.during, self.armed = where, label, during, armed
        self.every, self.misses, self.timeout = every, misses, timeout
        self.dead = threading.Event()
        self.cause = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Self:
        if self.where is not None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.timeout + 1)

    def _loop(self) -> None:
        assert self.where is not None
        host, port = self.where
        t0 = time.monotonic()
        armed, misses, silent = self.armed, 0, 0.0
        while True:
            at = time.monotonic()
            if answers(host, port, self.timeout):
                armed, misses = True, 0
            elif armed:
                misses += 1
                silent = silent if misses > 1 else at
                if misses >= self.misses:
                    during = self.during()
                    self.cause = (f"{self.label} stopped answering at +{int(silent - t0)}s"
                                  + (f" (during {during})" if during else ""))  # fmt: skip
                    self.dead.set()
                    return
            if self._stop.wait(self.every):
                return


def say(line: str) -> None:
    if line:
        print(f"h check: {line}", file=sys.stderr, flush=True)

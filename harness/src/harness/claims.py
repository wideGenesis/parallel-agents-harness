"""Task leases on origin: `refs/claims/<ID>` → a JSON blob saying who works on the task.

Why origin and not "a worktree exists": a worktree is visible only in its own clone, outlives
crashes and merges, and a cloud session never sees it (ADR-0002).
A ref on origin is seen by every clone, machine and session.

Atomicity comes from git itself (verified 2026-10-21 against a bare repo):
- push of a new ref without --force fails if the ref already exists → one winner on create;
- `--force-with-lease=<ref>:<sha>` is compare-and-swap → heartbeat, steal and release only
  succeed against the exact claim the caller read.
Refs outside refs/heads and refs/tags trigger no GitHub Actions run.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import socket
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import git
from .config import Config

REMOTE = "origin"
PREFIX = "refs/claims/"
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ClaimError(Exception):
    pass


class PushFailed(ClaimError):
    """origin could not be updated (network, 5xx, credentials) and the claim on it is still
    the one we read — not a takeover."""


RETRY_PAUSES_S = (2.0, 5.0)  # three attempts in all


@dataclass
class Claim:
    task: str
    owner: str  # host|worktree-path — identifies the claiming session's checkout
    branch: str
    by: str  # git user.email, for humans reading `h task status`
    started: str  # ISO-8601 UTC
    heartbeat: str  # ISO-8601 UTC

    def age_hours(self, now: dt.datetime | None = None) -> float:
        now = now or dt.datetime.now(dt.UTC)
        return (now - dt.datetime.fromisoformat(self.heartbeat)).total_seconds() / 3600

    def stale(self, ttl_hours: float, now: dt.datetime | None = None) -> bool:
        return self.age_hours(now) > ttl_hours


def check_id(task: str) -> str:
    if not TASK_ID.match(task):
        raise ClaimError(
            f"bad task id {task!r}: letters, digits, '.', '_', '-'; up to 64 chars"
        )
    return task


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def owner_key(worktree: Path) -> str:
    # Case-folded POSIX path: Windows paths compare case-insensitively and come back from
    # tools as C:\, c:/ or /c/.
    return f"{socket.gethostname().lower()}|{worktree.resolve().as_posix().lower()}"


def _git(
    cfg: Config, *argv: str, stdin: str | None = None
) -> subprocess.CompletedProcess:
    return git.run(cfg.root, *argv, stdin=stdin)


def _ok(r: subprocess.CompletedProcess, what: str) -> str:
    if r.returncode != 0:
        raise ClaimError(f"{what}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def _remote_sha(cfg: Config, task: str) -> str | None:
    out = _ok(_git(cfg, "ls-remote", REMOTE, PREFIX + task), "git ls-remote")
    line = out.strip()
    return line.split()[0] if line else None


def read(cfg: Config, task: str) -> tuple[str, Claim] | None:
    sha = _remote_sha(cfg, check_id(task))
    if sha is None:
        return None
    if _git(cfg, "cat-file", "-e", sha).returncode != 0:
        _ok(_git(cfg, "fetch", "--quiet", REMOTE, PREFIX + task), "git fetch claim")
    body = _ok(_git(cfg, "cat-file", "-p", sha), "git cat-file")
    try:
        return sha, Claim(**json.loads(body))
    except (json.JSONDecodeError, TypeError) as e:
        raise ClaimError(f"{PREFIX}{task} holds an unreadable claim ({e})") from e


def list_all(cfg: Config) -> list[str]:
    out = _ok(_git(cfg, "ls-remote", REMOTE, PREFIX + "*"), "git ls-remote")
    return sorted(line.split()[1][len(PREFIX) :] for line in out.splitlines() if line)


def _blob(cfg: Config, claim: Claim) -> str:
    body = json.dumps(asdict(claim), indent=1, sort_keys=True) + "\n"
    return _ok(
        _git(cfg, "hash-object", "-w", "--stdin", stdin=body), "git hash-object"
    ).strip()


def create(cfg: Config, claim: Claim) -> bool:
    """True if we won. False if the ref already exists (someone else got there first)."""
    blob = _blob(cfg, claim)
    r = _git(cfg, "push", "--quiet", REMOTE, f"{blob}:{PREFIX}{claim.task}")
    if r.returncode == 0:
        return True
    if _remote_sha(cfg, claim.task) is not None:
        return False
    raise ClaimError(f"push claim: {r.stderr.strip()}")


def _swap(cfg: Config, task: str, expect: str, new: str | None) -> bool:
    """Compare-and-swap `refs/claims/<task>` from `expect` to blob `new` (None deletes).
    False only when origin holds something else than `expect` — the lease is lost. A push
    that fails while origin still holds `expect` is retried, then raises PushFailed."""
    if new == expect:
        # Nothing to push (a heartbeat within the same second), but the lease must hold.
        try:
            return _remote_sha(cfg, task) == expect
        except ClaimError as e:
            raise PushFailed(f"check {PREFIX + task}: {e}") from e
    ref = PREFIX + task
    lease = f"--force-with-lease={ref}:{expect}"
    err = ""
    for pause in (*RETRY_PAUSES_S, None):
        r = _git(cfg, "push", "--quiet", lease, REMOTE, f"{new or ''}:{ref}")
        if r.returncode == 0:
            return True
        err = (r.stderr or r.stdout).strip()
        try:
            now = _remote_sha(cfg, task)
        except ClaimError as e:
            now, err = (
                expect,
                str(e),
            )  # origin unreachable: nothing says the lease is lost
        if now == new:
            return True  # the push landed; only its report was lost
        if now != expect:
            return False
        if pause is not None:
            time.sleep(pause)
    raise PushFailed(
        f"push {ref} failed: {err.splitlines()[-1] if err else 'unknown error'}"
    )


def replace(cfg: Config, expect: str, claim: Claim) -> bool:
    """Compare-and-swap: only replaces the claim whose blob is `expect`. False if the
    claim changed on origin; raises PushFailed if origin could not be updated."""
    return _swap(cfg, claim.task, expect, _blob(cfg, claim))


def read_json_ref(cfg: Config, ref: str) -> tuple[str | None, dict]:
    """A JSON blob kept on origin under `ref` (round counters); (None, {}) if absent.
    Raises when origin cannot be asked — a counter that reads as zero offline is a bypass."""
    out = _ok(_git(cfg, "ls-remote", REMOTE, ref), "git ls-remote").strip()
    if not out:
        return None, {}
    sha = out.split()[0]
    if _git(cfg, "cat-file", "-e", sha).returncode != 0:
        _ok(_git(cfg, "fetch", "--quiet", REMOTE, ref), f"git fetch {ref}")
    try:
        data = json.loads(_ok(_git(cfg, "cat-file", "-p", sha), "git cat-file") or "{}")
    except json.JSONDecodeError as e:
        raise ClaimError(f"{ref} holds unreadable JSON ({e})") from e
    return sha, data if isinstance(data, dict) else {}


def write_json_ref(cfg: Config, ref: str, expect: str | None, data: dict) -> bool:
    """Compare-and-swap `data` into `ref`; False if someone changed it since `expect`."""
    blob = _ok(_git(cfg, "hash-object", "-w", "--stdin", stdin=json.dumps(data) + "\n"),
               "git hash-object").strip()  # fmt: skip
    lease = f"--force-with-lease={ref}:{expect or ''}"
    return _git(cfg, "push", "--quiet", lease, REMOTE, f"{blob}:{ref}").returncode == 0


def delete(cfg: Config, task: str, expect: str) -> bool:
    """Like `replace`: False if the claim changed on origin, PushFailed if unreachable."""
    return _swap(cfg, task, expect, None)

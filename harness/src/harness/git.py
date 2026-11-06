"""Every git call `h` makes goes through `run`.

An agent cannot answer a prompt, so git never gets one: no terminal, no askpass helper, no
Git Credential Manager window; an auth failure comes back as an error with a hint. Each call
has a timeout that kills the whole process tree (fetch spawns git-remote-https, which keeps
the pipes open after git itself is killed). Variables a git hook exports (`GIT_DIR`,
`GIT_INDEX_FILE`, …) are dropped, so `-C <path>` always means that repository.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from .proc import TEXT

# Set by config.load from `[git] timeout_local_s` / `timeout_network_s`.
TIMEOUTS = {"local": 60.0, "network": 300.0}

NETWORK = {"fetch", "push", "pull", "clone", "ls-remote"}
# Reads that would otherwise take index.lock to refresh stat data — and fail a parallel
# session's `git commit` in the same clone.
READS = {
    "status", "diff", "rev-parse", "log", "ls-files", "merge-base", "show", "cat-file",
    "rev-list", "cherry", "for-each-ref", "worktree list",
}  # fmt: skip
# Repository selectors and injected config a parent git process (a hook) may export.
SCRUB = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "SSH_ASKPASS",
)  # fmt: skip
AUTH_MARKERS = (
    "terminal prompts disabled",
    "could not read username",
    "could not read password",
    "authentication failed",
)
AUTH_HINT = (
    "hint: git needs credentials and h never prompts — "
    "run `gh auth login` and `gh auth setup-git`, then retry"
)
SSH_MARKERS = ("permission denied (publickey)", "host key verification failed")
SSH_HINT = (
    "hint: ssh needs a key or a host-key answer and h never prompts — "
    "load the key into ssh-agent or accept the host key once in your terminal, then retry"
)


class GitError(Exception):
    pass


class GitTimeout(GitError):
    pass


def subcommand(argv: tuple[str, ...]) -> str:
    """The git command in `argv`, past global options like `-c k=v`; `worktree list` keeps
    its verb, since other `worktree` verbs write."""
    it = iter(argv)
    for a in it:
        if a in ("-c", "-C"):
            next(it, None)
        elif not a.startswith("-"):
            return f"{a} list" if a == "worktree" and next(it, None) == "list" else a
    return ""


def env(read_only: bool = False) -> dict[str, str]:
    e = {k: v for k, v in os.environ.items() if k not in SCRUB}
    e["GIT_TERMINAL_PROMPT"] = "0"
    e["GCM_INTERACTIVE"] = "never"
    e["GIT_ASKPASS"] = ""  # set but empty: git skips core.askPass and SSH_ASKPASS too
    if read_only:
        e["GIT_OPTIONAL_LOCKS"] = "0"
    return e


def run(
    cwd: Path | str | None,
    *argv: str,
    stdin: str | None = None,
    text: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """`git [-C cwd] argv`; never raises on a non-zero exit, raises GitTimeout on a hang."""
    sub = subcommand(argv)
    limit = timeout or TIMEOUTS["network" if sub in NETWORK else "local"]
    args = ["git", "-c", "safe.bareRepository=explicit"]
    args += ["-C", str(cwd)] if cwd is not None else []
    args += argv
    windows = os.name == "nt"
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env(read_only=sub in READS),
        **(TEXT if text else {}),
        start_new_session=not windows,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
    )
    try:
        out, err = proc.communicate(stdin, timeout=limit)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        raise GitTimeout(
            f"git {' '.join(argv)} timed out after {limit:g}s"
            + (f" (in {cwd})" if cwd is not None else "")
        ) from None
    except BaseException:  # Ctrl-C: the new process group would not get it
        _kill_tree(proc)
        raise
    if proc.returncode != 0 and text:
        low = err.lower()
        if any(m in low for m in AUTH_MARKERS):
            err = f"{err.rstrip()}\n{AUTH_HINT}\n"
        elif any(m in low for m in SSH_MARKERS):
            err = f"{err.rstrip()}\n{SSH_HINT}\n"
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, check=False)  # fmt: skip
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

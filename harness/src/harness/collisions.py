"""Paths that differ only by case.

git and Linux CI keep `Foo.ts` and `foo.ts` (or `docs/ADR/` and `docs/adr/`) apart; a
checkout on Windows or macOS writes both to one file, the worktree looks modified, and an
agent "fixes" edits nobody made. The check reads the file list only, it runs nothing.
A collision with a path the task touched is an error; an old one is a warning.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from . import affected, git
from .config import Config

STEP = "case-collisions"


@dataclass
class Found:
    errors: list[list[str]] = field(default_factory=list)  # touched by the task
    warnings: list[list[str]] = field(default_factory=list)  # already there
    paths: int = 0


def _names(path: str) -> Iterable[str]:
    """The path and each directory above it, directories with a trailing slash."""
    parts = path.split("/")
    prefix = ""
    for part in parts[:-1]:
        prefix += part + "/"
        yield prefix
    yield path


def find(paths: Iterable[str], changed: Iterable[str]) -> Found:
    """Group every path and directory prefix by casefold(). A file and a directory collide
    too (`foo` and `Foo/x`). Only the topmost collision is reported: `docs/ADR/` and
    `docs/adr/` are one, not one more per file name the two directories share."""
    groups: dict[str, set[str]] = {}
    count = 0
    for p in paths:
        count += 1
        for name in _names(p):
            groups.setdefault(name.rstrip("/").casefold(), set()).add(name)
    touched = {name for p in changed for name in _names(p)}
    found = Found(paths=count)
    for key, names in sorted(groups.items()):
        if len(names) < 2 or len(groups.get(key.rpartition("/")[0], ())) > 1:
            continue
        pair = sorted(names)
        (found.errors if touched & names else found.warnings).append(pair)
    return found


def scan(cfg: Config) -> Found:
    """Tracked and untracked (not ignored) files against what the task changed. Without a
    base branch every collision counts as the task's: a gate that cannot tell stays shut."""
    out = git.run(
        cfg.root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
    )
    if out.returncode != 0:
        raise git.GitError(f"git ls-files: {out.stderr.strip()}")
    paths = [p for p in out.stdout.split("\0") if p]
    try:
        changed = affected.changed_files(cfg, affected.resolve_base(cfg, None))
    except git.GitError:
        changed = paths
    return find(paths, changed)


def report(found: Found) -> tuple[bool, str, list[str]]:
    """(ok, log text, warning lines) for the check step."""
    lines = []
    if found.errors:
        lines.append(
            f"{STEP}: FAIL — {len(found.errors)} path(s) the task touched differ from "
            "another only by case; Windows and macOS write them to one file"
        )
        lines += [f"  {' | '.join(pair)}" for pair in found.errors]
        lines.append("  rename one so the names differ by more than case (git mv)")
    warnings = [f"old case collision: {' | '.join(pair)}" for pair in found.warnings]
    lines += warnings
    if not lines:
        lines.append(f"{STEP}: ok — {found.paths} paths, no case collisions")
    return not found.errors, "\n".join(lines) + "\n", warnings

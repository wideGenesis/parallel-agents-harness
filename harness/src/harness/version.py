"""The h a project requires: `[harness] version = ">=X.Y[.Z]"` in harness.toml.

One `h` per machine serves several projects. A project written for a newer `h` would have
its gates run by older rules, or fail on config keys the old `h` does not know, so every
command except the ones that diagnose (`--version`, `doctor`, `config`) refuses. Parsing is
our own on stdlib (ADR-0001): a minimum version is all a project needs to say.
"""

from __future__ import annotations

import re

from . import __version__, installed

SPEC = re.compile(r"^\s*>=\s*(\d+)\.(\d+)(?:\.(\d+))?\s*$")
VERSION = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?$")
# Commands that run on a mismatch: they are how the user finds out what is wrong.
UNCHECKED = ("doctor", "config")


def parse_spec(spec: str) -> tuple[int, int, int]:
    """The minimum version in `">=X.Y[.Z]"`; ValueError on anything else."""
    m = SPEC.match(spec)
    if not m:
        raise ValueError(f'harness.version: expected ">=X.Y[.Z]", got {spec!r}')
    return int(m[1]), int(m[2]), int(m[3] or 0)


def parse(v: str) -> tuple[int, int, int]:
    m = VERSION.match(v)
    if not m:
        raise ValueError(f"not a version: {v!r}")
    return int(m[1]), int(m[2]), int(m[3] or 0)


def update_command() -> str:
    source = installed._source()
    if source is None:
        return "reinstall h from the harness checkout: `uv tool install --reinstall <checkout>/harness`"
    src, editable = source
    if editable:
        return f"`git -C {src.as_posix()} pull`"
    return f"`uv tool install --reinstall {src.as_posix()}` (after `git pull` there)"


def mismatch(required: str, have: str = __version__) -> str | None:
    """Why the installed h may not run this project, or None when it may."""
    if not required or parse(have) >= parse_spec(required):
        return None
    return (f"harness.toml requires h {required.strip()}, installed h {have} — "
            f"update: {update_command()}")  # fmt: skip

"""Is a tool really usable — not just present on PATH — and which copy to run.

rustup installs proxies (`rustfmt`, `cargo-clippy`) for every component, so `which` finds
them even when the component is missing from the active toolchain; the proxy then fails
at call time. Running `<tool> --version` is the only honest check.
"""

from __future__ import annotations

import functools
import re
import shutil
import subprocess
import tomllib
from pathlib import Path


@functools.cache
def usable(tool: str) -> bool:
    exe = shutil.which(tool)
    if exe is None:
        return False
    try:
        r = subprocess.run(
            [exe, "--version"], capture_output=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


@functools.cache
def pinned_in(pyproject: Path, tool: str) -> bool:
    """True if the project declares `tool` as a dependency (any group or extra)."""
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    project = data.get("project", {})
    reqs = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        reqs += group
    for group in data.get("dependency-groups", {}).values():
        reqs += [r for r in group if isinstance(r, str)]
    name = re.compile(rf"^\s*{re.escape(tool)}\s*([<>=!~;\[ ]|$)", re.IGNORECASE)
    return any(name.match(r) for r in reqs)


def in_venv(stack_root: Path, tool: str) -> bool:
    """The tool is installed in the project's venv (`uv sync` put it there)."""
    venv = stack_root / ".venv"
    return any(
        (venv / d / name).is_file()
        for d in ("Scripts", "bin")
        for name in (tool, f"{tool}.exe")
    )


def project_tool(stack_root: Path, tool: str) -> list[str] | None:
    """The copy of a python tool the project pinned or installed in its venv, run through
    `uv run`; None when the project has none (a venv-only pyright was not found)."""
    if not usable("uv"):
        return None
    if pinned_in(stack_root / "pyproject.toml", tool) or in_venv(stack_root, tool):
        # --project: the hook runs from the project root, not the stack root.
        return ["uv", "run", "--quiet", "--project", str(stack_root), tool]
    return None


def ruff(stack_root: Path) -> list[str] | None:
    """The ruff the project pinned (`uv run ruff`), else a global one.

    A global ruff and the one CI downloads can be different versions with different rules:
    the slice of 2026-10-21 passed lint locally and failed RUF059 in CI. Pinning the linter
    in the project makes local and CI runs the same program.
    """
    if (cmd := project_tool(stack_root, "ruff")) is not None:
        return cmd
    if usable("ruff"):
        return ["ruff"]
    if usable("uvx"):
        return ["uvx", "ruff"]
    return None

"""Test tethering: every test file is run by some `h check` step.

A test that no step runs is worse than no test — it looks like coverage. Two silent cases
were found: a TS package with test files but no `test` script (the adapter runs
`pnpm run --if-present test`), and test files outside every package, which no scoped step
ever passes to a runner. The check reads the file list only, it runs nothing.
Deliberate exceptions live in `[check] untethered = { "<glob>" = "<reason>" }`, read from
the base branch like the other gate rules, so a task branch cannot excuse its own files.
"""

from __future__ import annotations

import configparser
import fnmatch
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import affected, git
from .config import Config
from .ratchet import VENDORED
from .stacks import Package, Stack

# By the stack's convention. Rust unit tests live in src/ and always run with the crate.
PY_TEST = re.compile(r"^(test_.*|.*_test)\.py$")
TS_TEST = re.compile(r"\.(test|spec)\.(ts|tsx|mts|cts|js|jsx|mjs|cjs)$")
# What decides whether pytest collects a file given an explicit package path: `testpaths`
# applies only without arguments, so these two. pytest's defaults, replaced by the config.
PY_FILES = ("test_*.py", "*_test.py")
NORECURSE = (
    "*.egg",
    ".*",
    "_darcs",
    "build",
    "CVS",
    "dist",
    "node_modules",
    "venv",
    "{arch}",
)
# Config files in pytest's order of precedence; the section that makes each one count.
PYTEST_INIS = (
    ("pytest.ini", "pytest"),
    ("pyproject.toml", ""),
    ("tox.ini", "pytest"),
    ("setup.cfg", "tool:pytest"),
)


@dataclass
class Loose:
    path: str  # repo-relative, posix
    stack: str
    why: str


def is_test(stack: str, rel: PurePosixPath) -> bool:
    if stack == "python":
        return bool(PY_TEST.match(rel.name))
    if stack == "ts":
        return bool(TS_TEST.search(rel.name))
    return len(rel.parts) >= 2 and rel.parts[-2] == "tests" and rel.suffix == ".rs"


def _files(cfg: Config) -> list[str]:
    r = git.run(
        cfg.root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
    )
    if r.returncode != 0:
        raise affected.GitError(f"git ls-files: {r.stderr.strip()}")
    return sorted(
        f
        for f in set(r.stdout.split("\0"))
        if f and not VENDORED & set(PurePosixPath(f).parts) and (cfg.root / f).is_file()
    )


def _owner(st: Stack, path: Path) -> Package | None:
    best = None
    for p in st.packages:
        if path.is_relative_to(p.path.resolve()) and (
            best is None or len(p.path.parts) > len(best.path.parts)
        ):
            best = p
    return best


def _has_test_script(pkg: Package) -> bool:
    try:
        pj = json.loads((pkg.path / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(pj.get("scripts", {}).get("test"))


def _excused(rel: str, exceptions: dict[str, str]) -> bool:
    return any(rel == g or fnmatch.fnmatchcase(rel, g) for g in exceptions)


def _read_ini(path: Path, section: str) -> dict[str, list[str]] | None:
    """pytest options from one file; None when the file is not a pytest config."""
    try:
        if path.name == "pyproject.toml":
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
            opts = raw.get("tool", {}).get("pytest", {}).get("ini_options")
            if opts is None:
                return None
        else:
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(path, encoding="utf-8")
            if not cp.has_section(section):
                return {} if path.name == "pytest.ini" else None
            opts = dict(cp.items(section))
    except (OSError, ValueError, configparser.Error):
        return None
    out: dict[str, list[str]] = {}
    for key in ("python_files", "norecursedirs"):
        v = opts.get(key)
        if v is not None:
            out[key] = [str(x) for x in v] if isinstance(v, list) else str(v).split()
    return out


def _pytest_opts(start: Path, stop: Path) -> dict[str, list[str]]:
    """The options of the config pytest would use: the nearest one walking up from the
    package to the stack root (pytest runs with the stack root as its working directory)."""
    d = start
    while True:
        for name, section in PYTEST_INIS:
            if (d / name).is_file() and (
                opts := _read_ini(d / name, section)
            ) is not None:
                return opts
        if d == stop or d.parent == d:
            return {}
        d = d.parent


def _uncollected(path: Path, pkg: Package, st_root: Path) -> str:
    """Why pytest, given the package path, would not collect this test file; "" if it would."""
    base = pkg.path.resolve()
    opts = _pytest_opts(base, st_root)
    rel = path.relative_to(base)
    files = opts.get("python_files", list(PY_FILES))
    if not any(fnmatch.fnmatch(rel.name, g) for g in files):
        return f"pytest python_files {files} does not match it"
    norecurse = opts.get("norecursedirs", list(NORECURSE))
    for part in rel.parts[:-1]:
        if any(fnmatch.fnmatch(part, g) for g in norecurse):
            return f"under {part}/, which pytest norecursedirs skips"
    return ""


def find(cfg: Config, stacks: list[Stack]) -> list[Loose]:
    """Test files that no check step of their stack would run."""
    loose: list[Loose] = []
    files = _files(cfg)
    root = cfg.root.resolve()
    for st in stacks:
        if cfg.stacks[st.name].commands.get("test"):
            continue  # an override command runs what it runs — harness cannot tell
        st_root = st.root.resolve()
        for rel in files:
            path = (root / rel).resolve()
            if not path.is_relative_to(st_root):
                continue
            if not is_test(
                st.name, PurePosixPath(path.relative_to(st_root).as_posix())
            ):
                continue
            if _excused(rel, cfg.untethered):
                continue
            pkg = _owner(st, path)
            if pkg is None:
                why = f"outside every {st.name} package — no step passes it to a runner"
            elif st.name == "ts" and not _has_test_script(pkg):
                why = f"package {pkg.name} has no `test` script in package.json"
            elif st.name == "python" and (why := _uncollected(path, pkg, st_root)):
                pass
            else:
                continue
            loose.append(Loose(rel, st.name, why))
    return loose


def has_tests(cfg: Config, st: Stack, pkgs: list[Package]) -> bool:
    """Whether the packages hold test files: then "no tests collected" is a failure.
    Outside a git checkout this cannot tell, and the old leniency stays."""
    try:
        files = _files(cfg)
    except affected.GitError:
        return False
    root = cfg.root.resolve()
    bases = [p.path.resolve() for p in pkgs]
    for rel in files:
        if _excused(rel, cfg.untethered):
            continue  # a test file run on purpose by nothing is not a test of the package
        path = (root / rel).resolve()
        for base in bases:
            if path.is_relative_to(base) and is_test(
                st.name, PurePosixPath(path.relative_to(base).as_posix())
            ):
                return True
    return False

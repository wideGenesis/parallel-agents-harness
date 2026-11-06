"""`h check` deadcode step: unused dependencies, functions and files per stack.

Agents leave unused functions, files and dependencies behind after a refactor; review does
not catch it. Each stack runs its own tools — Rust `cargo-machete`, Python `deptry` +
`vulture`, TS `knip` — and the harness parses their output itself, so one whitelist format
serves every stack and one rule decides what blocks:

- `deps` (an unused or undeclared dependency) blocks by default: the fix is one line;
- `code` (an unused function, export or file) only reports until a baseline exists.

`[check.deadcode] deps / code = "block" | "report"` switches them. False positives go to the
stack's whitelist file, one entry per line with the reason after `#`; the ratchet counts
the entries, so the whitelist grows only by an explicit `h ratchet --update --allow-growth`.

A tool that is not installed is a red step with the install command, not a skip: a check
that silently does not run is how the debt piles up.
"""

from __future__ import annotations

import json
import re
import tempfile
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from . import proc, tools
from .config import Config
from .stacks import Package, Stack

WHITELIST = ".harness/deadcode/{stack}.txt"  # default, relative to the project root
VULTURE_CONFIDENCE = 80  # agreed 2026-10-23: below it vulture guesses
SHOW = 10  # reported findings shown as warnings; the rest are in the log
SKIP_DIRS = {".venv", "venv", "node_modules", "__pycache__", ".git", ".artifacts",
             ".worktrees", "target"}  # fmt: skip
# Python dev tools `h` itself runs: pinned for h, never imported — not dead.
H_DRIVEN = {"ruff", "pyright", "ty", "pytest", "pytest-testmon", "deptry", "vulture"}


class Missing(Exception):
    """The stack's tool is not installed; the message says how to install it."""


@dataclass(frozen=True)
class Finding:
    kind: str  # deps | code
    name: str  # the dependency, symbol or file
    path: str  # project-relative, POSIX
    line: int  # 0 = unknown
    what: str

    def keys(self) -> set[str]:
        """Whitelist entries that silence it: the bare name or `path:name`."""
        return {self.name, f"{self.path}:{self.name}"}

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where}: {self.what}"


def whitelist_path(cfg: Config, stack: str) -> Path:
    sc = cfg.stacks.get(stack)
    rel = (sc.deadcode_whitelist if sc else "") or WHITELIST.format(stack=stack)
    return cfg.root / rel


def entries(path: Path) -> Iterator[tuple[int, str, str]]:
    """(line number, entry, reason) of every entry; blank and `#` lines are comments."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for i, raw in enumerate(text.split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entry, _, reason = line.partition("#")
        yield i, entry.strip(), reason.strip()


def _rel(cfg: Config, base: Path, path: str) -> str:
    p = Path(path.replace("\\", "/"))
    p = p if p.is_absolute() else base / p
    try:
        return p.resolve().relative_to(cfg.root.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def _line_of(file: Path, name: str) -> int:
    """The manifest line that declares `name` — so a dependency finding has file:line."""
    try:
        lines = file.read_text(encoding="utf-8").split("\n")
    except OSError:
        return 0
    decl = re.compile(rf"""(^|["'\s{{,\[]){re.escape(name)}(["'\s=<>~!;\[@:]|$)""",
                      re.IGNORECASE)  # fmt: skip
    return next((i for i, ln in enumerate(lines, 1) if decl.search(ln)), 0)


def _sel(cfg: Config, st: Stack, pkgs: list[Package]) -> list[str]:
    return [p.path.resolve().relative_to(st.root.resolve()).as_posix() or "."
            for p in pkgs]  # fmt: skip


def exited(code: int) -> str:
    """How a tool ended, for the first line of its failure: 137 or SIGKILL tells
    an out-of-memory kill from the tool's own bug."""
    if code < 0:
        name = SIGNALS.get(-code)
        return f"killed by {name}" if name else f"killed by signal {-code}"
    return f"exit {code}"


# POSIX numbers, not the `signal` module: on Windows it has no SIGKILL, and h names what
# the runner's process got.
SIGNALS = {
    1: "SIGHUP",
    2: "SIGINT",
    6: "SIGABRT",
    9: "SIGKILL",
    11: "SIGSEGV",
    15: "SIGTERM",
}


# --- Rust: cargo-machete -------------------------------------------------------------

MACHETE_CRATE = re.compile(r"^(\S+) -- (.+Cargo\.toml):$")


def rust(cfg: Config, st: Stack, pkgs: list[Package], everything: bool,
         env: dict[str, str]) -> tuple[list[Finding], str]:  # fmt: skip
    if not tools.usable("cargo-machete"):
        raise Missing(
            "cargo-machete not installed — `cargo install cargo-machete --locked` "
            "(or `cargo binstall cargo-machete`)"
        )
    cmd = ["cargo", "machete", *([] if everything else _sel(cfg, st, pkgs))]
    r = proc.run(cmd, st.root, env=env)
    out = r.stdout + r.stderr
    if r.returncode not in (0, 1):
        raise RuntimeError(f"$ {' '.join(cmd)} — {exited(r.returncode)}\n{out}")
    found, manifest = [], ""
    for line in out.splitlines():
        if m := MACHETE_CRATE.match(line):
            manifest = _rel(cfg, st.root, m.group(2))
        elif manifest and line.startswith("\t") and line.strip():
            dep = line.strip()
            n = _line_of(cfg.root / manifest, dep)
            found.append(
                Finding("deps", dep, manifest, n, f"unused dependency `{dep}`")
            )
        elif not line.strip():
            manifest = ""
    # Unused functions are rustc's dead_code lint: the lint step (clippy -D warnings).
    return found, f"$ {' '.join(cmd)}\n{out}"


# --- Python: deptry (runtime deps), dev groups, vulture ------------------------------

DEPTRY_CODES = ("DEP002", "DEP004")  # unused; dev dependency imported by the code
VULTURE = re.compile(r"^(.+?):(\d+): (.*?'([^']+)'.*?) \((\d+)% confidence")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _py_tool(st: Stack, tool: str) -> list[str]:
    cmd = tools.project_tool(st.root, tool)
    if cmd is None:
        raise Missing(
            f"{tool} is not a dependency of the project — `uv add --dev deptry vulture` "
            f"in {st.root.name or '.'}/ (the step runs the project's pinned copy)"
        )
    return cmd


def _imports(root: Path) -> set[str]:
    """Top-level modules imported anywhere under `root` (outside venvs and caches)."""
    out: set[str] = set()
    imp = re.compile(r"^\s*import\s+([\w.,\s]+)|^\s*from\s+(\w+)")
    for f in root.rglob("*.py"):
        if SKIP_DIRS & set(f.relative_to(root).parts):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.split("\n"):
            if m := imp.match(line):
                names = m.group(1).split(",") if m.group(1) else [m.group(2)]
                out.update(n.strip().split(".")[0].split(" ")[0] for n in names)
    return out


DISTS = ("import importlib.metadata as m, json; "
         "print(json.dumps(m.packages_distributions()))")  # fmt: skip


def _modules(st: Stack, env: dict[str, str]) -> dict[str, set[str]]:
    """Distribution (normalised) → the top-level modules it installs, from the venv."""
    r = proc.run(["uv", "run", "--quiet", "--project", str(st.root), "python", "-c",
                  DISTS], st.root, env=env)  # fmt: skip
    try:
        data = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}
    out: dict[str, set[str]] = {}
    for module, dists in data.items():
        for d in dists:
            out.setdefault(_norm(d), set()).add(module)
    return out


def _dev_groups(manifest: Path) -> list[str]:
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    reqs = [x for g in data.get("dependency-groups", {}).values() for x in g
            if isinstance(x, str)]  # fmt: skip
    reqs += data.get("tool", {}).get("uv", {}).get("dev-dependencies", [])
    return [m.group(1) for x in reqs if (m := re.match(r"\s*([A-Za-z0-9][\w.-]*)", x))]


def _dev_unused(cfg: Config, st: Stack, pkgs: list[Package], everything: bool,
                env: dict[str, str]) -> list[Finding]:  # fmt: skip
    """deptry never checks dev dependencies (DEP002 skips them); the user wants them
    checked too (2026-10-25): a dev dependency no file imports is unused. Root groups
    serve the whole workspace, so they are checked on a full or stack-wide run."""
    scopes = [(p.path, p.path / "pyproject.toml") for p in pkgs]
    if everything and all(p.path.resolve() != st.root.resolve() for p in pkgs):
        scopes.append((st.root, st.root / "pyproject.toml"))
    members = {_norm(p.name) for p in st.packages}
    wanted = [(scope, mf, d) for scope, mf in scopes for d in _dev_groups(mf)
              if _norm(d) not in members | H_DRIVEN]  # fmt: skip
    if not wanted:
        return []
    modules = _modules(st, env)
    imported: dict[Path, set[str]] = {}
    found = []
    for scope, manifest, dep in wanted:
        if scope not in imported:
            imported[scope] = _imports(scope)
        mods = modules.get(_norm(dep)) or {_norm(dep).replace("-", "_")}
        if not mods & imported[scope]:
            rel = _rel(cfg, st.root, str(manifest))
            n = _line_of(manifest, dep)
            what = f"unused dev dependency `{dep}` (no file imports it)"
            found.append(Finding("deps", dep, rel, n, what))
    return found


def python(cfg: Config, st: Stack, pkgs: list[Package], everything: bool,
           env: dict[str, str]) -> tuple[list[Finding], str]:  # fmt: skip
    deptry, vulture = _py_tool(st, "deptry"), _py_tool(st, "vulture")
    found: list[Finding] = []
    log: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, p in enumerate(pkgs):
            report = Path(tmp) / f"{i}.json"
            cmd = [*deptry, ".", "--json-output", str(report)]
            r = proc.run(cmd, p.path, env=env)
            log.append(f"$ {' '.join(cmd)}  (in {_rel(cfg, p.path, '.')})\n"
                       f"{r.stdout}{r.stderr}")  # fmt: skip
            try:
                issues = json.loads(report.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise RuntimeError(f"deptry produced no report — {exited(r.returncode)}\n"
                                   f"{log[-1]}") from None  # fmt: skip
            for it in issues:
                code = it.get("error", {}).get("code", "")
                if code not in DEPTRY_CODES:
                    continue
                loc = it.get("location", {})
                file = _rel(cfg, p.path, loc.get("file") or "pyproject.toml")
                name = it.get("module", "")
                n = loc.get("line") or _line_of(cfg.root / file, name)
                found.append(Finding("deps", name, file, n,
                                     f"{code} {it['error'].get('message', '')}"))  # fmt: skip
    found += _dev_unused(cfg, st, pkgs, everything, env)
    paths = _sel(cfg, st, pkgs)
    # vulture matches patterns against absolute paths: `.worktrees` (or `*/.worktrees/*`)
    # would exclude everything inside a task worktree. So: rooted at the stack, plus venvs
    # and caches at any depth, whose names never appear above a project.
    root = st.root.resolve().as_posix()
    skip = ",".join([f"{root}/{d}/*" for d in sorted(SKIP_DIRS)]
                    + [f"*/{d}/*" for d in (".venv", "node_modules", "__pycache__")])  # fmt: skip
    cmd = [*vulture, *paths, f"--min-confidence={VULTURE_CONFIDENCE}",
           f"--exclude={skip}"]  # fmt: skip
    r = proc.run(cmd, st.root, env=env)
    log.append(f"$ {' '.join(cmd)}\n{r.stdout}{r.stderr}")
    if r.returncode not in (0, 3):  # 3 = dead code found
        raise RuntimeError(f"vulture {exited(r.returncode)}\n" + "\n".join(log))
    for line in r.stdout.splitlines():
        if m := VULTURE.match(line):
            file = _rel(cfg, st.root, m.group(1))
            found.append(Finding("code", m.group(4), file, int(m.group(2)),
                                 m.group(3)))  # fmt: skip
    return found, "\n".join(log)


# --- TS: knip -------------------------------------------------------------------------

KNIP_DEPS = ("dependencies", "devDependencies", "optionalPeerDependencies", "unlisted",
             "binaries", "unresolved", "catalog", "catalogReferences")  # fmt: skip
KNIP_CODE = ("files", "exports", "types", "nsExports", "nsTypes", "enumMembers",
             "namespaceMembers", "duplicates")  # fmt: skip


def knip_bin(root: Path) -> str | None:
    # node_modules/.bin directly: `pnpm exec` may install first and rewrite the lockfile.
    for name in ("knip.cmd", "knip"):
        if (exe := root / "node_modules" / ".bin" / name).is_file():
            return str(exe)
    return None


def knip_pinned(root: Path) -> bool:
    try:
        pj = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pj = {}
    return "knip" in {**pj.get("dependencies", {}), **pj.get("devDependencies", {})}


def _knip(st: Stack) -> str:
    if exe := knip_bin(st.root):
        return exe
    raise Missing(
        "knip is pinned but not installed — run `pnpm install`" if knip_pinned(st.root)
        else "knip not installed — `pnpm add -Dw knip` (the step runs the project's copy)"
    )  # fmt: skip


def _last_json(out: str) -> dict:
    try:
        data = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}
    return data if isinstance(data, dict) else {}


def ts(cfg: Config, st: Stack, pkgs: list[Package], everything: bool,
       env: dict[str, str]) -> tuple[list[Finding], str]:  # fmt: skip
    sel = (
        []
        if everything
        else [a for d in _sel(cfg, st, pkgs) for a in ("--workspace", d)]
    )
    cmd = [_knip(st), "--reporter", "json", "--no-progress", *sel]
    r = proc.run(cmd, st.root, env=env)
    log = f"$ {' '.join(cmd)}\n{r.stdout}{r.stderr}"
    data = _last_json(r.stdout) if r.returncode in (0, 1) else {}
    if "issues" not in data:  # exit 2 or no report: knip itself failed
        raise RuntimeError(f"knip {exited(r.returncode)}\n{log}")
    found = []
    for issue in data["issues"]:
        file = _rel(cfg, st.root, issue.get("file", ""))
        for kind, keys in (("deps", KNIP_DEPS), ("code", KNIP_CODE)):
            for key in keys:
                for item in issue.get(key) or []:
                    # knip 5 listed plain names, knip 6 objects with a position
                    item = {"name": item} if isinstance(item, str) else item
                    name = item.get("name", "")
                    n = int(item.get("line") or 0) or (
                        _line_of(cfg.root / file, name) if kind == "deps" else 0
                    )
                    what = (
                        ""
                        if key in ("unlisted", "unresolved", "binaries")
                        else "unused "
                    )
                    found.append(Finding(kind, name, file, n, f"{what}{key}: `{name}`"))
    return found, log


ADAPTERS = {"rust": rust, "python": python, "ts": ts}


def missing(cfg: Config, st: Stack) -> str | None:
    """What `h doctor` reports: the stack's deadcode tool is not installed."""
    try:
        if st.name == "rust" and not tools.usable("cargo-machete"):
            raise Missing(
                "cargo-machete not installed — `cargo install cargo-machete --locked`"
            )
        if st.name == "python":
            _py_tool(st, "deptry")
            _py_tool(st, "vulture")
        if st.name == "ts":
            _knip(st)
    except Missing as e:
        return f"{st.name} deadcode: {e}"
    return None


def run(cfg: Config, st: Stack, pkgs: list[Package], everything: bool,
        env: dict[str, str]) -> tuple[bool, str, list[str]]:  # fmt: skip
    """(ok, log, warnings) for check.Step.call: blocking findings fail the step, reported
    ones are warnings on a green step (the first SHOW; all of them are in the log)."""
    wl_path = whitelist_path(cfg, st.name)
    wl_rel = _rel(cfg, cfg.root, str(wl_path))
    bad = [f"error: {wl_rel}:{n}: `{e}` needs a reason after `#`"
           for n, e, why in entries(wl_path) if not why]  # fmt: skip
    allowed = {e for _, e, _ in entries(wl_path)}
    try:
        found, log = ADAPTERS[st.name](cfg, st, pkgs, everything, env)
    except Missing as e:
        return False, f"error: {e}\n", []
    except RuntimeError as e:
        return False, f"error: {st.name} deadcode tool failed\n{e}\n", []
    kept = [f for f in found if not f.keys() & allowed]
    block = [f for f in kept if cfg.deadcode.get(f.kind) == "block"]
    report = [f for f in kept if f not in block]
    lines = [f"deadcode {st.name} findings (tool output above):"]
    lines += [f"warning: {f}" for f in report] + [f"error: {f}" for f in block] + bad
    lines.append(
        f"deadcode {st.name}: {len(block)} blocking, {len(report)} reported, "
        f"{len(found) - len(kept)} whitelisted — a false positive goes to {wl_rel} "
        "as `<name or path:name>  # reason`, then `h ratchet --update --allow-growth`"
    )
    warnings = [str(f) for f in report[:SHOW]]
    if len(report) > SHOW:
        warnings.append(f"… {len(report) - SHOW} more in the log")
    return not block and not bad, f"{log}\n" + "\n".join(lines) + "\n", warnings

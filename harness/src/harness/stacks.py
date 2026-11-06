"""Stack adapters: find packages, their internal dependencies, and the tools a stack needs.

Everything is read from manifests directly instead of calling `cargo metadata` / `uv` /
`pnpm`: it must be instant and work before dependencies are installed. The price: only
declared workspace dependencies form the graph — implicit links (shared fixtures, codegen)
are invisible, which is why the full suite still runs on main.
"""

from __future__ import annotations

import fnmatch
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import git
from .config import Config


@dataclass
class Package:
    name: str
    path: Path
    deps: list[str] = field(default_factory=list)  # names of packages in the same stack


@dataclass
class Stack:
    name: str
    root: Path
    manifest: Path
    packages: list[Package]
    tools: list[str]  # executables `h doctor` must find
    # Globs, relative to the stack root, of files that change how the whole stack builds,
    # lints or tests; touching one affects every package.
    global_files: list[str] = field(default_factory=list)

    def is_global(self, rel: str) -> bool:
        return any(fnmatch.fnmatchcase(rel, g) for g in self.global_files)

    def by_name(self) -> dict[str, Package]:
        return {p.name: p for p in self.packages}


def _expand(
    root: Path, patterns: list[str], exclude: list[str] | tuple[()] = ()
) -> list[Path]:
    dirs: set[Path] = set()
    for pat in patterns:
        dirs.update(p for p in root.glob(pat) if p.is_dir())
    for pat in exclude:
        dirs.difference_update(root.glob(pat))
    return sorted(dirs)


def _toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _rust(root: Path) -> Stack | None:
    manifest = root / "Cargo.toml"
    if not manifest.is_file():
        return None
    data = _toml(manifest)
    ws = data.get("workspace", {})
    dirs = _expand(root, ws.get("members", []), ws.get("exclude", []))
    if "package" in data:
        dirs.insert(0, root)

    # `foo.workspace = true` points at [workspace.dependencies]; only path entries are ours.
    ws_paths = {
        name: (root / spec["path"]).resolve()
        for name, spec in ws.get("dependencies", {}).items()
        if isinstance(spec, dict) and "path" in spec
    }
    pkgs, dep_paths = [], {}
    for d in dirs:
        m = d / "Cargo.toml"
        if not m.is_file():
            continue
        cdata = _toml(m)
        pkg = Package(cdata.get("package", {}).get("name", d.name), d)
        paths = []
        for section in ("dependencies", "dev-dependencies", "build-dependencies"):
            for name, spec in cdata.get(section, {}).items():
                if not isinstance(spec, dict):
                    continue
                if "path" in spec:
                    paths.append((d / spec["path"]).resolve())
                elif spec.get("workspace") and name in ws_paths:
                    paths.append(ws_paths[name])
        dep_paths[pkg.name] = paths
        pkgs.append(pkg)
    by_path = {p.path.resolve(): p.name for p in pkgs}
    for p in pkgs:
        p.deps = sorted({by_path[x] for x in dep_paths[p.name] if x in by_path})
    return Stack(
        "rust",
        root,
        manifest,
        pkgs,
        ["cargo", "rustfmt"],
        [
            "Cargo.toml",
            "Cargo.lock",
            "rust-toolchain.toml",
            "rust-toolchain",
            ".cargo/config.toml",
            "clippy.toml",
            "rustfmt.toml",
            ".rustfmt.toml",
            ".config/nextest.toml",
            "deny.toml",
            # sqlx::migrate! and query! read these at compile time; the sql step checks
            # them.
            "migrations/*",
            ".sqlx/*",
        ],
    )


_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _norm(name: str) -> str:
    # PEP 503: names compare case-insensitively with -, _ and . equivalent.
    return re.sub(r"[-_.]+", "-", name).lower()


def _python(root: Path) -> Stack | None:
    manifest = root / "pyproject.toml"
    if not manifest.is_file():
        return None
    data = _toml(manifest)
    ws = data.get("tool", {}).get("uv", {}).get("workspace", {})
    dirs = _expand(root, ws.get("members", []), ws.get("exclude", []))
    if "project" in data:
        dirs.insert(0, root)
    pkgs, reqs = [], {}
    for d in dirs:
        m = d / "pyproject.toml"
        if not m.is_file():
            continue
        pdata = _toml(m)
        project = pdata.get("project", {})
        pkg = Package(project.get("name", d.name), d)
        lines = list(project.get("dependencies", []))
        for group in project.get("optional-dependencies", {}).values():
            lines += group
        for group in pdata.get("dependency-groups", {}).values():
            lines += [x for x in group if isinstance(x, str)]
        reqs[pkg.name] = {
            _norm(mt.group(1)) for x in lines if (mt := _REQ_NAME.match(x))
        }
        pkgs.append(pkg)
    names = {_norm(p.name): p.name for p in pkgs}
    for p in pkgs:
        p.deps = sorted(
            names[r] for r in reqs[p.name] if r in names and names[r] != p.name
        )
    return Stack(
        "python",
        root,
        manifest,
        pkgs,
        ["uv"],
        [
            "pyproject.toml",
            "uv.lock",
            ".python-version",
            "ruff.toml",
            ".ruff.toml",
            "pyrightconfig.json",
            "conftest.py",
            "pytest.ini",
            "setup.cfg",
            "tox.ini",
        ],
    )


def _uncomment(line: str) -> str:
    """Drop a YAML comment: `#` at the start or after whitespace, outside quotes."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i].rstrip()
    return line.rstrip()


def _unquote(s: str) -> str:
    s = s.strip()
    return s[1:-1] if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"" else s


def _flow_items(s: str) -> list[str]:
    """`"a", 'b', c` → [a, b, c]; commas inside quotes stay."""
    items, cur, quote = [], "", None
    for ch in s:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == ",":
            items.append(cur)
            cur = ""
            continue
        cur += ch
    items.append(cur)
    return [_unquote(x) for x in items if x.strip()]


def _pnpm_patterns(text: str) -> list[str]:
    """The `packages:` list of pnpm-workspace.yaml without a YAML dependency:
    a block list at any indent (column 0 too), a flow list on one or more lines, quotes,
    inline comments and `!exclude` patterns."""
    out: list[str] = []
    inside, flow = False, None
    for raw in text.splitlines():
        line = _uncomment(raw)
        s = line.strip()
        if not s:
            continue
        if flow is None:
            if not line[0].isspace() and not s.startswith("-"):
                key, _, rest = s.partition(":")
                inside = _unquote(key) == "packages"
                if not (inside and rest.strip().startswith("[")):
                    continue
                flow, s = "", rest.strip()[1:]
            elif inside and (s == "-" or s.startswith("- ")):
                out.append(_unquote(s[1:]))
                continue
            else:
                continue
        flow += " " + s
        if flow.rstrip().endswith("]"):
            out += _flow_items(flow.rstrip()[:-1])
            flow, inside = None, False
    return [p for p in out if p]


def _ts(root: Path) -> Stack | None:
    manifest = root / "pnpm-workspace.yaml"
    if not manifest.is_file():
        return None
    pats = _pnpm_patterns(manifest.read_text(encoding="utf-8"))
    include = [p for p in pats if not p.startswith("!")]
    exclude = [p[1:] for p in pats if p.startswith("!")]
    pkgs, declared = [], {}
    for d in _expand(root, include, exclude):
        m = d / "package.json"
        if not m.is_file():
            continue
        pj = json.loads(m.read_text(encoding="utf-8"))
        pkg = Package(pj.get("name", d.name), d)
        declared[pkg.name] = {
            n
            for section in ("dependencies", "devDependencies", "peerDependencies")
            for n in pj.get(section, {})
        }
        pkgs.append(pkg)
    names = {p.name for p in pkgs}
    for p in pkgs:
        p.deps = sorted(declared[p.name] & names - {p.name})
    return Stack(
        "ts",
        root,
        manifest,
        pkgs,
        ["node", "pnpm"],
        [
            "pnpm-workspace.yaml",
            "pnpm-lock.yaml",
            "package.json",
            "tsconfig.json",
            "tsconfig.base.json",
            "biome.json",
            "biome.jsonc",
            "eslint.config.*",
            ".eslintrc*",
            ".prettierrc*",
            "prettier.config.*",
            ".npmrc",
        ],
    )


_DETECT = {"rust": _rust, "python": _python, "ts": _ts}


def detect(cfg: Config) -> list[Stack]:
    stacks = []
    for name, sc in cfg.stacks.items():
        if sc.enabled is False:
            continue
        stack = _DETECT[name](cfg.root / sc.root)
        if stack is None and sc.enabled:
            raise FileNotFoundError(
                f"stacks.{name} is enabled but no manifest found under {sc.root!r}"
            )
        if stack:
            stack.global_files += sc.global_files  # project-specific
            stacks.append(stack)
    return stacks


# Directories and files that mean a project speaks SQL without a single `.sql`.
_SQL_DIRS = {"migrations", "alembic", ".sqlx"}
_SQL_DEPS = ("dependencies", "dev-dependencies", "build-dependencies")


def _uses_sqlx(manifest: Path) -> bool:
    try:
        data = _toml(manifest)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    tables = [data.get(s, {}) for s in _SQL_DEPS]
    tables.append(data.get("workspace", {}).get("dependencies", {}))
    tables += [t.get(s, {}) for t in data.get("target", {}).values() for s in _SQL_DEPS]
    return any("sqlx" in t for t in tables if isinstance(t, dict))


def has_sql(cfg: Config) -> bool:
    """`[stacks] sql` if set; else tracked or new (not ignored) files: any `*.sql`, a
    `migrations/`, `alembic/` or `.sqlx/` directory, `alembic.ini`, sqlx in a Cargo.toml.
    Outside a git repository nothing is found: a later `h init` catches up."""
    if cfg.sql is not None:
        return cfg.sql
    out = git.run(
        cfg.root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
    )
    if out.returncode != 0:
        return False
    for rel in filter(None, out.stdout.split("\0")):
        *dirs, name = rel.split("/")
        if name.endswith(".sql") or name == "alembic.ini" or _SQL_DIRS & set(dirs):
            return True
        if name == "Cargo.toml" and _uses_sqlx(cfg.root / rel):
            return True
    return False


def present(cfg: Config) -> set[str]:
    """Names of the project's stacks, sql included: what language skills follow."""
    names = {st.name for st in detect(cfg)}
    return names | {"sql"} if has_sql(cfg) else names


def warnings(stacks: list[Stack]) -> list[str]:
    """pnpm-workspace.yaml exists only to list packages: if it yields none, nothing in the
    TS stack is ever checked — say so instead of a silent "nothing affected".
    Not for Python or Rust: a root pyproject.toml or Cargo.toml often only configures tools."""
    return [
        f"{st.name}: {st.manifest.name} found but it lists no packages that exist — "
        "nothing in this stack will be checked; fix its package list"
        for st in stacks
        if st.name == "ts" and not st.packages
    ]


def owner(stacks: list[Stack], file: Path) -> tuple[Stack, Package] | None:
    """The deepest package containing `file` — the unit for scoped commands."""
    file = file.resolve()
    best = None
    for st in stacks:
        for p in st.packages:
            try:
                file.relative_to(p.path.resolve())
            except ValueError:
                continue
            if best is None or len(p.path.parts) > len(best[1].path.parts):
                best = (st, p)
    return best

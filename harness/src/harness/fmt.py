"""Format one file with its stack's formatter — the PostToolUse tier (SUMMARY #4).

Only formatters and autofixers that work per file belong here; type checkers and clippy
need the whole package and run in `h check`. A missing tool is skipped silently: `h doctor`
is where missing tools are reported, a hook must never block an edit for that.
"""

from __future__ import annotations

import fnmatch
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import proc, tools
from .config import Config, expand_command

EXT_STACK = {
    ".rs": "rust",
    ".py": "python",
    ".pyi": "python",
    ".ts": "ts",
    ".tsx": "ts",
    ".js": "ts",
    ".jsx": "ts",
    ".mjs": "ts",
    ".cjs": "ts",
    ".mts": "ts",
    ".cts": "ts",
    ".json": "ts",
    ".css": "ts",
}


@dataclass
class Result:
    file: str
    status: str  # formatted | skipped | failed
    detail: str = ""


def is_generated(cfg: Config, rel: str) -> bool:
    return any(fnmatch.fnmatch(rel, pat) for pat in cfg.generated)


def _rust_edition(cfg: Config, file: Path) -> str:
    # rustfmt run on a bare file defaults to edition 2015 and misparses async/let-else.
    for d in file.parents:
        m = d / "Cargo.toml"
        if m.is_file():
            data = tomllib.loads(m.read_text(encoding="utf-8"))
            ed = data.get("package", {}).get("edition")
            if isinstance(ed, str):
                return ed
            ws = data.get("workspace", {}).get("package", {}).get("edition")
            if isinstance(ws, str):
                return ws
        if d == cfg.root:
            break
    return "2024"


_WINDOWS = os.name == "nt"


def _node_bin(cfg: Config, file: Path, name: str) -> str | None:
    # pnpm on Windows writes both a POSIX shell shim and `<name>.cmd`; only the .cmd starts
    # there (the shim is WinError 193). Elsewhere there is no .cmd.
    names = (f"{name}.cmd", name) if _WINDOWS else (name,)
    for d in file.parents:
        for cand in (d / "node_modules" / ".bin" / n for n in names):
            if cand.is_file():
                return str(cand)
        if d == cfg.root:
            break
    return None


def _commands(cfg: Config, stack: str, file: Path) -> list[list[str]] | None:
    # `fmt` is the check `h check` runs (e.g. `cargo fmt --check`); writing needs its own
    # command, `fmt_write` — without one, the stack's own formatter below.
    override = cfg.stacks[stack].commands.get("fmt_write")
    if override:
        names = []
        if "{names}" in override:
            from . import stacks  # only when asked: detection reads every manifest

            try:
                found = stacks.owner(stacks.detect(cfg), file)
            except FileNotFoundError:  # a stack enabled without its manifest: skip
                return None
            names = [found[1].name] if found else []
        argv, placed = expand_command(override, names, [str(file)])
        return [argv if placed else [*argv, str(file)]]
    f = str(file)
    if stack == "rust":
        if not tools.usable("rustfmt"):
            return None
        return [["rustfmt", "--edition", _rust_edition(cfg, file), f]]
    if stack == "python":
        ruff = tools.ruff(cfg.root / cfg.stacks["python"].root)
        if ruff is None:
            return None
        # --exit-zero: unfixable lint findings belong to `h check`, not to every edit.
        # --unfixable F401: an agent adds the import one Edit before the code that uses
        # it; removing it in between silently breaks the next edit (seen 2026-10-22).
        # Truly unused imports are still caught by `h check` lint.
        return [
            [*ruff, "format", "--quiet", f],
            [
                *ruff,
                "check",
                "--fix",
                "--unfixable",
                "F401",
                "--exit-zero",
                "--quiet",
                f,
            ],
        ]
    if stack == "ts":
        if biome := _node_bin(cfg, file, "biome"):
            return [[biome, "format", "--write", f]]
        if prettier := _node_bin(cfg, file, "prettier"):
            return [[prettier, "--write", "--log-level", "warn", f]]
        return None
    return None


def format_file(cfg: Config, file: Path) -> Result:
    file = file.resolve()
    try:
        rel = file.relative_to(cfg.root.resolve()).as_posix()
    except ValueError:
        return Result(file.as_posix(), "skipped", "outside project")
    stack = EXT_STACK.get(file.suffix.lower())
    if stack is None or not file.is_file():
        return Result(rel, "skipped", "no formatter for this file")
    if cfg.stacks[stack].enabled is False:
        return Result(rel, "skipped", f"stack {stack} disabled")
    if is_generated(cfg, rel):
        return Result(rel, "skipped", "generated")
    cmds = _commands(cfg, stack, file)
    if not cmds:
        return Result(rel, "skipped", f"{stack} formatter not installed")
    for cmd in cmds:
        try:
            r = proc.run(cmd, cfg.root)
        except OSError as err:  # the formatter cannot start
            return Result(rel, "failed", f"{cmd[0]}: {err}")
        if r.returncode != 0:
            msg = (r.stderr or r.stdout).strip().splitlines()
            return Result(rel, "failed", "\n".join(msg[:8]))
    return Result(rel, "formatted", Path(cmds[0][0]).stem)

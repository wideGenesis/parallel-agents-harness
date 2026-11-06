"""Faster Rust builds: what `h doctor` suggests for a Rust project.

Advice only — Cargo.toml and .cargo/config.toml belong to the project, so doctor never
edits them. Three settings, as ruff and codex use them (SUMMARY #12):
- `[profile.dev] debug = "line-tables-only"`: backtraces keep file:line, while debug info
  for variables (the bulk of target/ and of link time) is not written;
- a fast linker: mold on Linux (CI, prod), the rust-lld that rustup ships on Windows;
- `CARGO_INCREMENTAL=0` in CI: an incremental cache from a fresh checkout is never reused.
Plus the clippy lints that turn rules of the `rust` skill into `h check` failures.

The figures in the tips: a copy of a pilot project (348 crates), Windows, no sccache, 2026-10-25 —
default → line-tables-only → + rust-lld: cold 120 → 98 → 88 s, rebuild after a leaf edit
4.7 → 3.2 → 2.7 s, target/ 5.1 → 2.5 GB.
"""

from __future__ import annotations

import tomllib
from importlib import resources
from pathlib import Path

from . import init
from .config import Config
from .stacks import Stack

# Debug levels that already skip variable info (bool apart: True == 1 in Python).
LIGHT_DEBUG = {0, 1, "none", "line-directives-only", "line-tables-only", "limited"}
# Rules of the `rust` skill that clippy checks once they are on, as codex sets them.
SKILL_LINTS = ("unwrap_used", "expect_used", "undocumented_unsafe_blocks")
TEST_ALLOW = ("allow-unwrap-in-tests", "allow-expect-in-tests")


def _light(debug: object) -> bool:
    return debug is False if isinstance(debug, bool) else debug in LIGHT_DEBUG


def config_template() -> str:
    return (
        resources.files("harness")
        .joinpath("templates", "rust", "config.toml")
        .read_text(encoding="utf-8")
    )


def _dev_debug(manifest: Path) -> object:
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return data.get("profile", {}).get("dev", {}).get("debug", True)


def _clippy_lints(manifest: Path) -> tuple[str, dict[str, object]]:
    """The section clippy levels belong in and the levels set there."""
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "[lints.clippy]", {}
    if "workspace" in data:
        lints = data["workspace"].get("lints", {})
        return "[workspace.lints.clippy]", lints.get("clippy", {})
    return "[lints.clippy]", data.get("lints", {}).get("clippy", {})


def _off(value: object) -> bool:
    level = value.get("level") if isinstance(value, dict) else value
    return level in (None, "allow")


def _clippy_conf(root: Path) -> str:
    for name in ("clippy.toml", ".clippy.toml"):
        p = root / name
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
    return ""


def _cargo_config(root: Path) -> str:
    for name in ("config.toml", "config"):
        p = root / ".cargo" / name
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
    return ""


def _workflows(root: Path) -> list[Path]:
    flows = root / ".github" / "workflows"
    return sorted([*flows.glob("*.yml"), *flows.glob("*.yaml")])


def tips(cfg: Config, st: Stack) -> list[str]:
    out: list[str] = []
    debug = _dev_debug(st.manifest)
    if debug is not None and not _light(debug):
        out.append(
            f'rust: add `[profile.dev] debug = "line-tables-only"` to '
            f"{st.manifest.name} — backtraces keep file:line; on a 348-crate workspace "
            "target/ 5.1 → 2.5 GB, cold build −18%, rebuild −32%"
        )
    conf = _cargo_config(st.root) or _cargo_config(cfg.root)
    missing = [
        name
        for name, marks in (("mold on Linux", ("mold",)),
                            ("rust-lld on Windows", ("rust-lld", "lld-link")))
        if not any(m in conf for m in marks)
    ]  # fmt: skip
    if missing:
        body = "\n".join(f"         {ln}" for ln in config_template().splitlines())
        out.append(
            f"rust: no fast linker configured ({', '.join(missing)}) — rust-lld on the "
            "same workspace: rebuild 3.2 → 2.7 s, cold build −10%; .cargo/config.toml "
            f"template:\n{body}"
        )
    # The rules of the `rust` skill that clippy can enforce in `h check`.
    section, levels = _clippy_lints(st.manifest)
    off = [n for n in SKILL_LINTS if _off(levels.get(n))]
    conf = _clippy_conf(st.root)
    in_tests = [k for k in TEST_ALLOW if k not in conf]
    if off or in_tests:
        lines: list[str] = []
        if off:
            lines += [f"# {st.manifest.name} (members: `[lints] workspace = true`)",
                      section, *(f'{n} = "deny"' for n in off)]  # fmt: skip
        if in_tests:
            lines += ["# clippy.toml — tests may unwrap",
                      *(f"{k} = true" for k in in_tests)]  # fmt: skip
        body = "\n".join(f"         {ln}" for ln in lines)
        out.append(
            "rust: clippy does not enforce the rust skill (no unwrap/expect outside "
            f"tests, unsafe only with SAFETY:) — add:\n{body}"
        )
    # The harness reusable workflow sets it (check.yml).
    marks = ("CARGO_INCREMENTAL", f"{init.HARNESS_REPO}/.github/workflows/check.yml@")
    texts = [
        p.read_text(encoding="utf-8", errors="replace") for p in _workflows(cfg.root)
    ]
    if texts and not any(m in t for t in texts for m in marks):
        out.append(
            "rust: set `CARGO_INCREMENTAL: 0` in the CI job env — a fresh checkout never "
            "reuses the incremental cache, it only costs build time and cache space"
        )
    return out

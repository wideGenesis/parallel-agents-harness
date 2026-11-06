"""Which packages a change touches: changed files → owning packages → reverse dependencies.

The same logic serves the agent's local `h check` and the CI path filter (SUMMARY #8), so
"what CI will run" and "what I ran" cannot disagree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import git
from .config import CONFIG_NAME, Config
from .stacks import Stack, owner

GitError = git.GitError  # caught by the CLI; a git timeout is one too

# Project-wide files every stack depends on: what CI runs and how h is set up.
# Lock files and toolchains are per stack (Stack.global_files).
PROJECT_GLOBAL = (".github/workflows/**", ".github/actions/**", CONFIG_NAME)


def glob_match(path: str, pattern: str) -> bool:
    """`**` crosses directories, `*` and `?` do not — unlike fnmatch, `*.md` is root-only."""
    rx = ""
    for tok in re.split(r"(\*\*/?|\*|\?)", pattern):
        if tok.startswith("**"):
            rx += "(?:.*/)?" if tok.endswith("/") else ".*"
        elif tok == "*":
            rx += "[^/]*"
        elif tok == "?":
            rx += "[^/]"
        else:
            rx += re.escape(tok)
    return re.fullmatch(rx, path) is not None


def _git(cfg: Config, *argv: str) -> str:
    r = git.run(cfg.root, *argv)
    if r.returncode != 0:
        raise GitError(f"git {' '.join(argv)}: {r.stderr.strip()}")
    return r.stdout


def resolve_base(cfg: Config, base: str | None) -> str:
    """Merge-base with the base branch: what the branch would add if merged now.
    Prefers origin/<base> — the local branch may be stale in a long-lived worktree."""
    candidates = [base] if base else [f"origin/{cfg.base_branch}", cfg.base_branch]
    for ref in candidates:
        try:
            _git(cfg, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        except GitError:
            continue
        return _git(cfg, "merge-base", ref, "HEAD").strip()
    raise GitError(f"no base ref found (tried {', '.join(candidates)})")


def changed_files(cfg: Config, base_sha: str) -> list[str]:
    """Committed since base + staged + unstaged + untracked: what the agent has touched.

    `-z`: without it git quotes non-ASCII paths ("\\321\\202…"), and such a path matches
    no package, no policy glob and no formatter."""
    out: set[str] = set()
    for args in (
        ("diff", "--name-only", "-z", "--no-renames", "--relative", base_sha),
        ("ls-files", "-z", "--others", "--exclude-standard"),
    ):
        out.update(p for p in _git(cfg, *args).split("\0") if p)
    return sorted(out)


@dataclass
class StackImpact:
    changed: set[str] = field(default_factory=set)  # packages with changed files
    affected: set[str] = field(
        default_factory=set
    )  # changed + everything depending on them
    all: bool = False  # a stack-global file changed
    reason: str = ""
    files: set[str] = field(
        default_factory=set
    )  # changed files, relative to the stack root


def compute(
    cfg: Config, stacks: list[Stack], files: list[str]
) -> dict[str, StackImpact]:
    impact = {st.name: StackImpact() for st in stacks}
    for rel in files:
        if any(glob_match(rel, g) for g in PROJECT_GLOBAL):
            for imp in impact.values():
                if not imp.all:
                    imp.all, imp.reason = True, rel
        path = (cfg.root / rel).resolve()
        hit_global = False
        for st in stacks:
            try:
                in_stack = path.relative_to(st.root.resolve()).as_posix()
            except ValueError:
                continue
            impact[st.name].files.add(in_stack)
            if st.is_global(in_stack):
                imp = impact[st.name]
                if not imp.all:
                    imp.all, imp.reason = True, rel
                hit_global = True
        if hit_global:
            continue
        found = owner(stacks, path)
        if found:
            impact[found[0].name].changed.add(found[1].name)
        # An input of a generated file changes that file's package too: the
        # Rust contract changes the web client, so the web stack's checks run.
        for gen in cfg.generators:
            if any(glob_match(rel, g) for g in gen.inputs):
                made = owner(stacks, cfg.root / gen.path)
                if made:
                    impact[made[0].name].changed.add(made[1].name)

    for st in stacks:
        imp = impact[st.name]
        if imp.all:
            imp.affected = {p.name for p in st.packages}
            continue
        dependents: dict[str, set[str]] = {p.name: set() for p in st.packages}
        for p in st.packages:
            for d in p.deps:
                dependents.setdefault(d, set()).add(p.name)
        todo, seen = list(imp.changed), set(imp.changed)
        while todo:
            for nxt in dependents.get(todo.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        imp.affected = seen
    return impact


def docs_only(cfg: Config, files: list[str], impact: dict[str, StackImpact]) -> bool:
    """Only documentation changed: CI may skip its checks. A doc inside a package
    affects that package, so it never counts here."""
    if not files or any(imp.all or imp.affected for imp in impact.values()):
        return False
    return all(any(glob_match(f, g) for g in cfg.docs) for f in files)

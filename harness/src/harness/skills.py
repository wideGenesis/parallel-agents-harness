"""Skills and subagents: install harness templates, mirror `.agents/skills` → `.claude/skills`.

`.agents/skills/` is the vendor-neutral source of truth (sentry, ruff). Claude Code reads
`.claude/skills/`; a symlink would need Developer Mode on Windows, so we copy and record
what we copied in a manifest. Only manifest entries are ever replaced or removed — a
Claude-only skill the project keeps in `.claude/skills/` is never touched.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from . import __version__, stacks
from .config import Config

SOURCE = Path(".agents") / "skills"
MIRROR = Path(".claude") / "skills"
AGENTS = Path(".claude") / "agents"
MANIFEST = ".harness-sync.json"
# Present only in the harness repo itself: its `.agents/skills` and `.claude/agents` must
# equal what `h init` installs, or an edit made there never reaches projects.
TEMPLATES = Path("harness") / "src" / "harness" / "templates"
# What `install` put in place, by hash: tells an untouched copy from a project's edit.
INSTALLED = Path(".harness") / "installed.json"
# Language skill → the stack it serves: installed only where that stack is.
LANGUAGE = {"rust": "rust", "python": "python", "typescript": "ts", "sql": "sql"}
# Skills h renamed: old name → new. The old names were Claude Code's own commands
# (`/code-review`, `/security-review`), so a call could reach the wrong one.
RENAMED = {"code-review": "review-gate", "security-review": "security-rules"}


def _templates() -> Traversable:
    return resources.files("harness").joinpath("templates")


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode())
            h.update(b"\0")
            # Normalise EOL: git autocrlf must not make an unchanged skill look drifted.
            h.update(p.read_bytes().replace(b"\r\n", b"\n"))
            h.update(b"\0")
    return h.hexdigest()


def _copy_traversable(src: Traversable, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            _copy_traversable(item, target)
        else:
            target.write_bytes(item.read_bytes())


def _item_hash(item: Traversable) -> str:
    """`_tree_hash` of a skill directory, the EOL-normalised content hash of an agent file;
    works on a template inside the package as on the project's copy."""
    if not item.is_dir():
        return hashlib.sha256(item.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    files: list[tuple[str, Traversable]] = []
    stack: list[tuple[str, Traversable]] = [("", item)]
    while stack:
        prefix, d = stack.pop()
        for c in d.iterdir():
            if c.is_dir():
                stack.append((f"{prefix}{c.name}/", c))
            else:
                files.append((f"{prefix}{c.name}", c))
    h = hashlib.sha256()
    for rel, c in sorted(files, key=lambda f: f[0]):
        h.update(rel.encode())
        h.update(b"\0")
        h.update(c.read_bytes().replace(b"\r\n", b"\n"))
        h.update(b"\0")
    return h.hexdigest()


def _put(src: Traversable, dst: Path) -> None:
    if dst.is_dir():
        shutil.rmtree(dst)
    if src.is_dir():
        _copy_traversable(src, dst)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())


def _drop(dst: Path) -> None:
    if dst.is_dir():
        shutil.rmtree(dst)
    else:
        dst.unlink()


def install(
    cfg: Config,
    dry_run: bool = False,
    tpl: Traversable | None = None,
    have_stacks: set[str] | None = None,
) -> list[str]:
    """Install harness skills and agents, and keep them current.

    `INSTALLED` records the hash of what was put in place. A copy that still has it is the
    project's untouched install: a newer template replaces it, a template the harness
    dropped removes it. Any other copy was edited by the project and is only reported. A
    recorded item missing on disk was deleted on purpose and stays deleted. A language
    skill of a stack the project lacks counts as dropped.
    """
    changes: list[str] = []
    tpl = tpl or _templates()
    if have_stacks is None:
        have_stacks = stacks.present(cfg)
    path = cfg.root / INSTALLED
    try:
        old: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        old = {}
    if not isinstance(old, dict):
        old = {}
    new = dict(old)
    items: dict[str, tuple[Path, Traversable | None]] = {}
    gone: dict[str, str] = {}  # key → why it is not wanted although h has it
    for kind, base in (("skills", SOURCE), ("agents", AGENTS)):
        for item in tpl.joinpath(kind).iterdir():
            key = f"{kind}/{item.name}"
            stack = LANGUAGE.get(item.name) if kind == "skills" else None
            if stack and stack not in have_stacks:
                gone[key] = f"the project has no {stack} stack"
                continue  # not installed; a recorded copy goes as a dropped one
            items[key] = (base / item.name, item)
    for key in old:
        kind, _, name = key.partition("/")
        # Only names a template could have had: a hand-edited key never points outside.
        if (
            key not in items
            and kind in ("skills", "agents")
            and name not in ("", ".", "..")
            and Path(name).name == name
        ):
            items[key] = ((SOURCE if kind == "skills" else AGENTS) / name, None)
            if kind == "skills" and name in RENAMED:
                gone.setdefault(key, f"renamed to {RENAMED[name]} in h {__version__}")

    for key, (rel, t) in sorted(items.items()):
        dst = cfg.root / rel
        shown = rel.as_posix() + ("/" if key.startswith("skills/") else "")
        had = old.get(key)
        have = _item_hash(dst) if dst.exists() else None
        if t is None:  # dropped from the templates or its stack is gone
            if have is not None and have != had:
                reason = gone.get(key, f"no longer in h {__version__} templates")
                changes.append(
                    f"MODIFIED {shown}: edited by the project, {reason} — kept"
                )
            new.pop(key)  # the project's own from now on: said once
            if have is not None and have == had:
                reason = gone.get(key, "no longer in h templates")
                changes.append(f"remove {shown} ({reason})")
                if not dry_run:
                    _drop(dst)
            continue
        want = _item_hash(t)
        if have is None:
            if had is None:
                changes.append(f"create {shown}")
                new[key] = want
                if not dry_run:
                    _put(t, dst)
            continue  # recorded, then deleted by the project: stays deleted
        if have == want:
            new[key] = want
        elif have == had:
            changes.append(f"update {shown}")
            new[key] = want
            if not dry_run:
                _put(t, dst)
        elif want != had:  # an edit the template has not moved past stays quiet
            changes.append(f"MODIFIED {shown}: differs from the h {__version__} template "
                           f"— compare with {t}")  # fmt: skip
    if new != old and not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(new, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8", newline="\n")  # fmt: skip
    return changes


def missing(
    cfg: Config, have_stacks: set[str], tpl: Traversable | None = None
) -> list[str]:
    """Language skills of the project's stacks that `h init` would install now: not on
    disk and never installed (one recorded and then deleted was dropped on purpose)."""
    tpl = tpl or _templates()
    path = cfg.root / INSTALLED
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        old = {}
    if not isinstance(old, dict):
        old = {}
    return sorted(
        item.name
        for item in tpl.joinpath("skills").iterdir()
        if LANGUAGE.get(item.name) in have_stacks
        and f"skills/{item.name}" not in old
        and not (cfg.root / SOURCE / item.name).exists()
    )


def _sources(cfg: Config) -> dict[str, Path]:
    root = cfg.root / SOURCE
    if not root.is_dir():
        return {}
    return {p.name: p for p in sorted(root.iterdir()) if (p / "SKILL.md").is_file()}


def _manifest(cfg: Config) -> dict[str, str]:
    path = cfg.root / MIRROR / MANIFEST
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def drift(cfg: Config) -> list[str]:
    """Skills whose `.claude/skills` copy is missing, stale or orphaned."""
    out = []
    manifest = _manifest(cfg)
    sources = _sources(cfg)
    for name, src in sources.items():
        copy = cfg.root / MIRROR / name
        if not copy.is_dir():
            out.append(f"{name}: not mirrored")
        elif _tree_hash(copy) != _tree_hash(src):
            out.append(f"{name}: copy differs from {(SOURCE / name).as_posix()}")
    out += [
        f"{name}: mirrored but source removed"
        for name in manifest
        if name not in sources
    ]
    return out


def _files(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        return {}
    return {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file()}


def template_drift(cfg: Config) -> list[str]:
    """In the harness repo: files of `.agents/skills` and `.claude/agents` that are not
    an exact copy (EOL aside) of `TEMPLATES`, in either direction. Elsewhere: nothing."""
    tpl = cfg.root / TEMPLATES
    if not tpl.is_dir():
        return []
    out = []
    for kind, base in (("skills", SOURCE), ("agents", AGENTS)):
        want, have = _files(tpl / kind), _files(cfg.root / base)
        for rel in sorted(want.keys() | have.keys()):
            src = (TEMPLATES / kind / rel).as_posix()
            copy = (base / rel).as_posix()
            if rel not in have:
                problem = f"{copy}: missing, {src} has it"
            elif rel not in want:
                problem = f"{copy}: not in {src} — add it there"
            elif _item_hash(have[rel]) != _item_hash(want[rel]):  # EOL-blind
                problem = f"{copy}: differs from {src}"
            else:
                continue
            out.append(f"{problem} (source of truth: templates — edit there, "
                       "then copy over and run `h skills sync`)")  # fmt: skip
    return out


def sync(cfg: Config, dry_run: bool = False) -> tuple[list[str], list[str]]:
    """Returns (changes, conflicts). A conflict is an unmanaged `.claude/skills/<name>`
    with the same name as a source skill — left alone and reported."""
    changes: list[str] = []
    conflicts: list[str] = []
    old = _manifest(cfg)
    new: dict[str, str] = {}
    mirror = cfg.root / MIRROR

    for name, src in _sources(cfg).items():
        dst = mirror / name
        digest = _tree_hash(src)
        if dst.exists() and name not in old:
            conflicts.append(
                f"{(MIRROR / name).as_posix()} exists and is not managed by h"
            )
            continue
        new[name] = digest
        if dst.is_dir() and _tree_hash(dst) == digest:
            continue
        changes.append(f"mirror {name}")
        if not dry_run:
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)

    for name in old:
        if name not in new and (mirror / name).exists() and name not in _sources(cfg):
            changes.append(f"remove {name}")
            if not dry_run:
                shutil.rmtree(mirror / name)

    if (new != old or changes) and not dry_run:
        mirror.mkdir(parents=True, exist_ok=True)
        (mirror / MANIFEST).write_text(
            json.dumps(new, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    return changes, conflicts


def is_skill_source(cfg: Config, file: Path) -> bool:
    try:
        file.resolve().relative_to((cfg.root / SOURCE).resolve())
    except ValueError:
        return False
    return True

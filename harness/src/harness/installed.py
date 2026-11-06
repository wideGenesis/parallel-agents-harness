"""Is the `h` that runs the gates the `h` that is merged?

`uv tool install` puts a copy of the package into its own site-packages: a merge into
`harness/` reaches nobody until it is installed again, and an editable install follows a
clone that may be behind origin or on a branch. Gates built from old or unmerged code check
the wrong rules without saying so. The installed files are compared with the package on
`origin/<base>` of the repository they were installed from — by content, since the version
never changes, and without a fetch, so doctor stays offline.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from . import git

DIST = "harness-cli"
PACKAGE = Path(__file__).resolve().parent


@dataclass
class Status:
    state: str  # ok | behind | differs | unknown
    detail: str = ""  # unknown: why not checked; behind/differs: what differs
    fix: str = ""  # the command that brings the installed h up to origin

    @property
    def stale(self) -> bool:
        return self.state in ("behind", "differs")

    def message(self) -> str:
        if self.state == "behind":
            return f"installed h is behind {self.detail} — update: `{self.fix}`"
        if self.state == "differs":
            return (f"installed h differs from {self.detail} (installed from unmerged "
                    f"code) — reinstall from the main checkout: `{self.fix}`")  # fmt: skip
        return ""


def _run(cwd: Path, *argv: str) -> subprocess.CompletedProcess:
    return git.run(cwd, *argv)


def _source() -> tuple[Path, bool] | None:
    """The local directory `h` was installed from and whether the install is editable."""
    try:
        raw = metadata.distribution(DIST).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:  # a broken install record is "cannot check", never a crash
        return None
    url = data.get("url", "")
    if not url.startswith("file:"):
        return None
    path = urllib.request.url2pathname(urllib.parse.urlparse(url).path)
    return Path(path), bool(data.get("dir_info", {}).get("editable"))


def _blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _cache(rel: Path) -> bool:
    """A tool's cache, never part of a release: __pycache__, .mypy_cache, .pytest_cache,
    .ruff_cache, *.pyc."""
    return rel.suffix == ".pyc" or any(
        part == "__pycache__" or (part.startswith(".") and part.endswith("_cache"))
        for part in rel.parts
    )


def _files(pkg: Path) -> dict[str, Path]:
    return {p.relative_to(pkg).as_posix(): p for p in pkg.rglob("*")
            if p.is_file() and not _cache(p.relative_to(pkg))}  # fmt: skip


def _ignored(pkg: Path, names: list[str]) -> set[str]:
    """Of these files under `pkg`, the ones git ignores — an editable install sits in a
    checkout, where an ignored file is not part of the release."""
    if not names:
        return set()
    # NUL-separated: a text stdin on Windows turns "\n" into "\r\n", and git then looks
    # for "name\r", which nothing ignores.
    r = git.run(pkg, "check-ignore", "-z", "--stdin", stdin="\0".join(names) + "\0")
    return set(filter(None, r.stdout.split("\0"))) if r.returncode in (0, 1) else set()


def _tree(root: Path, ref: str, rel: str) -> dict[str, str] | None:
    r = _run(root, "ls-tree", "-r", "--full-tree", ref, "--", rel)
    if r.returncode != 0:
        return None
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        meta, path = line.split("\t", 1)
        _mode, kind, sha = meta.split()
        if kind == "blob":
            out[path[len(rel) + 1 :]] = sha
    return out


def _diff(pkg: Path, tree: dict[str, str]) -> list[str]:
    """Paths whose installed content is not the blob in `tree`, plus extra or missing files."""
    files = _files(pkg)
    extra = _ignored(pkg, sorted(set(files) - set(tree)))
    out = []
    for name in sorted((set(files) - extra) | set(tree)):
        if name not in files or name not in tree:
            out.append(name)
            continue
        data = files[name].read_bytes()
        # a checkout with CRLF line ends is the same file as the LF blob
        if tree[name] not in (_blob_id(data), _blob_id(data.replace(b"\r\n", b"\n"))):
            out.append(name)
    return out


def _base(root: Path) -> str:
    try:
        data = tomllib.loads((root / "harness.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "main"
    return data.get("project", {}).get("base", "main")


def check(
    pkg: Path = PACKAGE, source: tuple[Path, bool] | None | bool = True
) -> Status:
    """Compare the running package `pkg` with the one on origin/<base> of its source."""
    if source is True:
        source = _source()
    if not source:
        return Status("unknown", "h was not installed from a local directory")
    src, editable = source
    top = _run(src, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return Status("unknown", f"{src} is not a git checkout")
    root = Path(top.stdout.strip())
    pkg_src = src / "src" / pkg.name
    try:
        rel = pkg_src.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return Status("unknown", f"package not found under {root}")
    ref = f"origin/{_base(root)}"
    origin = _tree(root, ref, rel)
    if not origin:
        return Status("unknown", f"no {ref} in {root}")
    changed = _diff(pkg, origin)
    if not changed:
        return Status("ok")

    shown = ", ".join(changed[:3]) + (
        f" (+{len(changed) - 3})" if len(changed) > 3 else ""
    )
    detail = f"{ref} ({shown})"
    # The main checkout: a worktree's common dir is <main>/.git.
    common = _run(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    main = Path(common.stdout.strip()).parent if common.returncode == 0 else root
    main_src = main / src.resolve().relative_to(root.resolve())
    pull = f"git -C {main.as_posix()} pull"
    install = f"uv tool install --reinstall {main_src.as_posix()}"
    if editable:
        install = f"uv tool install --reinstall --editable {main_src.as_posix()}"
    # Ahead: the source has package commits origin does not have, so the install is from a
    # branch. Otherwise the source is behind origin or the copy predates it.
    ahead = _run(root, "rev-list", "--count", f"{ref}..HEAD", "--", rel)
    if main != root or ahead.stdout.strip() not in ("", "0"):
        return Status("differs", detail, f"{pull} && {install}")
    # An editable install runs uncommitted edits too: a pull does not clear those.
    if editable and _run(root, "status", "--porcelain", "--", rel).stdout.strip():
        return Status("differs", detail, f"git -C {main.as_posix()} status -- {rel}"
                      " — commit or discard the edits; the main checkout stays clean")  # fmt: skip
    return Status("behind", detail, pull if editable else f"{pull} && {install}")

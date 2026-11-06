"""The `rust sql` step of `h check`: what a sqlx project relies on and no compile
proves.

1. Migrations on two paths: the base branch's `migrations/`, then the working tree's on top.
   A migration that only applies to an empty schema fails here, and sqlx refuses one that is
   already on the base but was edited since (its checksum changed).
2. The committed `.sqlx/` query cache matches the migrated schema
   (`cargo sqlx prepare --workspace --check`). With SQLX_OFFLINE a missing entry fails to
   compile, but a stale one does not.

Both run on a throwaway database on the server DATABASE_URL points at, as sqlx::test does;
the named database is never touched, and the throwaway one is dropped at the end.
"""

from __future__ import annotations

import io
import re
import secrets
import shlex
import tarfile
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import git, proc, stacks, tools
from .stacks import Stack

MIGRATIONS = "migrations"
CACHE = ".sqlx"
NO_SERVER = (
    "DATABASE_URL is not set: the sql step needs a Postgres server to create a throwaway "
    "database on — set it in harness.toml [check.env] (CI sets it with [ci] postgres-image)\n"
)
INSTALL = (
    "cargo install sqlx-cli --locked --no-default-features --features rustls,postgres"
)


def planned(st: Stack) -> bool:
    """A Rust stack that uses sqlx and keeps migrations or a query cache at its root."""
    if st.name != "rust":
        return False
    if not ((st.root / MIGRATIONS).is_dir() or (st.root / CACHE).is_dir()):
        return False
    manifests = [st.manifest, *(p.path / "Cargo.toml" for p in st.packages)]
    return any(stacks._uses_sqlx(m) for m in manifests)


def cli_version(st: Stack) -> str | None:
    """The sqlx-cli CI installs for this stack: the version of its `sqlx` crate in
    Cargo.lock, since `cargo sqlx prepare` writes the cache the crate's macros read.
    None when the sql step does not run, or the lock names no sqlx."""
    if not planned(st):
        return None
    try:
        lock = tomllib.loads((st.root / "Cargo.lock").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    found = [p.get("version", "") for p in lock.get("package", [])
             if p.get("name") == "sqlx" and _SEMVER.fullmatch(p.get("version", ""))]  # fmt: skip
    return max(found, key=lambda v: tuple(int(x) for x in v.split(".")), default=None)


_SEMVER = re.compile(r"\d+\.\d+\.\d+")


def throwaway_url(url: str, name: str) -> str:
    """DATABASE_URL with its database replaced: same server, credentials and options."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{name}"))


def _base_migrations(root: Path, st: Stack, base: str, into: Path) -> Path | None:
    """The base commit's migrations/ unpacked under `into`, byte for byte; None when the
    base has none (first migrations on this branch: one path only)."""
    rel = (st.root / MIGRATIONS).resolve().relative_to(root.resolve()).as_posix()
    r = git.run(root, "archive", "--format=tar", base, "--", rel, text=False)
    if r.returncode != 0:  # the path does not exist at base
        return None
    # Regular files only, by hand: extractall's safe filter needs Python 3.11.4+.
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tar:
        for m in tar.getmembers():
            src = tar.extractfile(m) if m.isfile() else None
            if src is None or m.name.startswith("/") or ".." in m.name.split("/"):
                continue
            out = into / m.name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(src.read())
    found = into / rel
    return found if found.is_dir() else None


def run(
    cfg_root: Path, st: Stack, env: dict[str, str], base: str | None
) -> tuple[bool, str, list[str]]:
    log: list[str] = []

    def sh(argv: list[str], extra: dict[str, str] | None = None) -> bool:
        r = proc.run(argv, st.root, env={**env, **(extra or {})})
        # The URL carries the server's password: the log shows the database name only,
        # also where a tool echoes the URL back.
        shown = [name if a == db else a for a in argv]
        out = f"{r.stdout}{r.stderr}".replace(db, name).replace(url, "DATABASE_URL")
        log.append(f"$ {shlex.join(shown)}\n{out}")
        return r.returncode == 0

    url = env.get("DATABASE_URL", "")
    if not url:
        return False, NO_SERVER, []
    if not tools.usable("sqlx"):
        return False, f"sqlx-cli is not installed: {INSTALL}\n", []
    name = f"h_sql_{secrets.token_hex(4)}"
    db = throwaway_url(url, name)
    if not sh(["sqlx", "database", "create", "-D", db]):
        log.append(f"could not create the throwaway database {name}\n")
        return False, "".join(log), []
    ok, why = True, ""
    try:
        with tempfile.TemporaryDirectory(prefix="h-sql-") as tmp:
            old = _base_migrations(cfg_root, st, base, Path(tmp)) if base else None
            mine = st.root / MIGRATIONS
            if old is not None and not sh(
                ["sqlx", "migrate", "run", "--source", str(old), "-D", db]
            ):
                ok, why = False, f"the base branch's migrations do not apply ({base})"
            elif mine.is_dir() and not sh(
                ["sqlx", "migrate", "run", "--source", str(mine), "-D", db]
            ):
                where = "on top of the base branch's" if old else "on an empty database"
                ok, why = False, f"this branch's migrations fail {where}"
            elif (st.root / CACHE).is_dir() and not sh(
                ["cargo", "sqlx", "prepare", "--workspace", "--check"],
                {"DATABASE_URL": db, "SQLX_OFFLINE": "false"},
            ):
                ok, why = False, ".sqlx/ is stale: run `cargo sqlx prepare --workspace`"
    finally:
        if not sh(["sqlx", "database", "drop", "-y", "-D", db]):
            ok = False
            why = (
                why + "; " if why else ""
            ) + f"could not drop the throwaway database {name}"
    if why:
        log.append(f"sql: {why}\n")
    return ok, "".join(log), []

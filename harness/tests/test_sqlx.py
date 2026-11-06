"""The `rust sql` step: migrations on two paths and the .sqlx/ cache, on a
throwaway database. Unit tests fake sqlx-cli; the last ones run the real one against a
real Postgres (H_TEST_DATABASE_URL — CI sets it, locally optional)."""

import os
import subprocess

import pytest
from conftest import git

from harness import check, config, sqlx, stacks, tools

CARGO = '[package]\nname = "db"\nversion = "0.1.0"\nedition = "2024"\n\n[dependencies]\nsqlx = "0.9"\n'
URL = "postgres://u:secret@127.0.0.1:5432/app?sslmode=disable"


def make_project(tmp_path, migrations: dict[str, str], cache: bool = False):
    """A sqlx crate on `main` with `migrations`, a `work` branch checked out."""
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "Cargo.toml").write_text(CARGO, encoding="utf-8")
    (proj / "src" / "lib.rs").write_text("", encoding="utf-8")
    (proj / "harness.toml").write_text('[project]\nname = "p"\n', encoding="utf-8")
    (proj / "migrations").mkdir()
    for name, sql in migrations.items():
        (proj / "migrations" / name).write_text(sql, encoding="utf-8")
    if cache:
        (proj / ".sqlx").mkdir()
        (proj / ".sqlx" / "query-x.json").write_text("{}", encoding="utf-8")
    git(proj, "init", "-q", "-b", "main")
    git(proj, "add", ".")
    git(proj, "commit", "-q", "-m", "init")
    git(proj, "switch", "-q", "-c", "work")
    return proj


def rust_stack(proj):
    [st] = [s for s in stacks.detect(config.load(proj)) if s.name == "rust"]
    return st


def base_of(proj):
    return subprocess.run(
        ["git", "rev-parse", "main"],
        cwd=proj,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class FakeSqlx:
    """Records argv; `fail` maps a predicate on argv to the exit code to return."""

    def __init__(self, fail=None):
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.fail = fail or (lambda argv: False)

    def __call__(self, argv, cwd, **kw):
        argv = list(argv)
        self.calls.append(argv)
        self.envs.append(kw.get("env", {}))
        if argv[:3] == ["sqlx", "migrate", "run"]:
            src = argv[argv.index("--source") + 1]
            self.sources = [*getattr(self, "sources", []), sorted(os.listdir(src))]
        code = 1 if self.fail(argv) else 0
        echoed = f"connecting to {argv[-1]}" + chr(10) if argv[0] == "sqlx" else ""
        return subprocess.CompletedProcess(argv, code, "out" + chr(10), echoed)


@pytest.fixture
def fake(monkeypatch):
    def make(fail=None):
        f = FakeSqlx(fail)
        monkeypatch.setattr(sqlx.proc, "run", f)
        monkeypatch.setattr(sqlx.tools, "usable", lambda t: True)
        return f

    return make


def test_throwaway_url_keeps_server_and_options():
    assert sqlx.throwaway_url(URL, "h_sql_1") == (
        "postgres://u:secret@127.0.0.1:5432/h_sql_1?sslmode=disable"
    )


def test_base_then_branch_then_cache_then_drop(tmp_path, fake):
    proj = make_project(tmp_path, {"0001_a.sql": "create table a();"}, cache=True)
    (proj / "migrations" / "0002_b.sql").write_text(
        "create table b();", encoding="utf-8"
    )
    f = fake()
    ok, log, _ = sqlx.run(proj, rust_stack(proj), {"DATABASE_URL": URL}, base_of(proj))
    assert ok, log
    verbs = [" ".join(c[:3]) for c in f.calls]
    assert verbs == [
        "sqlx database create",
        "sqlx migrate run",
        "sqlx migrate run",
        "cargo sqlx prepare",
        "sqlx database drop",
    ]
    assert f.sources == [
        ["0001_a.sql"],
        ["0001_a.sql", "0002_b.sql"],
    ]  # base, then tree
    db = f.calls[0][-1]
    assert db.startswith("postgres://u:secret@127.0.0.1:5432/h_sql_")
    assert all(
        c[-1] == db for c in f.calls if c[0] == "sqlx"
    )  # never the named database
    assert f.envs[3]["DATABASE_URL"] == db and f.envs[3]["SQLX_OFFLINE"] == "false"
    assert "secret" not in log  # the password stays out of the log, echoed or not
    assert "connecting to h_sql_" in log


def test_branch_migration_failing_on_top_of_base_is_red_and_dropped(tmp_path, fake):
    proj = make_project(tmp_path, {"0001_a.sql": "x"})
    seen = []

    def second_migrate(argv):
        if argv[:3] == ["sqlx", "migrate", "run"]:
            seen.append(argv)
            return len(seen) == 2
        return False

    f = fake(second_migrate)
    ok, log, _ = sqlx.run(proj, rust_stack(proj), {"DATABASE_URL": URL}, base_of(proj))
    assert not ok
    assert "this branch's migrations fail on top of the base branch's" in log
    assert f.calls[-1][:3] == ["sqlx", "database", "drop"]  # cleaned up on failure


def test_no_migrations_at_base_is_one_path(tmp_path, fake):
    proj = make_project(tmp_path, {})
    (proj / "migrations" / "0001_a.sql").write_text("x", encoding="utf-8")
    f = fake()
    ok, log, _ = sqlx.run(proj, rust_stack(proj), {"DATABASE_URL": URL}, base_of(proj))
    assert ok, log
    assert f.sources == [["0001_a.sql"]]


def test_stale_cache_is_red(tmp_path, fake):
    proj = make_project(tmp_path, {"0001_a.sql": "x"}, cache=True)
    fake(lambda argv: argv[:2] == ["cargo", "sqlx"])
    ok, log, _ = sqlx.run(proj, rust_stack(proj), {"DATABASE_URL": URL}, base_of(proj))
    assert not ok and ".sqlx/ is stale" in log


def test_without_database_url_or_sqlx_cli_says_what_to_do(tmp_path, fake, monkeypatch):
    proj = make_project(tmp_path, {"0001_a.sql": "x"})
    f = fake()
    ok, log, _ = sqlx.run(proj, rust_stack(proj), {}, None)
    assert not ok and "[check.env]" in log and not f.calls
    monkeypatch.setattr(sqlx.tools, "usable", lambda t: False)
    ok, log, _ = sqlx.run(proj, rust_stack(proj), {"DATABASE_URL": URL}, None)
    assert not ok and "cargo install sqlx-cli" in log and not f.calls


def test_plan_has_sql_only_for_a_sqlx_project(tmp_path, repo):
    proj = make_project(tmp_path, {"0001_a.sql": "x"})
    for where, expected in ((proj, ["sql"]), (repo, [])):
        cfg = config.load(where)
        steps = check.plan(cfg, stacks.detect(cfg), None, ("sql",))
        assert [s.kind for s in steps if s.stack == "rust"] == expected


def test_migrations_and_cache_affect_the_rust_stack(tmp_path):
    proj = make_project(tmp_path, {"0001_a.sql": "x"})
    st = rust_stack(proj)
    assert st.is_global("migrations/0002_b.sql") and st.is_global(".sqlx/query-1.json")


# Real sqlx-cli and Postgres: the path CI takes. Required in CI, optional locally.
REAL = os.environ.get("H_TEST_DATABASE_URL", "")
real = pytest.mark.skipif(
    not REAL and os.environ.get("CI") != "true",
    reason="H_TEST_DATABASE_URL not set (a Postgres server for throwaway databases)",
)


def _gone(log: str) -> bool:
    """The throwaway database named in the log no longer exists (connecting fails)."""
    import re

    [name] = set(re.findall(r"h_sql_[0-9a-f]{8}", log))
    url = sqlx.throwaway_url(REAL, name)
    r = subprocess.run(
        ["sqlx", "migrate", "info", "--source", ".", "-D", url],
        capture_output=True,
        check=False,
    )
    return r.returncode != 0


@real
def test_real_two_path_catches_a_migration_that_needs_an_empty_table(tmp_path):
    assert REAL, "CI must set H_TEST_DATABASE_URL"
    assert tools.usable("sqlx"), "CI must install sqlx-cli"
    base = {"0001_t.sql": "create table t (id int);\ninsert into t values (1);\n"}
    proj = make_project(tmp_path, base)
    st, env = rust_stack(proj), {**os.environ, "DATABASE_URL": REAL}
    good = "alter table t add column x int;\n"
    (proj / "migrations" / "0002_x.sql").write_text(good, encoding="utf-8")
    ok, log, _ = sqlx.run(proj, st, env, base_of(proj))
    assert ok, log
    assert _gone(log)
    # NOT NULL without a default applies to an empty table only: red on two paths.
    bad = "alter table t add column y int not null;\n"
    (proj / "migrations" / "0002_x.sql").write_text(bad, encoding="utf-8")
    ok, log, _ = sqlx.run(proj, st, env, base_of(proj))
    assert not ok and "migration 2" in log and "on top of the base branch's" in log
    assert _gone(log)


@real
def test_real_edited_base_migration_is_red(tmp_path):
    proj = make_project(tmp_path, {"0001_t.sql": "create table t (id int);\n"})
    (proj / "migrations" / "0001_t.sql").write_text(
        "create table t (id bigint);\n", encoding="utf-8"
    )
    ok, log, _ = sqlx.run(
        proj, rust_stack(proj), {**os.environ, "DATABASE_URL": REAL}, base_of(proj)
    )
    assert not ok and "1" in log and "modified" in log

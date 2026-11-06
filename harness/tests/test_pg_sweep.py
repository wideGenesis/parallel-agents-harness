"""a sweep never removes a live run's throwaway Postgres. A free lock alone was the
proof a run was gone, and a test with its own lock dir saw every real run's lock free."""

import json
import os
import subprocess

import pytest

from harness import cli, config, lock, pg, proc


class Docker:
    """`ps` lists `names`, `inspect` gives their creation time, `rm` removes."""

    def __init__(self, names, created=None):
        self.names = set(names)
        self.created = created or {}
        self.calls = []

    def __call__(self, argv, cwd, **kw):
        argv = list(argv)
        self.calls.append(argv)
        out = ""
        if argv[1] == "ps":
            out = "\n".join(sorted(self.names))
        elif argv[1] == "rm":
            self.names.discard(argv[-1])
        elif argv[1] == "inspect":
            out = "\n".join(f"/{n} {self.created.get(n, '2026-11-04T10:00:00.1Z')}"
                            for n in argv[4:])  # fmt: skip
        return subprocess.CompletedProcess(argv, 0, out, "")


@pytest.fixture
def fake(monkeypatch):
    def make(names, alive=(), created=None):
        d = Docker(names, created)
        monkeypatch.setattr(pg.proc, "run", d)
        monkeypatch.setattr(pg.proc, "alive", lambda pid: pid in alive)
        return d

    return make


def name(ns, pid, tag="aaaaaa"):
    return f"{pg.PREFIX}{ns}-{pid}-{tag}" if ns else f"{pg.PREFIX}{pid}-{tag}"


def test_another_namespaces_container_is_never_swept(fake):
    """A test's own lock dir, or the other side of Windows/WSL on one Docker Desktop."""
    other = name("0badc0de", 111)
    d = fake([other])
    assert pg.sweep("docker") == []
    assert other in d.names


def test_own_dead_run_is_swept(fake):
    dead = name(pg.namespace(), 111)
    d = fake([dead])
    assert pg.sweep("docker") == [dead] and not d.names


def test_own_run_whose_process_lives_is_kept_even_with_a_free_lock(fake):
    live = name(pg.namespace(), 222)
    d = fake([live], alive={222})
    assert pg.sweep("docker") == [] and live in d.names


def test_own_run_holding_its_lock_is_kept(fake):
    held_name = name(pg.namespace(), 333)
    d = fake([held_name])
    held = lock.try_acquire(1, f"pg-{held_name}")
    assert held is not None
    try:
        assert pg.sweep("docker") == []
    finally:
        lock.release(held[1])
    assert held_name in d.names


@pytest.mark.parametrize(("alive", "swept"), [((), True), ((444,), False)])
def test_a_container_of_h_030_goes_only_when_its_process_is_gone(fake, alive, swept):
    old = name(None, 444)
    d = fake([old], alive=set(alive))
    assert (pg.sweep("docker") == [old]) is swept
    assert (old in d.names) is not swept


def test_names_h_did_not_make_are_left_alone(fake):
    d = fake([f"{pg.PREFIX}mine", f"{pg.PREFIX}x-y-z-w"])
    assert pg.sweep("docker") == [] and len(d.names) == 2


def test_namespace_follows_the_lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_LOCK_DIR", str(tmp_path / "a"))
    a = pg.namespace()
    monkeypatch.setenv("HARNESS_LOCK_DIR", str(tmp_path / "b"))
    assert pg.namespace() != a and len(a) == 8


def test_namespace_follows_the_platform(monkeypatch):
    here = pg.namespace()
    monkeypatch.setattr(
        pg.sys, "platform", "linux" if pg.sys.platform == "win32" else "win32"
    )
    assert pg.namespace() != here


def test_the_throwaway_is_named_with_its_namespace_and_pid(tmp_path, fake, monkeypatch):
    d = fake([])
    started = []

    def run(argv, cwd, **kw):
        if argv[1] == "run":
            started.append(argv[argv.index("--name") + 1])
            return subprocess.CompletedProcess(argv, 0, "abc\n", "")
        if argv[1] == "port":
            return subprocess.CompletedProcess(argv, 0, "127.0.0.1:55999\n", "")
        return d(argv, cwd)

    monkeypatch.setattr(pg.proc, "run", run)
    monkeypatch.setattr(pg, "_docker", lambda: "docker")
    monkeypatch.setattr(pg, "_ready", lambda docker, name, deadline: True)
    monkeypatch.delenv("CI", raising=False)
    cfg = config.Config(root=tmp_path, name="p")
    cfg.ci_postgres_image = "postgres:18.6"
    with pg.throwaway(
        cfg, [type("S", (), {"kind": "test", "cmd": ["x"], "call": None})()]
    ):
        pass
    [started_name] = started
    assert started_name.startswith(f"{pg.PREFIX}{pg.namespace()}-{os.getpid()}-")


def test_tests_never_see_real_runs_containers():
    """conftest gives every test its own prefix; `name=h-check-pg-` does not match it."""
    assert pg.PREFIX == "h-pytest-pg-" and "h-check-pg-" not in pg.PREFIX


def test_orphans_are_another_namespaces_older_than_a_day(fake):
    import datetime

    old, young, own = (
        name("0badc0de", 1),
        name("0badc0de", 2, "bbbbbb"),
        name(pg.namespace(), 3),
    )
    created = {old: "2026-11-02T09:00:00.123456789Z", young: "2026-11-04T09:00:00.1Z",
               own: "2026-11-01T09:00:00.1Z"}  # fmt: skip
    fake([old, young, own], created=created)
    now = datetime.datetime(2026, 11, 4, 12, 0, tzinfo=datetime.UTC)
    assert pg.orphans("docker", now) == [old]


def test_doctor_names_the_orphans(repo, monkeypatch, capsys):
    toml = repo / "harness.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + '\n[ci]\npostgres-image = "postgres:18.6"\n',
                    encoding="utf-8")  # fmt: skip
    monkeypatch.setattr(pg, "_docker", lambda: "docker")
    monkeypatch.setattr(pg, "orphans", lambda docker: ["h-check-pg-0badc0de-1-aaaaaa"])
    cli.main(["-C", str(repo), "doctor", "--json"])
    tips = json.loads(capsys.readouterr().out)["tips"]
    assert any("docker rm -f -v h-check-pg-0badc0de-1-aaaaaa" in t for t in tips)


def test_alive():
    assert proc.alive(os.getpid())
    assert not proc.alive(0) and not proc.alive(-1)
    assert not proc.alive(4194300)  # beyond any PID in use


def test_ci_yml_s_own_database_is_not_h_s(fake):
    """ci.yml names its test database `h-test-pg-<run id>-<attempt>`: no PID, no h tag —
    a CI run once read the run id as a PID (OverflowError in os.kill)."""
    ci = f"{pg.PREFIX}41833380646-1"
    d = fake([ci])
    assert pg._owner(ci) is None
    assert pg.sweep("docker") == [] and ci in d.names


def test_tests_prefix_differs_from_ci_yml_s_container():
    ci = (__import__("pathlib").Path(__file__).resolve().parents[2]
          / ".github/workflows/ci.yml").read_text(encoding="utf-8")  # fmt: skip
    assert f"PG: {pg.PREFIX}" not in ci


def test_a_number_beyond_any_pid_is_not_alive():
    assert not proc.alive(41833380646)

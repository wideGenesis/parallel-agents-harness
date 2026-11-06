"""A throwaway Postgres per h check run: its own server on a free port, removed
when the run ends — also on failure — and swept after a killed run."""

import os
import subprocess
from types import SimpleNamespace

import pytest

from harness import check, config, lock, pg

IMAGE = "postgres:18.6"


class FakeDocker:
    """Records docker argv; `run` succeeds unless told otherwise, port 55999."""

    def __init__(self, server=True, existing=()):
        self.calls: list[list[str]] = []
        self.server = server
        self.containers = set(existing)

    def __call__(self, argv, cwd, **kw):
        argv = list(argv)
        self.calls.append(argv)
        sub = argv[1]
        out, code = "", 0
        if sub == "version":
            out, code = ("29.0\n", 0) if self.server else ("", 1)
        elif sub == "ps":
            out = "\n".join(sorted(self.containers))
        elif sub == "run":
            self.containers.add(argv[argv.index("--name") + 1])
            out = "abc\n"
        elif sub == "port":
            out = "127.0.0.1:55999\n"
        elif sub == "rm":
            self.containers.discard(argv[-1])
        return subprocess.CompletedProcess(argv, code, out, "")


def _cfg(tmp_path, image=IMAGE, on=True):
    cfg = config.Config(root=tmp_path, name="p")
    cfg.ci_postgres_image, cfg.check_postgres = image, on
    cfg.check_env = {"DATABASE_URL": "postgres://dev@127.0.0.1:55432/dev"}
    return cfg


TEST = [SimpleNamespace(kind="test", cmd=["cargo", "test"], call=None)]


@pytest.fixture
def docker(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(pg.shutil, "which", lambda name: "docker")

    def make(**kw):
        fake = FakeDocker(**kw)
        monkeypatch.setattr(pg.proc, "run", fake)
        return fake

    return make


def test_the_run_gets_its_own_server_and_it_is_removed(tmp_path, docker):
    fake = docker()
    cfg = _cfg(tmp_path)
    with pg.throwaway(cfg, TEST) as server:
        line = server.line
        assert "throwaway Postgres postgres:18.6 on 127.0.0.1:55999" in line
        assert cfg.check_env["DATABASE_URL"].endswith("@127.0.0.1:55999/postgres")
        [name] = fake.containers
        assert name.startswith(pg.PREFIX)
    assert not fake.containers  # removed, with its volume
    assert ["docker", "rm", "-f", "-v", name] in fake.calls
    assert cfg.check_env["DATABASE_URL"] == "postgres://dev@127.0.0.1:55432/dev"


def test_removed_when_the_run_fails(tmp_path, docker):
    fake = docker()
    with pytest.raises(KeyboardInterrupt), pg.throwaway(_cfg(tmp_path), TEST):
        raise KeyboardInterrupt  # Ctrl-C mid-run
    assert not fake.containers


def test_a_killed_runs_container_is_swept_a_live_one_kept(
    tmp_path, docker, monkeypatch
):
    ns = pg.namespace()
    dead, live = f"{pg.PREFIX}{ns}-111-aaaaaa", f"{pg.PREFIX}{ns}-222-bbbbbb"
    monkeypatch.setattr(pg.proc, "alive", lambda pid: False)  # only the lock tells here
    fake = docker(existing=[dead, live])
    held = lock.try_acquire(1, f"pg-{live}")  # the live run holds its lock
    assert held is not None
    try:
        assert pg.sweep("docker") == [dead]
    finally:
        lock.release(held[1])
    assert fake.containers == {live}


def test_no_docker_falls_back_to_check_env(tmp_path, docker):
    fake = docker(server=False)
    cfg = _cfg(tmp_path)
    with pg.throwaway(cfg, TEST) as server:
        line = server.line
        assert "no Docker" in line and "[check.env]" in line
        assert cfg.check_env["DATABASE_URL"].endswith(":55432/dev")
    assert not any(c[1] == "run" for c in fake.calls)


@pytest.mark.parametrize("case", ["off", "no image", "ci", "no test step"])
def test_not_started_when_not_wanted(tmp_path, docker, monkeypatch, case):
    fake = docker()
    cfg = _cfg(tmp_path, on=case != "off", image="" if case == "no image" else IMAGE)
    if case == "ci":
        monkeypatch.setenv("CI", "true")
    steps = (
        [SimpleNamespace(kind="lint", cmd=["x"], call=None)]
        if case == "no test step"
        else TEST
    )
    with pg.throwaway(cfg, steps) as server:
        line = server.line
        assert line == ""
    assert fake.calls == []


def test_check_run_wraps_the_steps(tmp_path, monkeypatch):
    seen = []
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(check, "_run_all", lambda c, s, w=None: seen.append(
        c.check_env.get("DATABASE_URL")) or [])  # fmt: skip

    class Fixed:
        def __enter__(self):
            cfg.check_env["DATABASE_URL"] = "postgres://throwaway"
            return pg.Server()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(check.pg, "throwaway", lambda c, s: Fixed())
    check.run(cfg, [])
    assert seen == ["postgres://throwaway"]


def test_postgres_flag_is_validated(tmp_path):
    (tmp_path / "harness.toml").write_text(
        '[check]\npostgres = "no"\n', encoding="utf-8"
    )
    with pytest.raises(config.ConfigError, match="check.postgres"):
        config.load(tmp_path)


def test_real_docker_or_the_fallback(tmp_path, monkeypatch):
    """The real path where Docker runs (CI, this machine); the fallback where it does not.
    Never skipped: either way the run ends with no container of its own left."""
    monkeypatch.delenv("CI", raising=False)
    cfg = _cfg(tmp_path, image="postgres:18.6")
    docker = pg._docker()
    with pg.throwaway(cfg, TEST) as server:
        line = server.line
        if docker is None:
            assert "no Docker" in line
            return
        url = cfg.check_env["DATABASE_URL"]
        if "did not start" in line:  # no image pull possible here: still cleaned up
            assert url.endswith(":55432/dev")
        else:
            assert "127.0.0.1:" in url and url != "postgres://dev@127.0.0.1:55432/dev"
    r = subprocess.run([docker, "ps", "-a", "--filter", f"name={pg.PREFIX}", "--format",
                        "{{.Names}}"], capture_output=True, text=True, check=False)  # fmt: skip
    assert f"{pg.PREFIX}{os.getpid()}-" not in r.stdout


def test_image_cannot_look_like_a_flag(tmp_path):
    toml = '[ci]\npostgres-image = "--privileged"\n'
    (tmp_path / "harness.toml").write_text(toml, encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load(tmp_path)

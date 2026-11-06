"""worktree setup on Windows, setup failures, and the machine port registry."""

import json
import os

from harness import claims, cli, config, task

WINDOWS = os.name == "nt"


def tool_on_path(tmp_path, monkeypatch):
    """A `record` program on PATH that writes its first argument to arg.txt — a .cmd shim
    on Windows, like npm and pnpm."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if WINDOWS:
        (bin_dir / "record.cmd").write_text("@echo %~1> arg.txt\r\n", encoding="utf-8")
    else:
        script = bin_dir / "record"
        script.write_text('#!/bin/sh\necho "$1" > arg.txt\n', encoding="utf-8")
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def test_setup_finds_a_shim_on_path_and_keeps_backslashes(world, tmp_path, monkeypatch):
    _, a, _ = world
    tool_on_path(tmp_path, monkeypatch)
    cfg = config.load(a)
    arg = r"C:\deps\cache" if WINDOWS else "deps/cache"
    cfg.worktree_setup = [f'record "{arg}"']
    r = task.start(cfg, "T-1")
    assert any(n.startswith("setup ok") for n in r.notes), r.notes
    assert (r.path / "arg.txt").read_text(encoding="utf-8").strip() == arg
    assert (r.path / task.SETUP_OK).is_file()


def test_setup_argv_keeps_windows_paths(monkeypatch):
    monkeypatch.setattr(task.os, "name", "nt")
    monkeypatch.setattr(task.shutil, "which", lambda _p: None)
    assert task.setup_argv(r'tool C:\a\b "D:\with space\x"') == [
        "tool", r"C:\a\b", r"D:\with space\x",
    ]  # fmt: skip


def test_failed_setup_keeps_the_claim_and_resume_redoes_it(
    world, tmp_path, monkeypatch
):
    _, a, _ = world
    cfg = config.load(a)
    cfg.worktree_setup = ["no-such-program-gh97 --flag"]
    r = task.start(cfg, "T-1")  # no traceback: a missing program is a failed step
    [failed] = [n for n in r.notes if n.startswith("setup FAILED")]
    assert "setup.log" in failed and "--resume" in failed
    assert "no-such-program-gh97" in (r.path / task.SETUP_LOG).read_text(
        encoding="utf-8"
    )
    assert not (r.path / task.SETUP_OK).exists()
    assert claims.read(cfg, "T-1") is not None

    tool_on_path(tmp_path, monkeypatch)
    cfg.worktree_setup = ["record fixed"]
    again = task.start(cfg, "T-1", resume=True)
    assert any(n.startswith("setup ok") for n in again.notes), again.notes
    assert (again.path / task.SETUP_OK).is_file()
    third = task.start(cfg, "T-1", resume=True)  # done once: not repeated
    assert not any(n.startswith("setup") for n in third.notes)


def test_a_broken_port_registry_does_not_block_start(world):
    _, a, _ = world
    cfg = config.load(a)
    path = task._ports_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"half": {"api": 2', encoding="utf-8")  # a crash mid-write
    r = task.start(cfg, "T-1")
    assert set(r.ports) == {"api", "web"}
    assert any("port registry was unreadable" in n for n in r.notes)
    assert path.with_suffix(".json.bak").is_file()
    assert task.has_ports(cfg, "T-1")


def test_registry_entries_of_the_wrong_shape_are_dropped_not_the_good_ones(world):
    """valid JSON, wrong shape inside — start used to crash on every run."""
    _, a, _ = world
    cfg = config.load(a)
    path = task._ports_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    good = {"api": 20001, "web": 20002}
    path.write_text(
        json.dumps({
            "/other/wt": good,
            "/list/wt": [20003],
            "/str/wt": {"api": "20004"},
            "/bool/wt": {"api": True},
        }),
        encoding="utf-8",
    )  # fmt: skip
    r = task.start(cfg, "T-1")
    assert set(r.ports) == {"api", "web"}
    assert not set(r.ports.values()) & set(good.values())
    assert any("port registry was partly broken" in n for n in r.notes), r.notes
    registry = json.loads(path.read_text(encoding="utf-8"))
    assert registry["/other/wt"] == good
    assert not {"/list/wt", "/str/wt", "/bool/wt"} & set(registry)
    assert not path.with_suffix(".json.bak").exists()


def test_ports_are_unique_across_clones_and_outside_the_ephemeral_range(world):
    _, a, b = world
    ra = task.start(config.load(a), "T-1")
    rb = task.start(config.load(b), "T-2")
    ports = [*ra.ports.values(), *rb.ports.values()]
    assert len(set(ports)) == 4
    lo, hi = task.PORT_RANGE
    assert all(lo <= p < hi for p in ports)


def test_registry_writes_leave_no_temp_files(world):
    _, a, _ = world
    task.start(config.load(a), "T-1")
    leftovers = [p.name for p in task._ports_file().parent.glob("*.tmp")]
    assert leftovers == [], leftovers


def test_cli_start_exits_nonzero_on_failed_setup(world, tmp_path, monkeypatch, capsys):
    # the agent must not read a failed setup as a ready worktree.
    _, a, _ = world
    toml = a / "harness.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            'ports = ["api", "web"]',
            'ports = ["api", "web"]\nsetup = ["no-such-program-gh136"]',
        ),
        encoding="utf-8",
    )
    assert cli.main(["-C", str(a), "task", "start", "T-1", "--json"]) == 1
    got = json.loads(capsys.readouterr().out)
    assert got["setup_failed"] and got["worktree"].endswith("T-1")
    assert claims.read(config.load(a), "T-1") is not None  # kept: the fix happens there

    tool_on_path(tmp_path, monkeypatch)
    toml.write_text(
        toml.read_text(encoding="utf-8").replace("no-such-program-gh136", "record ok"),
        encoding="utf-8",
    )
    assert cli.main(["-C", str(a), "task", "start", "T-1", "--resume", "--json"]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["resumed"] and not again["setup_failed"]

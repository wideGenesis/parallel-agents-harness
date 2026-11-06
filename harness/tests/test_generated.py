"""[[check.generated]]: a committed file generated from another stack's files is
regenerated and compared when its inputs or the file change; the tree is left as it was."""

import json
import sys

import pytest
from conftest import git

from harness import affected, check, config, stacks

CORE = "crates/poly-core/src/lib.rs"
OUT = "ts/app/src/api.txt"
# The generator: the client is the Rust source's line count — any change to it shows.
GEN = """import pathlib
src = pathlib.Path("crates/poly-core/src/lib.rs").read_text(encoding="utf-8")
out = pathlib.Path("ts/app/src/api.txt")
out.write_text("lines=" + str(len(src.splitlines())) + chr(10), encoding="utf-8")
"""


def _setup(repo):
    py = sys.executable.replace("\\", "/")
    (repo / "gen.py").write_text(GEN, encoding="utf-8")
    with (repo / "harness.toml").open("a", encoding="utf-8") as f:
        # quoted: a path with spaces stays one argument (POSIX quoting on every OS)
        f.write(f"\n[[check.generated]]\npath = \"{OUT}\"\ncommand = '\"{py}\" gen.py'\n"
                'inputs = ["crates/poly-core/**"]\n')  # fmt: skip
    lines = len((repo / CORE).read_text(encoding="utf-8").splitlines())
    (repo / OUT).write_text(f"lines={lines}\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "gen")
    git(repo, "branch", "-f", "main", "HEAD")


def _plan(repo):
    cfg = config.load(repo)
    found = stacks.detect(cfg)
    files = affected.changed_files(cfg, affected.resolve_base(cfg, None))
    impact = affected.compute(cfg, found, files)
    return cfg, impact, check.plan(cfg, found, impact, ("generated",), files=files)


def _commit(repo, rel, text):
    p = repo / rel
    p.write_text(p.read_text(encoding="utf-8") + text, encoding="utf-8")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "change")


def test_stale_generated_file_is_red_and_the_tree_is_untouched(repo):
    _setup(repo)
    _commit(repo, CORE, "// one more line\n")
    cfg, impact, steps = _plan(repo)
    [step] = steps
    assert step.stack == "generated" and step.note == OUT
    assert "@poly/app" in impact["ts"].affected  # the input affects the file's package
    before = (repo / OUT).read_bytes()
    r = check._run(cfg, step)
    assert r.status == "fail" and f"{OUT} is stale" in r.tail
    assert (repo / OUT).read_bytes() == before


def test_regenerated_and_committed_file_is_green(repo):
    _setup(repo)
    _commit(repo, CORE, "// one more line\n")
    lines = len((repo / CORE).read_text(encoding="utf-8").splitlines())
    (repo / OUT).write_text(f"lines={lines}\n", encoding="utf-8")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "regen")
    cfg, _, [step] = _plan(repo)
    r = check._run(cfg, step)
    assert r.status == "ok", r.tail


def test_unrelated_change_plans_no_generated_step(repo):
    _setup(repo)
    _commit(repo, "py/poly-lib/src/poly_lib/__init__.py", "# x\n")
    assert _plan(repo)[2] == []


def test_config_validates_entries():
    ok = {
        "check": {"generated": [{"path": "a.ts", "command": "gen", "inputs": ["x/**"]}]}
    }
    assert config._generated(ok)[0].inputs == ("x/**",)
    for bad in ([{"path": "a.ts", "command": "gen"}], [{"path": "../a", "command": "g",
                 "inputs": ["x"]}], "a.ts"):  # fmt: skip
        with pytest.raises(config.ConfigError, match="check.generated"):
            config._generated({"check": {"generated": bad}})


def test_generator_runs_after_the_waves_in_a_slot(repo, monkeypatch):
    """never beside the typecheck and tests that read the same file."""
    _setup(repo)
    _commit(repo, CORE, "// one more line\n")
    cfg = config.load(repo)
    found = stacks.detect(cfg)
    files = affected.changed_files(cfg, affected.resolve_base(cfg, None))
    steps = check.plan(cfg, found, affected.compute(cfg, found, files),
                       check.FAST + check.SLOW, files=files)  # fmt: skip
    order, slots = [], []
    monkeypatch.setattr(check, "_run", lambda c, s, d=None: order.append(s.kind)
                        or check.StepResult(s, "ok"))  # fmt: skip
    real = check.lock.slot

    def slot(limit, on_wait=None):
        slots.append(limit)
        return real(limit, on_wait=on_wait)

    monkeypatch.setattr(check.lock, "slot", slot)
    monkeypatch.setattr(check, "_run_gated", lambda c, s, w, d: check._run(c, s, d))
    check.run(cfg, steps)
    assert order[-1] == "generated" and order.count("generated") == 1
    assert slots  # the generator held a heavy slot


def test_plan_json_and_config_show_the_generator(repo, capsys):
    from harness import cli

    _setup(repo)
    _commit(repo, CORE, "// one more line\n")
    assert cli.main(["-C", str(repo), "check", "--plan", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert {"stack": "generated", "kind": "generated", "runs": True} in plan
    assert cli.main(["-C", str(repo), "config", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["generators"][0]["path"] == OUT


def test_a_wave_step_that_regenerates_in_place_does_not_hide_staleness(repo):
    """A project's typecheck regenerates the client in place before the
    generated step runs. The step compares with the file as planned, and restores it."""
    _setup(repo)
    _commit(repo, CORE, "// one more line\n")
    cfg, _, [step] = _plan(repo)
    committed = (repo / OUT).read_bytes()
    lines = len((repo / CORE).read_text(encoding="utf-8").splitlines())
    (repo / OUT).write_text(
        f"lines={lines}\n", encoding="utf-8"
    )  # the wave's typecheck
    r = check._run(cfg, step)
    assert r.status == "fail" and f"{OUT} is stale" in r.tail
    assert (repo / OUT).read_bytes() == committed


def test_exclusive_generated_runs_alone(repo, monkeypatch):
    _setup(repo)
    _commit(repo, CORE, "// one more line\n")
    cfg, _, [step] = _plan(repo)
    cfg.exclusive = ["generated"]
    machine = []
    real = check.lock.machine

    def whole(limit, on_wait=None):
        machine.append(limit)
        return real(limit, on_wait=on_wait)

    monkeypatch.setattr(check.lock, "machine", whole)
    monkeypatch.setattr(check, "_run", lambda c, s, d=None: check.StepResult(s, "ok"))
    check.run(cfg, [step])
    assert machine

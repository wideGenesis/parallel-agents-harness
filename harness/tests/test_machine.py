"""The machine budget: at most max_cores for heavy steps of all sessions, no start
above max_load, each step's tools held to its share."""

import os
import sys

import pytest

from harness import check, config, machine


def _file(tmp_path, monkeypatch, text):
    f = tmp_path / "machine.toml"
    f.write_text(text, encoding="utf-8")
    monkeypatch.setenv("HARNESS_MACHINE", str(f))


def test_defaults_on_a_24_thread_machine(monkeypatch):
    monkeypatch.setattr(machine, "threads", lambda: 24)
    b = machine.budget()
    assert (b.max_cores, b.max_load, b.slots, b.share) == (20, 0.8, 3, 6)
    assert b.slots * b.share + machine.POSTGRES_CORES <= b.max_cores


@pytest.mark.parametrize(
    ("n", "expected"), [(2, (2, 1, 2)), (4, (4, 2, 2)), (8, (6, 2, 2))]
)
def test_small_machines_keep_useful_shares(monkeypatch, n, expected):
    """A 2-vCPU CI runner: one step with both cores, not cargo -j1."""
    monkeypatch.setattr(machine, "threads", lambda: n)
    b = machine.budget()
    assert (b.max_cores, b.slots, b.share) == expected


def test_machine_file_and_heavy_jobs(tmp_path, monkeypatch):
    _file(tmp_path, monkeypatch, "max_cores = 10\nmax_load = 0.5\n")
    b = machine.budget()
    assert (b.max_cores, b.max_load, b.slots, b.share) == (10, 0.5, 3, 3)
    b = machine.budget(heavy_jobs=2)  # harness.toml's old knob: steps at once
    assert (b.slots, b.share) == (2, 4)
    assert b.env()["CARGO_BUILD_JOBS"] == "4" and b.env()["H_CORES"] == "4"
    assert b.env()["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "4"


@pytest.mark.parametrize("text", ["max_cores = 0\n", "max_cores = true\n", "max_load = 1.5\n",
                                  "max_load = 'x'\n", "max_cores = [\n"])  # fmt: skip
def test_bad_machine_file(tmp_path, monkeypatch, text):
    _file(tmp_path, monkeypatch, text)
    with pytest.raises(machine.BudgetError):
        machine.budget()


def test_load_reads_this_machine():
    now = machine.load(sample=0.2)
    assert now is None or 0.0 <= now <= 1.0
    if sys.platform == "win32" or os.path.isfile("/proc/stat"):
        assert now is not None


def _step():
    return check.Step("rust", "test", [sys.executable, "-c", "pass"])


def test_heavy_step_waits_for_load_then_gets_its_share(tmp_path, monkeypatch):
    readings = iter([0.95, 0.9, 0.5])
    monkeypatch.setattr(machine, "load", lambda sample=0.5: next(readings))
    monkeypatch.setattr(check.time, "sleep", lambda s: None)
    seen, waits = [], []
    monkeypatch.setattr(check, "_run", lambda c, s, d=None: seen.append(dict(s.env))
                        or check.StepResult(s, "ok"))  # fmt: skip
    cfg = config.Config(root=tmp_path, name="p")
    check._heavy(cfg, _step(), lambda s, why="": waits.append(why), None)
    assert waits and "machine load (95%)" in waits[0] and "80%" in waits[0]
    b = machine.budget()
    assert seen[0]["CARGO_BUILD_JOBS"] == str(b.share)


def test_exclusive_step_gets_the_whole_budget(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(machine, "load", lambda sample=0.5: 0.0)
    monkeypatch.setattr(check, "_run", lambda c, s, d=None: seen.append(dict(s.env))
                        or check.StepResult(s, "ok"))  # fmt: skip
    cfg = config.Config(root=tmp_path, name="p")
    check._heavy(cfg, _step(), None, None, whole=True)
    b = machine.budget()
    assert seen[0]["CARGO_BUILD_JOBS"] == str(b.slots * b.share)


def test_slots_bound_the_steps_of_all_sessions(tmp_path, monkeypatch):
    """Take every slot the budget allows: the next step must wait."""
    from harness import lock

    b = machine.budget()
    held = [lock.try_acquire(b.slots) for _ in range(b.slots)]
    try:
        assert lock.try_acquire(b.slots) is None
    finally:
        for h in held:
            assert h is not None
            lock.release(h[1])


def test_bad_machine_file_is_an_error_not_a_traceback(tmp_path, monkeypatch, capsys):
    from harness import cli

    _file(tmp_path, monkeypatch, "max_cores = 0\n")
    (tmp_path / "harness.toml").write_text('[project]\nname = "p"\n', encoding="utf-8")
    assert cli.main(["-C", str(tmp_path), "config", "--json"]) != 0
    assert "max_cores" in capsys.readouterr().err


def test_check_env_wins_over_the_share(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(check, "_run", lambda c, s, d=None: seen.append(dict(s.env))
                        or check.StepResult(s, "ok"))  # fmt: skip
    cfg = config.Config(root=tmp_path, name="p")
    cfg.check_env = {"CARGO_BUILD_JOBS": "2"}
    check._heavy(cfg, _step(), None, None)
    assert "CARGO_BUILD_JOBS" not in seen[0] and seen[0]["RUST_TEST_THREADS"]


def test_load_wait_has_a_ceiling(tmp_path, monkeypatch):
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(check.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(check.time, "sleep", lambda s: None)
    monkeypatch.setattr(machine, "load", lambda sample=0.5: 1.0)  # never drops
    monkeypatch.setattr(check, "_run", lambda c, s, d=None: check.StepResult(s, "ok"))
    r = check._heavy(config.Config(root=tmp_path, name="p"), _step(), None, None)
    assert r.status == "ok"  # started after LOAD_WAIT_S instead of waiting for ever

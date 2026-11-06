"""Test tethering: a test file no check step runs makes `h check` red."""

import json
import sys

import pytest
from conftest import git

from harness import affected, check, cli, config, stacks

PY = sys.executable.replace("\\", "/")


def write(proj, rel, text="def test_x():\n    pass\n"):
    p = proj / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def on_base(repo, toml):
    """Append to harness.toml on main (the base) and come back to the work branch."""
    git(repo, "switch", "-q", "main")
    with (repo / "harness.toml").open("a", encoding="utf-8") as f:
        f.write(toml)
    git(repo, "commit", "-qam", "base rules")
    git(repo, "switch", "-q", "work")
    git(repo, "merge", "-q", "main")


def tether(repo, capsys):
    code = cli.main(["-C", str(repo), "tether"])
    return code, capsys.readouterr().out.replace("\\", "/")


def test_clean_polygon_is_tethered(repo, capsys):
    assert tether(repo, capsys) == (
        0,
        "tether: ok — every test file is run by a check step\n",
    )


def test_ts_package_without_test_script(repo, capsys):
    write(repo, "ts/app/src/app.test.ts", "export {};\n")
    code, out = tether(repo, capsys)
    assert code == 1
    assert "ts/app/src/app.test.ts — package @poly/app has no `test` script" in out


def test_python_test_outside_every_package(repo, capsys):
    write(repo, "scripts/test_stray.py")
    code, out = tether(repo, capsys)
    assert code == 1
    assert "scripts/test_stray.py — outside every python package" in out


def test_json_lists_the_loose_files(repo, capsys):
    write(repo, "scripts/test_stray.py")
    assert cli.main(["-C", str(repo), "tether", "--json"]) == 1
    [loose] = json.loads(capsys.readouterr().out)
    assert loose["path"] == "scripts/test_stray.py" and loose["stack"] == "python"


def test_exception_on_the_base_excuses_the_file(repo, capsys):
    write(repo, "ts/app/src/app.test.ts", "export {};\n")
    on_base(
        repo, '\n[check.untethered]\n"ts/app/src/*.test.ts" = "fixture for a demo"\n'
    )
    assert tether(repo, capsys)[0] == 0


def test_branch_cannot_excuse_its_own_file(repo, capsys):
    write(repo, "ts/app/src/app.test.ts", "export {};\n")
    with (repo / "harness.toml").open("a", encoding="utf-8") as f:
        f.write('\n[check.untethered]\n"ts/app/src/*.test.ts" = "my own excuse"\n')
    assert tether(repo, capsys)[0] == 1  # gate rules come from the base


def test_exception_needs_a_reason(repo):
    on_base(repo, '\n[check.untethered]\n"scripts/*" = " "\n')
    with pytest.raises(config.ConfigError, match="needs a reason"):
        config.load(repo)


def test_override_test_command_is_trusted(repo, capsys):
    write(repo, "ts/app/src/app.test.ts", "export {};\n")
    with (repo / "harness.toml").open("a", encoding="utf-8") as f:
        f.write('\n[stacks.ts.commands]\ntest = "vitest run"\n')
    assert tether(repo, capsys)[0] == 0


def _python_test_step(repo, touched):
    write(repo, touched, "# touched\n")
    cfg = config.load(repo)
    found = stacks.detect(cfg)
    impact = affected.compute(
        cfg, found, affected.changed_files(cfg, affected.resolve_base(cfg, None))
    )
    return next(
        s for s in check.plan(cfg, found, impact, ("test",)) if s.stack == "python"
    )


def test_no_tests_collected_fails_when_test_files_exist(repo):
    step = _python_test_step(repo, "py/poly-lib/src/poly_lib/extra.py")
    assert step.ok_codes == (0,)  # poly-lib has tests/test_add.py


def test_no_tests_collected_is_fine_without_test_files(repo):
    step = _python_test_step(repo, "py/poly-app/src/poly_app/extra.py")
    assert step.ok_codes == (0, 5)  # poly-app has none: exit 5 is fine


def test_exit_5_failure_explains_itself(repo):
    cfg = config.load(repo)
    step = check.Step("python", "test", [PY, "-c", "raise SystemExit(5)"], hint="why")
    [r] = check.run(cfg, [step])
    assert r.status == "fail" and r.tail.endswith("why")


def test_check_runs_tether_with_lint(repo, capsys):
    write(repo, "ts/app/src/app.test.ts", "export {};\n")
    cfg = config.load(repo)
    found = stacks.detect(cfg)
    steps = check.plan(cfg, found, None, ("lint",))
    [t] = [s for s in steps if s.stack == "tether"]
    [r] = check.run(cfg, [t])
    assert r.status == "fail" and "app.test.ts" in r.tail


def test_excused_only_test_file_keeps_exit_5_lenient(repo):
    write(repo, "py/poly-app/tests/test_demo.py")
    on_base(repo, '\n[check.untethered]\n"py/poly-app/tests/*" = "demo, run by hand"\n')
    step = _python_test_step(repo, "py/poly-app/src/poly_app/extra.py")
    assert step.ok_codes == (0, 5)  # the excused file is not a test of the package


def pytest_config(repo, body):
    p = repo / "py/poly-lib/pyproject.toml"
    p.write_text(
        p.read_text(encoding="utf-8") + f"\n[tool.pytest.ini_options]\n{body}\n",
        encoding="utf-8",
    )


def test_file_not_matching_python_files(repo, capsys):
    pytest_config(repo, 'python_files = ["test_*.py"]')
    write(repo, "py/poly-lib/tests/add_test.py")
    code, out = tether(repo, capsys)
    assert code == 1
    assert "py/poly-lib/tests/add_test.py — pytest python_files" in out
    assert "test_add.py" not in out  # its neighbour is collected


def test_file_under_norecursedirs(repo, capsys):
    pytest_config(repo, 'norecursedirs = ["slow"]')
    write(repo, "py/poly-lib/tests/slow/test_big.py")
    code, out = tether(repo, capsys)
    assert code == 1
    assert "tests/slow/test_big.py — under slow/, which pytest norecursedirs" in out


def test_default_python_files_from_ini_without_the_key(repo, capsys):
    pytest_config(repo, 'addopts = "-q"')
    write(repo, "py/poly-lib/tests/add_test.py")
    assert tether(repo, capsys)[0] == 0  # defaults: test_*.py and *_test.py

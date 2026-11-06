"""Python tests selected by changed files through pytest-testmon."""

import sys

from conftest import git

from harness import affected, check, config, stacks

PY = sys.executable.replace("\\", "/")


def pin_testmon(repo):
    """polygon declares pytest-testmon on main, so the work branch has not changed it."""
    git(repo, "switch", "-q", "main")
    p = repo / "pyproject.toml"
    text = p.read_text(encoding="utf-8")
    p.write_text(text.replace('"pytest>=8"', '"pytest>=8", "pytest-testmon"'),
                 encoding="utf-8")  # fmt: skip
    git(repo, "commit", "-qam", "testmon")
    git(repo, "switch", "-q", "work")
    git(repo, "reset", "-q", "--hard", "main")


def touch(repo, rel):
    p = repo / rel
    p.write_text(p.read_text(encoding="utf-8") + "# x\n", encoding="utf-8")


def plan_test_step(repo, select=True, full=False):
    cfg = config.load(repo)
    found = stacks.detect(cfg)
    impact = None
    if not full:
        files = affected.changed_files(cfg, affected.resolve_base(cfg, None))
        impact = affected.compute(cfg, found, files)
    steps = check.plan(cfg, found, impact, ("test",), select_tests=select)
    return next(s for s in steps if s.stack == "python")


def test_without_testmon_the_test_step_is_unchanged(repo):
    touch(repo, "py/poly-lib/src/poly_lib/__init__.py")
    step = plan_test_step(repo)
    assert step.cmd is not None and "--testmon" not in step.cmd
    assert not step.testmon and step.note == ""


def test_first_run_builds_the_base_then_selects(repo):
    pin_testmon(repo)
    touch(repo, "py/poly-lib/src/poly_lib/__init__.py")
    step = plan_test_step(repo)
    assert step.cmd is not None and "--testmon" in step.cmd
    assert "no testmon base" in step.note
    # A full run keeps the rule: poly-lib has test files, so collecting none is a failure.
    assert step.ok_codes == (0,)
    data = step.env["TESTMON_DATAFILE"]
    assert ".artifacts" in data  # never committed, gone with the worktree
    (repo / check.TESTMON_DIR / "python.testmondata").write_bytes(b"")
    step = plan_test_step(repo)
    assert step.cmd is not None and "--testmon" in step.cmd and step.note == ""
    assert step.ok_codes == (
        0,
        5,
    )  # selecting: exit 5 is testmon finding nothing affected


def test_shared_files_run_everything(repo):
    pin_testmon(repo)
    (repo / check.TESTMON_DIR).mkdir(parents=True)
    (repo / check.TESTMON_DIR / "python.testmondata").write_bytes(b"")
    (repo / "py/poly-lib/tests").mkdir(parents=True, exist_ok=True)
    (repo / "py/poly-lib/tests/conftest.py").write_text("# shared\n", encoding="utf-8")
    step = plan_test_step(repo)
    assert step.cmd is not None and "--testmon-noselect" in step.cmd
    assert "conftest.py changed" in step.note


def test_full_review_and_ship_run_everything(repo):
    pin_testmon(repo)
    touch(repo, "py/poly-lib/src/poly_lib/__init__.py")
    for step in (plan_test_step(repo, full=True), plan_test_step(repo, select=False)):
        assert step.cmd is not None and not any("testmon" in a for a in step.cmd)


def test_selected_counts_the_run_and_takes_the_total_from_the_base(tmp_path):
    out = "F.\n1 failed, 1 passed, 1 skipped in 0.5s\n"
    assert check.selected(out, 179) == "selected 3 of 179 tests (testmon)"
    assert check.selected(out) == "selected 3 tests (testmon)"  # base unreadable
    assert "none affected" in check.selected("\nno tests ran in 0.00s\n", 179)
    assert check.known_tests(str(tmp_path / "missing.testmondata")) is None


def test_testmon_really_selects_the_changed_module(repo, tmp_path):
    """The real plugin: a change in one module runs only its test."""
    proj = tmp_path / "proj"
    proj.mkdir()
    for name in ("a", "b"):
        (proj / f"{name}.py").write_text(
            f"def f():\n    return '{name}'\n", encoding="utf-8"
        )
        (proj / f"test_{name}.py").write_text(
            f"from {name} import f\n\n\ndef test_f():\n    assert f() == '{name}'\n",
            encoding="utf-8",
        )
    cfg = config.load(repo)
    data = tmp_path / "base.testmondata"
    step = check.Step(
        "python", "test", [PY, "-m", "pytest", "-q", "--testmon"],
        ok_codes=(0, 5), cwd=proj, env={"TESTMON_DATAFILE": str(data)}, testmon=True,
    )  # fmt: skip
    [first] = check.run(cfg, [step])
    assert first.status == "ok", first.tail
    assert first.note == "selected 2 of 2 tests (testmon)"
    (proj / "a.py").write_text("def f():\n    return 'a' + ''\n", encoding="utf-8")
    [second] = check.run(cfg, [step])
    assert second.status == "ok", second.tail
    assert second.note == "selected 1 of 2 tests (testmon)"
    # testmon compares code, not text: nothing to run is exit 5, and that is ok here.
    (proj / "b.py").write_text("def f():\n    return 'b'  # note\n", encoding="utf-8")
    [third] = check.run(cfg, [step])
    assert third.status == "ok", third.tail
    assert third.note.startswith("selected 0 of 2 tests")

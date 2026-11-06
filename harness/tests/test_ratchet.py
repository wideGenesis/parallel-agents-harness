"""suppression and test-skip counts may only go down."""

import json

import pytest
from conftest import git

from harness import check, cli, config, init, ratchet, stacks

# Built from pieces so this file does not count towards the harness repo's own base.
IGNORE = "# type" + ": ignore"
NOQA = "# no" + "qa: E501"
SKIP = "@pytest.mark." + "skip"
TEST_PY = "py/poly-lib/tests/test_add.py"


def keys(line, path="x.py"):
    return [h.key for h in ratchet._hits(path, 1, line)]


@pytest.mark.parametrize(
    ("line", "path", "want"),
    [
        (f"x = f()  {IGNORE}[attr-defined]", "a.py",
         ["python:suppress:type: ignore[attr-defined]"]),
        (f"x = 1  {IGNORE}", "a.py", ["python:suppress:type: ignore"]),
        ("x = 1  # no" + "qa: E501, F401", "a.py",
         ["python:suppress:noqa[E501]", "python:suppress:noqa[F401]"]),
        ("x = 1  # pyright" + ": ignore", "a.py", ["python:suppress:pyright: ignore"]),
        (SKIP + "if(True, reason='x')", "a.py", ["python:skip-test:pytest.mark[skipif]"]),
        ("    pytest." + "skip('no cargo')", "a.py", ["python:skip-test:pytest[skip]"]),
        ("#[al" + "low(dead_code, clippy::x)]", "a.rs",
         ["rust:suppress:allow[dead_code]", "rust:suppress:allow[clippy::x]"]),
        ("#[ig" + "nore]", "a.rs", ["rust:skip-test:#[ignore]"]),
        ("// eslint" + "-disable-next-line no-console", "a.ts",
         ["ts:suppress:eslint-disable[no-console]"]),
        # the justification after " -- " is not a rule
        ("// eslint" + "-disable-next-line no-console -- legacy, see #12", "a.ts",
         ["ts:suppress:eslint-disable[no-console]"]),
        ("/* eslint" + "-disable no-a, no-b no-c -- why */", "a.ts",
         ["ts:suppress:eslint-disable[no-a]", "ts:suppress:eslint-disable[no-b]",
          "ts:suppress:eslint-disable[no-c]"]),
        ("// eslint" + "-disable-line -- why", "a.ts", ["ts:suppress:eslint-disable"]),
        ("// @ts" + "-ignore", "a.tsx", ["ts:suppress:@ts[ignore]"]),
        ("it." + "only('x', () => {})", "a.test.ts", ["ts:skip-test:test[only]"]),
        ("x" + "it('x', () => {})", "a.test.js", ["ts:skip-test:x-test[xit]"]),
        (f"x = 1  {IGNORE}", "a.md", []),  # not a source file
    ],
)  # fmt: skip
def test_patterns(line, path, want):
    assert keys(line, path) == want


def test_strict_xfail_with_an_issue_is_accepted():
    ok = "@pytest.mark." + "xfail(strict=True, reason='bug on main, #123')"
    assert keys(ok) == []
    assert keys("@pytest.mark." + "xfail(reason='flaky')") == [
        "python:skip-test:pytest.mark[xfail]"
    ]


def add_line(repo, rel, line):
    p = repo / rel
    p.write_text(p.read_text(encoding="utf-8") + line + "\n", encoding="utf-8")


def run(repo, capsys, *argv):
    code = cli.main(["-C", str(repo), "ratchet", *argv])
    return code, capsys.readouterr().out


def test_first_base_never_blocks_existing_debt(repo, capsys):
    add_line(repo, TEST_PY, f"X = 1  {IGNORE}")
    git(repo, "commit", "-qam", "debt")
    init.run(config.load(repo))
    base = json.loads((repo / ratchet.BASE).read_text(encoding="utf-8"))
    assert base["counts"]["python:suppress:type: ignore"] == 1
    code, out = run(repo, capsys)
    assert code == 0 and "none new" in out


def test_growth_fails_and_names_the_new_line(repo, capsys):
    git(repo, "switch", "-q", "main")  # the debt and its base are on main already
    add_line(repo, TEST_PY, f"A = 1  {IGNORE}")
    ratchet.update(config.load(repo))
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "debt and base")
    git(repo, "switch", "-q", "work")
    git(repo, "rebase", "-q", "main")
    add_line(repo, TEST_PY, f"B = 2  {IGNORE}")
    git(repo, "commit", "-qam", "new")  # committed on the branch, not just in the tree
    code, out = run(repo, capsys)
    assert code == 1
    assert "python:suppress:type: ignore: 1 → 2" in out
    lines = (repo / TEST_PY).read_text(encoding="utf-8").splitlines()
    assert f"{TEST_PY}:{len(lines)}" in out
    assert f"{TEST_PY}:{len(lines) - 1}" not in out  # the old one is not "new"


def grow_on_branch(repo, rel, old, new):
    """`old` with its base on main, `new` committed on the work branch."""
    git(repo, "switch", "-q", "main")
    (repo / rel).write_bytes(old.encode())
    ratchet.update(config.load(repo))
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "debt and base")
    git(repo, "switch", "-q", "work")
    git(repo, "rebase", "-q", "main")
    (repo / rel).write_bytes((old + new).encode())
    git(repo, "commit", "-qam", "new")


def test_changing_an_eslint_justification_is_not_growth(repo, capsys):
    rule = "// eslint" + "-disable-next-line no-console"
    (repo / "ts/a.ts").write_text(f"{rule} -- old reason\nconsole.log(1)\n", "utf-8")
    ratchet.update(config.load(repo))
    (repo / "ts/a.ts").write_text(f"{rule} -- new reason\nconsole.log(1)\n", "utf-8")
    code, out = run(repo, capsys)
    assert code == 0, out


@pytest.mark.parametrize(
    ("rel", "pad"),
    [
        ("py/my mod.py", "X = 0\n"),  # git appends a tab to "+++ b/my mod.py"
        ("py/cr.py", "S = 'a\rb\x0cc\u2028d'\n"),  # git counts lines by LF only
    ],
)
def test_new_line_is_named_despite_path_or_line_breaks(repo, capsys, rel, pad):
    grow_on_branch(repo, rel, f"{pad}A = 1  {NOQA}\n", f"B = 2  {NOQA}\n")
    code, out = run(repo, capsys)
    assert code == 1
    assert f"{rel}:3" in out
    assert f"{rel}:2" not in out  # the old one is not "new"


def test_skipping_a_test_fails(repo, capsys):
    ratchet.update(config.load(repo))
    add_line(repo, TEST_PY, f"\n{SKIP}\ndef test_x():\n    assert False")
    code, out = run(repo, capsys)
    assert code == 1 and "python:skip-test:pytest.mark[skip]" in out
    assert TEST_PY in out


def test_new_untracked_file_with_a_non_ascii_name(repo, capsys):
    ratchet.update(config.load(repo))
    (repo / "py" / "モジュール.py").write_text(f"X = 1  {NOQA}\n", encoding="utf-8")
    code, out = run(repo, capsys)
    assert code == 1 and "py/モジュール.py:1" in out


def test_drop_passes_and_asks_to_commit_the_lower_base(repo, capsys):
    add_line(repo, TEST_PY, f"A = 1  {IGNORE}")
    ratchet.update(config.load(repo))
    p = repo / TEST_PY
    p.write_text(p.read_text(encoding="utf-8").replace(f"  {IGNORE}", ""), "utf-8")
    code, out = run(repo, capsys)
    assert code == 0 and "h ratchet --update" in out
    assert run(repo, capsys, "--update")[0] == 0
    base = json.loads((repo / ratchet.BASE).read_text(encoding="utf-8"))
    assert "python:suppress:type: ignore" not in base["counts"]


def test_update_refuses_growth_unless_allowed(repo, capsys):
    ratchet.update(config.load(repo))
    before = (repo / ratchet.BASE).read_text(encoding="utf-8")
    add_line(repo, TEST_PY, f"A = 1  {IGNORE}")
    code, out = run(repo, capsys, "--update")
    assert code == 1 and "NOT UPDATED" in out
    assert (repo / ratchet.BASE).read_text(encoding="utf-8") == before
    assert run(repo, capsys, "--update", "--allow-growth")[0] == 0
    assert run(repo, capsys)[0] == 0


def test_check_runs_ratchet_only_when_enabled(repo):
    # Repo-wide: it runs even when no package is affected (impact without stacks).
    cfg = config.load(repo)

    def repo_wide(kinds):
        return [s for s in check.plan(cfg, [], {}, kinds) if s.stack == "ratchet"]

    assert repo_wide(("lint",)) == []
    ratchet.update(cfg)
    [step] = repo_wide(("fmt", "lint"))
    assert (step.stack, step.kind) == ("ratchet", "lint")
    assert repo_wide(("fmt",)) == []
    assert any(
        s.stack == "ratchet"
        for s in check.plan(cfg, stacks.detect(cfg), None, ("lint",))
    )


def test_check_step_goes_red_on_growth(repo):
    cfg = config.load(repo)
    ratchet.update(cfg)
    add_line(repo, TEST_PY, f"A = 1  {IGNORE}")
    [step] = [s for s in check.plan(cfg, [], {}, ("lint",)) if s.stack == "ratchet"]
    [res] = check.run(cfg, [step])
    assert res.status == "fail" and TEST_PY in res.tail


def test_unreadable_base_is_an_error(repo, capsys):
    (repo / ratchet.BASE).parent.mkdir()
    (repo / ratchet.BASE).write_text("{", encoding="utf-8")
    assert cli.main(["-C", str(repo), "ratchet"]) == 2
    assert "restore it from git" in capsys.readouterr().err

"""rust test runs through nextest (one pool for every test binary) with the doctests
after it, and says so when nextest is missing — in CI that was the silent sequential path."""

import json
import re
import sys
from pathlib import Path

import pytest
from conftest import POLYGON

from harness import check, cli, config, stacks, tools

PY = sys.executable
REPO = Path(__file__).resolve().parents[2]


def rust_test(proj, monkeypatch, nextest):
    monkeypatch.setattr(
        tools, "usable", lambda t: nextest if t == "cargo-nextest" else True
    )
    cfg = config.load(proj)
    steps = check.plan(cfg, stacks.detect(cfg), None, ("test",))
    return next(s for s in steps if s.stack == "rust")


def test_with_nextest_the_doctests_of_lib_packages_follow(monkeypatch):
    step = rust_test(POLYGON, monkeypatch, True)
    assert step.cmd is not None and step.cmd[:3] == ["cargo", "nextest", "run"]
    # polygon: poly-core is a lib, poly-cli a binary — `cargo test --doc` refuses the latter
    assert step.then == ["cargo", "test", "--doc", "--no-fail-fast", "-p", "poly-core"]
    assert step.note == ""


def test_without_nextest_cargo_test_runs_and_the_report_says_why_it_is_slow(
    monkeypatch,
):
    step = rust_test(POLYGON, monkeypatch, False)
    assert step.cmd is not None and step.cmd[:2] == ["cargo", "test"]
    assert step.then is None  # cargo test runs the doctests itself
    assert "cargo-nextest not installed" in step.note and "one at a time" in step.note


def test_bin_only_crates_get_no_doctest_command(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "b"\n', encoding="utf-8")
    assert not check.has_lib(tmp_path)
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "b"\n[lib]\npath = "src/x.rs"\n', encoding="utf-8"
    )
    assert check.has_lib(tmp_path)


def _step(tmp_path, first: str, then: str | None) -> check.Step:
    return check.Step("rust", "test", [PY, "-c", first], ok_codes=(0, 4), cwd=tmp_path,
                      then=[PY, "-c", then] if then else None)  # fmt: skip


def test_a_failing_doctest_fails_the_step_with_both_outputs_in_one_log(tmp_path):
    cfg = config.load(tmp_path)
    doc = "print('---- src/lib.rs - add (line 3) stdout ----'); print('error: doctest failed'); raise SystemExit(101)"
    r = check._run_step(
        cfg, _step(tmp_path, "print('nextest: 3 passed')", doc), tmp_path
    )
    assert r.status == "fail"
    log = (tmp_path / "rust-test.log").read_text(encoding="utf-8")
    assert log.index("nextest: 3 passed") < log.index("doctest failed")
    assert "error: doctest failed" in r.tail


def test_doctests_do_not_run_after_a_failed_nextest(tmp_path):
    cfg = config.load(tmp_path)
    r = check._run_step(
        cfg, _step(tmp_path, "raise SystemExit(100)", "print('DOC RAN')"), tmp_path
    )
    assert r.status == "fail"
    assert "DOC RAN" not in (tmp_path / "rust-test.log").read_text(encoding="utf-8")


def test_nextest_with_no_tests_still_runs_the_doctests(tmp_path):
    # exit 4: nothing selected for nextest — the doctests are still tests to run
    cfg = config.load(tmp_path)
    r = check._run_step(
        cfg, _step(tmp_path, "raise SystemExit(4)", "print('doc ok')"), tmp_path
    )
    assert r.status == "ok"
    assert "doc ok" in (tmp_path / "rust-test.log").read_text(encoding="utf-8")


def test_a_doctest_exit_code_that_nextest_may_use_is_still_a_failure(tmp_path):
    cfg = config.load(tmp_path)
    r = check._run_step(cfg, _step(tmp_path, "pass", "raise SystemExit(4)"), tmp_path)
    assert r.status == "fail"


def test_both_commands_share_one_time_limit(tmp_path):
    cfg = config.load(tmp_path)
    cfg.step_timeout_minutes = 0.05  # 3 s for both
    slow = "import time; time.sleep(300)"
    r = check._run_step(
        cfg, _step(tmp_path, "import time; time.sleep(2)", slow), tmp_path
    )
    assert r.status == "fail" and "timed out after 0.05 min" in r.tail


def test_ci_installs_cargo_nextest():
    text = (REPO / ".github/workflows/check.yml").read_text(encoding="utf-8")
    tools_line = re.search(r"\n          tool: (.+)\n", text)
    assert tools_line and "cargo-nextest" in [
        t.split("@")[0] for t in tools_line.group(1).split(",")
    ]


@pytest.mark.parametrize("installed", [True, False])
def test_doctor_tips_nextest_only_when_missing(monkeypatch, capsys, installed):
    monkeypatch.setattr(
        tools, "usable", lambda t: installed if t == "cargo-nextest" else True
    )
    cli.main(["-C", str(POLYGON), "doctor", "--json"])
    tips = json.loads(capsys.readouterr().out)["tips"]
    assert any("cargo-nextest" in t for t in tips) is not installed

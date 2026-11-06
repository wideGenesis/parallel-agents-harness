"""the rust test row says how much of the step was building the test binaries, how
much running them, and the rest — a pilot project's 1196 s could not tell which."""

import json
import sys

from harness import check, cli, config, phases

PY = sys.executable

# polygon's rust-test.log under h check (nextest 0.9.151, Windows), first command only
NEXTEST = """\
   Compiling poly-core v0.1.0 (D:\\polygon\\crates\\poly-core)
note: did not finalize incremental compilation session directory
  = help: the next build will not be able to reuse work from this compilation

   Compiling poly-cli v0.1.0 (D:\\polygon\\crates\\poly-cli)
    Finished `test` profile [unoptimized + debuginfo] target(s) in 0.55s
────────────
 Nextest run ID 449800ad-bd66-44a6-b4f1-b1a6140b51ac with nextest profile: default
    Starting 1 test across 2 binaries
        PASS [   0.008s] (1/1) poly-core tests::adds
────────────
     Summary [   0.008s] 1 test run: 1 passed, 0 skipped
"""

# `cargo test` without nextest: one binary after another, the doctests in the same command
CARGO_TEST = """\
    Finished `test` profile [unoptimized + debuginfo] target(s) in 1m 05s
     Running unittests src/lib.rs (target/debug/deps/poly_core-1)
test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.50s
     Running tests/api.rs (target/debug/deps/api-2)
test result: ok. 40 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 12.25s
   Doc-tests poly_core
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.25s
"""


def test_nextest_output_splits_into_build_tests_and_rest():
    p = phases.rust_test(NEXTEST, 10.0)
    assert p is not None
    assert (p.build, p.tests) == (0.55, 0.008)
    assert p.rest is not None and abs(p.rest - 9.442) < 1e-9
    assert str(p) == "build 1s, tests 0s, rest 9s"


def test_cargo_minutes_and_the_older_finished_line():
    p = phases.rust_test(
        "    Finished test [unoptimized + debuginfo] target(s) in 75m 03s\n"
        "     Summary [ 312.456s] 2464 tests run: 2464 passed, 0 skipped\n",
        5000.0,
    )
    assert p is not None and p.build == 4503 and p.tests == 312.456
    assert str(p) == "build 4503s, tests 312s, rest 185s"


def test_without_nextest_the_tests_are_the_sum_of_the_binaries():
    p = phases.rust_test(CARGO_TEST, 80.0)
    assert p is not None and p.build == 65 and p.tests == 13.0
    assert p.rest == 2.0


def test_a_build_that_never_reached_the_tests_shows_the_build_only():
    # killed by the step limit or a failed test binary run with nothing summed
    p = phases.rust_test(
        "    Finished `test` profile [unoptimized] target(s) in 13m 40s\n", 1800.0
    )
    assert p is not None and (p.build, p.tests, p.rest) == (820, None, None)
    assert str(p) == "build 820s"


def test_no_known_line_no_split():
    out = "   Compiling app v0.1.0\nerror[E0425]: cannot find value `x`\n"
    assert phases.rust_test(out, 30.0) is None
    assert phases.rust_test("", 0.0) is None


def _step(tmp_path, stack, kind, first, then=None, code=0):
    src = tmp_path / f"{kind}-first.txt"
    src.write_text(first, encoding="utf-8")
    # bytes, as nextest writes them: a Windows console encoding cannot print its rules
    cat = f"import sys; sys.stdout.buffer.write(open(r'{src.as_posix()}', 'rb').read())"
    nxt = None
    if then is not None:
        doc = tmp_path / f"{kind}-then.txt"
        doc.write_text(then, encoding="utf-8")
        nxt = [PY, "-c", cat.replace(src.as_posix(), doc.as_posix())]
    cmd = [PY, "-c", f"{cat}; raise SystemExit({code})"]
    return check.Step(stack, kind, cmd, cwd=tmp_path, then=nxt)


def test_the_step_parses_its_first_command_colours_stripped_doctests_in_the_rest(
    tmp_path,
):
    cfg = config.load(tmp_path)
    coloured = NEXTEST.replace("Finished", "\x1b[1m\x1b[32mFinished\x1b[0m").replace(
        "Summary", "\x1b[1m\x1b[32mSummary\x1b[0m"
    )
    doctests = (
        "    Finished `test` profile [unoptimized + debuginfo] target(s) in 3.00s\n"
        "test result: ok. 1 passed; 0 failed; finished in 2.00s\n"
    )
    r = check._run_step(
        cfg, _step(tmp_path, "rust", "test", coloured, doctests), tmp_path
    )
    assert r.status == "ok" and r.phases is not None
    assert (r.phases.build, r.phases.tests) == (0.55, 0.008)
    # the doctests' own Finished and "finished in" lines are neither build nor tests: the
    # rest (here the fake 0.558 s outlast the real step, so it is cut at zero)
    assert r.phases.rest is not None
    assert abs(r.phases.rest - max(0.0, r.seconds - 0.558)) < 1e-6


def test_other_steps_get_no_split(tmp_path):
    cfg = config.load(tmp_path)
    r = check._run_step(cfg, _step(tmp_path, "rust", "sql", NEXTEST), tmp_path)
    assert r.status == "ok" and r.phases is None


def test_a_failed_rust_test_keeps_the_parts_it_reached(tmp_path):
    cfg = config.load(tmp_path)
    failed = NEXTEST.replace("1 passed", "1 failed")
    r = check._run_step(
        cfg, _step(tmp_path, "rust", "test", failed, code=100), tmp_path
    )
    assert r.status == "fail" and r.phases is not None and r.phases.build == 0.55


def res(stack, kind, started, seconds, split=None, status="ok", waited=0.0):
    r = check.StepResult(
        check.Step(stack, kind, ["x"]), status, seconds, "x.log", "boom", phases=split
    )
    r.started, r.waited = started, waited
    return r


SLOW_RUN = phases.Phases(820.0, 312.0, 64.0)


def test_the_ci_timeline_row_carries_the_split(repo, monkeypatch, capsys):
    monkeypatch.setenv("CI", "true")
    rows = [res("rust", "lint", 11, 133, waited=11), res("rust", "test", 144, 1196, SLOW_RUN),
            res("ts", "test", 1341, 113)]  # fmt: skip
    monkeypatch.setattr(check, "run", lambda cfg, steps, on_wait=None: rows)
    cli.main(["-C", str(repo), "check", "--full"])
    out = capsys.readouterr().out
    assert "    +144s rust test 1196s ok (build 820s, tests 312s, rest 64s)\n" in out
    assert "    +11s rust lint 133s ok (waited 11s)\n" in out
    assert "  rust test: build" not in out  # the timeline row has it in CI


def test_wait_and_split_share_the_brackets(repo, monkeypatch, capsys):
    monkeypatch.setenv("CI", "true")
    rows = [res("rust", "test", 144, 1196, SLOW_RUN, waited=5)]
    monkeypatch.setattr(check, "run", lambda cfg, steps, on_wait=None: rows)
    cli.main(["-C", str(repo), "check", "--full"])
    assert "+144s rust test 1196s ok (waited 5s; build 820s, tests 312s, rest 64s)" in (
        capsys.readouterr().out
    )


def test_locally_the_summary_has_a_line_for_it(repo, monkeypatch, capsys):
    monkeypatch.delenv("CI", raising=False)
    rows = [res("rust", "test", 0, 1196, SLOW_RUN)]
    monkeypatch.setattr(check, "run", lambda cfg, steps, on_wait=None: rows)
    cli.main(["-C", str(repo), "check", "--full"])
    out = capsys.readouterr().out
    assert "  rust test: build 820s, tests 312s, rest 64s\n" in out
    assert "timeline" not in out


def test_json_rows_carry_the_split_as_numbers(repo, monkeypatch, capsys):
    rows = [res("rust", "test", 0, 1196, SLOW_RUN), res("ts", "test", 0, 113)]
    monkeypatch.setattr(check, "run", lambda cfg, steps, on_wait=None: rows)
    cli.main(["-C", str(repo), "check", "--full", "--json"])
    got = {r["stack"]: r for r in json.loads(capsys.readouterr().out)}
    assert (got["rust"]["build_s"], got["rust"]["tests_s"], got["rust"]["rest_s"]) == (
        820.0, 312.0, 64.0
    )  # fmt: skip
    assert (got["ts"]["build_s"], got["ts"]["tests_s"], got["ts"]["rest_s"]) == (
        None, None, None
    )  # fmt: skip


def test_the_fail_line_names_what_was_reached(repo, monkeypatch, capsys):
    monkeypatch.delenv("CI", raising=False)
    built = phases.Phases(820.0, None, None)
    rows = [res("rust", "test", 0, 1800, built, status="fail")]
    monkeypatch.setattr(check, "run", lambda cfg, steps, on_wait=None: rows)
    assert cli.main(["-C", str(repo), "check", "--full"]) == 1
    assert "  FAIL rust test (1800.0s; build 820s) — full log: x.log" in (
        capsys.readouterr().out
    )


def test_without_a_split_the_rows_look_as_before(repo, monkeypatch, capsys):
    monkeypatch.setenv("CI", "true")
    rows = [res("rust", "test", 144, 1196), res("rust", "lint", 0, 9, status="fail")]
    monkeypatch.setattr(check, "run", lambda cfg, steps, on_wait=None: rows)
    cli.main(["-C", str(repo), "check", "--full"])
    out = capsys.readouterr().out
    assert "    +144s rust test 1196s ok\n" in out
    assert "  FAIL rust lint (9.0s) — full log: x.log" in out

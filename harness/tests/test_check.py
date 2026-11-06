import json
import sys

import pytest
from conftest import POLYGON

from harness import affected, check, cli, config, stacks, tools

PY = sys.executable.replace("\\", "/")


def plan_for(proj, full=False, kinds=check.FAST + check.SLOW):
    cfg = config.load(proj)
    found = stacks.detect(cfg)
    impact = None
    if not full:
        files = affected.changed_files(cfg, affected.resolve_base(cfg, None))
        impact = affected.compute(cfg, found, files)
    return cfg, check.plan(cfg, found, impact, kinds)


def touch(proj, rel, text):
    p = proj / rel
    p.write_text(p.read_text(encoding="utf-8") + text, encoding="utf-8")


def test_plan_covers_only_affected_stack_and_packages(repo):
    touch(repo, "py/poly-lib/src/poly_lib/__init__.py", "# x\n")
    _, steps = plan_for(repo)
    assert {s.stack for s in steps} == {"python", "tether", "case-collisions"}
    lint = next(s for s in steps if s.kind == "lint")
    assert lint.cmd is not None
    assert lint.cmd[-2:] == ["py/poly-app", "py/poly-lib"]


def test_plan_full_uses_workspace_flags():
    _, steps = plan_for(POLYGON, full=True)
    rust = {s.kind: s for s in steps if s.stack == "rust"}
    assert rust["fmt"].cmd == ["cargo", "fmt", "--check", "--all"]
    assert "--workspace" in (rust["lint"].cmd or [])
    assert rust["typecheck"].cmd is None  # clippy already type-checks
    ts = next(s for s in steps if s.stack == "ts" and s.kind == "fmt")
    assert ts.cmd is None or ts.cmd[-1] == "format:check"


def test_plan_scopes_rust_by_package(repo):
    touch(repo, "crates/poly-cli/src/main.rs", "// x\n")
    _, steps = plan_for(repo, kinds=("lint",))
    [lint] = [s for s in steps if s.stack == "rust"]
    assert lint.cmd is not None
    assert lint.cmd[:4] == ["cargo", "clippy", "-p", "poly-cli"]


@pytest.mark.parametrize("nextest", [False, True])
def test_rust_tests_run_every_binary_past_a_failure(repo, monkeypatch, nextest):
    # without --no-fail-fast the first failing test binary hid all the others.
    monkeypatch.setattr(
        tools, "usable", lambda t: nextest if t == "cargo-nextest" else True
    )
    _, steps = plan_for(repo, full=True, kinds=("test",))
    rust = next(s for s in steps if s.stack == "rust")
    assert rust.cmd is not None
    assert rust.cmd[:3] == (
        ["cargo", "nextest", "run"] if nextest else ["cargo", "test", "--no-fail-fast"]
    )
    assert "--no-fail-fast" in rust.cmd
    assert rust.ok_codes == ((0, 4) if nextest else (0,))


@pytest.mark.parametrize("installed", [True, False])
def test_rust_deps_runs_cargo_deny_where_deny_toml_is(repo, monkeypatch, installed):
    """polygon has deny.toml, so the plan has `rust deps`; without cargo-deny the
    step is red and says how to install it, not cargo's "no such command"."""
    monkeypatch.setattr(
        tools, "usable", lambda t: installed if t == "cargo-deny" else True
    )
    _, steps = plan_for(repo, full=True, kinds=("deps",))
    [deps] = [s for s in steps if s.kind == "deps"]
    assert deps.stack == "rust"
    if installed:
        assert deps.cmd == ["cargo", "deny", "check"]
    else:
        result = check._run(config.load(repo), deps)
        assert result.status == "fail"
        assert "cargo install --locked cargo-deny" in result.tail


def test_no_deny_toml_no_deps_step(repo):
    """without a policy there is nothing to check and nothing to show as skipped."""
    (repo / "deny.toml").unlink()
    _, steps = plan_for(repo, full=True)
    assert not [s for s in steps if s.kind == "deps"]


def test_deps_step_can_be_skipped(repo):
    (repo / "harness.toml").write_text(
        '[stacks.rust]\nskip = ["deps"]\n', encoding="utf-8"
    )
    _, steps = plan_for(repo, full=True, kinds=("deps",))
    [deps] = [s for s in steps if s.kind == "deps"]
    assert deps.disabled and deps.cmd is None


def test_override_expands_names_and_paths(repo):
    (repo / "harness.toml").write_text(
        '[stacks.rust.commands]\ntest = "my-runner --pkgs {names} --dirs {paths}"\n',
        encoding="utf-8",
    )
    _, steps = plan_for(repo, full=True, kinds=("test",))
    rust = next(s for s in steps if s.stack == "rust")
    assert rust.cmd == [
        "my-runner",
        "--pkgs",
        "poly-cli",
        "poly-core",
        "--dirs",
        "crates/poly-cli",
        "crates/poly-core",
    ]


def test_run_reports_short_tail_without_ansi_or_root(repo):
    cfg = config.load(repo)
    noisy = (
        "import sys; root = sys.argv[1]; "
        "print('\\x1b[1m' + root + '/src/x.rs:3: boom\\x1b[0m'); sys.exit(3)"
    )
    steps = [
        check.Step("rust", "lint", [PY, "-c", noisy, str(cfg.root)]),
        check.Step(
            "rust", "test", [PY, "-c", "import sys; sys.exit(5)"], ok_codes=(0, 5)
        ),
        check.Step("python", "typecheck", None, "not installed"),
    ]
    by_kind = {r.step.kind: r for r in check.run(cfg, steps)}
    assert by_kind["lint"].status == "fail"
    assert by_kind["lint"].tail == "src/x.rs:3: boom"
    assert by_kind["test"].status == "ok"
    assert by_kind["typecheck"].status == "skip"
    log = (repo / by_kind["lint"].log).read_text(encoding="utf-8")
    assert "\x1b" not in log and log.startswith("$ ")


def test_cli_check_nothing_affected(repo, capsys):
    assert cli.main(["-C", str(repo), "check", "--only", "fmt,test"]) == 0
    assert "nothing affected" in capsys.readouterr().out


def test_cli_rejects_unknown_kind(repo, capsys):
    assert cli.main(["-C", str(repo), "check", "--only", "bench"]) == 2


@pytest.mark.skipif(not tools.usable("ruff"), reason="ruff not usable")
def test_cli_check_catches_python_format_error(repo, capsys):
    touch(repo, "py/poly-lib/src/poly_lib/__init__.py", "x=1\n")
    assert cli.main(["-C", str(repo), "check", "--only", "fmt,lint"]) == 1
    out = capsys.readouterr().out
    assert "FAIL python fmt" in out
    assert "py/poly-lib/src/poly_lib/__init__.py" in out.replace("\\", "/")


def test_cache_env_adds_sccache_for_rust_only(monkeypatch):
    monkeypatch.delenv("RUSTC_WRAPPER", raising=False)
    monkeypatch.setattr(tools, "usable", lambda t: t == "sccache")
    assert check.cache_env("rust") == {"RUSTC_WRAPPER": "sccache"}
    assert check.cache_env("python") == {}


def test_cache_env_respects_explicit_wrapper(monkeypatch):
    monkeypatch.setenv("RUSTC_WRAPPER", "")  # user opted out
    monkeypatch.setattr(tools, "usable", lambda t: True)
    assert check.cache_env("rust") == {}


def test_cache_env_without_sccache(monkeypatch):
    monkeypatch.delenv("RUSTC_WRAPPER", raising=False)
    monkeypatch.setattr(tools, "usable", lambda t: False)
    assert check.cache_env("rust") == {}


def test_pinned_ruff_runs_through_uv(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[dependency-groups]\ndev = ["pytest>=8", "ruff==0.15.12"]\n', encoding="utf-8"
    )
    tools.pinned_in.cache_clear()
    cmd = tools.ruff(tmp_path)
    assert cmd is not None and cmd[:2] == ["uv", "run"] and cmd[-1] == "ruff"
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n', encoding="utf-8"
    )
    tools.pinned_in.cache_clear()
    assert tools.ruff(tmp_path) != cmd


def test_typecheck_finds_pyright_in_project_venv(repo, monkeypatch):
    # pyright only in the project's venv, none on PATH.
    monkeypatch.setattr(tools, "usable", lambda t: t == "uv")
    _, steps = plan_for(repo, full=True, kinds=("typecheck",))
    [tc] = [s for s in steps if s.stack == "python"]
    assert tc.cmd is None
    exe = "Scripts/pyright.exe" if sys.platform == "win32" else "bin/pyright"
    (repo / ".venv" / exe).parent.mkdir(parents=True, exist_ok=True)
    (repo / ".venv" / exe).write_text("", encoding="utf-8")
    _, steps = plan_for(repo, full=True, kinds=("typecheck",))
    [tc] = [s for s in steps if s.stack == "python"]
    assert tc.cmd is not None
    assert tc.cmd[:2] == ["uv", "run"] and tc.cmd[-3:] == [
        "pyright",
        "py/poly-app",
        "py/poly-lib",
    ]


def test_ts_runs_only_packages_with_the_script(repo, monkeypatch):
    # --if-present reported a package without the script as ok.
    monkeypatch.setattr(tools, "usable", lambda t: True)
    _, steps = plan_for(repo, full=True)
    ts = {s.kind: s for s in steps if s.stack == "ts"}
    assert ts["test"].cmd == ["pnpm", "--filter", "@poly/lib", "run", "test"]
    assert ts["lint"].cmd is None
    assert (
        "no `lint` script in @poly/app, @poly/lib or the root package.json"
        in ts["lint"].reason
    )
    results = check.run(config.load(repo), [ts["lint"]])
    assert results[0].status == "skip" and results[0].blocks_ship


def test_ts_runs_root_scripts(repo, monkeypatch):
    # `pnpm -r` skips the root package.json, where biome/eslint usually live.
    monkeypatch.setattr(tools, "usable", lambda t: True)
    (repo / "package.json").write_text(
        '{"name": "polygon", "scripts": {"lint": "biome lint .", "test": "x"}}',
        encoding="utf-8",
    )
    _, steps = plan_for(repo, full=True)
    ts = {s.kind: s for s in steps if s.stack == "ts"}
    assert ts["lint"].cmd == ["pnpm", "--workspace-root", "run", "lint"]
    assert ts["test"].cmd == [
        "pnpm", "--filter", "@poly/lib", "--workspace-root", "run", "test"
    ]  # fmt: skip


def test_ts_root_script_that_walks_the_packages_runs_them_once(repo, monkeypatch):
    # a root `pnpm -r run test` plus --filter ran each package twice, concurrently.
    monkeypatch.setattr(tools, "usable", lambda t: True)
    (repo / "package.json").write_text(
        '{"name": "polygon", "scripts": {'
        '"test": "pnpm -r --if-present run test", '
        '"lint": "biome lint . && pnpm --recursive run lint", '
        '"typecheck": "pnpm -r run typecheck"}}',
        encoding="utf-8",
    )
    _, steps = plan_for(repo, full=True)
    ts = {s.kind: s for s in steps if s.stack == "ts"}
    assert ts["test"].cmd == ["pnpm", "--filter", "@poly/lib", "run", "test"]
    assert ts["lint"].cmd == ["pnpm", "--workspace-root", "run", "lint"]
    assert ts["typecheck"].cmd is None
    assert "its script only runs the packages'" in ts["typecheck"].reason


def test_plan_lists_steps_and_runs_nothing(repo, monkeypatch, capsys):
    # CI starts a database only when a test step will run.
    def no_run(*a, **k):
        raise AssertionError("--plan ran a step")

    monkeypatch.setattr(check, "run", no_run)
    monkeypatch.setattr(tools, "usable", lambda t: t != "pnpm")
    assert cli.main(["-C", str(repo), "check", "--plan", "--json", "--full"]) == 0
    planned = json.loads(capsys.readouterr().out)
    by = {(p["stack"], p["kind"]): p["runs"] for p in planned}
    assert by[("python", "test")] is True
    assert by[("ts", "test")] is False  # no pnpm: skipped, so no database for it

    assert cli.main(["-C", str(repo), "check", "--plan", "--only", "fmt,lint"]) == 0
    assert " test:" not in capsys.readouterr().out


def test_ts_root_recursion_kinds():
    assert check._recursion("biome lint .") == "none"
    assert check._recursion("pnpm -r run test") == "only"
    assert check._recursion("pnpm run -r test") == "only"
    assert check._recursion("pnpm --filter=./apps/* test; pnpm -r lint") == "only"
    assert check._recursion("tsc -b && pnpm -r run test") == "mixed"
    assert check._recursion("pnpm run build:r") == "none"


def test_dotenv_feeds_steps_and_process_env_wins(repo, monkeypatch):
    (repo / ".env").write_text(
        '# comment\nexport DB="postgres://x" # db\nKEEP=from-file\nNOTE=a # trailing\n',
        encoding="utf-8",
    )
    (repo / ".env.harness").write_text("PORT_API=5555\n", encoding="utf-8")
    monkeypatch.setenv("KEEP", "from-process")
    env = check.dotenv(repo)
    assert env == {
        "DB": "postgres://x",
        "KEEP": "from-file",
        "NOTE": "a",
        "PORT_API": "5555",
    }
    cfg = config.load(repo)
    probe = "import os, sys; sys.exit(0 if os.environ['DB'] == 'postgres://x' and os.environ['KEEP'] == 'from-process' else 7)"
    [r] = check.run(cfg, [check.Step("python", "test", [PY, "-c", probe])])
    assert r.status == "ok"


def test_tail_shows_the_failure_not_the_build_noise(repo):
    cfg = config.load(repo)
    script = (
        "import sys\n"
        "print('test api::x ... FAILED')\n"
        "print(\"thread 'x' panicked at src/x.rs:9:5\")\n"
        "for i in range(80): print(f'   Compiling crate{i} v1.0', file=sys.stderr)\n"
        "print('test result: FAILED. 1 passed; 1 failed')\n"
        "sys.exit(101)\n"
    )
    [r] = check.run(cfg, [check.Step("rust", "test", [PY, "-c", script])])
    assert "panicked at src/x.rs:9:5" in r.tail
    assert "test result: FAILED" in r.tail
    assert r.tail.count("Compiling") <= 3
    assert len(r.tail.splitlines()) <= check.TAIL


def test_tail_ignores_test_names_that_mention_errors():
    lines = [f"test http::error::tests::case_{i} ... ok" for i in range(40)]
    lines += ["test queue::a_failed_rebuild_waits ... ok", "test x::y ... FAILED",
              "thread 'x::y' panicked at src/y.rs:3:1:", "boom",
              "test result: FAILED. 40 passed; 1 failed"]  # fmt: skip
    tail = check.focus_tail(lines)
    assert not any(ln.endswith("... ok") for ln in tail)
    assert "test x::y ... FAILED" in tail and "boom" in tail


@pytest.mark.parametrize("ci", [False, True])
def test_check_env_beats_dotenv_and_process(repo, monkeypatch, ci):
    # in CI the workflow's own DATABASE_URL (its database, its port) wins.
    (repo / ".env").write_text(
        "DATABASE_URL=postgres://real-server/db\n", encoding="utf-8"
    )
    monkeypatch.setenv("DATABASE_URL", "postgres://exported/db")
    if ci:
        monkeypatch.setenv("CI", "true")
    else:
        monkeypatch.delenv("CI", raising=False)
    # Gate rules come from the base branch: commit the rule on main.
    from conftest import git

    git(repo, "switch", "-q", "main")
    (repo / "harness.toml").write_text(
        '[check.env]\nDATABASE_URL = "postgres://localhost:15432/test"\n',
        encoding="utf-8",
    )
    git(repo, "commit", "-qam", "check env")
    git(repo, "switch", "-q", "work")
    cfg = config.load(repo)
    want = "postgres://exported/db" if ci else "postgres://localhost:15432/test"
    assert check.step_env(cfg, "rust")["DATABASE_URL"] == want
    probe = (
        f"import os, sys; sys.exit(0 if os.environ['DATABASE_URL'] == {want!r} else 9)"
    )
    [r] = check.run(cfg, [check.Step("rust", "test", [PY, "-c", probe])])
    assert r.status == "ok"
    # Without its own variable CI still gets [check.env]: the rule yields, never vanishes.
    monkeypatch.delenv("DATABASE_URL")
    assert check.step_env(cfg, "rust")["DATABASE_URL"].endswith(":15432/test")


def test_parallel_runs_keep_their_own_logs(repo):
    """The edit hook and ship can run h check at once in one checkout."""
    from concurrent.futures import ThreadPoolExecutor

    cfg = config.load(repo)

    def failing(tag):
        code = f"import sys, time; time.sleep(0.3); print('error: {tag}'); sys.exit(1)"
        return check.run(cfg, [check.Step("python", "lint", [PY, "-c", code])])[0]

    with ThreadPoolExecutor(2) as pool:
        a, b = pool.map(failing, ["run-a", "run-b"])
    assert a.log != b.log
    assert "run-a" in a.tail and "run-a" in (repo / a.log).read_text(encoding="utf-8")
    assert "run-b" in b.tail and "run-b" in (repo / b.log).read_text(encoding="utf-8")


def test_old_runs_are_rotated_and_latest_names_the_newest(repo):
    cfg = config.load(repo)
    step = check.Step("python", "lint", [PY, "-c", "pass"])
    results = [check.run(cfg, [step])[0] for _ in range(check.KEEP_RUNS + 3)]
    root = repo / check.ARTIFACTS
    assert len([d for d in root.iterdir() if d.is_dir()]) == check.KEEP_RUNS
    latest = (root / "latest").read_text(encoding="utf-8").strip()
    assert results[-1].log.startswith(latest + "/") and (repo / latest).is_dir()


def test_check_runs_fmt_never_fmt_write(repo, capsys):
    """h check is a check — the writing command is the hook's, not check's."""
    toml = repo / "harness.toml"
    text = toml.read_text(encoding="utf-8")
    toml.write_text(text.replace("[stacks.python]\n", "[stacks.python]\n[stacks.python.commands]\n"
                                 "fmt = \"fmt-check {names}\"\nfmt_write = \"fmt-write {paths}\"\n"),
                    encoding="utf-8")  # fmt: skip
    _, steps = plan_for(repo, full=True, kinds=["fmt"])
    [py] = [s for s in steps if s.stack == "python"]
    assert py.cmd is not None and py.cmd[0] == "fmt-check" and "poly-lib" in py.cmd
    cli.main(["-C", str(repo), "doctor"])
    assert "set `fmt_write`" not in capsys.readouterr().out  # both set: no tip
    toml.write_text(text.replace("[stacks.python]\n", "[stacks.python]\n[stacks.python.commands]\n"
                                 "fmt = \"fmt-check\"\n"), encoding="utf-8")  # fmt: skip
    cli.main(["-C", str(repo), "doctor"])
    assert "stacks.python.commands: `fmt` is the check" in capsys.readouterr().out


def test_fmt_write_placeholders(repo, monkeypatch):
    from harness import fmt

    toml = repo / "harness.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace(
        "[stacks.python]\n", "[stacks.python]\n[stacks.python.commands]\n"
        "fmt_write = \"w --pkg {names} --file {file}\"\n"), encoding="utf-8")  # fmt: skip
    f = repo / "py" / "poly-lib" / "src" / "poly_lib" / "__init__.py"
    cmds = fmt._commands(config.load(repo), "python", f)
    assert cmds is not None
    [cmd] = cmds
    assert cmd == ["w", "--pkg", "poly-lib", "--file", str(f)]


def test_skip_must_name_check_steps(tmp_path):
    (tmp_path / "harness.toml").write_text('[stacks.python]\nskip = ["tpyecheck"]\n',
                                           encoding="utf-8")  # fmt: skip
    with pytest.raises(config.ConfigError, match="skip must list steps"):
        config.load(tmp_path)


def _skips(monkeypatch):
    results = [
        check.StepResult(check.Step("python", "lint", ["ruff"]), "ok"),
        check.StepResult(check.Step("python", "typecheck", None, "pyright not installed"),
                         "skip"),
        check.StepResult(check.Step("ts", "test", None, check.DISABLED, disabled=True),
                         "skip"),
        check.StepResult(check.Step("rust", "typecheck", None, "in clippy", covered=True),
                         "skip"),
    ]  # fmt: skip
    monkeypatch.setattr(check, "run", lambda *a: results)


def test_check_tells_which_skips_block_ship(repo, monkeypatch, capsys):
    """the blocker shows up in h check, not first in h ship."""
    _skips(monkeypatch)
    assert cli.main(["-C", str(repo), "check", "--full"]) == 0  # skips never fail check
    out = capsys.readouterr().out
    assert "1 ok, 0 failed, 2 skipped, 1 disabled" in out
    assert ("skip python typecheck: pyright not installed — blocks h ship; make it "
            'run, or on purpose: [stacks.python] skip = ["typecheck"]') in out  # fmt: skip
    assert "  disabled ts test (harness.toml)" in out
    assert "  skip rust typecheck: in clippy\n" in out


def test_check_json_marks_ship_blockers(repo, monkeypatch, capsys):
    _skips(monkeypatch)
    cli.main(["-C", str(repo), "check", "--full", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert [r["blocks_ship"] for r in rows] == [False, True, False, False]


def test_check_without_skips_prints_no_disabled_count(repo, monkeypatch, capsys):
    monkeypatch.setattr(check, "run", lambda *a: [
        check.StepResult(check.Step("python", "lint", ["ruff"]), "ok")])  # fmt: skip
    cli.main(["-C", str(repo), "check", "--full"])
    assert "check: 1 ok, 0 failed, 0 skipped (" in capsys.readouterr().out

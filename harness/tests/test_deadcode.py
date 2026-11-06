"""unused dependencies block, dead code reports, false positives go to a whitelist."""

import json
import subprocess
import sys

import pytest
from conftest import git
from test_crash_summary import NODE_CRASH

from harness import check, cli, config, deadcode, proc, ratchet, stacks, tools

MACHETE = """Analyzing dependencies of crates in this directory...
cargo-machete found the following unused dependencies in this directory:
poly-cli -- .\\crates\\poly-cli\\Cargo.toml:
\tserde
\tanyhow

Done!
"""


def plan_deadcode(repo, stack):
    cfg = config.load(repo)
    found = stacks.detect(cfg)
    steps = check.plan(cfg, found, None, ("deadcode",))
    return cfg, next(s for s in steps if s.stack == stack)


def call(step):
    assert step.call is not None
    return step.call()


def fake_run(monkeypatch, outputs):
    """proc.run answering by the tool in argv; records every call."""
    calls = []

    def run(argv, cwd, **kw):
        calls.append((list(argv), cwd))
        for key, (code, out) in outputs.items():
            if any(key in str(a) for a in argv):
                if callable(out):
                    out = out(argv)
                return subprocess.CompletedProcess(argv, code, out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def run_logged(argv, cwd, log, header="", env=None, timeout_s=None, stop=None):
        r = run(argv, cwd)
        log.write_text(header + str(r.stdout), encoding="utf-8")
        return r.returncode, str(r.stdout)

    monkeypatch.setattr(proc, "run", run)
    monkeypatch.setattr(proc, "run_logged", run_logged)
    return calls


def test_deadcode_is_a_fast_check_step():
    assert "deadcode" in check.FAST
    assert "deadcode" in config.CHECK_KINDS and "deadcode" in config.COMMAND_KINDS


def test_rust_unused_dependency_fails_with_manifest_and_line(repo, monkeypatch):
    (repo / "crates/poly-cli/Cargo.toml").write_text(
        (repo / "crates/poly-cli/Cargo.toml").read_text(encoding="utf-8")
        + 'serde = "1"\nanyhow = "1"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(tools, "usable", lambda t: t == "cargo-machete")
    fake_run(monkeypatch, {"machete": (1, MACHETE)})
    _, step = plan_deadcode(repo, "rust")
    assert step.call is not None
    ok, log, warnings = call(step)
    assert not ok and not warnings
    assert "error: crates/poly-cli/Cargo.toml:9: unused dependency `serde`" in log
    assert "error: crates/poly-cli/Cargo.toml:10: unused dependency `anyhow`" in log


def test_rust_scopes_machete_to_affected_crates(repo, monkeypatch):
    monkeypatch.setattr(tools, "usable", lambda t: t == "cargo-machete")
    calls = fake_run(monkeypatch, {"machete": (0, "Done!\n")})
    cfg = config.load(repo)
    found = stacks.detect(cfg)
    st = next(s for s in found if s.name == "rust")
    pkg = [p for p in st.packages if p.name == "poly-cli"]
    ok, _, _ = deadcode.run(cfg, st, pkg, False, {})
    assert ok
    assert calls[0][0] == ["cargo", "machete", "crates/poly-cli"]


def test_missing_tool_is_red_with_the_install_command(repo, monkeypatch):
    monkeypatch.setattr(tools, "usable", lambda t: False)
    _, step = plan_deadcode(repo, "rust")
    ok, log, _ = call(step)
    assert not ok
    assert "cargo install cargo-machete" in log


def test_ts_without_knip_is_red(repo):
    _, step = plan_deadcode(repo, "ts")  # node_modules is not copied
    ok, log, _ = call(step)
    assert not ok and "pnpm install" in log  # polygon pins knip


def test_python_without_pinned_tools_is_red(repo, monkeypatch):
    p = repo / "pyproject.toml"
    p.write_text(p.read_text(encoding="utf-8").replace('"deptry>=0.25.1",', ""),
                 encoding="utf-8")  # fmt: skip
    tools.pinned_in.cache_clear()
    monkeypatch.setattr(tools, "usable", lambda t: t == "uv")
    _, step = plan_deadcode(repo, "python")
    ok, log, _ = call(step)
    assert not ok and "uv add --dev deptry vulture" in log


def knip_out(**issue):
    return json.dumps({"issues": [{"file": "ts/app/package.json", **issue}]})


def ts_setup(repo, monkeypatch, out, code=1):
    bin_ = repo / "node_modules/.bin"
    bin_.mkdir(parents=True)
    (bin_ / "knip").write_text("", encoding="utf-8")
    return fake_run(monkeypatch, {"knip": (code, out)})


def test_ts_dependencies_block_and_exports_report(repo, monkeypatch):
    p = repo / "ts/app/package.json"
    p.write_text(p.read_text(encoding="utf-8").replace(
        '"@poly/lib": "workspace:*"', '"@poly/lib": "workspace:*", "left-pad": "1"'),
        encoding="utf-8")  # fmt: skip
    out = knip_out(dependencies=[{"name": "left-pad"}], devDependencies=["is-odd"])
    out = json.dumps({"issues": [
        *json.loads(out)["issues"],
        {"file": "ts/lib/src/index.ts", "exports": [{"name": "unused", "line": 3}]},
    ]})  # fmt: skip
    ts_setup(repo, monkeypatch, out)
    _, step = plan_deadcode(repo, "ts")
    ok, log, warnings = call(step)
    assert not ok
    assert "error: ts/app/package.json:5: unused dependencies: `left-pad`" in log
    assert "unused devDependencies: `is-odd`" in log  # old knip: plain strings
    assert warnings == ["ts/lib/src/index.ts:3: unused exports: `unused`"]


def test_ts_knip_crash_is_red_with_its_output(repo, monkeypatch):
    ts_setup(repo, monkeypatch, "ERROR: bad config", code=2)
    _, step = plan_deadcode(repo, "ts")
    ok, log, _ = call(step)
    assert not ok and "deadcode tool failed" in log and "bad config" in log


# a crash says how the tool ended; here because conftest stubs the
# deadcode adapters in every other module.
def test_knip_crash_says_its_exit_code(repo, monkeypatch):
    ts_setup(repo, monkeypatch, "\n".join(NODE_CRASH), code=134)
    _, step = plan_deadcode(repo, "ts")
    ok, log, _ = call(step)
    assert not ok
    first = log.splitlines()[1]  # after "error: ts deadcode tool failed"
    assert "exit 134" in first


def test_a_tool_killed_by_a_signal_says_which(repo, monkeypatch):
    # -9: how subprocess reports a POSIX kill; the fake returns it on any OS
    ts_setup(repo, monkeypatch, "Killed", code=-9)
    _, step = plan_deadcode(repo, "ts")
    _, log, _ = call(step)
    assert "killed by SIGKILL" in log


def test_the_crash_message_reaches_the_step_summary(repo, monkeypatch):
    ts_setup(repo, monkeypatch, "\n".join(NODE_CRASH), code=1)
    cfg = config.load(repo)
    _, step = plan_deadcode(repo, "ts")
    [r] = check.run(cfg, [step])
    assert r.status == "fail"
    assert "TypeError: Cannot read properties of undefined" in r.tail
    assert "exit 1" in r.tail


VULTURE_OUT = (
    "py/poly-app/src/poly_app/__init__.py:4: unused function 'helper' (60% confidence)\n"
    "py/poly-lib/src/poly_lib/__init__.py:9: unused import 'os' (90% confidence)\n"
)


def py_setup(repo, monkeypatch, deptry=(), modules=None):
    monkeypatch.setattr(tools, "usable", lambda t: t == "uv")

    def deptry_run(argv):
        report = argv[argv.index("--json-output") + 1]
        with open(report, "w", encoding="utf-8") as f:
            json.dump(list(deptry), f)
        return ""

    dists = json.dumps(modules or {"pytest": ["pytest"], "_pytest": ["pytest"]})
    return fake_run(monkeypatch, {
        "deptry": (1 if deptry else 0, deptry_run),
        "vulture": (3, VULTURE_OUT),
        "packages_distributions": (0, dists + "\n"),
    })  # fmt: skip


def test_python_deps_block_dead_code_reports(repo, monkeypatch):
    issue = {"error": {"code": "DEP002", "message": "'tomli' defined as a dependency "
             "but not used in the codebase"}, "module": "tomli",
             "location": {"file": "pyproject.toml", "line": None}}  # fmt: skip
    missing = {**issue, "error": {"code": "DEP001", "message": "x"}, "module": "x"}
    p = repo / "py/poly-app/pyproject.toml"
    p.write_text(p.read_text(encoding="utf-8").replace('["poly-lib"]', '["poly-lib", "tomli"]'),
                 encoding="utf-8")  # fmt: skip
    py_setup(repo, monkeypatch, deptry=[issue, missing])
    _, step = plan_deadcode(repo, "python")
    ok, log, warnings = call(step)
    assert not ok
    assert "error: py/poly-app/pyproject.toml:5: DEP002 'tomli'" in log
    assert (
        "DEP001" not in log.split("findings")[-1]
    )  # undeclared imports: not this check
    # B2: dead code is visible with file:line, but does not fail the step
    assert "py/poly-lib/src/poly_lib/__init__.py:9: unused import 'os'" in warnings


def test_python_unused_dev_dependency_blocks(repo, monkeypatch):
    p = repo / "pyproject.toml"
    p.write_text(p.read_text(encoding="utf-8").replace('"vulture>=2.16",',
                 '"vulture>=2.16",\n    "requests",'), encoding="utf-8")  # fmt: skip
    py_setup(repo, monkeypatch)
    _, step = plan_deadcode(repo, "python")
    ok, log, _ = call(step)
    assert not ok
    assert "error: pyproject.toml:9: unused dev dependency `requests`" in log
    # tools h runs itself (pytest, deptry, vulture) are not dead
    assert "`pytest`" not in log and "`deptry`" not in log


def test_dev_dependency_imported_by_tests_is_used(repo, monkeypatch):
    p = repo / "pyproject.toml"
    p.write_text(p.read_text(encoding="utf-8").replace('"vulture>=2.16",',
                 '"vulture>=2.16",\n    "hypothesis",'), encoding="utf-8")  # fmt: skip
    (repo / "py/poly-lib/tests/test_h.py").write_text(
        "from hypothesis import given\n", encoding="utf-8"
    )
    py_setup(repo, monkeypatch)
    _, step = plan_deadcode(repo, "python")
    _, log, _ = call(step)
    assert "hypothesis" not in log.split("findings")[-1]


def test_whitelist_silences_a_finding_and_needs_a_reason(repo, monkeypatch):
    monkeypatch.setattr(tools, "usable", lambda t: t == "cargo-machete")
    fake_run(monkeypatch, {"machete": (1, MACHETE)})
    wl = repo / ".harness/deadcode/rust.txt"
    wl.parent.mkdir(parents=True)
    wl.write_text("# false positives\nserde  # used by a derive macro\n"
                  "crates/poly-cli/Cargo.toml:anyhow  # build script\n", encoding="utf-8")  # fmt: skip
    _, step = plan_deadcode(repo, "rust")
    ok, log, _ = call(step)
    assert ok, log
    assert "2 whitelisted" in log
    wl.write_text("serde\nanyhow  # x\n", encoding="utf-8")
    ok, log, _ = call(step)
    assert not ok
    assert "error: .harness/deadcode/rust.txt:1: `serde` needs a reason" in log


def test_whitelist_path_from_harness_toml(repo, monkeypatch):
    toml = repo / "harness.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace(
        "[stacks.rust]", '[stacks.rust]\ndeadcode_whitelist = "ci/machete.txt"'),
        encoding="utf-8")  # fmt: skip
    cfg = config.load(repo)
    assert deadcode.whitelist_path(cfg, "rust") == repo / "ci/machete.txt"
    assert deadcode.whitelist_path(cfg, "ts") == repo / ".harness/deadcode/ts.txt"


def test_modes_switch_per_kind_and_come_from_the_base(repo, monkeypatch):
    toml = repo / "harness.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + '\n[check.deadcode]\ncode = "block"\n'
                    'deps = "report"\n', encoding="utf-8")  # fmt: skip
    # A gate rule: the task branch cannot relax it for itself.
    cfg = config.load(repo)
    assert cfg.deadcode == {"deps": "block", "code": "report"} and cfg.rules_changed
    git(repo, "commit", "-qam", "modes")
    git(repo, "branch", "-qf", "main", "HEAD")
    cfg = config.load(repo)
    assert cfg.deadcode == {"deps": "report", "code": "block"}
    monkeypatch.setattr(tools, "usable", lambda t: t == "cargo-machete")
    fake_run(monkeypatch, {"machete": (1, MACHETE)})
    st = next(s for s in stacks.detect(cfg) if s.name == "rust")
    ok, _, warnings = deadcode.run(cfg, st, st.packages, True, {})
    assert ok and len(warnings) == 2  # deps only report now


def test_bad_mode_is_a_config_error(repo):
    toml = repo / "harness.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + '\n[check.deadcode]\ndeps = "warn"\n',
                    encoding="utf-8")  # fmt: skip
    with pytest.raises(config.ConfigError, match="check.deadcode.deps"):
        config.load(repo)


def test_override_and_skip(repo):
    toml = repo / "harness.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace(
        "[stacks.rust]", '[stacks.rust]\nskip = ["deadcode"]').replace(
        "[stacks.ts]", '[stacks.ts]\n[stacks.ts.commands]\ndeadcode = "pnpm knip"'),
        encoding="utf-8")  # fmt: skip
    cfg = config.load(repo)
    steps = check.plan(cfg, stacks.detect(cfg), None, ("deadcode",))
    by = {s.stack: s for s in steps}
    assert by["rust"].disabled
    assert by["ts"].cmd == ["pnpm", "knip"] and by["ts"].call is None
    assert by["python"].call is not None


def test_ratchet_counts_whitelist_entries(repo):
    wl = repo / ".harness/deadcode/python.txt"
    wl.parent.mkdir(parents=True)
    wl.write_text("# header\npytest-xdist  # plugin\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "whitelist")
    git(
        repo, "branch", "-qf", "main", "HEAD"
    )  # on main: the new line is the only new one
    cfg = config.load(repo)
    ratchet.write_base(cfg, ratchet.count(ratchet.scan(cfg)))
    base = ratchet.load_base(cfg)
    assert base is not None and base["python:suppress:deadcode-whitelist"] == 1
    wl.write_text(wl.read_text(encoding="utf-8") + "helper  # called from C\n",
                  encoding="utf-8")  # fmt: skip
    res = ratchet.check(cfg)
    assert res is not None
    assert res.grown == {"python:suppress:deadcode-whitelist": (1, 2)}
    assert [h.text for h in res.new["python:suppress:deadcode-whitelist"]] == ["helper"]


def test_doctor_names_the_missing_deadcode_tool(repo, monkeypatch, capsys):
    monkeypatch.setattr(tools, "usable", lambda t: t != "cargo-machete")
    cli.main(["-C", str(repo), "doctor"])
    out = capsys.readouterr().out
    assert "rust deadcode: cargo-machete not installed" in out
    assert "ts deadcode: knip is pinned but not installed" in out


# --- T4: the real tools on polygon ----------------------------------------------------
# No skip when a tool is missing: like the step itself (B4), a missing tool is a failure.
# CI's harness job installs cargo-machete and pnpm; locally `h doctor` names what is missing.


def real_check(repo, stack):
    cfg = config.load(repo)
    st = next(s for s in stacks.detect(cfg) if s.name == stack)
    return deadcode.run(cfg, st, st.packages, True, check.step_env(cfg, stack))


def test_real_rust_unused_dependency(repo):
    assert real_check(repo, "rust")[0]
    m = repo / "crates/poly-cli/Cargo.toml"
    m.write_text(m.read_text(encoding="utf-8") + 'serde = "1"\n', encoding="utf-8")
    ok, log, _ = real_check(repo, "rust")
    assert not ok and "crates/poly-cli/Cargo.toml:9: unused dependency `serde`" in log


def test_real_python_unused_dependency(repo):
    assert real_check(repo, "python")[0]
    m = repo / "py/poly-app/pyproject.toml"
    m.write_text(m.read_text(encoding="utf-8").replace('["poly-lib"]', '["poly-lib", "tomli"]'),
                 encoding="utf-8")  # fmt: skip
    ok, log, _ = real_check(repo, "python")
    assert not ok and "py/poly-app/pyproject.toml:5: DEP002 'tomli'" in log


def test_real_ts_unused_dependency(repo):
    install = ["pnpm", "install", "--prefer-offline", "--frozen-lockfile"]
    r = subprocess.run(install, cwd=repo, capture_output=True, text=True, check=False,
                       shell=sys.platform == "win32")  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr
    assert real_check(repo, "ts")[0]
    p = repo / "ts/app/package.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["dependencies"]["left-pad"] = "1.3.0"  # declared, never imported
    p.write_text(json.dumps(data), encoding="utf-8")
    ok, log, _ = real_check(repo, "ts")
    assert not ok and "ts/app/package.json" in log and "`left-pad`" in log


# `pnpm run` installs by itself, the deadcode step does not go through it, and the
# fast wave starts everything at once: a fresh worktree looked for knip before any install.


def ts_steps(repo):
    return [
        check.Step("ts", "fmt", ["pnpm", "run", "fmt"], cwd=repo),
        check.Step("ts", "deadcode", ["knip-step"], cwd=repo),
    ]


def installs(calls):
    return [argv for argv, _ in calls if argv[:2] == ["pnpm", "install"]]


@pytest.fixture
def pnpm_here(monkeypatch):
    monkeypatch.setattr(tools, "usable", lambda tool: tool == "pnpm")


def test_fresh_tree_installs_once_before_any_step(repo, monkeypatch, pnpm_here):
    calls = fake_run(monkeypatch, {})
    results = check._run_all(config.load(repo), ts_steps(repo))
    assert calls[0][0] == ["pnpm", "install", "--frozen-lockfile"]
    assert calls[0][1] == repo
    assert len(installs(calls)) == 1
    assert [(r.step.kind, r.status) for r in results] == [
        ("fmt", "ok"),
        ("deadcode", "ok"),
    ]
    run_dir = next(p for p in (repo / check.ARTIFACTS).iterdir() if p.is_dir())
    assert (run_dir / "ts-install.log").is_file()


def test_installed_tree_runs_no_install(repo, monkeypatch, pnpm_here):
    bin_ = repo / "node_modules/.bin"
    bin_.mkdir(parents=True)
    (bin_ / "knip").write_text("", encoding="utf-8")
    calls = fake_run(monkeypatch, {})
    check._run_all(config.load(repo), ts_steps(repo))
    assert not installs(calls)


def test_pinned_knip_missing_from_an_existing_install_installs(
    repo, monkeypatch, pnpm_here
):
    (repo / "node_modules/.bin").mkdir(parents=True)  # a branch that just added knip
    calls = fake_run(monkeypatch, {})
    check._run_all(config.load(repo), ts_steps(repo))
    assert len(installs(calls)) == 1


def test_unpinned_knip_with_node_modules_installs_nothing(repo, monkeypatch, pnpm_here):
    (repo / "node_modules").mkdir()
    (repo / "package.json").write_text('{"name": "polygon"}', encoding="utf-8")
    calls = fake_run(monkeypatch, {})
    check._run_all(config.load(repo), ts_steps(repo))
    assert not installs(calls)


def test_no_lockfile_no_install(repo, monkeypatch, pnpm_here):
    (repo / "pnpm-lock.yaml").unlink()
    calls = fake_run(monkeypatch, {})
    check._run_all(config.load(repo), ts_steps(repo))
    assert not installs(calls)


def test_no_ts_step_that_runs_no_install(repo, monkeypatch, pnpm_here):
    calls = fake_run(monkeypatch, {})
    steps = [
        check.Step("ts", "fmt", None, "disabled", cwd=repo),
        check.Step("python", "fmt", ["ruff"], cwd=repo),
    ]
    check._run_all(config.load(repo), steps)
    assert not installs(calls)


def test_no_pnpm_no_install(repo, monkeypatch):
    monkeypatch.setattr(tools, "usable", lambda tool: False)
    calls = fake_run(monkeypatch, {})
    check._run_all(config.load(repo), ts_steps(repo))
    assert not installs(calls)


def test_failed_install_is_a_failed_step_and_the_others_still_run(
    repo, monkeypatch, pnpm_here
):
    reason = "ERR_PNPM_OUTDATED_LOCKFILE Cannot install with frozen-lockfile"
    calls = fake_run(monkeypatch, {"install": (1, reason)})
    results = check._run_all(config.load(repo), ts_steps(repo))
    by = {r.step.kind: r for r in results}
    assert by["install"].status == "fail" and reason in by["install"].tail
    assert by["fmt"].status == "ok" and by["deadcode"].status == "ok"
    assert len(calls) == 3


def test_a_run_with_a_good_install_is_reusable(repo, monkeypatch, pnpm_here):
    """Not a step of the plan: `h ship` compares plans and must not redo a green check."""
    cfg = config.load(repo)
    fake_run(monkeypatch, {})
    steps = ts_steps(repo)
    start = check.snapshot(cfg)
    results = check._run_all(cfg, steps)
    check.record(cfg, start, results, full=True)
    assert check.reusable(cfg, steps) is not None

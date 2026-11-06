"""a hung step fails by name with its output, a long check says what it is doing,
and CI's Node is a pinned version, not whatever is LTS that day."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness import check, cli, config, init, proc

PY = sys.executable
FLOW = ".github/workflows/harness.yml"
REPO = Path(__file__).resolve().parents[2]


def alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, check=False).stdout  # fmt: skip
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_output_reaches_the_log_while_the_step_runs(tmp_path):
    log = tmp_path / "s.log"
    code = "import time; print('started', flush=True); time.sleep(3)"
    writer = (
        "from harness import proc; import sys; from pathlib import Path; "
        f"proc.run_logged([sys.executable, '-c', {code!r}], Path('.'), "
        f"Path({str(log)!r}), '$ x')"
    )
    # The same writer, observed from outside while it runs.
    child = subprocess.Popen([PY, "-c", writer], cwd=tmp_path)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if log.is_file() and "started" in log.read_text(encoding="utf-8"):
                break
            time.sleep(0.2)
        assert "started" in log.read_text(encoding="utf-8")
        assert child.poll() is None  # still running: the log did not wait for the end
    finally:
        child.wait(timeout=30)


def test_a_step_past_its_limit_is_killed_with_its_children(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    grandchild = f"import time; open({str(pidfile)!r}, 'w').write(str(__import__('os').getpid())); time.sleep(300)"
    script = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
        "print('before the hang', flush=True); time.sleep(300)"
    )
    start = time.monotonic()
    code, out = proc.run_logged([PY, "-c", script], tmp_path, tmp_path / "s.log",
                                timeout_s=3)  # fmt: skip
    assert code is None and time.monotonic() - start < 60
    assert "before the hang" in out
    for _ in range(20):
        if pidfile.is_file() and pidfile.read_text():
            break
        time.sleep(0.2)
    pid = int(pidfile.read_text())
    for _ in range(25):
        if not alive(pid):
            break
        time.sleep(0.2)
    assert not alive(pid), "the step's own child outlived the kill"


def test_a_hung_step_fails_by_name_and_keeps_its_log(tmp_path):
    cfg = config.load(tmp_path)
    cfg.step_timeout_minutes = 0.05  # 3 s
    hang = "print('test 1 ok', flush=True); import time; time.sleep(300)"
    step = check.Step("ts", "test", [PY, "-c", hang], cwd=tmp_path)
    r = check._run_step(cfg, step, tmp_path)
    assert r.status == "fail"
    assert "timed out after 0.05 min" in r.tail
    log = (tmp_path / "ts-test.log").read_text(encoding="utf-8")
    assert "test 1 ok" in log and "killed with its child processes" in log


def test_the_other_steps_still_run_after_a_timeout(tmp_path):
    cfg = config.load(tmp_path)
    cfg.step_timeout_minutes = 0.05
    hung = check.Step("ts", "test", [PY, "-c", "import time; time.sleep(300)"],
                      cwd=tmp_path)  # fmt: skip
    fine = check.Step("python", "test", [PY, "-c", "print('ok')"], cwd=tmp_path)
    by = {r.step.stack: r.status for r in check._run_all(cfg, [hung, fine])}
    assert by == {"ts": "fail", "python": "ok"}


@pytest.mark.parametrize("bad", ["0", "1441", '"30"', "true", "2.5"])
def test_step_timeout_is_validated(tmp_path, bad):
    (tmp_path / "harness.toml").write_text(
        f"[check]\nstep-timeout-minutes = {bad}\n", encoding="utf-8"
    )
    with pytest.raises(config.ConfigError, match="step-timeout-minutes"):
        config.load(tmp_path)


def test_step_timeout_default_and_setting(tmp_path):
    assert config.load(tmp_path).step_timeout_minutes == 30
    (tmp_path / "harness.toml").write_text(
        "[check]\nstep-timeout-minutes = 40\n", encoding="utf-8"
    )
    assert config.load(tmp_path).step_timeout_minutes == 40


def test_progress_names_running_and_waiting_steps(monkeypatch):
    p = check._Progress()
    monkeypatch.setattr(check.machine, "load", lambda sample=0.5: 0.34)
    run = check.Step("ts", "test", ["x"])
    wait = check.Step("ts", "typecheck", ["x"])
    p.begin(run, "")
    p.begin(wait, "a heavy-job slot")
    line = p.line()
    assert line is not None
    assert "running: ts test 0m" in line
    assert "waiting: ts typecheck for a heavy-job slot 0m (load 34%)" in line
    p.end(run)
    p.end(wait)
    assert p.line() is None


def test_a_long_check_reports_on_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(check, "PROGRESS_S", 0.5)
    cfg = config.load(tmp_path)
    step = check.Step("ts", "test", [PY, "-c", "import time; time.sleep(2)"],
                      cwd=tmp_path)  # fmt: skip
    check._run_all(cfg, [step])
    assert "h check: running: ts test" in capsys.readouterr().err


def test_ci_node_is_pinned_and_settable(tmp_path, capsys):
    init.run(config.load(tmp_path))
    assert "node-version" not in (tmp_path / FLOW).read_text(encoding="utf-8")
    (tmp_path / "harness.toml").write_text(
        '[ci]\nnode-version = "26"\n', encoding="utf-8"
    )
    (tmp_path / FLOW).unlink()
    init.run(config.load(tmp_path))
    assert "      node-version: '26'\n" in (tmp_path / FLOW).read_text(encoding="utf-8")
    assert cli.main(["-C", str(tmp_path), "config", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ci"]["node-version"] == "26"


def test_the_workflow_default_node_is_the_config_default():
    text = (REPO / ".github/workflows/check.yml").read_text(encoding="utf-8")
    block = text[text.index("      node-version:\n") :].split("\n      pnpm-version:")[
        0
    ]
    assert f'default: "{config.CI_NODE_VERSION}"' in block
    assert "lts/*" not in block.split("default:")[1]


@pytest.mark.parametrize("bad", ["24", '"24; rm -rf /"', '""', '"a b"'])
def test_node_version_is_validated(tmp_path, bad):
    (tmp_path / "harness.toml").write_text(
        f"[ci]\nnode-version = {bad}\n", encoding="utf-8"
    )
    with pytest.raises(config.ConfigError, match="ci.node-version"):
        config.load(tmp_path)

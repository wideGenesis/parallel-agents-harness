"""The one git runner: no prompts, a timeout, a clean env."""

import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from conftest import git as sh

from harness import config, git

SRC = Path(git.__file__).parent


def _repo(path: Path) -> Path:
    path.mkdir()
    sh(path, "init", "-q", "-b", "main")
    sh(path, "commit", "-q", "--allow-empty", "-m", "init")
    return path


def test_no_git_call_bypasses_the_runner():
    direct = [
        f"{p.name}:{n}"
        for p in SRC.glob("*.py")
        if p.name != "git.py"
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"""\[\s*["']git["']""", line)
    ]
    assert direct == []


def test_inherited_git_dir_does_not_redirect_the_command(tmp_path, monkeypatch):
    mine, other = _repo(tmp_path / "mine"), _repo(tmp_path / "other")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))
    top = git.run(mine, "rev-parse", "--show-toplevel").stdout.strip()
    assert Path(top).resolve() == mine.resolve()


def test_env_never_prompts_and_reads_take_no_optional_locks(monkeypatch):
    monkeypatch.setenv("GIT_ASKPASS", "/some/editor/askpass")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.hookspath'='/x'")
    e = git.env(read_only=True)
    assert e["GIT_TERMINAL_PROMPT"] == "0" and e["GCM_INTERACTIVE"] == "never"
    assert e["GIT_ASKPASS"] == "" and "GIT_CONFIG_PARAMETERS" not in e
    assert e["GIT_OPTIONAL_LOCKS"] == "0"
    assert "GIT_OPTIONAL_LOCKS" not in git.env(read_only=False)


@pytest.mark.parametrize(
    ("argv", "sub"),
    [
        (("status",), "status"),
        (("-c", "a=b", "fetch", "origin"), "fetch"),
        (("worktree", "list", "--porcelain"), "worktree list"),
        (("worktree", "add", "x"), "worktree"),
        ((), ""),
    ],
)
def test_subcommand_skips_global_options(argv, sub):
    assert git.subcommand(argv) == sub
    assert (sub in git.READS) == (sub in ("status", "worktree list"))


def test_a_git_timeout_does_not_loosen_the_path_policy(repo, monkeypatch):
    from harness import affected, policy

    def hang(*_a, **_k):
        raise git.GitTimeout("git diff timed out after 60s")

    monkeypatch.setattr(affected, "changed_files", hang)
    with pytest.raises(git.GitTimeout):
        policy.for_worktree(config.load(repo), repo)


class _Unauthorized(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="t"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        pass


def test_auth_request_fails_fast_with_a_hint(tmp_path):
    repo = _repo(tmp_path / "r")
    server = HTTPServer(("127.0.0.1", 0), _Unauthorized)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/repo.git"
        started = time.monotonic()
        r = git.run(repo, "-c", "credential.helper=", "fetch", url, timeout=30)
    finally:
        server.shutdown()
    assert r.returncode != 0
    assert git.AUTH_HINT in r.stderr
    assert time.monotonic() - started < 20


def test_hung_remote_times_out_and_names_the_command(tmp_path):
    repo = _repo(tmp_path / "r")
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen()  # accepts the connection, never answers
    try:
        url = f"http://127.0.0.1:{silent.getsockname()[1]}/repo.git"
        started = time.monotonic()
        with pytest.raises(git.GitTimeout, match=r"git .*fetch .* timed out after 2s"):
            git.run(repo, "-c", "credential.helper=", "fetch", url, timeout=2)
    finally:
        silent.close()
    assert (
        time.monotonic() - started < 15
    )  # the remote helper did not keep the pipes open


def test_timeouts_come_from_config(tmp_path):
    (tmp_path / "harness.toml").write_text(
        "[git]\ntimeout_local_s = 7\ntimeout_network_s = 70\n", encoding="utf-8"
    )
    cfg = config.load(tmp_path)
    assert (cfg.git_timeout_local_s, cfg.git_timeout_network_s) == (7, 70)
    assert git.TIMEOUTS == {"local": 7, "network": 70}
    (tmp_path / "harness.toml").write_text(
        "[git]\ntimeout_local_s = 0\n", encoding="utf-8"
    )
    with pytest.raises(config.ConfigError, match="timeout_local_s"):
        config.load(tmp_path)


@pytest.fixture(autouse=True)
def _restore_timeouts():
    saved = dict(git.TIMEOUTS)
    yield
    git.TIMEOUTS.update(saved)

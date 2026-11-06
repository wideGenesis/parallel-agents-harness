"""the tests' Postgres dies mid-run — h stops the test and sql steps with one
cause line instead of letting every test wait out its 30-s pool timeout, and keeps the
container's logs before removing it."""

import socket
import subprocess
import sys
import threading
import time

import pytest

from harness import check, config, pg, proc

PY = sys.executable


class FakePostgres:
    """A TCP server on 127.0.0.1 that answers SSLRequest with `reply` (b"N" like Postgres,
    b"\\x00" like a dead Docker forwarder); `reply = None` closes without a word."""

    def __init__(self, reply: bytes | None = b"N"):
        self.reply = reply
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            with conn:
                try:
                    conn.recv(8)
                    if self.reply is not None:
                        conn.sendall(self.reply)
                except OSError:
                    pass

    def close(self):
        self._stop = True
        self.sock.close()


@pytest.fixture
def server():
    made = []

    def make(reply=b"N"):
        s = FakePostgres(reply)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


@pytest.mark.parametrize(("reply", "ok"), [(b"N", True), (b"S", True), (b"\x00", False),
                                           (None, False)])  # fmt: skip
def test_only_a_postgres_answer_counts(server, reply, ok):
    s = server(reply)
    assert pg.answers("127.0.0.1", s.port, timeout=1) is ok


def test_a_closed_port_does_not_answer():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert not pg.answers("127.0.0.1", port, timeout=1)


@pytest.mark.parametrize(("url", "want"), [
    ("postgres://u:p@127.0.0.1:55999/db", ("127.0.0.1", 55999)),
    ("postgresql://db.local/x", ("db.local", 5432)),
    ("postgres:///db?host=/var/run/postgresql", None),
    ("mysql://127.0.0.1:3306/x", None),
    ("", None),
    (None, None),
])  # fmt: skip
def test_target(url, want):
    assert pg.target(url) == want


def watch(port, **kw):
    kw.setdefault("every", 0.05)
    kw.setdefault("timeout", 0.5)
    return pg.Watch(("127.0.0.1", port), "throwaway Postgres", **kw)


def until(cond, limit=5.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_a_server_that_dies_is_dead_after_three_misses(server):
    s = server()
    with watch(s.port, during=lambda: "rust test") as w:
        time.sleep(0.2)
        assert not w.dead.is_set()
        s.reply = b"\x00"  # the forwarder answers, nothing behind it
        assert until(w.dead.is_set)
    assert w.cause.startswith("throwaway Postgres stopped answering at +")
    assert w.cause.endswith("(during rust test)")


def test_one_miss_then_an_answer_is_not_death(server, monkeypatch):
    s = server()
    flaky = iter([True, False, True, False, False, True] + [True] * 200)
    monkeypatch.setattr(pg, "answers", lambda *a, **k: next(flaky))
    with watch(s.port, misses=3) as w:
        time.sleep(0.5)
    assert not w.dead.is_set()


def test_a_server_that_never_answered_is_not_declared_dead():
    """The project's DATABASE_URL may point at a dev server its tests do not use."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with watch(port) as w:
        time.sleep(0.4)
    assert not w.dead.is_set()


def test_no_target_no_thread():
    with pg.Watch(None) as w:
        assert w._thread is None


def test_run_logged_stops_a_running_command_with_its_tree(tmp_path):
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    start = time.monotonic()
    sleeper = "print('started', flush=True); import time; time.sleep(60)"
    code, out = proc.run_logged(
        [PY, "-c", sleeper], tmp_path, tmp_path / "x.log", stop=stop
    )
    assert code is None and "started" in out
    assert time.monotonic() - start < 10


def _dead(cause="throwaway Postgres stopped answering at +12s (during rust test)"):
    w = pg.Watch(None)
    w.cause = cause
    w.dead.set()
    return w


def test_test_and_sql_steps_do_not_start_once_it_is_dead(tmp_path, monkeypatch):
    cfg = config.load(tmp_path)
    monkeypatch.setattr(check, "_WATCH", _dead())
    test = check.Step("rust", "test", [PY, "-c", "print('RAN')"], cwd=tmp_path)
    lint = check.Step("rust", "lint", [PY, "-c", "print('lint ok')"], cwd=tmp_path)
    by = {r.step.kind: r for r in check._run_all(cfg, [test, lint])}
    assert by["test"].status == "fail" and by["test"].tail.endswith("— not started")
    assert "stopped answering at +12s" in by["test"].tail
    assert by["lint"].status == "ok"


def test_a_running_test_step_is_stopped_with_one_cause_line(tmp_path, monkeypatch):
    cfg = config.load(tmp_path)
    w = pg.Watch(None)
    monkeypatch.setattr(check, "_WATCH", w)

    def die():
        w.cause = "throwaway Postgres stopped answering at +3s (during rust test)"
        w.dead.set()

    threading.Timer(0.5, die).start()
    noisy = ("import time\nfor i in range(600):\n"
             "    print(f'test t{i} ... FAILED: failed to connect', flush=True); time.sleep(0.1)")  # fmt: skip
    step = check.Step("rust", "test", [PY, "-c", noisy], cwd=tmp_path)
    r = check._run_step(cfg, step, tmp_path)
    assert r.status == "fail"
    assert (
        r.tail
        == "throwaway Postgres stopped answering at +3s (during rust test) — stopped"
    )
    log = (tmp_path / "rust-test.log").read_text(encoding="utf-8")
    assert "killed with its child processes" in log


def test_a_steps_own_failure_is_unchanged_without_a_dead_database(
    tmp_path, monkeypatch
):
    cfg = config.load(tmp_path)
    monkeypatch.setattr(check, "_WATCH", pg.Watch(None))
    step = check.Step("rust", "test", [PY, "-c", "print('test x ... FAILED'); raise SystemExit(101)"],
                      cwd=tmp_path)  # fmt: skip
    r = check._run_step(cfg, step, tmp_path)
    assert r.status == "fail" and "test x ... FAILED" in r.tail


class Docker:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, cwd, **kw):
        self.calls.append(list(argv))
        out = {"inspect": '{"Status":"exited","ExitCode":137,"OOMKilled":true}\n',
               "logs": "FATAL:  the database system is shutting down\n"}.get(argv[1], "")  # fmt: skip
        return subprocess.CompletedProcess(argv, 0, out, "")


def _result(tmp_path, kind, status):
    log = tmp_path / ".artifacts" / "check" / "run1" / f"rust-{kind}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("x", encoding="utf-8")
    step = check.Step("rust", kind, ["x"])
    return check.StepResult(
        step, status, 1.0, f".artifacts/check/run1/rust-{kind}.log", "t"
    )


@pytest.mark.parametrize(("dead", "test_status", "kept"), [
    (True, "fail", True),
    (False, "fail", True),  # a failed test step keeps them too: the cause may be the db
    (False, "ok", False),
])  # fmt: skip
def test_the_containers_logs_are_kept_before_it_goes(tmp_path, monkeypatch, dead,
                                                     test_status, kept):  # fmt: skip
    fake = Docker()
    monkeypatch.setattr(pg.proc, "run", fake)
    cfg = config.load(tmp_path)
    server = pg.Server("line", "h-check-pg-1", "docker")
    w = _dead() if dead else pg.Watch(None)
    results = [_result(tmp_path, "test", test_status), _result(tmp_path, "lint", "ok")]
    check._db_evidence(cfg, server, w, results)
    path = tmp_path / ".artifacts/check/run1/postgres.log"
    assert path.is_file() is kept
    if kept:
        text = path.read_text(encoding="utf-8")
        assert '"OOMKilled":true' in text and "shutting down" in text
        assert "postgres: .artifacts/check/run1/postgres.log" in results[0].tail
        assert ["docker", "logs", "--tail", "200", "h-check-pg-1"] in fake.calls


def test_no_container_of_ours_no_evidence(tmp_path, monkeypatch):
    fake = Docker()
    monkeypatch.setattr(pg.proc, "run", fake)
    check._db_evidence(config.load(tmp_path), pg.Server(), _dead(),
                       [_result(tmp_path, "test", "fail")])  # fmt: skip
    assert fake.calls == []


def test_run_watches_the_url_the_test_steps_get(tmp_path, monkeypatch, server):
    """In CI the workflow's own Postgres: DATABASE_URL from the environment."""
    s = server()
    monkeypatch.setenv("DATABASE_URL", f"postgres://p:p@127.0.0.1:{s.port}/db")
    cfg = config.load(tmp_path)
    seen = {}

    def run_all(c, steps, w=None):
        seen["watch"] = check._WATCH
        return []

    monkeypatch.setattr(check, "_run_all", run_all)
    step = check.Step("rust", "test", ["cargo", "test"], cwd=tmp_path)
    check.run(cfg, [step])
    w = seen["watch"]
    assert (
        w.where == ("127.0.0.1", s.port)
        and w.label == f"Postgres at 127.0.0.1:{s.port}"
    )
    assert check._WATCH is None  # reset after the run


def test_a_run_without_test_or_sql_steps_probes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://p:p@127.0.0.1:1/db")
    cfg = config.load(tmp_path)
    seen = {}
    monkeypatch.setattr(check, "_run_all",
                        lambda c, s, w=None: seen.setdefault("w", check._WATCH) and [])  # fmt: skip
    check.run(cfg, [check.Step("rust", "lint", ["cargo", "clippy"], cwd=tmp_path)])
    assert seen["w"].where is None


def test_progress_names_the_running_db_steps():
    p = check._Progress()
    p.begin(check.Step("rust", "test", None), "")
    p.begin(check.Step("ts", "test", None), "a heavy-job slot")
    p.begin(check.Step("rust", "lint", None), "")
    assert p.running(check.DB_KINDS) == "rust test"


def test_ci_prints_the_databases_logs_when_the_job_failed():
    from pathlib import Path

    text = (
        Path(__file__).resolve().parents[2] / ".github/workflows/check.yml"
    ).read_text(encoding="utf-8")
    step = text[text.index("      - name: Postgres down") :]
    assert "JOB_STATUS: ${{ job.status }}" in step
    assert step.index('docker logs --tail 200 "$PG"') < step.index(
        'docker rm -f -v "$PG"'
    )


def test_the_throwaway_is_watched_from_the_first_second():
    """h saw it ready; a host forwarder broken from the start must not cost 30 s a test."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with watch(port, armed=True) as w:
        assert until(w.dead.is_set)


def test_evidence_that_cannot_be_written_keeps_the_results(
    tmp_path, monkeypatch, capsys
):
    cfg = config.load(tmp_path)
    server = pg.Server("line", "h-check-pg-1", "docker")

    def boom(path):
        raise OSError("disk full")

    monkeypatch.setattr(server, "keep_evidence", boom)
    results = [_result(tmp_path, "test", "fail")]
    check._db_evidence(cfg, server, _dead(), results)
    assert (
        results[0].tail == "t"
        and "postgres logs not kept (disk full)" in capsys.readouterr().err
    )

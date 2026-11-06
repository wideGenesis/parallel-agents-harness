import json
import subprocess
import sys
import threading
import time

from harness import check, config, lock


def test_slots_are_exclusive_up_to_limit():
    a = lock.try_acquire(2)
    b = lock.try_acquire(2)
    assert a and b and a[0] != b[0]
    assert lock.try_acquire(2) is None
    lock.release(a[1])
    c = lock.try_acquire(2)
    assert c is not None and c[0] == a[0]
    lock.release(b[1])
    lock.release(c[1])


HOLDER = """
import sys, time
from harness import lock
got = lock.try_acquire(1)
print("held" if got else "busy", flush=True)
time.sleep(60)
"""


def test_slot_is_freed_when_holder_process_dies():
    # The property that makes the semaphore safe: a crashed session releases its slot.
    p = subprocess.Popen(
        [sys.executable, "-c", HOLDER], stdout=subprocess.PIPE, text=True
    )
    try:
        assert p.stdout is not None
        assert p.stdout.readline().strip() == "held"
        assert lock.try_acquire(1) is None
    finally:
        p.kill()
        p.wait()
    # Windows releases a dead process's locks asynchronously (measured 0–16 ms).
    deadline = time.monotonic() + 5
    while (got := lock.try_acquire(1)) is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert got is not None
    lock.release(got[1])


def test_slot_waits_and_reports_once():
    held = lock.try_acquire(1)
    assert held is not None
    waits = []
    entered = threading.Event()

    def worker():
        with lock.slot(1, on_wait=lambda: waits.append(1), poll=0.05):
            entered.set()

    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.3)
    assert not entered.is_set()
    lock.release(held[1])
    t.join(5)
    assert entered.is_set() and waits == [1]


def test_heavy_steps_queue_on_the_semaphore(repo, monkeypatch):
    (repo / "harness.toml").write_text("[limits]\nheavy_jobs = 1\n", encoding="utf-8")
    cfg = config.load(repo)
    py = sys.executable
    sleep = [py, "-c", "import time; time.sleep(0.4)"]
    steps = [
        check.Step("rust", "test", sleep),
        check.Step("python", "test", sleep),
        check.Step("python", "fmt", sleep),  # light: never queues
    ]
    waited = []
    results = check.run(cfg, steps, on_wait=lambda s: waited.append(s.stack))
    assert all(r.status == "ok" for r in results)
    assert len(waited) == 1  # one of the two heavy steps had to queue
    assert max(r.waited for r in results) >= 0.3
    assert next(r for r in results if r.step.kind == "fmt").waited == 0


def test_many_threads_on_fresh_slots_never_error():
    # Regression: first use raced on writing a byte into a slot another thread had locked.
    errors, held = [], []
    barrier = threading.Barrier(8)

    def grab():
        barrier.wait()
        try:
            got = lock.try_acquire(3)
            if got:
                held.append(got)
        except OSError as e:
            errors.append(e)

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(held) == 3
    for _, fd in held:
        lock.release(fd)


def test_machine_waits_for_every_slot_and_holds_back_new_heavy_steps():
    # the whole machine, and a freed slot is not taken again under the waiter.
    held = lock.try_acquire(2)  # a running heavy step elsewhere
    assert held is not None
    inside, late = threading.Event(), threading.Event()

    def whole():
        with lock.machine(2, poll=0.05):
            inside.set()
            time.sleep(0.3)

    def newcomer():
        with lock.slot(2, poll=0.05):
            late.set()

    a = threading.Thread(target=whole)
    a.start()
    time.sleep(0.2)  # the gate is taken, slot 1 too; slot 0 still busy
    b = threading.Thread(target=newcomer)
    b.start()
    time.sleep(0.2)
    assert (
        not inside.is_set() and not late.is_set()
    )  # the newcomer waits behind the gate
    lock.release(held[1])
    assert inside.wait(5)
    assert not late.is_set()  # still the whole machine
    a.join(5)
    b.join(5)
    assert late.is_set()


def test_two_whole_machine_steps_queue_instead_of_deadlocking():
    order = []

    def whole(name):
        with lock.machine(3, poll=0.02):
            order.append(f"{name}+")
            time.sleep(0.2)
            order.append(f"{name}-")

    ts = [threading.Thread(target=whole, args=(n,)) for n in "ab"]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert not any(t.is_alive() for t in ts)
    assert order in (["a+", "a-", "b+", "b-"], ["b+", "b-", "a+", "a-"])


def test_exclusive_steps_run_after_the_others(repo):
    (repo / "harness.toml").write_text(
        '[limits]\nheavy_jobs = 2\n[check]\nexclusive = ["ts.test"]\n', encoding="utf-8"
    )
    cfg = config.load(repo)
    py = sys.executable
    stamp = repo / "stamps.txt"

    def step(stack, kind, sleep):
        code = (f"import time; open({str(stamp)!r}, 'a').write('{stack}.{kind}+ '); "
                f"time.sleep({sleep}); open({str(stamp)!r}, 'a').write('{stack}.{kind}- ')")  # fmt: skip
        return check.Step(stack, kind, [py, "-c", code])

    steps = [
        step("ts", "test", 0.1),
        step("rust", "test", 0.4),
        step("python", "fmt", 0.4),
    ]
    results = check.run(cfg, steps)
    assert all(r.status == "ok" for r in results)
    lines = stamp.read_text(encoding="utf-8").split()
    assert lines[-2:] == ["ts.test+", "ts.test-"]  # alone, after both waves
    assert [r.step.stack for r in results][-1] == "ts"


def test_exclusive_is_validated(tmp_path):
    import pytest

    (tmp_path / "harness.toml").write_text(
        '[check]\nexclusive = ["e2e"]\n', encoding="utf-8"
    )
    with pytest.raises(config.ConfigError, match="check.exclusive"):
        config.load(tmp_path)


WHOLE = """
import sys, time
from harness import lock
with lock.machine(2, poll=0.02):
    print("in", time.time(), flush=True)
    time.sleep(float(sys.argv[1]))
    print("out", time.time(), flush=True)
"""


def test_whole_machine_is_one_at_a_time_across_processes():
    # the gate is an OS lock, so another session's process waits for it.
    def start(hold):
        return subprocess.Popen([sys.executable, "-c", WHOLE, hold],
                                stdout=subprocess.PIPE, text=True)  # fmt: skip

    first = start("1.0")
    try:
        assert first.stdout is not None and first.stdout.readline().startswith("in")
        out2, _ = start("0").communicate(timeout=20)
        out1, _ = first.communicate(timeout=20)
        left = float(out1.split()[1])  # "out <t>" of the first
        entered = float(out2.split()[1])  # "in <t>" of the second
        assert entered >= left
    finally:
        first.kill()
        first.wait()


def test_ci_log_shows_the_steps_in_order_with_start_and_wait(repo, monkeypatch, capsys):
    # a project could not see from the CI log that its exclusive e2e ran last.
    from harness import cli

    (repo / "harness.toml").write_text(
        '[limits]\nheavy_jobs = 2\n[check]\nexclusive = ["ts.test"]\n', encoding="utf-8"
    )
    py = sys.executable
    sleep = [py, "-c", "import time; time.sleep(0.3)"]
    steps = [check.Step("ts", "test", sleep), check.Step("rust", "test", sleep)]
    monkeypatch.setattr(check, "plan", lambda *a, **k: steps)

    monkeypatch.delenv("CI", raising=False)
    assert cli.main(["-C", str(repo), "check", "--full"]) == 0
    assert "timeline" not in capsys.readouterr().out  # the agent's output stays short

    monkeypatch.setenv("CI", "true")
    assert cli.main(["-C", str(repo), "check", "--full", "--json"]) == 0
    started = {r["stack"]: r["started"] for r in json.loads(capsys.readouterr().out)}
    assert started["ts"] > started["rust"]  # exclusive: after the other step
    assert cli.main(["-C", str(repo), "check", "--full"]) == 0
    out = capsys.readouterr().out
    lines = out[out.index("timeline:") :].splitlines()[1:3]
    assert (
        "rust test" in lines[0]
        and "ts test" in lines[1]
        and lines[1].strip().startswith("+")
    )

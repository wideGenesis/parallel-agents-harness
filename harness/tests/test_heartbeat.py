import io
import json
import os
import time

import pytest

from harness import claims, cli, config, task


def beat_age_h(cfg, task_id):
    got = claims.read(cfg, task_id)
    assert got is not None
    return got[1].age_hours()


def age(cfg, task_id, hours):
    import datetime as dt

    sha, c = claims.read(cfg, task_id)  # type: ignore[misc]
    c.heartbeat = (
        (dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours))
        .replace(microsecond=0)
        .isoformat()
    )
    assert claims.replace(cfg, sha, c)


def test_check_inside_worktree_sends_heartbeat(world):
    _, a, _ = world
    r = task.start(config.load(a), "T-1")
    age(config.load(a), "T-1", 3)
    assert task.auto_heartbeat(config.load(r.path), r.path) is None
    assert beat_age_h(config.load(a), "T-1") < 0.1


def test_outside_worktree_does_nothing(world):
    _, a, _ = world
    assert task.auto_heartbeat(config.load(a), a) is None


def test_hook_throttle(world):
    _, a, _ = world
    cfg = config.load(a)
    r = task.start(cfg, "T-1")  # start does not stamp; the first beat does
    task.auto_heartbeat(cfg, r.path)
    age(cfg, "T-1", 1)
    task.auto_heartbeat(cfg, r.path, task.HOOK_INTERVAL_S)  # stamp is fresh → skipped
    assert beat_age_h(cfg, "T-1") > 0.9
    stamp = task._stamp(cfg, "T-1")
    old = time.time() - task.HOOK_INTERVAL_S - 5
    os.utime(stamp, (old, old))
    task.auto_heartbeat(cfg, r.path, task.HOOK_INTERVAL_S)  # stale stamp → sent
    assert beat_age_h(cfg, "T-1") < 0.1


def test_network_failure_is_a_warning(world, monkeypatch):
    _, a, _ = world
    cfg = config.load(a)
    r = task.start(cfg, "T-1")

    def offline(*_a, **_k):
        raise claims.ClaimError("git ls-remote: could not resolve host")

    monkeypatch.setattr(claims, "read", offline)
    warn = task.auto_heartbeat(cfg, r.path)
    assert warn is not None and warn.startswith("heartbeat: not sent")


def test_taken_over_stops_work(world):
    _, a, _ = world
    cfg = config.load(a)
    r = task.start(cfg, "T-1")
    sha, held = claims.read(cfg, "T-1")  # type: ignore[misc]
    held.owner = "other|/x"
    assert claims.replace(cfg, sha, held)
    with pytest.raises(task.TakenOver):
        task.auto_heartbeat(cfg, r.path)


def test_post_edit_hook_reports_takeover_with_exit_2(world, monkeypatch, capsys):
    _, a, _ = world
    cfg = config.load(a)
    r = task.start(cfg, "T-1")
    sha, held = claims.read(cfg, "T-1")  # type: ignore[misc]
    held.owner = "other|/x"
    assert claims.replace(cfg, sha, held)
    f = r.path / "notes.txt"
    f.write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"tool_input": {"file_path": str(f)}}))
    )
    assert cli.main(["hook", "post-edit"]) == 2
    assert "stop" in capsys.readouterr().err


def test_offline_attempt_engages_the_throttle(world, monkeypatch):
    _, a, _ = world
    cfg = config.load(a)
    r = task.start(cfg, "T-1")
    calls = []

    def offline(*_a, **_k):
        calls.append(1)
        raise claims.ClaimError("git ls-remote: could not resolve host")

    monkeypatch.setattr(claims, "read", offline)
    task.auto_heartbeat(cfg, r.path, task.HOOK_INTERVAL_S)
    task.auto_heartbeat(cfg, r.path, task.HOOK_INTERVAL_S)
    assert len(calls) == 1  # the second edit does not wait on the network again


# --- a failed push is not a takeover ---------------------------------------------


@pytest.fixture
def no_pauses(monkeypatch):
    monkeypatch.setattr(claims, "RETRY_PAUSES_S", (0.0, 0.0))


def break_push(clone):
    # Reads (ls-remote, fetch) still reach origin; every push fails like a 5xx would.
    from conftest import git

    git(clone, "config", "remote.origin.pushurl", str(clone / "no-such-remote"))


def test_failed_push_is_not_a_takeover(world, no_pauses, monkeypatch, capsys):
    _, a, _ = world
    cfg = config.load(a)
    r = task.start(cfg, "T-1")
    age(cfg, "T-1", 1)  # the beat must change the claim, not rewrite the same second
    break_push(a)
    pushes = []
    real = claims._git

    def counting(c, *argv, **k):
        if argv[0] == "push":
            pushes.append(argv)
        return real(c, *argv, **k)

    monkeypatch.setattr(claims, "_git", counting)
    with pytest.raises(claims.PushFailed):
        task.heartbeat(cfg, "T-1", r.path)
    assert len(pushes) == 3  # retried
    warn = task.auto_heartbeat(cfg, r.path)
    assert warn is not None and warn.startswith("heartbeat: not sent")

    f = r.path / "notes.txt"
    f.write_text("x\n", encoding="utf-8")
    os.utime(task._stamp(cfg, "T-1"), (0, 0))  # past the hook throttle
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"tool_input": {"file_path": str(f)}}))
    )
    assert cli.main(["hook", "post-edit"]) == 0
    err = capsys.readouterr().err
    assert "warning" in err and "stop" not in err


def test_failed_push_after_a_takeover_is_a_takeover(world, no_pauses):
    _, a, b = world
    cfg = config.load(a)
    r = task.start(cfg, "T-1")
    got = claims.read(cfg, "T-1")
    assert got is not None
    sha, held = got
    stolen = claims.Claim(
        "T-1", "other|/x", held.branch, "b@b", held.started, held.heartbeat
    )
    assert claims.replace(config.load(b), sha, stolen)
    held.heartbeat = "2026-01-01T00:00:00+00:00"  # a real change, so a push is needed
    assert claims.replace(cfg, sha, held) is False  # lease lost, not PushFailed
    with pytest.raises(task.TakenOver):
        task.heartbeat(cfg, "T-1", r.path)


@pytest.fixture
def same_second(monkeypatch):
    """Every beat lands in the second of `start`: the new claim blob equals the old one."""
    monkeypatch.setattr(claims, "now_iso", lambda: "2026-10-25T00:00:00+00:00")


REAL_REPLACE = claims.replace  # tests below patch claims.replace


def steal(cfg_b, sha, held):
    stolen = claims.Claim(
        "T-1", "other|/x", held.branch, "b@b", held.started, held.heartbeat
    )
    assert REAL_REPLACE(cfg_b, sha, stolen)


def test_unchanged_claim_still_needs_the_lease(world, same_second):
    _, a, b = world
    cfg = config.load(a)
    task.start(cfg, "T-1")
    got = claims.read(cfg, "T-1")
    assert got is not None
    sha, held = got
    steal(config.load(b), sha, held)
    assert (
        claims.replace(cfg, sha, held) is False
    )  # nothing to push, but the lease is lost


def test_unchanged_claim_with_origin_unreachable_is_a_push_failure(
    world, same_second, monkeypatch
):
    _, a, _ = world
    cfg = config.load(a)
    task.start(cfg, "T-1")
    got = claims.read(cfg, "T-1")
    assert got is not None
    sha, held = got

    def unreachable(*_):
        raise claims.ClaimError("git ls-remote: could not resolve host")

    monkeypatch.setattr(claims, "_remote_sha", unreachable)
    with pytest.raises(claims.PushFailed):
        claims.replace(cfg, sha, held)


def test_same_second_takeover_stops_the_beat(world, same_second, monkeypatch):
    _, a, b = world
    cfg = config.load(a)
    r = task.start(cfg, "T-1")

    def taken_between_read_and_swap(c, expect, claim):
        got = claims.read(c, claim.task)
        assert got is not None
        if got[1].owner != "other|/x":
            steal(config.load(b), *got)
        return REAL_REPLACE(c, expect, claim)

    monkeypatch.setattr(claims, "replace", taken_between_read_and_swap)
    with pytest.raises(task.TakenOver):
        task.heartbeat(cfg, "T-1", r.path)


def test_losing_the_swap_to_ourselves_is_a_success(world, same_second, monkeypatch):
    _, a, _ = world
    cfg = config.load(a)
    r = task.start(cfg, "T-1")
    age(cfg, "T-1", 1)  # within start's second the beat changes nothing and never swaps
    real = claims.replace
    calls = []

    def another_beat_lands_first(c, expect, claim):
        calls.append(1)
        if len(calls) == 1:  # our other process (hook, h check) beat in between
            got = claims.read(c, claim.task)
            assert got is not None
            sha, mine = got
            mine.heartbeat = "2026-01-01T00:00:00+00:00"
            assert real(c, sha, mine)
            return real(c, expect, claim)  # stale expect → False
        return real(c, expect, claim)

    monkeypatch.setattr(claims, "replace", another_beat_lands_first)
    held = task.heartbeat(cfg, "T-1", r.path)
    assert len(calls) == 2 and held.owner == claims.owner_key(r.path)


def test_parallel_heartbeats_of_one_worktree_all_succeed(world):
    import threading

    _, a, _ = world
    cfg = config.load(a)
    r = task.start(cfg, "T-1")
    errors = []

    def beat():
        try:
            task.heartbeat(cfg, "T-1", r.path)
        except claims.ClaimError as e:
            errors.append(e)

    threads = [threading.Thread(target=beat) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

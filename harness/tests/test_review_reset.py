"""The review limit counts rejections: only the user resets it, a re-review after
an approve spends no attempt, and the approve survives a recreated worktree."""

import json
import shutil
import subprocess

import pytest
from conftest import git
from test_review import write_verdict
from test_review import wt as _wt

from harness import cli, config, init, review

wt = _wt  # the task worktree fixture from test_review
LIB = "py/poly-lib/src/poly_lib/__init__.py"


def commit(wt, text):
    p = wt / LIB
    p.write_text(p.read_text(encoding="utf-8") + text, encoding="utf-8")
    git(wt, "commit", "-qam", "fix: more")


def counters(wt):
    return review._origin_state(config.load(wt), "gh-5")[1]


def reject_three_times(wt):
    cfg = config.load(wt)
    for i in range(3):
        review.prepare(cfg, wt)
        write_verdict(wt, "changes-requested", rnd=i + 1)
        commit(wt, f"# fix {i}\n")


def test_reset_is_the_users_and_reopens_the_rounds(wt, monkeypatch, capsys):
    cfg = config.load(wt)
    reject_three_times(wt)
    with pytest.raises(review.ReviewError, match="h review reset"):
        review.prepare(cfg, wt)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert cli.main(["-C", str(wt), "review", "reset", "gh-5"]) == 2
    assert "the user's decision" in capsys.readouterr().err
    assert counters(wt)  # still there
    monkeypatch.delenv("CLAUDECODE")
    assert cli.main(["-C", str(wt), "review", "reset", "gh-5", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cleared"] and out["counters"]["rounds"] == 3
    assert counters(wt) == {} and not (wt / review.STATE).exists()
    assert cli.main(["-C", str(wt), "review", "prepare", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["round"] == 1
    assert cli.main(["-C", str(wt), "review", "reset", "gh-5"]) == 0
    assert cli.main(["-C", str(wt), "review", "reset", "gh-5"]) == 0
    assert "no counter" in capsys.readouterr().out


def test_init_denies_review_reset_to_agents(tmp_path):
    init.run(config.load(tmp_path))
    s = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "Bash(h review reset*)" in s["permissions"]["deny"]


def test_re_review_after_an_approve_spends_no_attempt(wt):
    cfg = config.load(wt)
    review.prepare(cfg, wt)
    write_verdict(wt, "changes-requested")
    commit(wt, "# fixed\n")
    review.prepare(cfg, wt)
    write_verdict(wt, "approve", rnd=2)
    commit(wt, "# after the approve\n")  # e.g. a rebase that needed a real change
    pkg, rnd, _ = review.prepare(cfg, wt)
    assert rnd == 2 and counters(wt)["rejections"] == 1
    assert "Re-review after an approve" in pkg.read_text(encoding="utf-8")


def test_an_approve_recorded_by_ship_survives_a_recreated_worktree(wt):
    cfg = config.load(wt)
    reject_three_times(wt)
    shutil.rmtree(wt / review.DIR)  # the third rejection is counted all the same
    with pytest.raises(review.ReviewError, match="rejected 3 times"):
        review.prepare(cfg, wt)
    review.reset(cfg, "gh-5")
    review.prepare(cfg, wt)
    write_verdict(wt, "approve")
    v = review.gate(cfg, wt)
    assert v is not None
    review.record_verdict(cfg, "gh-5", v)  # what ship does after the gate
    shutil.rmtree(wt / ".artifacts" / "review")  # the worktree is recreated
    commit(wt, "# after the move\n")
    _, rnd, _ = review.prepare(cfg, wt)
    assert rnd == 1 and counters(wt)["rejections"] == 0


def test_a_lost_verdict_counts_as_a_rejection(wt):
    cfg = config.load(wt)
    review.prepare(cfg, wt)
    write_verdict(wt, "approve")  # approved, but never shipped: origin does not know
    shutil.rmtree(wt / review.DIR)
    commit(wt, "# more\n")
    _, rnd, _ = review.prepare(cfg, wt)
    assert rnd == 2


def test_a_counter_from_before_rejections_were_counted(wt):
    """An old counter counted packages: an approved last round is not a rejection."""
    cfg = config.load(wt)
    review.prepare(cfg, wt)
    write_verdict(wt, "approve")
    sha, _ = review._origin_state(cfg, "gh-5")
    review._record(cfg, "gh-5", sha, {"rounds": 3, "delta": 0})
    commit(wt, "# more\n")
    _, rnd, _ = review.prepare(cfg, wt)
    assert rnd == 3


def test_status_keeps_a_checked_approve_on_origin(wt, capsys):
    """the approve reaches origin right after the reviewer, not only at ship."""
    cfg = config.load(wt)
    review.prepare(cfg, wt)
    write_verdict(wt, "approve")
    assert cli.main(["-C", str(wt), "review", "status"]) == 0
    assert "approve recorded on origin" in capsys.readouterr().out
    assert counters(wt)["last"]["verdict"] == "approve"
    shutil.rmtree(wt / ".artifacts" / "review")  # the worktree is lost before ship
    commit(wt, "# after the move\n")
    _, rnd, _ = review.prepare(cfg, wt)
    assert rnd == 1 and counters(wt)["rejections"] == 0


def test_status_does_not_record_an_unchecked_approve(wt, capsys):
    cfg = config.load(wt)
    review.prepare(cfg, wt)
    pkg = wt / review.PACKAGE
    pkg.write_bytes(pkg.read_bytes() + b"tampered\n")
    write_verdict(wt, "approve")
    assert cli.main(["-C", str(wt), "review", "status", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert not out["recorded"] and "package changed" in out["why"]
    assert "last" not in counters(wt)


def test_reset_keeps_the_local_state_when_origin_refuses(wt, monkeypatch):
    cfg = config.load(wt)
    review.prepare(cfg, wt)
    real = review.claims._git

    def no_push(cfg, *argv, **kw):
        if argv[0] == "push":
            return subprocess.CompletedProcess(argv, 1, "", "remote rejected")
        return real(cfg, *argv, **kw)

    monkeypatch.setattr(review.claims, "_git", no_push)
    with pytest.raises(review.ReviewError, match="not cleared: remote rejected"):
        review.reset(cfg, "gh-5")
    assert (wt / review.STATE).is_file() and counters(wt)

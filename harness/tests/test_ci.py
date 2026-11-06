import json
import subprocess
import sys

import pytest
from conftest import git

from harness import cards, ci, cli, config, gh, proc, ship, task

TRIVIAL = cards.render({"Problem": "p", "Assumptions": "none", "Verify": "h check",
                        "Size": "S", "Trivial": "yes", "Trivial reason": "t"})  # fmt: skip

RAW = "\n".join(
    [f"test (windows)\tRun tests\t2026-10-22T10:00:{i:02d}.0000000Z noise line {i}" for i in range(60)]
    + ["test (windows)\tRun tests\t2026-10-22T10:01:00.0000000Z E   AssertionError: 2 != 3",
       "test (windows)\tRun tests\t2026-10-22T10:01:01.0000000Z tests/test_x.py:12: in test_add"]
    + [f"test (windows)\tRun tests\t2026-10-22T10:02:{i:02d}.0000000Z after {i}" for i in range(60)]
)  # fmt: skip


def test_compress_keeps_errors_and_tail_drops_noise():
    out = ci.compress(RAW)
    assert "E   AssertionError: 2 != 3" in out
    assert "tests/test_x.py:12" in out
    assert "2026-10-22T" not in out  # timestamps gone
    assert "noise line 5" not in out.splitlines()  # far from any error
    assert "noise line 59" in out.splitlines()  # context right before the error
    assert "after 59" in out  # the tail
    assert len(out.splitlines()) <= ci.CAP + 2


ORIGINAL_RUN_LOG_FAILED = gh.run_log_failed


class FakeRuns:
    """Successive CI results for one sha: each poll pops the next state list."""

    def __init__(self, github, sequence):
        self.github, self.sequence, self.reruns = github, list(sequence), []

    def runs(self, cwd, sha):
        return self.sequence.pop(0) if len(self.sequence) > 1 else self.sequence[0]

    def rerun(self, cwd, run_id):
        self.reruns.append(run_id)


def run_(status="completed", conclusion="success"):
    return {"databaseId": 11, "status": status, "conclusion": conclusion, "name": "harness",
            "workflowName": "ci", "url": "https://x/run/11", "event": "pull_request"}  # fmt: skip


@pytest.fixture
def wt(world, github):
    _, a, _ = world
    github.add_issue(TRIVIAL, ["ac-confirmed"], number=9)
    r = task.start(config.load(a), "gh-9")
    github.prs.append({"number": 9, "state": "OPEN", "headRefName": r.branch,
                       "mergeCommit": None, "url": "https://x/pull/9", "auto": False})  # fmt: skip
    git(r.path, "push", "-q", "origin", r.branch)  # CI runs only for what origin has
    return r.path


def install(monkeypatch, github, seq, checks=()):
    fake = FakeRuns(github, seq)
    monkeypatch.setattr(gh, "runs_for_commit", fake.runs)
    monkeypatch.setattr(gh, "rerun_failed", fake.rerun)
    monkeypatch.setattr(gh, "run_log_failed", lambda cwd, rid: RAW)
    monkeypatch.setattr(gh, "pr_checks", lambda cwd, branch: list(checks))
    return fake


def watch(wt):
    return ci.watch(config.load(wt), wt, timeout_s=0, sleep=lambda s: None)


@pytest.mark.parametrize("state", [None, "MERGED"])
def test_no_open_pr_refuses_at_once(wt, github, monkeypatch, state):
    # a merged PR runs no CI for new commits; waiting only timed out.
    if state is None:
        github.prs.clear()
    else:
        github.prs[-1]["state"] = state
    fake = install(monkeypatch, github, [[run_()]])
    slept: list[float] = []
    r = ci.watch(config.load(wt), wt, timeout_s=3600, sleep=slept.append)
    assert r.status == "no-pr" and "h ship" in r.lines[-1]
    assert slept == [] and fake.reruns == []


def no_gh(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("gh must not be asked about an unpushed HEAD")

    for name in ("runs_for_commit", "pr_for_branch", "pr_checks", "rerun_failed"):
        monkeypatch.setattr(gh, name, boom)


@pytest.mark.parametrize("case", ["ahead", "no-branch", "behind"])
def test_head_not_on_origin_refuses_without_asking_ci(wt, github, monkeypatch, case):
    # a commit not pushed yet gets no CI run; waiting only timed out after ~45 min.
    branch = task._git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if case == "ahead":
        git(wt, "commit", "-q", "--allow-empty", "-m", "fix, not pushed")
    elif case == "no-branch":
        git(wt, "push", "-q", "origin", f":{branch}")
    else:  # origin moved on from elsewhere; HEAD is the older commit
        git(wt, "commit", "-q", "--allow-empty", "-m", "pushed elsewhere")
        git(wt, "push", "-q", "origin", "HEAD")
        git(wt, "reset", "-q", "--hard", "HEAD~1")
    no_gh(monkeypatch)
    slept: list[float] = []
    r = ci.watch(config.load(wt), wt, timeout_s=3600, sleep=slept.append)
    assert r.status == "unpushed" and slept == []
    assert "h ship" in r.lines[-1]
    assert ("rebase" in r.lines[-1]) == (case == "behind")
    assert ci.rounds(config.load(wt), "gh-9") == 0


def test_unpushed_exits_nonzero(wt, github, monkeypatch, capsys):
    git(wt, "commit", "-q", "--allow-empty", "-m", "fix, not pushed")
    no_gh(monkeypatch)
    assert cli.main(["-C", str(wt), "ci", "watch"]) == 6
    assert "next: h ship" in capsys.readouterr().out


def test_green(wt, github, monkeypatch):
    install(monkeypatch, github, [[run_()]])
    assert watch(wt).status == "green"


def test_flake_passes_on_rerun_and_is_not_a_round(wt, github, monkeypatch):
    fake = install(monkeypatch, github, [[run_(conclusion="failure")], [run_()]])
    assert watch(wt).status == "green"
    assert fake.reruns == [11]
    assert ci.rounds(config.load(wt), "gh-9") == 0


def test_real_failures_count_and_block_after_two(wt, github, monkeypatch):
    cfg = config.load(wt)
    install(monkeypatch, github, [[run_(conclusion="failure")]])
    r1 = watch(wt)
    assert r1.status == "red" and r1.log == ".artifacts/ci/round-1.log"
    assert "AssertionError" in (wt / r1.log).read_text(encoding="utf-8")
    assert any("ci-triage" in ln for ln in r1.lines)
    assert watch(wt).status == "red"  # same sha again: not a second round
    assert ci.rounds(cfg, "gh-9") == 1

    git(wt, "commit", "-q", "--allow-empty", "-m", "fix attempt")
    git(wt, "push", "-q", "origin", "HEAD")
    r2 = watch(wt)
    assert r2.status == "blocked" and "ci-blocked" in github.issues[9]["labels"]
    with pytest.raises(ci.CiBlocked):
        watch(wt)
    with pytest.raises(ship.ShipRefused, match="CI failed 2 times"):
        ship.ship(cfg, wt)

    assert ci.reset(cfg, "gh-9") is True
    assert ci.rounds(cfg, "gh-9") == 0


def test_log_with_non_utf8_bytes_is_written(wt, github, monkeypatch):
    # A Windows runner's log is not always UTF-8; the round must still get its log.
    install(monkeypatch, github, [[run_(conclusion="failure")]])
    monkeypatch.setattr(gh, "run_log_failed", ORIGINAL_RUN_LOG_FAILED)
    monkeypatch.setattr(gh, "pr_for_branch", lambda cwd, branch: "https://x/pull/9")
    emit = "import sys; sys.stdout.buffer.write('E   «error» €'.encode('cp1251'))"
    monkeypatch.setattr(
        gh, "run", lambda cwd, *argv: proc.run([sys.executable, "-c", emit], cwd)
    )
    r = watch(wt)
    assert r.status == "red"
    assert "E   " in (wt / r.log).read_text(encoding="utf-8")


def test_pending(wt, github, monkeypatch):
    install(monkeypatch, github, [[run_(status="in_progress", conclusion="")]])
    assert watch(wt).status == "pending"


def test_conflicting_pr_stops_instead_of_waiting(wt, github, monkeypatch):
    # GitHub starts no pull_request run for a PR that conflicts with the base
    install(monkeypatch, github, [[]])
    asked = []
    monkeypatch.setattr(
        gh, "pr_mergeable", lambda cwd, branch: asked.append(branch) or "CONFLICTING"
    )
    slept = []
    r = ci.watch(config.load(wt), wt, timeout_s=3600, sleep=slept.append)
    assert r.status == "conflict" and not slept
    assert asked and asked[0].startswith("task/gh-9")  # the worktree's own branch
    assert any("rebase" in ln for ln in r.lines)


def test_unknown_mergeability_keeps_waiting(wt, github, monkeypatch):
    install(monkeypatch, github, [[]])
    monkeypatch.setattr(gh, "pr_mergeable", lambda cwd, branch: "UNKNOWN")
    assert watch(wt).status == "pending"


def test_init_denies_ci_reset_to_agents(tmp_path):
    import json

    from harness import init

    init.run(config.load(tmp_path))
    s = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "Bash(h ci reset*)" in s["permissions"]["deny"]
    assert (tmp_path / ".claude" / "agents" / "ci-triage.md").is_file()


def test_rerun_waits_for_the_new_attempt(wt, github, monkeypatch):
    # GitHub still shows the old failed attempt right after `gh run rerun`.
    old = {**run_(conclusion="failure"), "attempt": 1}
    new = {**run_(), "attempt": 2}
    install(monkeypatch, github, [[old], [old], [old], [new]])
    real = ci._wait
    monkeypatch.setattr(
        ci, "_wait", lambda cfg, sha, t, s, m=None, b="": real(cfg, sha, 60, s, m, b)
    )
    assert watch(wt).status == "green"
    assert ci.rounds(config.load(wt), "gh-9") == 0


def test_round_log_budget_is_shared():
    out = [ci.compress(RAW, cap=40) for _ in range(5)]
    assert sum(len(o.splitlines()) for o in out) <= 5 * 42


def test_reset_is_refused_inside_an_agent_session(wt, github, monkeypatch, capsys):
    cfg = config.load(wt)
    ci._write(cfg, "gh-9", None, ci.State(rounds=["a", "b"]))
    monkeypatch.setenv("CLAUDECODE", "1")
    for argv in (["ci", "reset", "gh-9"], ["-C", str(wt), "ci", "reset", "gh-9"]):
        assert cli.main(argv) == 2
        assert "the user's decision" in capsys.readouterr().err
    assert ci.rounds(cfg, "gh-9") == 2
    monkeypatch.delenv("CLAUDECODE")
    assert cli.main(["-C", str(wt), "ci", "reset", "gh-9"]) == 0
    assert ci.rounds(cfg, "gh-9") == 0


def test_reset_reports_a_failed_push(wt, github, monkeypatch):
    cfg = config.load(wt)
    ci._write(cfg, "gh-9", None, ci.State(rounds=["a"]))
    real = ci.claims._git

    def no_push(cfg, *argv, **kw):
        if argv[0] == "push":
            return subprocess.CompletedProcess(argv, 1, "", "remote rejected")
        return real(cfg, *argv, **kw)

    monkeypatch.setattr(ci.claims, "_git", no_push)
    with pytest.raises(ci.claims.ClaimError, match="not cleared: remote rejected"):
        ci.reset(cfg, "gh-9")


def test_reset_cli_fails_when_the_push_is_rejected(wt, github, monkeypatch, capsys):
    # the user sees the error, not "cleared"
    cfg = config.load(wt)
    ci._write(cfg, "gh-9", None, ci.State(rounds=["a"]))
    real = ci.claims._git

    def no_push(cfg, *argv, **kw):
        if argv[0] == "push":
            return subprocess.CompletedProcess(argv, 1, "", "remote rejected")
        return real(cfg, *argv, **kw)

    monkeypatch.setattr(ci.claims, "_git", no_push)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    assert cli.main(["-C", str(wt), "ci", "reset", "gh-9"]) != 0
    out = capsys.readouterr()
    assert "remote rejected" in out.err and "cleared" not in out.out


def test_cancelled_run_is_no_round_and_no_rerun(wt, github, monkeypatch):
    fake = install(monkeypatch, github, [[run_(conclusion="cancelled")]])
    r = watch(wt)
    assert r.status == "blocked" and "cancelled" in r.lines[0]
    assert any("gh run rerun 11" in ln for ln in r.lines)
    assert fake.reruns == [] and ci.rounds(config.load(wt), "gh-9") == 0


def test_cancelled_run_superseded_by_a_newer_one_counts_the_newer(
    wt, github, monkeypatch
):
    old = run_(conclusion="cancelled")
    new = {**run_(), "databaseId": 12}
    install(monkeypatch, github, [[old, new]])
    assert watch(wt).status == "green"


def test_cancelled_run_waits_for_a_replacement(wt, github, monkeypatch):
    old = run_(conclusion="cancelled")
    new = {**run_(), "databaseId": 12}
    install(monkeypatch, github, [[old], [old], [new, old]])
    slept = []
    r = ci.watch(config.load(wt), wt, timeout_s=3600, sleep=slept.append)
    assert r.status == "green" and len(slept) == 2


@pytest.mark.parametrize(
    "status,conclusion",
    [("completed", "action_required"), ("completed", "startup_failure"),
     ("completed", "stale"), ("waiting", "")],
)  # fmt: skip
def test_runs_that_never_tested_the_code_block_without_a_round(
    wt, github, monkeypatch, status, conclusion
):
    fake = install(monkeypatch, github, [[run_(status=status, conclusion=conclusion)]])
    r = watch(wt)
    assert r.status == "blocked" and "no round counted" in r.lines[0]
    assert fake.reruns == [] and ci.rounds(config.load(wt), "gh-9") == 0


def test_red_external_check_is_not_green_and_no_round(wt, github, monkeypatch):
    ext = {"name": "codecov/patch", "state": "FAILURE", "bucket": "fail",
           "link": "https://codecov/x", "workflow": ""}  # fmt: skip
    actions = {"name": "harness", "state": "SUCCESS", "bucket": "pass", "link": "",
               "workflow": "ci"}  # fmt: skip
    fake = install(monkeypatch, github, [[run_()]], checks=[actions, ext])
    r = watch(wt)
    assert r.status == "blocked" and any("codecov/patch" in ln for ln in r.lines)
    assert fake.reruns == [] and ci.rounds(config.load(wt), "gh-9") == 0


def test_pending_external_check_is_not_green(wt, github, monkeypatch):
    ext = {"name": "deploy-preview", "state": "PENDING", "bucket": "pending",
           "link": "", "workflow": ""}  # fmt: skip
    install(monkeypatch, github, [[run_()]], checks=[ext])
    assert watch(wt).status == "pending"


def test_runs_of_another_branch_at_the_same_commit_are_ignored(wt, github, monkeypatch):
    other = {**run_(conclusion="failure"), "databaseId": 12, "headBranch": "elsewhere",
             "workflowName": "other"}  # fmt: skip
    branch = task._git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    mine = {**run_(), "headBranch": branch}
    install(monkeypatch, github, [[mine, other]])
    assert watch(wt).status == "green"


@pytest.mark.parametrize("fails", ["cat-file", "ls-remote"])
def test_unreadable_round_counter_refuses_instead_of_resetting(
    wt, github, monkeypatch, fails
):
    # a network error must not read as "no rounds yet"
    cfg = config.load(wt)
    ci._write(cfg, "gh-9", None, ci.State(rounds=["a"]))
    real = ci.claims._git

    def broken(cfg, *argv, **kw):
        if argv[0] == fails and "-e" not in argv:
            return subprocess.CompletedProcess(argv, 128, "", "network down")
        return real(cfg, *argv, **kw)

    monkeypatch.setattr(ci.claims, "_git", broken)
    install(monkeypatch, github, [[run_(conclusion="failure")]])
    with pytest.raises(ci.claims.ClaimError, match="unreadable.*network down"):
        watch(wt)
    monkeypatch.setattr(ci.claims, "_git", real)
    assert ci.rounds(cfg, "gh-9") == 1


@pytest.mark.parametrize(
    "code,out,err,expected",
    [(8, '[{"name":"a","bucket":"pending","workflow":""}]', "", 1),
     (1, "", "no checks reported on the 'x' branch", 0),
     (1, "", "no pull requests found for branch \"x\"", 0)],
)  # fmt: skip
def test_pr_checks_reads_json_on_nonzero_exit(
    monkeypatch, tmp_path, code, out, err, expected
):
    monkeypatch.setattr(
        gh, "run", lambda cwd, *argv: subprocess.CompletedProcess(argv, code, out, err)
    )
    assert len(gh.pr_checks(tmp_path, "x")) == expected


def test_pr_checks_raises_on_other_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(
        gh,
        "run",
        lambda cwd, *argv: subprocess.CompletedProcess(argv, 1, "", "HTTP 502"),
    )
    with pytest.raises(gh.GhError, match="HTTP 502"):
        gh.pr_checks(tmp_path, "x")


def _artifact(monkeypatch, calls, *, last=True, fail_log="TypeError: x is undefined\n"):
    """A fake `gh run download` that unpacks what check.yml uploads: last.json and the
    run directory of step logs."""

    def download(cwd, rid, name, dest):
        calls.append((rid, name))
        run = dest / "20261103-202148-1-abc"
        run.mkdir(parents=True)
        (run / "ts.log").write_text("$ knip\n" + fail_log, encoding="utf-8")
        (run / "rust.log").write_text("all fine\n", encoding="utf-8")
        if last:
            steps = [{"stack": "ts", "kind": "deadcode", "status": "fail",
                      "log": ".artifacts/check/20261103-202148-1-abc/ts.log"},
                     {"stack": "rust", "kind": "test", "status": "ok",
                      "log": ".artifacts/check/20261103-202148-1-abc/rust.log"}]  # fmt: skip
            (dest / "last.json").write_text(
                json.dumps({"steps": steps}), encoding="utf-8"
            )

    monkeypatch.setattr(gh, "run_download", download)


def _lost(*a):
    raise gh.GhError("gh run view 11: log not found: 126167412650")


@pytest.mark.parametrize("job_log", ["lost", "empty"])
def test_a_lost_job_log_takes_the_step_logs_artifact(wt, github, monkeypatch, job_log):
    install(monkeypatch, github, [[run_(conclusion="failure")]])
    monkeypatch.setattr(
        gh, "run_log_failed", _lost if job_log == "lost" else lambda c, r: ""
    )
    calls: list = []
    _artifact(monkeypatch, calls)
    r = watch(wt)
    assert r.status == "red" and calls == [(11, "h-check-logs-1")]
    text = (wt / r.log).read_text(encoding="utf-8")
    assert "step logs from artifact h-check-logs-1" in text
    assert (
        "TypeError: x is undefined" in text and "all fine" not in text
    )  # failed steps only
    assert any(ln.startswith("job log unavailable for run 11") for ln in r.lines)
    assert "ci-triage" in r.lines[-1]
    assert ci.rounds(config.load(wt), "gh-9") == 1  # the code failed: a round


def test_a_cancelled_job_without_last_json_gives_every_step_log(
    wt, github, monkeypatch
):
    install(monkeypatch, github, [[run_(conclusion="failure")]])
    monkeypatch.setattr(gh, "run_log_failed", _lost)
    _artifact(monkeypatch, [], last=False)
    text = (wt / watch(wt).log).read_text(encoding="utf-8")
    assert "TypeError: x is undefined" in text and "all fine" in text


def test_the_artifact_of_the_failed_attempt_is_taken(wt, github, monkeypatch):
    install(monkeypatch, github, [[{**run_(conclusion="failure"), "attempt": 2}]])
    monkeypatch.setattr(gh, "run_log_failed", _lost)
    calls: list = []
    _artifact(monkeypatch, calls)
    watch(wt)
    assert calls == [(11, "h-check-logs-2")]


def test_a_second_watch_downloads_again_over_its_own_copy(wt, github, monkeypatch):
    install(monkeypatch, github, [[run_(conclusion="failure")]])
    monkeypatch.setattr(gh, "run_log_failed", _lost)
    calls: list = []
    _artifact(monkeypatch, calls)
    watch(wt)
    assert watch(wt).status == "red" and len(calls) == 2


def test_no_job_log_and_no_artifact_is_red_with_the_reason(
    wt, github, monkeypatch, capsys
):
    install(monkeypatch, github, [[run_(conclusion="failure")]])
    monkeypatch.setattr(gh, "run_log_failed", _lost)

    def no_artifact(cwd, rid, name, dest):
        raise gh.GhError(f"gh run download {rid} -n {name}: no artifact matches")

    monkeypatch.setattr(gh, "run_download", no_artifact)
    real = ci.watch  # through the CLI, without its 45-minute wait for the rerun
    monkeypatch.setattr(ci, "watch", lambda cfg, start: real(cfg, start, timeout_s=0,
                                                            sleep=lambda s: None))  # fmt: skip
    assert cli.main(["-C", str(wt), "ci", "watch"]) == 1  # red, never 0 or blocked's 2
    out = capsys.readouterr().out
    assert "log unavailable for run 11: gh run view 11: log not found" in out
    assert "no artifact matches" in (wt / ".artifacts/ci/round-1.log").read_text(
        encoding="utf-8"
    )
    assert "nothing to triage" in out and "gh run rerun 11 --failed" in out
    assert ci.rounds(config.load(wt), "gh-9") == 1


def test_a_gh_failure_in_watch_has_its_own_exit_code(wt, github, monkeypatch, capsys):
    install(monkeypatch, github, [[run_()]])

    def down(*a):
        raise gh.GhError("gh run list: HTTP 502")

    monkeypatch.setattr(gh, "runs_for_commit", down)
    assert cli.main(["-C", str(wt), "ci", "watch"]) == cli.WATCH_GH_ERROR
    assert "CI state unknown" in capsys.readouterr().err


def test_a_cancelled_run_names_its_step_logs(wt, github, monkeypatch):
    install(monkeypatch, github, [[{**run_(conclusion="cancelled"), "attempt": 3}]])
    r = watch(wt)
    assert "step logs: gh run download 11 -n h-check-logs-3" in r.lines


def test_step_logs_stay_inside_the_downloaded_artifact(tmp_path):
    """A last.json naming `../` must not read a file outside the artifact."""
    dest = tmp_path / "art"
    run = dest / "20261103-1"
    run.mkdir(parents=True)
    (run / "py.log").write_text("ok\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("token\n", encoding="utf-8")
    steps = [{"status": "fail", "log": ".artifacts/check/../secret.txt"}]
    (dest / "last.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")
    assert ci._step_logs(dest) == [run / "py.log"]  # falls back to the run's own logs

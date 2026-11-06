import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import git, out

from harness import cards, claims, cli, config, done, epics, lock, task

TRIVIAL = cards.render({
    "Problem": "p", "Assumptions": "none", "Verify": "h check", "Size": "S",
    "Trivial": "yes", "Trivial reason": "test",
})  # fmt: skip


@pytest.fixture
def merged_task(world, github):
    """gh-N with milestone `auth`, worked in a worktree, pushed, then squash-merged."""
    _origin, a, b = world
    with (a / "harness.toml").open("a", encoding="utf-8") as f:
        f.write('\n[deploy]\nworkflow = "deploy.yml"\n')
    epic = a / epics.EPICS_DIR / "auth.md"
    epic.parent.mkdir(parents=True)
    epic.write_text(epics.TEMPLATE.format(slug="auth", title="t", start=epics.START, end=epics.END),
                    encoding="utf-8")  # fmt: skip
    git(a, "add", ".")
    git(a, "commit", "-qm", "setup")
    git(a, "push", "-q", "origin", "main")
    n = github.add_issue(TRIVIAL, ["ac-confirmed"])
    github.issues[n]["milestone"] = {"title": "auth"}
    cfg = config.load(a)
    r = task.start(cfg, f"gh-{n}")
    (r.path / "feature.txt").write_text("x\n", encoding="utf-8")
    git(r.path, "add", "feature.txt")
    git(r.path, "commit", "-qm", "feat")
    git(r.path, "push", "-q", "origin", r.branch)
    git(b, "pull", "-q", "--ff-only", "origin", "main")
    git(b, "fetch", "-q", "origin", r.branch)
    git(b, "merge", "-q", "--squash", f"origin/{r.branch}")
    git(b, "commit", "-qm", "squash")
    git(b, "push", "-q", "origin", "main")
    sha = out(b, "rev-parse", "HEAD")
    github.prs.append({"number": 7, "state": "MERGED", "headRefName": r.branch,
                       "mergeCommit": {"oid": sha}, "url": "https://x/pull/7", "auto": False})  # fmt: skip
    return cfg, n, r, sha, github


def run_done(cfg, n):
    return done.done(cfg, f"gh-{n}", timeout_s=0, sleep=lambda s: None)


def test_closes_after_deploy_cleans_and_ticks_epic(merged_task):
    cfg, n, r, sha, github = merged_task
    github.runs[sha] = {
        "status": "completed",
        "conclusion": "success",
        "url": "https://x/run/1",
        "event": "push",
    }
    rep = run_done(cfg, n)
    assert rep.closed and rep.stage == "closed", rep.lines
    assert github.issues[n]["state"] == "CLOSED"
    assert "https://x/run/1" in github.closed_with[n]
    assert not r.path.exists() and claims.read(cfg, f"gh-{n}") is None
    assert done.leftovers(cfg, f"gh-{n}", r.branch) == []
    epic = out(cfg.root, "show", "origin/chore/epic-auth:docs/epics/auth.md")
    assert f"- [x] #{n} Login with Google — Done" in epic
    assert github.prs[-1]["auto"] is True


def bury(github, count=150):
    """`count` newer PRs of other branches on top of the task's, as in a busy repo."""
    top = max(p["number"] for p in github.prs)
    for k in range(1, count + 1):
        github.prs.append({"number": top + k, "state": "MERGED", "headRefName": f"chore/x-{k}",
                           "mergeCommit": None, "url": f"https://x/pull/{top + k}", "auto": False})  # fmt: skip


def test_finds_the_pr_of_an_old_task_past_100_newer_prs(merged_task):
    cfg, n, _r, sha, github = merged_task
    bury(github)
    github.runs[sha] = {"status": "completed", "conclusion": "success",
                        "url": "https://x/run/1", "event": "push"}  # fmt: skip
    rep = run_done(cfg, n)
    assert rep.closed, rep.lines
    assert "merged: https://x/pull/7" in "\n".join(rep.lines)


def test_fresh_merge_not_yet_in_search_is_found_among_recent_prs(merged_task):
    cfg, n, _r, _sha, github = merged_task
    github.unindexed.add(7)
    rep = run_done(cfg, n)
    assert "merged: https://x/pull/7" in "\n".join(rep.lines), rep.lines


def test_not_merged_changes_nothing(world, github):
    _, a, _ = world
    n = github.add_issue(TRIVIAL, ["ac-confirmed"])
    rep = run_done(config.load(a), n)
    assert not rep.closed and rep.stage == "not-merged"
    assert github.issues[n]["state"] == "OPEN"


@pytest.mark.parametrize(("run", "stage"), [
    (None, "deploy-pending"),
    ({"status": "in_progress", "conclusion": "", "url": "u", "event": "push"}, "deploy-pending"),
    ({"status": "completed", "conclusion": "failure", "url": "u", "event": "push"}, "deploy-failed"),
])  # fmt: skip
def test_deploy_gate(merged_task, run, stage):
    cfg, n, r, sha, github = merged_task
    if run:
        github.runs[sha] = run
    rep = run_done(cfg, n)
    assert not rep.closed and rep.stage == stage
    assert "merged" in github.issues[n]["labels"]
    assert r.path.exists()  # nothing cleaned before the deploy is proven


def test_leftover_blocks_closing_then_rerun_finishes(merged_task, monkeypatch):
    cfg, n, r, sha, github = merged_task
    github.runs[sha] = {
        "status": "completed",
        "conclusion": "success",
        "url": "u",
        "event": "push",
    }
    real = task._remove_dir
    monkeypatch.setattr(task, "_remove_dir", lambda p: False)  # Windows keeps the dir
    real_git = task._git

    def keep_dir(cwd, *argv):
        if argv[:2] == ("worktree", "remove"):
            res = real_git(cwd, "worktree", "remove", "--force", argv[2])
            r.path.mkdir(exist_ok=True)
            (r.path / "locked.bin").write_text("x", encoding="utf-8")
            return res
        return real_git(cwd, *argv)

    monkeypatch.setattr(task, "_git", keep_dir)
    rep = run_done(cfg, n)
    assert not rep.closed and rep.stage == "cleanup-blocked"
    assert "cleanup-blocked" in github.issues[n]["labels"]
    assert any("Remove-Item" in m or "rm -rf" in m for m in rep.manual)
    assert github.issues[n]["state"] == "OPEN"

    monkeypatch.setattr(task, "_git", real_git)
    monkeypatch.setattr(task, "_remove_dir", real)
    (r.path / "locked.bin").unlink()  # the human did the manual step
    r.path.rmdir()
    rep = run_done(cfg, n)
    assert rep.closed, rep.lines
    assert "cleanup-blocked" not in github.issues[n]["labels"]


def test_epic_lines_and_status(world, github):
    _, a, _ = world
    github.milestones.append("auth")
    n1 = github.add_issue(TRIVIAL, ["ac-confirmed"], title="A", state="CLOSED")
    n2 = github.add_issue(TRIVIAL, ["ac-confirmed", "in-progress"], title="B")
    n3 = github.add_issue(
        TRIVIAL, ["ac-confirmed", "deployed", "cleanup-blocked"], title="C"
    )
    for n in (n1, n2, n3):
        github.issues[n]["milestone"] = {"title": "auth"}
    text = epics.TEMPLATE.format(
        slug="auth", title="t", start=epics.START, end=epics.END
    )
    synced = epics.sync_text(config.load(a), "auth", text)
    assert f"- [x] #{n1} A — Done" in synced
    assert f"- [ ] #{n2} B — In progress" in synced
    assert f"- [ ] #{n3} C — Deployed, cleanup blocked" in synced
    assert epics.sync_text(config.load(a), "auth", synced) == synced
    [(slug, counts)] = epics.status(config.load(a))
    assert slug == "auth" and counts == {
        "Done": 1,
        "In progress": 1,
        "Cleanup blocked": 1,
    }


def test_second_publish_reuses_the_epic_pr(merged_task):
    # Two tasks of one epic closing back to back must not open two conflicting PRs.
    cfg, n, _r, _sha, github = merged_task
    url1 = epics.publish_sync(cfg, "auth")
    github.issues[n]["state"] = "CLOSED"
    url2 = epics.publish_sync(cfg, "auth")
    assert url1 == url2
    assert sum(p["headRefName"] == "chore/epic-auth" for p in github.prs) == 1
    assert f"- [x] #{n}" in out(
        cfg.root, "show", "origin/chore/epic-auth:docs/epics/auth.md"
    )


def test_cancelled_run_superseded_by_a_green_later_one(merged_task):
    cfg, n, _r, sha, github = merged_task
    github.runs[sha] = {
        "status": "completed",
        "conclusion": "cancelled",
        "url": "u/1",
        "event": "push",
    }
    main = task.main_root(cfg)
    git(main, "pull", "-q", "--ff-only", "origin", "main")
    (main / "later.txt").write_text("x\n", encoding="utf-8")
    git(main, "add", "later.txt")
    git(main, "commit", "-qm", "later")
    later = out(main, "rev-parse", "HEAD")
    github.runs[later] = {
        "status": "completed",
        "conclusion": "success",
        "url": "u/2",
        "event": "push",
    }
    rep = run_done(cfg, n)
    assert rep.closed, rep.lines
    assert any("superseded run on" in line for line in rep.lines)


def test_a_later_run_that_skipped_its_check_is_no_verdict(merged_task):
    """a docs-only push skips `check`; it never tested the earlier merge."""
    cfg, n, _r, sha, github = merged_task
    github.runs[sha] = {"databaseId": 50, "status": "completed", "conclusion": "cancelled",
                        "url": "u/50", "event": "push", "jobs": []}  # fmt: skip
    main = task.main_root(cfg)
    git(main, "pull", "-q", "--ff-only", "origin", "main")
    (main / "later.md").write_text("x\n", encoding="utf-8")
    git(main, "add", "later.md")
    git(main, "commit", "-qm", "docs")
    later = out(main, "rev-parse", "HEAD")
    github.runs[later] = {"databaseId": 51, "status": "completed", "conclusion": "success",
                          "url": "u/51", "event": "push",
                          "jobs": [{"name": "check / changes", "conclusion": "success"},
                                   {"name": "check / check", "conclusion": "skipped"},
                                   {"name": "required", "conclusion": "success"}]}  # fmt: skip
    rep = run_done(cfg, n)
    assert not rep.closed and rep.stage == "deploy-pending", rep.lines


def test_unreadable_jobs_of_a_later_run_vouch_for_nothing(merged_task, monkeypatch):
    cfg, n, _r, sha, github = merged_task
    github.runs[sha] = {"databaseId": 52, "status": "completed", "conclusion": "cancelled",
                        "url": "u/52", "event": "push", "jobs": []}  # fmt: skip
    main = task.main_root(cfg)
    git(main, "pull", "-q", "--ff-only", "origin", "main")
    (main / "later.txt").write_text("x\n", encoding="utf-8")
    git(main, "add", "later.txt")
    git(main, "commit", "-qm", "later")
    github.runs[out(main, "rev-parse", "HEAD")] = {
        "databaseId": 53, "status": "completed", "conclusion": "success", "url": "u/53",
        "event": "push"}  # fmt: skip

    def down(*a):
        raise done.gh.GhError("gh run view 53: HTTP 502")

    monkeypatch.setattr(done.gh, "run_skipped_check", down)
    rep = run_done(cfg, n)
    assert not rep.closed and rep.stage == "deploy-pending", rep.lines


def test_a_docs_only_merge_closes_on_its_skipped_run(merged_task):
    cfg, n, _r, sha, github = merged_task
    github.runs[sha] = {"databaseId": 54, "status": "completed", "conclusion": "success",
                        "url": "u/54", "event": "push",
                        "jobs": [{"name": "check / check", "conclusion": "skipped"}]}  # fmt: skip
    rep = run_done(cfg, n)
    assert rep.closed, rep.lines
    assert any(line.startswith("check skipped: no code changed") for line in rep.lines)


REUSED_JOBS = [
    {"name": "check / changes", "conclusion": "success",
     "steps": [{"name": "Reuse the PR check", "conclusion": "success"}]},
    {"name": "check / check", "conclusion": "skipped"},
]  # fmt: skip


def test_a_merge_whose_run_reused_the_pr_check_closes_as_tested(merged_task):
    """main took the PR's green check of the same tree — a verdict, not a skip."""
    cfg, n, _r, sha, github = merged_task
    github.runs[sha] = {"databaseId": 55, "status": "completed", "conclusion": "success",
                        "url": "u/55", "event": "push", "jobs": REUSED_JOBS}  # fmt: skip
    rep = run_done(cfg, n)
    assert rep.closed, rep.lines
    assert any(
        line.startswith("check reused: main took the PR's") for line in rep.lines
    )
    assert not any(line.startswith("check skipped") for line in rep.lines)


def test_a_later_run_that_reused_a_pr_check_is_a_verdict(merged_task):
    cfg, n, _r, sha, github = merged_task
    github.runs[sha] = {"databaseId": 56, "status": "completed", "conclusion": "cancelled",
                        "url": "u/56", "event": "push", "jobs": []}  # fmt: skip
    main = task.main_root(cfg)
    git(main, "pull", "-q", "--ff-only", "origin", "main")
    (main / "later.txt").write_text("x\n", encoding="utf-8")
    git(main, "add", "later.txt")
    git(main, "commit", "-qm", "later")
    github.runs[out(main, "rev-parse", "HEAD")] = {
        "databaseId": 57, "status": "completed", "conclusion": "success", "url": "u/57",
        "event": "push", "jobs": REUSED_JOBS}  # fmt: skip
    rep = run_done(cfg, n)
    assert rep.closed, rep.lines


def test_cancelled_without_a_later_run_is_pending_not_failed(merged_task):
    cfg, n, _r, sha, github = merged_task
    github.runs[sha] = {
        "status": "completed",
        "conclusion": "cancelled",
        "url": "u/1",
        "event": "push",
    }
    rep = run_done(cfg, n)
    assert not rep.closed and rep.stage == "deploy-pending"


def test_cancelled_after_a_job_failed_is_a_failure_at_once(merged_task):
    # a main run: Windows hit timeout-minutes, `required` failed,
    # the run reads "cancelled"; done must not wait half an hour for a newer run.
    cfg, n, _r, sha, github = merged_task
    github.runs[sha] = {
        "databaseId": 7,
        "status": "completed",
        "conclusion": "cancelled",
        "url": "u/1",
        "event": "push",
        "jobs": [{"conclusion": "cancelled"}, {"conclusion": "success"},
                 {"conclusion": "failure"}],
    }  # fmt: skip

    def no_waiting(_s):
        raise AssertionError("done waited on a run that already failed")

    rep = done.done(cfg, f"gh-{n}", timeout_s=3600, sleep=no_waiting)
    assert not rep.closed and rep.stage == "deploy-failed", rep.lines
    assert any("time limit" in line and "u/1" in line for line in rep.lines)


def test_cancelled_by_the_time_limit_is_a_rerun_not_a_revert(merged_task):
    """A real run: `required` has a failed step, the check job a
    cancelled one, so this is no lost runner; no check judged the commit."""
    cfg, n, _r, sha, github = merged_task
    github.runs[sha] = {"databaseId": 8, "status": "completed", "conclusion": "cancelled",
                        "url": "u/8", "event": "push",
                        "jobs": [{"conclusion": "cancelled", "steps": [
                                     {"name": "h check", "conclusion": "cancelled"}]},
                                 {"conclusion": "failure", "steps": [
                                     {"name": "All required", "conclusion": "failure"}]}]}  # fmt: skip
    rep = run_done(cfg, n)
    assert rep.stage == "deploy-failed" and not rep.closed
    assert any("before a verdict" in line for line in rep.lines)
    assert any("gh run rerun 8 --failed" in m for m in rep.manual)
    assert not any("h revert" in m for m in rep.manual)


def test_cancelled_in_the_queue_still_waits_for_a_newer_run(merged_task):
    cfg, n, _r, sha, github = merged_task
    github.runs[sha] = {
        "databaseId": 8,
        "status": "completed",
        "conclusion": "cancelled",
        "url": "u/1",
        "event": "push",
        "jobs": [],
    }
    rep = run_done(cfg, n)
    assert not rep.closed and rep.stage == "deploy-pending"


def test_later_run_not_containing_the_commit_does_not_count(merged_task):
    cfg, n, _r, sha, github = merged_task
    github.runs[sha] = {
        "status": "completed",
        "conclusion": "cancelled",
        "url": "u/1",
        "event": "push",
    }
    github.runs["0" * 40] = {
        "status": "completed",
        "conclusion": "success",
        "url": "u/x",
        "event": "push",
    }
    rep = run_done(cfg, n)
    assert not rep.closed and rep.stage == "deploy-pending"


def test_epic_pr_gets_the_review_check(merged_task):
    """Generated epic lists carry no code: the required review check must not hold them."""
    from harness import prreview

    cfg, _n, _r, _sha, github = merged_task
    epics.publish_sync(cfg, "auth")
    head = out(cfg.root, "rev-parse", "origin/chore/epic-auth")
    st = github.statuses[head]
    assert st["context"] == prreview.STATUS_CONTEXT and "epic file" in st["description"]


@pytest.fixture
def squash_after_neighbour(world, github):
    """a neighbour PR edits the lines next to the task's; GitHub's squash commit
    then differs from the branch, and no patch of the branch is found in base."""
    _, a, b = world
    with (a / "harness.toml").open("a", encoding="utf-8") as f:
        f.write('\n[deploy]\nworkflow = "deploy.yml"\n')
    (a / "mod.txt").write_text("1\n2\n3\n4\n5\n", encoding="utf-8")
    git(a, "add", ".")
    git(a, "commit", "-qm", "setup")
    git(a, "push", "-q", "origin", "main")
    n = github.add_issue(TRIVIAL, ["ac-confirmed"])
    cfg = config.load(a)
    r = task.start(cfg, f"gh-{n}")
    (r.path / "mod.txt").write_text("1\n2\n3\nfour\n5\n", encoding="utf-8")
    git(r.path, "commit", "-qam", "feat")
    git(r.path, "push", "-q", "origin", r.branch)
    head = out(r.path, "rev-parse", "HEAD")
    git(b, "pull", "-q", "--ff-only", "origin", "main")
    (b / "mod.txt").write_text("1\ntwo\n3\n4\n5\n", encoding="utf-8")
    git(b, "commit", "-qam", "neighbour")
    git(b, "fetch", "-q", "origin", r.branch)
    git(b, "merge", "-q", "--squash", f"origin/{r.branch}")
    git(b, "commit", "-qm", "squash")
    git(b, "push", "-q", "origin", "main")
    sha = out(b, "rev-parse", "HEAD")
    github.prs.append({"number": 7, "state": "MERGED", "headRefName": r.branch,
                       "headRefOid": head, "mergeCommit": {"oid": sha},
                       "url": "https://x/pull/7", "auto": False})  # fmt: skip
    github.runs[sha] = {"status": "completed", "conclusion": "success",
                        "url": "u", "event": "push"}  # fmt: skip
    git(a, "fetch", "-q", "origin", "main")
    assert task.merge_state(cfg, r.branch) == "unmerged"  # what once blocked a task
    return cfg, n, r, github


def test_squash_that_differs_from_the_branch_is_cleaned(squash_after_neighbour):
    cfg, n, r, github = squash_after_neighbour
    rep = run_done(cfg, n)
    assert rep.closed and rep.stage == "closed", rep.lines
    assert not r.path.exists() and claims.read(cfg, f"gh-{n}") is None
    assert r.branch not in out(cfg.root, "branch", "--list", r.branch)
    assert github.issues[n]["state"] == "CLOSED"


def test_commit_outside_the_merged_pr_keeps_the_worktree(squash_after_neighbour):
    cfg, n, r, github = squash_after_neighbour
    (r.path / "late.txt").write_text("not in the PR\n", encoding="utf-8")
    git(r.path, "add", "late.txt")
    git(r.path, "commit", "-qm", "late")
    rep = run_done(cfg, n)
    assert not rep.closed and rep.stage == "cleanup-blocked"
    assert any("outside the merged PR" in ln for ln in rep.lines), rep.lines
    assert (r.path / "late.txt").is_file()
    assert github.issues[n]["state"] == "OPEN"


def test_concurrent_publishes_of_one_epic_both_land(merged_task):
    # two sessions syncing one epic at once removed each other's worktree.
    cfg, n, _r, _sha, github = merged_task
    github.issues[n]["state"] = "CLOSED"
    with ThreadPoolExecutor(2) as pool:
        runs = [pool.submit(epics.publish_sync, cfg, "auth") for _ in range(2)]
    for r in runs:
        r.result()  # re-raises what the publish raised
    assert sum(p["headRefName"] == "chore/epic-auth" for p in github.prs) == 1
    assert f"- [x] #{n}" in out(
        cfg.root, "show", "origin/chore/epic-auth:docs/epics/auth.md"
    )


def _left_publish(cfg, branch, suffix="0badc0de"):
    """A worktree a killed publish of `branch` left behind."""
    path = task.common_dir(cfg) / "harness" / f"pub-{branch.replace('/', '-')}-{suffix}"
    git(task.main_root(cfg), "worktree", "add", "-q", "--detach", str(path), "HEAD")
    return path


def test_publish_removes_worktrees_its_branch_left(merged_task):
    cfg, _n, _r, _sha, _github = merged_task
    own = _left_publish(cfg, "chore/epic-auth")
    new = _left_publish(cfg, "chore/epic-auth-new")
    other = _left_publish(cfg, "chore/epic-billing")
    assert epics.publish_sync(cfg, "auth")
    assert not own.exists()
    assert new.is_dir() and other.is_dir()
    assert str(own) not in out(cfg.root, "worktree", "list")


def test_doctor_shows_and_cleans_leftovers_but_not_a_live_publish(merged_task, capsys):
    cfg, _n, _r, _sha, _github = merged_task
    a = _left_publish(cfg, "chore/epic-auth")
    b = _left_publish(cfg, "chore/epic-billing")
    live = _left_publish(cfg, "chore/epic-ops")
    held = lock.try_acquire(1, epics._lock_name(task.common_dir(cfg), "chore-epic-ops"))
    assert held is not None
    try:
        cli.main(["-C", str(cfg.root), "doctor"])
        text = capsys.readouterr().out
        assert "2 worktree(s) left by killed epic publishes" in text
        assert "h doctor --clean" in text and str(live) not in text
        assert a.is_dir() and b.is_dir()
        cli.main(["-C", str(cfg.root), "doctor", "--clean", "--json"])
        report = json.loads(capsys.readouterr().out)
    finally:
        lock.release(held[1])
    states = {Path(e["path"]).name: e["state"] for e in report["stale_publish"]}
    assert states == {a.name: "removed", b.name: "removed", live.name: "in use"}
    assert not a.exists() and not b.exists() and live.is_dir()


def test_red_main_offers_the_revert(merged_task):
    """heavy checks run after the merge; red main is reverted, not closed over."""
    cfg, n, _r, sha, github = merged_task
    github.runs[sha] = {"status": "completed", "conclusion": "failure", "url": "run/5",
                        "event": "push"}  # fmt: skip
    rep = run_done(cfg, n)
    assert rep.stage == "deploy-failed" and not rep.closed
    assert any(f"h revert {sha[:10]} --run run/5" in m for m in rep.manual)


def test_the_init_ci_workflow_is_the_deploy_without_one(merged_task):
    cfg, n, _r, _sha, _github = merged_task
    cfg.deploy_workflow = ""
    flow = cfg.root / ".github" / "workflows" / "harness.yml"
    flow.parent.mkdir(parents=True)
    flow.write_text("name: harness" + chr(10), encoding="utf-8")
    rep = run_done(cfg, n)
    assert rep.stage != "deploy-unconfigured"
    assert cfg.deploy_workflow == "harness.yml"


def test_runner_lost_mid_job_is_a_rerun_not_a_revert(merged_task):
    """a failed job with no failed step — the runner went offline."""
    cfg, n, _r, sha, github = merged_task
    github.runs[sha] = {"databaseId": 11, "status": "completed", "conclusion": "failure",
                        "url": "run/11", "event": "push",
                        "jobs": [{"conclusion": "failure", "steps": [
                            {"name": "h check", "conclusion": None},
                            {"name": "Set up job", "conclusion": "success"}]}]}  # fmt: skip
    rep = run_done(cfg, n)
    assert rep.stage == "deploy-failed" and not rep.closed
    assert any("runner went offline" in line for line in rep.lines)
    assert any("gh run rerun 11 --failed" in m for m in rep.manual)
    assert not any("h revert" in m for m in rep.manual)


def test_red_superseding_run_does_not_blame_this_pr(merged_task):
    """the newer run holds later merges; rerun this merge's own run instead."""
    cfg, n, _r, sha, github = merged_task
    github.runs[sha] = {"databaseId": 20, "status": "completed", "conclusion": "cancelled",
                        "url": "run/20", "event": "push", "jobs": []}  # fmt: skip
    main = task.main_root(cfg)
    git(main, "pull", "-q", "--ff-only", "origin", "main")
    (main / "later.txt").write_text("x\n", encoding="utf-8")
    git(main, "add", "later.txt")
    git(main, "commit", "-qm", "later")
    later = out(main, "rev-parse", "HEAD")
    github.runs[later] = {"databaseId": 21, "status": "completed", "conclusion": "failure",
                          "url": "run/21", "event": "push",
                          "jobs": [{"conclusion": "failure", "steps": [
                              {"name": "h check", "conclusion": "failure"}]}]}  # fmt: skip
    rep = run_done(cfg, n)
    assert rep.stage == "deploy-failed" and not rep.closed
    assert any("holds later merges" in line for line in rep.lines)
    assert any("gh run rerun 20" in m for m in rep.manual)
    assert not any("h revert" in m for m in rep.manual)


def test_own_red_run_with_a_failed_step_offers_the_revert(merged_task):
    cfg, n, _r, sha, github = merged_task
    github.runs[sha] = {"databaseId": 30, "status": "completed", "conclusion": "failure",
                        "url": "run/30", "event": "push",
                        "jobs": [{"conclusion": "failure", "steps": [
                            {"name": "h check", "conclusion": "failure"}]}]}  # fmt: skip
    rep = run_done(cfg, n)
    assert any(f"h revert {sha[:10]} --run run/30" in m for m in rep.manual)


def test_unreadable_jobs_fall_back_to_the_revert(merged_task, monkeypatch):
    cfg, n, _r, sha, github = merged_task
    github.runs[sha] = {"databaseId": 40, "status": "completed", "conclusion": "failure",
                        "url": "run/40", "event": "push"}  # fmt: skip

    def down(*a):
        raise done.gh.GhError("gh run view 40: HTTP 502")

    monkeypatch.setattr(done.gh, "run_lost_runner", down)
    rep = run_done(cfg, n)
    assert any(f"h revert {sha[:10]} --run run/40" in m for m in rep.manual)

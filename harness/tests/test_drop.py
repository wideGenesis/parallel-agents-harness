import pytest
from conftest import git, out

from harness import cards, claims, cli, config, drop, epics, task

TRIVIAL = cards.render({
    "Problem": "p", "Assumptions": "none", "Verify": "h check", "Size": "S",
    "Trivial": "yes", "Trivial reason": "test",
})  # fmt: skip


def test_drop_untouched_card(world, github):
    _, a, _ = world
    n = github.add_issue(TRIVIAL, ["ac-confirmed", "ready"])
    rep = drop.drop(config.load(a), f"gh-{n}", "not needed")
    assert not rep.manual
    i = github.issues[n]
    assert i["state"] == "CLOSED" and i["state_reason"] == "not planned"
    assert "Dropped: not needed" in github.closed_with[n]
    assert "dropped" in i["labels"] and "ready" not in i["labels"]


def test_drop_in_progress_closes_pr_and_cleans_but_keeps_remote_branch(world, github):
    _origin, a, _ = world
    cfg = config.load(a)
    n = github.add_issue(TRIVIAL, ["ac-confirmed"])
    r = task.start(cfg, f"gh-{n}")
    (r.path / "f.txt").write_text("x\n", encoding="utf-8")
    git(r.path, "add", "f.txt")
    git(r.path, "commit", "-qm", "wip")
    git(r.path, "push", "-q", "origin", r.branch)
    github.prs.append({"number": 9, "state": "OPEN", "headRefName": r.branch,
                       "mergeCommit": None, "url": "https://x/pull/9", "auto": False})  # fmt: skip

    rep = drop.drop(cfg, f"gh-{n}", "superseded")
    assert not rep.manual, rep.lines
    assert github.prs[-1]["state"] == "CLOSED"
    assert claims.read(cfg, f"gh-{n}") is None
    assert not r.path.exists()
    assert out(a, "branch", "--list", "task/*") == ""
    assert out(
        a, "ls-remote", "--heads", "origin", r.branch
    )  # work can be picked up again


def test_drop_closes_every_open_pr_of_the_task_however_old(world, github):
    _, a, _ = world
    cfg = config.load(a)
    n = github.add_issue(TRIVIAL, ["ac-confirmed"])
    github.prs.append({"number": 3, "state": "OPEN", "headRefName": f"task/gh-{n}-first",
                       "mergeCommit": None, "url": "https://x/pull/3", "auto": False})  # fmt: skip
    for k in range(4, 154):
        github.prs.append({"number": k, "state": "MERGED", "headRefName": f"chore/x-{k}",
                           "mergeCommit": None, "url": f"https://x/pull/{k}", "auto": False})  # fmt: skip
    github.prs.append({"number": 154, "state": "OPEN", "headRefName": f"task/gh-{n}",
                       "mergeCommit": None, "url": "https://x/pull/154", "auto": False})  # fmt: skip
    drop.drop(cfg, f"gh-{n}", "superseded")
    assert {p["number"] for p in github.prs if p["state"] == "OPEN"} == set()


def test_drop_refuses_to_lose_unsaved_work(world, github):
    _, a, _ = world
    cfg = config.load(a)
    n = github.add_issue(TRIVIAL, ["ac-confirmed"])
    r = task.start(cfg, f"gh-{n}")
    (r.path / "unsaved.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(drop.DropRefused, match="unsaved.txt"):
        drop.drop(cfg, f"gh-{n}", "nope")
    assert github.issues[n]["state"] == "OPEN" and r.path.exists()
    drop.drop(cfg, f"gh-{n}", "nope", discard=True)
    assert not r.path.exists()


def commit(path, name, msg):
    (path / name).write_text("x\n", encoding="utf-8")
    git(path, "add", name)
    git(path, "commit", "-qm", msg)


@pytest.mark.parametrize("pushed_before", [True, False])
def test_drop_refuses_to_lose_unpushed_commits(world, github, pushed_before):
    # committed but not pushed — `branch -D` would lose it with the worktree.
    _, a, _ = world
    cfg = config.load(a)
    n = github.add_issue(TRIVIAL, ["ac-confirmed"])
    r = task.start(cfg, f"gh-{n}")
    if pushed_before:  # the branch is on origin, the last commit is not
        commit(r.path, "old.txt", "pushed work")
        git(r.path, "push", "-q", "origin", r.branch)
    commit(r.path, "new.txt", "local only work")
    with pytest.raises(drop.DropRefused, match="local only work") as e:
        drop.drop(cfg, f"gh-{n}", "nope")
    assert "pushed work" not in str(e.value).replace("local only work", "")
    assert github.issues[n]["state"] == "OPEN"
    assert claims.read(cfg, f"gh-{n}") is not None and r.path.exists()
    assert "local only work" in out(a, "log", "--format=%s", r.branch)

    drop.drop(cfg, f"gh-{n}", "nope", discard=True)
    assert not r.path.exists() and out(a, "branch", "--list", r.branch) == ""


def test_drop_does_not_refuse_a_squash_merged_branch_deleted_on_origin(world, github):
    _, a, b = world
    cfg = config.load(a)
    n = github.add_issue(TRIVIAL, ["ac-confirmed"])
    r = task.start(cfg, f"gh-{n}")
    commit(r.path, "f.txt", "work")
    git(r.path, "push", "-q", "origin", r.branch)
    git(b, "fetch", "-q", "origin")
    git(b, "merge", "-q", "--squash", f"origin/{r.branch}")
    git(b, "commit", "-qm", "squashed")
    git(b, "push", "-q", "origin", "main")
    git(b, "push", "-q", "origin", "--delete", r.branch)
    git(a, "fetch", "-q", "--prune", "origin")
    drop.drop(cfg, f"gh-{n}", "merged anyway")
    assert not r.path.exists()


def test_reason_required(world, github, capsys):
    _, a, _ = world
    n = github.add_issue(TRIVIAL, ["ac-confirmed"])
    assert cli.main(["-C", str(a), "task", "drop", f"gh-{n}"]) == 2
    assert "reason is required" in capsys.readouterr().err


def test_dropped_in_epic_and_status(world, github):
    _, a, _ = world
    github.milestones.append("auth")
    done_ = github.add_issue(TRIVIAL, ["ac-confirmed"], title="A", state="CLOSED")
    dropped = github.add_issue(
        TRIVIAL, ["ac-confirmed", "dropped"], title="B", state="CLOSED"
    )
    for n in (done_, dropped):
        github.issues[n]["milestone"] = {"title": "auth"}
    text = epics.TEMPLATE.format(
        slug="auth", title="t", start=epics.START, end=epics.END
    )
    synced = epics.sync_text(config.load(a), "auth", text)
    assert f"- ~~#{dropped} B~~ — dropped" in synced
    [(_, counts)] = epics.status(config.load(a))
    assert counts == {"Done": 1, "Dropped": 1}


def test_drop_does_not_claim_a_release_that_failed(world, github, monkeypatch):
    _, a, _ = world
    cfg = config.load(a)
    n = github.add_issue(TRIVIAL, ["ac-confirmed"])
    task.start(cfg, f"gh-{n}")

    def unreachable(*_a, **_k):
        raise claims.PushFailed("push refs/claims/x failed: could not resolve host")

    monkeypatch.setattr(claims, "delete", unreachable)
    rep = drop.drop(cfg, f"gh-{n}", "nope")
    assert "claim released" not in rep.lines
    assert any("claim not released" in m and "--force" in m for m in rep.manual)

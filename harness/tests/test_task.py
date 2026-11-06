import datetime as dt
import json
import subprocess

import pytest
from conftest import git, out

from harness import claims, cli, config, init, task
from harness.claims import ClaimError


def cfg_of(path):
    return config.load(path)


def test_start_claims_creates_worktree_env_and_ports(world):
    _, a, _ = world
    r = task.start(cfg_of(a), "T-1", slug="demo")
    assert r.branch == "task/T-1-demo" and not r.resumed
    assert (r.path / ".git").is_file()
    assert (r.path / ".env").read_text(encoding="utf-8") == "SECRET=1\n"
    env = (r.path / task.ENV_FILE).read_text(encoding="utf-8")
    assert "HARNESS_TASK=T-1" in env and f"PORT_API={r.ports['api']}" in env
    assert r.ports["api"] != r.ports["web"]
    got = claims.read(cfg_of(a), "T-1")
    assert got is not None and got[1].owner == claims.owner_key(r.path)


def test_second_session_is_refused(world):
    _, a, b = world
    task.start(cfg_of(a), "T-1")
    with pytest.raises(ClaimError, match="taken by"):
        task.start(cfg_of(b), "T-1")
    assert not (b / ".worktrees" / "T-1").exists()  # loser leaves nothing behind


def test_second_session_in_the_same_clone_is_refused(world):
    # the owner key is host|worktree, the same for two sessions of one clone.
    _, a, _ = world
    first = task.start(cfg_of(a), "T-1")
    with pytest.raises(ClaimError, match="--resume") as e:
        task.start(cfg_of(a), "T-1")
    assert "already in progress in this clone" in str(e.value)
    assert first.path.as_posix() in str(e.value)


def test_same_clone_race_loser_is_refused_before_the_worktree(world, monkeypatch):
    # Both sessions saw no claim; the loser's create fails and it must not step in.
    _, a, _ = world
    winner = claims.Claim(
        "T-1", claims.owner_key(a / ".worktrees" / "T-1"), "task/T-1", "a@a",
        claims.now_iso(), claims.now_iso(),
    )  # fmt: skip
    assert claims.create(cfg_of(a), winner)
    real_read = claims.read
    seen = []

    def read_before_the_winner(cfg, task_id):
        seen.append(task_id)
        return None if len(seen) <= 2 else real_read(cfg, task_id)

    monkeypatch.setattr(claims, "read", read_before_the_winner)
    with pytest.raises(ClaimError, match="--resume"):
        task.start(cfg_of(a), "T-1")
    assert not (a / ".worktrees" / "T-1").exists()


def test_resume_continues_after_a_crashed_session(world):
    _, a, _ = world
    first = task.start(cfg_of(a), "T-1", slug="x")
    (first.path / "work.txt").write_text("half done\n", encoding="utf-8")
    again = task.start(cfg_of(a), "T-1", resume=True)
    assert again.resumed and again.path == first.path and again.branch == first.branch
    assert again.ports == first.ports
    assert (again.path / "work.txt").read_text(encoding="utf-8") == "half done\n"


def test_start_from_inside_a_worktree_does_not_nest(world):
    _, a, _ = world
    r1 = task.start(cfg_of(a), "T-1")
    r2 = task.start(cfg_of(r1.path), "T-2")
    assert r2.path == a / ".worktrees" / "T-2"


def local_branches(clone):
    return out(
        clone, "for-each-ref", "--format=%(refname:short)", "refs/heads/"
    ).split()


def test_failed_worktree_add_frees_the_task(world, monkeypatch):
    # a claim left without a worktree hides the task from `h task next` for 4h.
    _, a, b = world
    real_git = task._git

    def failing_add(cwd, *argv):
        if argv[:2] == ("worktree", "add"):
            return subprocess.CompletedProcess(argv, 128, "", "fatal: disk full")
        return real_git(cwd, *argv)

    monkeypatch.setattr(task, "_git", failing_add)
    with pytest.raises(ClaimError, match="disk full") as e:
        task.start(cfg_of(a), "T-1", slug="x")
    assert "rolled back: claim" in str(e.value)
    assert claims.read(cfg_of(a), "T-1") is None
    assert "task/T-1-x" not in local_branches(a)
    monkeypatch.undo()
    assert task.start(cfg_of(b), "T-1").branch  # free for another agent


def test_failure_after_the_worktree_removes_what_this_run_made(world, monkeypatch):
    _, a, _ = world
    cfg = cfg_of(a)

    def broken(*_a, **_k):
        raise OSError("port registry is locked")

    monkeypatch.setattr(task, "allocate_ports", broken)
    with pytest.raises(ClaimError, match="port registry is locked") as e:
        task.start(cfg, "T-1", slug="x")
    assert "claim" in str(e.value) and "worktree" in str(e.value)
    assert "branch task/T-1-x" in str(e.value)
    assert claims.read(cfg, "T-1") is None
    assert not (a / ".worktrees" / "T-1").exists()
    assert "task/T-1-x" not in local_branches(a)
    assert "T-1" not in out(a, "worktree", "list")


def test_rollback_keeps_a_branch_that_existed_before(world, monkeypatch):
    _, a, _ = world
    git(a, "branch", "task/T-1-x")
    monkeypatch.setattr(task, "allocate_ports", lambda *_a, **_k: 1 / 0)
    with pytest.raises(ClaimError, match="division by zero"):
        task.start(cfg_of(a), "T-1", slug="x")
    assert "task/T-1-x" in local_branches(a)
    assert claims.read(cfg_of(a), "T-1") is None


def test_failed_resume_leaves_the_claim_and_worktree(world, monkeypatch):
    _, a, _ = world
    cfg = cfg_of(a)
    first = task.start(cfg, "T-1", slug="x")
    monkeypatch.setattr(task, "allocate_ports", lambda *_a, **_k: 1 / 0)
    with pytest.raises(ZeroDivisionError):  # not wrapped: nothing was rolled back
        task.start(cfg, "T-1", resume=True)
    assert claims.read(cfg, "T-1") is not None
    assert (first.path / ".git").is_file()
    assert "task/T-1-x" in local_branches(a)
    assert task.has_ports(cfg, "T-1")


def age_claim(cfg, task_id, hours):
    sha, c = claims.read(cfg, task_id)  # type: ignore[misc]
    old = dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)
    c.heartbeat = old.replace(microsecond=0).isoformat()
    assert claims.replace(cfg, sha, c)


def test_stale_claim_needs_steal_and_keeps_branch(world):
    _, a, b = world
    r = task.start(cfg_of(a), "T-1", slug="x")
    (r.path / "work.txt").write_text("from a\n", encoding="utf-8")
    git(r.path, "add", "work.txt")
    git(r.path, "-c", "user.name=a", "-c", "user.email=a@a", "commit", "-q", "-m", "a")
    git(r.path, "push", "-q", "origin", r.branch)
    age_claim(cfg_of(a), "T-1", 5)

    with pytest.raises(ClaimError, match="--steal"):
        task.start(cfg_of(b), "T-1")
    taken = task.start(cfg_of(b), "T-1", steal=True)
    assert taken.stolen_from and taken.branch == "task/T-1-x"
    assert (taken.path / "work.txt").read_text(encoding="utf-8") == "from a\n"
    with pytest.raises(ClaimError):  # a's heartbeat now fails: it lost the task
        task.heartbeat(cfg_of(a), "T-1", r.path)


def push_task_branch(clone, branch):
    git(clone, "checkout", "-q", "-b", branch)
    (clone / "work.txt").write_text(f"{branch}\n", encoding="utf-8")
    git(clone, "add", "work.txt")
    git(clone, "commit", "-q", "-m", "work")
    git(clone, "push", "-q", "origin", branch)
    git(clone, "checkout", "-q", "main")


def test_start_without_slug_resumes_the_branch_on_origin(world):
    # a released task or a fresh clone has no claim, only the branch on origin.
    _, a, b = world
    push_task_branch(b, "task/T-1-old")
    r = task.start(cfg_of(a), "T-1")
    assert r.branch == "task/T-1-old"
    assert (r.path / "work.txt").read_text(encoding="utf-8") == "task/T-1-old\n"
    assert any("task/T-1-old" in n for n in r.notes)


def test_start_without_slug_refuses_several_branches(world):
    _, a, b = world
    push_task_branch(b, "task/T-1-one")
    push_task_branch(b, "task/T-1-two")
    with pytest.raises(ClaimError, match="--slug") as e:
        task.start(cfg_of(a), "T-1")
    assert "task/T-1-one" in str(e.value) and "task/T-1-two" in str(e.value)
    assert claims.read(cfg_of(a), "T-1") is None
    assert not (a / ".worktrees" / "T-1").exists()


def test_start_ignores_branches_of_other_tasks(world):
    _, a, b = world
    push_task_branch(b, "task/T-10-other")
    assert task.start(cfg_of(a), "T-1").branch == "task/T-1"


def test_explicit_slug_wins_and_warns_about_another_branch(world):
    _, a, b = world
    push_task_branch(b, "task/T-1-old")
    r = task.start(cfg_of(a), "T-1", slug="new")
    assert r.branch == "task/T-1-new"
    assert any("task/T-1-old" in n for n in r.notes)


def test_create_race_has_one_winner(world):
    _, a, b = world
    now = claims.now_iso()
    ca = claims.Claim("T-9", "a", "task/T-9", "a@a", now, now)
    cb = claims.Claim("T-9", "b", "task/T-9", "b@b", now, now)
    assert claims.create(cfg_of(a), ca) is True
    assert claims.create(cfg_of(b), cb) is False


def test_release_only_by_holder(world):
    _, a, b = world
    task.start(cfg_of(a), "T-1")
    with pytest.raises(ClaimError, match="--force"):
        task.release(cfg_of(b), "T-1")
    assert task.release(cfg_of(a), "T-1") == "T-1"
    assert claims.read(cfg_of(a), "T-1") is None


def squash_merge(clone, branch):
    git(clone, "fetch", "-q", "origin")
    git(clone, "switch", "-q", "main")
    git(clone, "pull", "-q", "--ff-only", "origin", "main")
    git(clone, "merge", "-q", "--squash", f"origin/{branch}")
    git(
        clone,
        "-c",
        "user.name=m",
        "-c",
        "user.email=m@m",
        "commit",
        "-q",
        "-m",
        "squash",
    )
    git(clone, "push", "-q", "origin", "main")


def commit_in(wt, name):
    (wt / name).write_text(name + "\n", encoding="utf-8")
    git(wt, "add", name)
    git(wt, "-c", "user.name=a", "-c", "user.email=a@a", "commit", "-q", "-m", name)


def test_clean_removes_squash_merged_keeps_the_rest(world):
    _, a, b = world
    cfg = cfg_of(a)
    done = task.start(cfg, "T-1")
    commit_in(done.path, "done.txt")
    git(done.path, "push", "-q", "origin", done.branch)
    open_ = task.start(cfg, "T-2")
    commit_in(open_.path, "wip.txt")
    dirty = task.start(cfg, "T-3")
    commit_in(dirty.path, "d.txt")
    git(dirty.path, "push", "-q", "origin", dirty.branch)
    squash_merge(b, done.branch)
    squash_merge(b, dirty.branch)
    (dirty.path / "scratch.txt").write_text("unsaved\n", encoding="utf-8")

    preview = {i.task: i.action for i in task.clean(cfg, dry_run=True)}
    assert preview == {
        "T-1": "would remove",
        "T-2": "kept: not merged",
        "T-3": "kept: uncommitted changes",
    }
    items = {i.task: i for i in task.clean(cfg)}
    assert items["T-1"].state == "squash-merged" and items["T-1"].action == "removed"
    assert not done.path.exists()
    assert claims.read(cfg, "T-1") is None  # claim released with the worktree
    assert claims.read(cfg, "T-2") is not None
    assert open_.path.exists() and dirty.path.exists()
    # ports of the removed task return to the pool
    assert not task.has_ports(cfg, "T-1") and task.has_ports(cfg, "T-2")


def test_global_clean_keeps_an_untouched_worktree_with_a_live_claim(world):
    # a session reading code before its first edit must not lose its worktree.
    _, a, _ = world
    r = task.start(cfg_of(a), "T-1")
    [item] = task.clean(cfg_of(a))
    assert item.state == "empty" and item.action.startswith("kept: in progress")
    assert r.path.exists() and claims.read(cfg_of(a), "T-1") is not None


def test_untouched_worktree_is_cleaned_by_id_or_when_stale(world):
    _, a, _ = world
    task.start(cfg_of(a), "T-1")
    [item] = task.clean(cfg_of(a), only="T-1")
    assert (item.state, item.action) == ("empty", "removed")
    task.start(cfg_of(a), "T-2")
    age_claim(cfg_of(a), "T-2", 5)  # past the 4h TTL: abandoned
    [item] = task.clean(cfg_of(a))
    assert (item.state, item.action) == ("empty", "removed")


def test_bad_task_id_rejected(world):
    _, a, _ = world
    with pytest.raises(ClaimError, match="bad task id"):
        task.start(cfg_of(a), "../evil")


def test_cli_task_flow(world, capsys):
    _, a, _ = world
    assert cli.main(["-C", str(a), "task", "start", "T-1"]) == 0
    assert "started on task/T-1" in capsys.readouterr().out
    assert cli.main(["-C", str(a), "task", "status"]) == 0
    assert "T-1:" in capsys.readouterr().out
    assert cli.main(["-C", str(a), "task", "start", "../x"]) == 2


def test_init_adds_gitignore_lines(tmp_path):
    (tmp_path / ".gitignore").write_text("target/\n", encoding="utf-8")
    init.run(config.load(tmp_path))
    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "target/"
    assert {"/.worktrees/", "/.artifacts/", task.TASK_FILE, task.ENV_FILE} <= set(lines)
    init.run(config.load(tmp_path))
    again = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert again == lines  # idempotent


def test_clean_finishes_when_git_unregistered_but_dir_stayed(world, monkeypatch):
    # 2026-10-22: `git worktree remove` unregistered the worktree, then failed to delete
    # the directory (the session's cwd). Branch, ports and claim must still be released.
    _, a, _ = world
    cfg = cfg_of(a)
    r = task.start(cfg, "T-1")
    real_git = task._git

    def remove_but_leave_dir(cwd, *argv):
        if argv[:2] == ("worktree", "remove"):
            real_git(cwd, "worktree", "remove", "--force", argv[2])
            argv_path = r.path
            argv_path.mkdir()  # the directory "survives"
            return subprocess.CompletedProcess(argv, 1, "", "error: Permission denied")
        return real_git(cwd, *argv)

    monkeypatch.setattr(task, "_git", remove_but_leave_dir)
    monkeypatch.setattr(task, "_remove_dir", lambda p: False)  # still locked
    [item] = task.clean(cfg, only="T-1")
    assert item.action == "removed from git; directory left"
    assert "Remove-Item" in item.manual or "rm -rf" in item.manual
    assert claims.read(cfg, "T-1") is None
    assert "task/T-1" not in out(a, "branch", "--list", "task/T-1")


def test_clean_removes_empty_orphan_dir_and_orphan_claim(world):
    _, a, _ = world
    cfg = cfg_of(a)
    r = task.start(cfg, "T-1")
    git(a, "worktree", "remove", "--force", str(r.path))  # git forgets it…
    r.path.mkdir()  # …an empty directory is left
    task.start(cfg, "T-2")
    t2 = a / ".worktrees" / "T-2"
    git(a, "worktree", "remove", "--force", str(t2))  # gone entirely; claim remains
    age_claim(cfg, "T-1", 0.5)  # past the grace a start needs to create its worktree
    age_claim(cfg, "T-2", 0.5)
    items = {i.task: i for i in task.clean(cfg)}
    assert items["T-1"].action == "removed" and not r.path.exists()
    assert items["T-2"].action == "claim released"
    assert claims.read(cfg, "T-1") is None and claims.read(cfg, "T-2") is None


def test_clean_keeps_a_fresh_claim_whose_worktree_is_being_created(world, capsys):
    # between the claim and `worktree add` a start has a claim and no directory
    # (or an empty one); a clean in that window must not free the task.
    _, a, _ = world
    cfg = cfg_of(a)
    for tid in ("T-1", "T-2"):
        now = claims.now_iso()
        wt = a / ".worktrees" / tid
        owner = claims.owner_key(wt)
        assert claims.create(
            cfg, claims.Claim(tid, owner, f"task/{tid}", "a@a", now, now)
        )
    (a / ".worktrees" / "T-2").mkdir(parents=True)  # `worktree add` under way
    assert cli.main(["-C", str(a), "task", "clean", "--json"]) == 0
    rows = {r["task"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["T-1"]["action"].startswith("kept: in progress (heartbeat")
    assert rows["T-2"]["action"].startswith("kept: in progress (heartbeat")
    assert claims.read(cfg, "T-1") is not None and claims.read(cfg, "T-2") is not None
    assert (a / ".worktrees" / "T-2").is_dir()


def test_clean_reports_orphan_dir_with_files(world, capsys):
    _, a, _ = world
    orphan = a / ".worktrees" / "X-1"
    orphan.mkdir(parents=True)
    (orphan / "notes.txt").write_text("keep me\n", encoding="utf-8")
    assert cli.main(["-C", str(a), "task", "clean"]) == 1  # needs a human
    assert "manual:" in capsys.readouterr().out
    assert (orphan / "notes.txt").exists()


def bounce_back(world):
    """b worked on T-1 and pushed, a stole it and pushed more, now b steals it back
    with its old local branch."""
    _, a, b = world
    mine = task.start(cfg_of(b), "T-1", slug="x")
    commit_in(mine.path, "b1.txt")
    git(mine.path, "push", "-q", "origin", mine.branch)
    age_claim(cfg_of(b), "T-1", 5)
    theirs = task.start(cfg_of(a), "T-1", steal=True)
    commit_in(theirs.path, "a1.txt")
    git(theirs.path, "push", "-q", "origin", theirs.branch)
    age_claim(cfg_of(a), "T-1", 5)
    return mine, out(theirs.path, "rev-parse", "HEAD")


def test_steal_fast_forwards_an_old_worktree_to_origin(world):
    _, _, b = world
    mine, pushed = bounce_back(world)
    task.start(cfg_of(b), "T-1", steal=True)
    assert out(mine.path, "rev-parse", "HEAD") == pushed


def test_steal_fast_forwards_an_old_branch_without_worktree(world):
    _, _, b = world
    mine, pushed = bounce_back(world)
    git(b, "worktree", "remove", "--force", str(mine.path))
    again = task.start(cfg_of(b), "T-1", steal=True)
    assert out(again.path, "rev-parse", "HEAD") == pushed
    assert out(b, "rev-parse", f"refs/heads/{again.branch}") == pushed


def test_steal_refuses_a_diverged_branch_and_keeps_the_claim(world):
    _, _, b = world
    mine, _pushed = bounce_back(world)
    commit_in(mine.path, "b2.txt")
    local = out(mine.path, "rev-parse", "HEAD")
    with pytest.raises(ClaimError, match="diverged") as e:
        task.start(cfg_of(b), "T-1", steal=True)
    assert "b2.txt" in str(e.value) and "a1.txt" in str(e.value)
    assert out(mine.path, "rev-parse", "HEAD") == local  # nothing overwritten
    held = claims.read(cfg_of(b), "T-1")
    assert held is not None and held[1].owner == claims.owner_key(mine.path)


def test_steal_refuses_to_update_a_dirty_worktree(world):
    _, _, b = world
    mine, _ = bounce_back(world)
    (mine.path / "b1.txt").write_text("edited\n", encoding="utf-8")
    old = out(mine.path, "rev-parse", "HEAD")
    with pytest.raises(ClaimError, match="uncommitted"):
        task.start(cfg_of(b), "T-1", steal=True)
    assert out(mine.path, "rev-parse", "HEAD") == old


def test_resume_fast_forwards_to_work_pushed_from_elsewhere(world):
    # A1: any start, not only --steal, brings the branch up to origin.
    _, a, _ = world
    r = task.start(cfg_of(a), "T-1", slug="x")
    commit_in(r.path, "one.txt")
    git(r.path, "push", "-q", "origin", r.branch)
    git(r.path, "reset", "-q", "--hard", "HEAD~1")
    task.start(cfg_of(a), "T-1", resume=True)
    assert (r.path / "one.txt").is_file()


def test_clean_reports_every_item_when_origin_fails(world, monkeypatch):
    # a network error after removing one worktree used to cut the report short.
    _, a, b = world
    cfg = cfg_of(a)
    for tid in ("T-1", "T-2"):
        r = task.start(cfg, tid)
        commit_in(r.path, f"{tid}.txt")
        git(r.path, "push", "-q", "origin", r.branch)
        squash_merge(b, r.branch)

    def offline(*_a, **_k):
        raise ClaimError("git ls-remote: could not resolve host")

    monkeypatch.setattr(claims, "read", offline)
    monkeypatch.setattr(claims, "list_all", offline)
    items = {i.task: i for i in task.clean(cfg)}
    for tid in ("T-1", "T-2"):
        assert items[tid].action.startswith("removed; claim not released"), items
        assert items[tid].manual
    assert items["claims"].action.startswith("not checked")


def _leftover(a, name, files):
    """What a worktree removal leaves when a shell stands inside it."""
    d = a / ".worktrees" / name
    (d / "harness").mkdir(parents=True)  # the directory the shell stood in
    for rel, data in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_bytes(data)
    return d


def test_clean_removes_a_leftover_of_base_copies(world):
    _, a, _ = world
    tracked = out(a, "ls-files").split()[0]
    base = (a / tracked).read_bytes().replace(b"\r\n", b"\n")  # as committed
    d = _leftover(a, "X-2", {tracked: base.replace(b"\n", b"\r\n")})  # CRLF: same file
    items = {i.task: i for i in task.clean(cfg_of(a))}
    assert items["X-2"].action == "removed" and not d.exists()


@pytest.mark.parametrize("kind", ["changed", "new"])
def test_clean_keeps_a_leftover_with_real_work(world, kind):
    _, a, _ = world
    tracked = out(a, "ls-files").split()[0]
    rel, data = (
        (tracked, b"edited\n") if kind == "changed" else ("notes.txt", b"mine\n")
    )
    d = _leftover(a, "X-3", {rel: data})
    items = {i.task: i for i in task.clean(cfg_of(a))}
    assert items["X-3"].action.startswith("kept: not a worktree") and d.exists()

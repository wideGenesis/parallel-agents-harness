"""Ship lowers the ratchet base by itself: the lower base lands in the PR as a
tool commit the review gate lets through only when it lowers, and a rebase conflict in
the base alone is settled by a recount."""

import subprocess

import pytest
from conftest import git
from test_review import CHECKS, TRIVIAL, write_verdict
from test_ship import FakeGh

from harness import config, ratchet, review, ship, task

KEY = "python:suppress:type: ignore"
SUPS = ("sup_a.py", "sup_b.py", "sup_c.py")
# Split, or the ratchet of this repo would count the fixture as its own suppression.
IGNORED = "X = 1  # type" + ": ignore\n"


@pytest.fixture
def wt(world, github, monkeypatch):
    """A project with three type-ignores under a committed base, and a task worktree."""
    _, a, _ = world
    with (a / "harness.toml").open("a", encoding="utf-8") as f:
        f.write(CHECKS)
    for name in SUPS:
        (a / name).write_text(IGNORED, encoding="utf-8")
    ratchet.update(config.load(a))
    git(a, "add", ".")
    git(a, "commit", "-qm", "ratchet base")
    git(a, "push", "-q", "origin", "main")
    github.add_issue(TRIVIAL, ["ac-confirmed"], number=5)
    monkeypatch.setattr(ship, "gh", FakeGh())
    monkeypatch.setattr(ship.tools, "usable", lambda t: True)
    return task.start(config.load(a), "gh-5").path


def drop_ignores(root, *names, base=False):
    for name in names:
        (root / name).write_text("X = 1\n", encoding="utf-8")
    if base:  # the agent ran `h ratchet --update` by hand
        ratchet.update(config.load(root))
    git(root, "add", *names, *([ratchet.BASE.as_posix()] if base else []))
    git(root, "commit", "-qm", f"fix: drop ignores in {', '.join(names)}")


def approve(wt):
    review.prepare(config.load(wt), wt)
    write_verdict(wt, "approve")
    return review._head(wt)


def counts_at(wt, rev="HEAD"):
    return review._base_at(wt, rev)


def subject(wt, rev="HEAD"):
    return git_out(wt, "log", "-1", "--format=%s", rev)


def git_out(wt, *argv):
    return subprocess.run(["git", "-C", str(wt), *argv], capture_output=True,
                          text=True, check=True).stdout.strip()  # fmt: skip


def test_ship_commits_the_lower_base_and_the_old_count_is_red(wt):
    drop_ignores(wt, "sup_b.py", "sup_c.py")
    approved = approve(wt)
    ship.ship(config.load(wt), wt)
    assert subject(wt) == ship.RATCHET_MESSAGE
    assert (
        git_out(wt, "diff", "--name-only", "HEAD~1", "HEAD") == ratchet.BASE.as_posix()
    )
    assert counts_at(wt)[KEY] == 1 and counts_at(wt, approved)[KEY] == 3
    assert _commits(wt)[0] == ship.RATCHET_MESSAGE
    # B2: the base commit needs no second review; a rerun of ship goes through.
    v = review.gate(config.load(wt), wt)
    assert v is not None and v.head == approved
    # B1: bringing an ignore back is red now.
    (wt / "sup_b.py").write_text(IGNORED, encoding="utf-8")
    res = ratchet.check(config.load(wt))
    assert res is not None and res.grown == {KEY: (1, 2)}


def _commits(wt):
    return git_out(wt, "log", "--format=%s", "origin/main..HEAD").splitlines()


def test_nothing_lowered_adds_no_commit(wt):
    (wt / "notes.txt").write_text("n\n", encoding="utf-8")
    git(wt, "add", "notes.txt")
    git(wt, "commit", "-qm", "docs: notes")
    approved = approve(wt)
    ship.ship(config.load(wt), wt)
    assert review._head(wt) == approved


def _tool_commit(wt, counts, extra=None):
    ratchet.write_base(config.load(wt), counts)
    if extra:
        (wt / extra).write_text("sneaked in\n", encoding="utf-8")
    git(wt, "add", ratchet.BASE.as_posix(), *([extra] if extra else []))
    git(wt, "commit", "-qm", ship.RATCHET_MESSAGE)


def test_a_base_commit_that_only_lowers_needs_no_review(wt):
    drop_ignores(wt, "sup_c.py")
    approve(wt)
    _tool_commit(wt, {**counts_at(wt), KEY: 2})
    assert review.gate(config.load(wt), wt) is not None


@pytest.mark.parametrize("change", ["raise", "new key", "other file", "deleted"])
def test_a_base_commit_that_does_more_needs_review(wt, change):
    drop_ignores(wt, "sup_c.py")
    approve(wt)
    counts = counts_at(wt)
    if change == "raise":
        _tool_commit(wt, {**counts, KEY: 4})
    elif change == "new key":
        _tool_commit(wt, {**counts, "python:suppress:noqa": 1})
    elif change == "other file":
        _tool_commit(wt, {**counts, KEY: 2}, extra="sup_c.py")
    else:
        git(wt, "rm", "-q", ratchet.BASE.as_posix())
        git(wt, "commit", "-qm", ship.RATCHET_MESSAGE)
    with pytest.raises(review.ReviewError, match="review the new commits"):
        review.gate(config.load(wt), wt)


def other_pr_lowers_the_base(world):
    """Another task merged meanwhile: it dropped the ignore in sup_a.py with its base."""
    _, _, b = world
    git(b, "pull", "-q", "origin", "main")
    drop_ignores(b, "sup_a.py", base=True)
    git(b, "push", "-q", "origin", "main")


@pytest.mark.parametrize("by", ["ship", "hand"])
def test_a_conflict_only_in_the_base_is_settled_by_a_recount(world, wt, by):
    drop_ignores(wt, "sup_b.py", "sup_c.py", base=by == "hand")
    approved = approve(wt)
    if by == "ship":
        ship.ship(config.load(wt), wt)  # the first PR: ship commits the lower base
    other_pr_lowers_the_base(world)
    git(wt, "fetch", "-q", "origin")
    assert review._conflicts(wt, "origin/main") == [ratchet.BASE.as_posix()]
    r = ship.ship(config.load(wt), wt)
    assert "rebased onto origin/main (+1 commit(s))" in r.notes
    assert KEY not in counts_at(wt)  # all three ignores are gone on main + branch
    assert git_out(wt, "status", "--porcelain", "--untracked-files=no") == ""
    v = review.gate(config.load(wt), wt)
    assert v is not None and v.head == approved
    assert ratchet.load_base(config.load(wt)) == counts_at(wt)  # no conflict markers


def test_a_conflict_in_another_file_still_refuses(world, wt):
    _, _, b = world
    drop_ignores(wt, "sup_b.py", base=True)
    approve(wt)
    git(b, "pull", "-q", "origin", "main")
    (b / "sup_b.py").write_text("Y = 2" + IGNORED[5:], encoding="utf-8")
    ratchet.update(config.load(b), allow_growth=True)
    (b / "sup_c.py").write_text("X = 1\n", encoding="utf-8")
    ratchet.update(config.load(b))
    git(b, "commit", "-qam", "main: rewrite sup_b, drop sup_c")
    git(b, "push", "-q", "origin", "main")
    before = review._head(wt)
    with pytest.raises(ship.ShipRefused, match="conflicts with it"):
        ship.ship(config.load(wt), wt)
    assert review._head(wt) == before


def test_the_recount_keeps_only_growth_the_branch_recorded():
    onto, parent = {KEY: 2, "k": 1}, {KEY: 3, "k": 1}
    assert ratchet.limit(onto, parent, {KEY: 1, "k": 1}) == {KEY: 2, "k": 1}
    # a reviewed --allow-growth survives the merge; a new key of the branch too
    assert ratchet.limit(onto, parent, {KEY: 4, "n": 1}) == {KEY: 3, "k": 1, "n": 1}

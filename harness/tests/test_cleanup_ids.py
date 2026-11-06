"""clean-up finds a task without its card and a claim under an old spelling."""

import pytest
from conftest import git
from test_branch import with_tasks
from test_drop import TRIVIAL

from harness import claims, cli, config, drop, task

CARDS = 'card_path = "docs/backlog/*/cards/{id}.md"\n'


def started_without_card(a):
    """An external task whose card was removed after the start."""
    card = a / "docs" / "backlog" / "e1" / "cards" / "ROADMAP.md"
    card.parent.mkdir(parents=True)
    card.write_text("### ROADMAP\n\nProblem: x\n", encoding="utf-8")
    git(a, "add", "docs")
    with_tasks(a, CARDS)
    r = task.start(config.load(a), "ROADMAP")
    git(a, "rm", "-q", "-r", "docs/backlog")
    git(a, "commit", "-qm", "card moved away")
    git(a, "push", "-q", "origin", "main")
    return r


def test_release_works_after_the_card_is_gone(world, capsys):
    _, a, _ = world
    started_without_card(a)
    assert cli.main(["-C", str(a), "task", "release", "roadmap"]) == 0
    assert "ROADMAP: claim released" in capsys.readouterr().out
    assert claims.read(config.load(a), "ROADMAP") is None


def test_clean_by_id_works_after_the_card_is_gone(world, capsys):
    _, a, _ = world
    r = started_without_card(a)
    assert cli.main(["-C", str(a), "task", "clean", "ROADMAP"]) == 0
    assert "ROADMAP: empty, removed" in capsys.readouterr().out
    assert not r.path.exists() and claims.read(config.load(a), "ROADMAP") is None


def test_nothing_to_release_without_card_or_claim(world, capsys):
    _, a, _ = world
    with_tasks(a, CARDS)
    assert cli.main(["-C", str(a), "task", "release", "GONE-1"]) == 0
    assert "nothing to release" in capsys.readouterr().out
    assert cli.main(["-C", str(a), "task", "clean", "GONE-1"]) == 0
    with pytest.raises(claims.ClaimError, match="no card for GONE-1"):
        task.start(config.load(a), "GONE-1")


def old_claim(a, spelling):
    """A claim an older h took under another spelling of the issue id."""
    cfg = config.load(a)
    wt = task.worktree_path(cfg, spelling)
    now = claims.now_iso()
    owner = claims.owner_key(wt)
    assert claims.create(
        cfg, claims.Claim(spelling, owner, f"task/{spelling}", "a@a", now, now)
    )
    return cfg


@pytest.mark.parametrize("typed", ["gh-7", "GH-7", "7"])
def test_release_finds_a_claim_under_an_old_spelling(world, capsys, typed):
    _, a, _ = world
    cfg = old_claim(a, "GH-7")
    assert cli.main(["-C", str(a), "task", "release", typed]) == 0
    assert "gh-7: claim released (as GH-7)" in capsys.readouterr().out
    assert claims.read(cfg, "GH-7") is None


def test_drop_finds_a_claim_under_an_old_spelling(world, github):
    _, a, _ = world
    n = github.add_issue(TRIVIAL, ["ac-confirmed"])
    cfg = old_claim(a, f"GH-{n}")
    rep = drop.drop(cfg, f"gh-{n}", "not needed")
    assert f"claim released (as GH-{n})" in rep.lines
    assert claims.read(cfg, f"GH-{n}") is None

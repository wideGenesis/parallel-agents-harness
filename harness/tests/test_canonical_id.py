import json

import pytest
from conftest import git

from harness import cards, claims, cli, config, task
from harness.claims import ClaimError

TRIVIAL = cards.render({
    "Problem": "p", "Assumptions": "none", "Verify": "h check", "Size": "S",
    "Trivial": "yes", "Trivial reason": "test",
})  # fmt: skip


def with_cards(clone, *ids):
    for i in ids:
        card = clone / "docs" / "backlog" / "e1" / "cards" / f"{i}.md"
        card.parent.mkdir(parents=True, exist_ok=True)
        card.write_text(f"### {i}\n\nProblem: x\n", encoding="utf-8")
    toml = clone / "harness.toml"
    text = toml.read_text(encoding="utf-8")
    toml.write_text(
        text.replace(
            "[tasks]\n", '[tasks]\ncard_path = "docs/backlog/*/cards/{id}.md"\n', 1
        ),
        encoding="utf-8",
    )
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "cards")
    git(clone, "push", "-q", "origin", "main")


@pytest.mark.parametrize("raw", ["5", "#5", "gh-5", "GH-5", "Gh-05"])
def test_issue_spellings_are_one_id(world, raw):
    _, a, _ = world
    assert task.canonical_id(config.load(a), raw) == "gh-5"


@pytest.mark.parametrize("raw", ["GH-5", "5", "#5"])
def test_other_spelling_in_another_clone_is_taken(world, github, capsys, raw):
    # T2: start gh-5 in clone A, then GH-5 / 5 in clone B → refused, no second claim.
    _, a, b = world
    n = github.add_issue(TRIVIAL, ["ac-confirmed", "ready"])
    assert cli.main(["-C", str(a), "task", "start", f"gh-{n}"]) == 0
    raw = raw.replace("5", str(n))
    capsys.readouterr()
    assert cli.main(["-C", str(b), "task", "start", raw]) != 0
    assert f"gh-{n} is taken by" in capsys.readouterr().err
    assert claims.list_all(config.load(a)) == [f"gh-{n}"]
    assert not any((b / ".worktrees").glob("*"))


def test_start_prints_the_canonical_id(world, github, capsys):
    _, a, _ = world
    n = github.add_issue(TRIVIAL, ["ac-confirmed", "ready"])
    assert cli.main(["-C", str(a), "task", "start", f"GH-{n}"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"task gh-{n}: started") and f"GH-{n}" not in out
    assert (a / ".worktrees" / f"gh-{n}").is_dir()


def test_claim_under_an_old_spelling_holds_the_task(world, github, capsys):
    # a claim an older h took as GH-N is not offered by `h task next`,
    # and `start gh-N` does not take a second one.
    _, a, b = world
    cfg = config.load(a)
    n = github.add_issue(TRIVIAL, ["ac-confirmed", "ready", "agent-ok"])
    legacy = claims.Claim(f"GH-{n}", "other|/x", f"task/GH-{n}", "o@o",
                          claims.now_iso(), claims.now_iso())  # fmt: skip
    assert claims.create(cfg, legacy)
    assert cli.main(["-C", str(a), "task", "next", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == []
    with pytest.raises(ClaimError, match=f"taken as GH-{n}"):
        task.start(config.load(b), f"gh-{n}")
    assert claims.list_all(cfg) == [f"GH-{n}"]


def test_external_id_takes_the_card_spelling(world):
    # T3: any case finds the card; branch, claim and worktree use the file's spelling.
    _, a, b = world
    with_cards(a, "ROADMAP")
    git(b, "pull", "-q", "origin", "main")
    r = task.start(config.load(a), "roadmap")
    assert r.task == "ROADMAP" and r.branch == "task/ROADMAP"
    assert r.path.name == "ROADMAP"
    with pytest.raises(ClaimError, match="taken by"):
        task.start(config.load(b), "Roadmap")
    assert claims.list_all(config.load(a)) == ["ROADMAP"]


def test_external_id_without_a_card_is_refused(world):
    _, a, _ = world
    with_cards(a, "ROADMAP")
    with pytest.raises(ClaimError, match="no card for NOPE-01"):
        task.start(config.load(a), "NOPE-01")
    assert claims.list_all(config.load(a)) == []
    assert not (a / ".worktrees" / "NOPE-01").exists()


def test_cards_differing_only_in_case_are_ambiguous(world, monkeypatch):
    # A case-sensitive file system can hold both files.
    _, a, _ = world
    with_cards(a, "ROADMAP")
    monkeypatch.setattr(task, "_card_ids", lambda _cfg: ["ROADMAP", "roadmap"])
    with pytest.raises(ClaimError, match="differ only in case"):
        task.canonical_id(config.load(a), "roadMap")

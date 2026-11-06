import pytest
from conftest import git
from test_ship import FAST_CHECKS, FakeGh

from harness import cards, config, policy, ship, task

TRIVIAL = cards.render({"Problem": "p", "Assumptions": "none", "Verify": "v", "Size": "S",
                        "Trivial": "yes", "Trivial reason": "t"})  # fmt: skip


@pytest.fixture
def wt(world, github, monkeypatch):
    """A task worktree whose base requires review (FAST_CHECKS minus `required = false`)."""
    _, a, _ = world
    strict = FAST_CHECKS.replace("[review]\nrequired = false\n", "")
    with (a / "harness.toml").open("a", encoding="utf-8") as f:
        f.write(strict)
    git(a, "commit", "-qam", "base rules")
    git(a, "push", "-q", "origin", "main")
    github.add_issue(TRIVIAL, ["ac-confirmed"], number=3)
    monkeypatch.setattr(ship, "gh", FakeGh())
    monkeypatch.setattr(ship.tools, "usable", lambda t: True)
    return task.start(config.load(a), "gh-3").path


def test_branch_cannot_waive_its_own_review(wt):
    toml = wt / "harness.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + "\n[review]\nrequired = false\n",
                    encoding="utf-8")  # fmt: skip
    git(wt, "commit", "-qam", "chore: relax rules")
    cfg = config.load(wt)
    assert cfg.rules_from == "base" and cfg.rules_changed
    assert cfg.review_required is True  # base wins
    with pytest.raises(ship.ShipRefused, match=r"no review.*built-in harness.toml"):
        ship.ship(cfg, wt)


def test_editing_harness_toml_needs_user_review_and_merge(wt):
    toml = wt / "harness.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + "\n# note\n", encoding="utf-8")
    d = policy.decide(config.load(wt), ["harness.toml"])
    assert (d.review, d.merge) == (True, "user")
    assert d.reason == "built-in harness.toml ← harness.toml"


def test_unchanged_rules_behave_as_before(wt):
    cfg = config.load(wt)
    assert cfg.rules_from == "base" and not cfg.rules_changed
    assert cfg.review_required is True


def test_without_git_the_local_file_is_used(tmp_path):
    (tmp_path / "harness.toml").write_text(
        "[review]\nrequired = false\n", encoding="utf-8"
    )
    cfg = config.load(tmp_path)
    assert cfg.rules_from == "worktree" and cfg.review_required is False

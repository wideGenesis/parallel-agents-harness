import pytest

from harness import cli, config, policy

TIERS = """
[review]
required = true
[ship]
merge = "agent"

[[policy]]
paths = ["migrations/**", ".github/workflows/**"]
review = true
merge = "user"

[[policy]]
paths = ["docs/**", "*.md"]
review = false
"""


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "harness.toml").write_text(TIERS, encoding="utf-8")
    return config.load(tmp_path)


def test_migration_needs_user_merge_and_names_the_reason(cfg):
    d = policy.decide(cfg, ["migrations/0042_add.sql"])
    assert (d.review, d.merge) == (True, "user")
    assert d.reason == "policy #1 migrations/** ← migrations/0042_add.sql"


def test_docs_only_skips_review_and_agent_merges(cfg):
    d = policy.decide(cfg, ["docs/a.md", "README.md"])
    assert (d.review, d.merge) == (False, "agent")


def test_mixed_change_takes_the_strictest(cfg):
    d = policy.decide(cfg, ["docs/a.md", "migrations/0042_add.sql"])
    assert (d.review, d.merge) == (True, "user")


def test_unmatched_file_falls_back_to_defaults_and_stays_strict(cfg):
    # code + docs: the docs rule must not waive review for the code file
    d = policy.decide(cfg, ["docs/a.md", "crates/api/src/lib.rs"])
    assert (d.review, d.merge) == (True, "agent")


def test_no_rules_means_defaults(tmp_path):
    c = config.load(tmp_path)
    d = policy.decide(c, ["anything.rs"])
    assert (d.review, d.merge, d.reason) == (True, "user", "")


@pytest.mark.parametrize(
    ("body", "msg"),
    [
        ("[[policy]]\npaths = []\nreview = true\n", "non-empty"),
        ('[[policy]]\npaths = ["x"]\n', "set review"),
        ('[[policy]]\npaths = ["x"]\nmerge = "robot"\n', "merge"),
        ('[[policy]]\npaths = ["x"]\nreview = "yes"\n', "true or false"),
        ('[[policy]]\npaths = ["x"]\nreview = true\ncard = true\n', "unknown keys"),
    ],
)
def test_bad_policy_is_a_config_error(tmp_path, body, msg):
    (tmp_path / "harness.toml").write_text(body, encoding="utf-8")
    with pytest.raises(config.ConfigError, match=msg):
        config.load(tmp_path)


def test_merge_line_names_the_rule(cfg):
    d = policy.decide(cfg, ["migrations/0042_add.sql"])
    line = cli.merge_line(cfg, d)
    assert line.startswith("merge: user — PR ready, waiting")
    assert "policy #1 migrations/** ← migrations/0042_add.sql" in line
    assert cli.merge_line(cfg) == "merge: agent — after green CI"


@pytest.mark.parametrize("docs_first", [True, False])
def test_file_matching_several_rules_gets_the_strictest(tmp_path, docs_first):
    docs = '[[policy]]\npaths = ["*.md"]\nreview = false\nmerge = "agent"\n'
    mig = '[[policy]]\npaths = ["migrations/**"]\nreview = true\nmerge = "user"\n'
    body = '[ship]\nmerge = "agent"\n' + (docs + mig if docs_first else mig + docs)
    (tmp_path / "harness.toml").write_text(body, encoding="utf-8")
    d = policy.decide(config.load(tmp_path), ["migrations/notes.md"])
    assert (d.review, d.merge) == (True, "user")
    assert "migrations/**" in d.reason


def test_ship_follows_the_policy(world, github, monkeypatch):
    from conftest import git
    from test_ship import FAST_CHECKS, FakeGh

    from harness import cards, review, ship, task

    _, a, _ = world
    # FAST_CHECKS turns review off; the policy turns it back on for migrations only.
    rules = '\n[[policy]]\npaths = ["migrations/**"]\nreview = true\nmerge = "user"\n'
    with (a / "harness.toml").open("a", encoding="utf-8") as f:
        f.write(FAST_CHECKS + rules)
    git(a, "commit", "-qam", "cfg")
    git(a, "push", "-q", "origin", "main")
    trivial = cards.render({"Problem": "p", "Assumptions": "none", "Verify": "v", "Size": "S",
                            "Trivial": "yes", "Trivial reason": "t"})  # fmt: skip
    github.add_issue(trivial, ["ac-confirmed"], number=8)
    r = task.start(config.load(a), "gh-8")
    (r.path / "migrations").mkdir()
    (r.path / "migrations" / "0001_x.sql").write_text("select 1;\n", encoding="utf-8")
    git(r.path, "add", "migrations")
    git(r.path, "commit", "-qm", "feat: migration")
    monkeypatch.setattr(ship, "gh", FakeGh())
    monkeypatch.setattr(ship.tools, "usable", lambda t: True)
    with pytest.raises(
        ship.ShipRefused,
        match=r"no review.*policy #1 migrations/\*\* ← migrations/0001_x.sql",
    ):
        ship.ship(config.load(r.path), r.path)
    assert review.read_verdict(r.path) is None

import pytest

from harness import config, deps, gh, policy, prreview

RUFF = "build(deps-dev): bump ruff from 0.15.12 to 0.17.1 in /harness in the harness-dev group"
UV_BUILD = (
    "build(deps-dev): update uv-build requirement from <0.11,>=0.9 to >=0.13.1,<0.14 "
    "in /harness"
)
GROUP_BODY = """Bumps the actions group with 2 updates.

Updates `actions/checkout` from 4.1.0 to 4.2.1
Updates `astral-sh/setup-uv` from 5.0.0 to 6.0.0
"""


@pytest.mark.parametrize(
    ("title", "body", "kind"),
    [
        (RUFF, "", "minor-0.x"),  # the user's
        (UV_BUILD, "", "minor-0.x"),  # lower bounds 0.9 → 0.13.1
        ("bump requests from 2.31.0 to 2.31.1", "", "patch"),
        ("bump requests from 2.31.0 to 2.32.0", "", "minor"),
        ("bump ruff from 0.15.1 to 0.15.2", "", "patch"),
        ("bump actions/checkout from 4 to 5", "", "major"),
        ("bump the actions group with 2 updates", GROUP_BODY, "major"),  # worst wins
        ("chore: something Dependabot never wrote", "", "unknown"),
        ("chore: no versions here", "Updates `x` from 1.0.0 to 1.0.1\n", "unknown"),
        ("bump x from 2.0.0 to 1.0.0", "", "unknown"),  # a downgrade is no update
        # Release notes in the body are third-party text: a planted line cannot turn
        # the title's major into a patch.
        ("bump x from 1.0.0 to 2.0.0", "Updates `x` from 1.0.0 to 1.0.1\n", "major"),
    ],
)
def test_kind_of_dependabot_titles(title, body, kind):
    assert deps.classify(title, body)[0] == kind


@pytest.fixture
def pr(repo, github, monkeypatch):
    """PR #7 by Dependabot at sha 'abc…', CI green unless a test changes `state`."""
    state = {
        "author": deps.DEPENDABOT,
        "title": "bump requests from 2.31.0 to 2.31.1",
        "labels": [],
        "checks": [{"name": "harness", "bucket": "pass"},
                   {"name": prreview.STATUS_CONTEXT, "bucket": "pending"}],
    }  # fmt: skip
    monkeypatch.setattr(gh, "pr_info", lambda cwd, n: {
        "title": state["title"], "body": "", "author": state["author"],
        "sha": "abc" + "0" * 37, "labels": state["labels"]})  # fmt: skip
    monkeypatch.setattr(gh, "pr_checks", lambda cwd, branch: state["checks"])
    monkeypatch.delenv("CLAUDECODE", raising=False)
    return config.load(repo), github, state


def test_agent_passes_a_green_patch(pr):
    cfg, github, _ = pr
    d = deps.review(cfg, "https://github.com/o/r/pull/7")
    assert d.number == 7 and d.kind == "patch"
    st = github.statuses["abc" + "0" * 37]
    assert st["state"] == "success" and st["context"] == prreview.STATUS_CONTEXT


def test_agent_leaves_minor_of_0x_to_the_user(pr, monkeypatch):
    cfg, github, state = pr
    state["title"], state["labels"] = RUFF, ["security"]
    with pytest.raises(
        deps.DepsError, match=r"minor-0\.x update \(security\).*h deps approve 7"
    ):
        deps.review(cfg, "7")
    assert not github.statuses
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(policy.UserOnly):
        deps.approve(cfg, "7")
    monkeypatch.delenv("CLAUDECODE")
    assert deps.approve(cfg, "7").kind == "minor-0.x"
    assert "user" in github.statuses["abc" + "0" * 37]["description"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"author": "me"}, "not Dependabot"),
        ({"checks": [{"name": "harness", "bucket": "fail"}]}, "CI red: harness"),
        ({"checks": [{"name": "harness", "bucket": "pending"}]}, "CI still running"),
        ({"checks": []}, "no CI checks"),
    ],
)
def test_refused_without_a_status(pr, change, message):
    cfg, github, state = pr
    state.update(change)
    for path in (deps.review, deps.approve):
        with pytest.raises(deps.DepsError, match=message):
            path(cfg, "7")
    assert not github.statuses


def test_refused_when_the_pr_moves_while_ci_is_read(pr, monkeypatch):
    cfg, github, state = pr
    shas = iter(["a" * 40, "b" * 40])
    monkeypatch.setattr(gh, "pr_info", lambda cwd, n: {
        "title": state["title"], "body": "", "author": deps.DEPENDABOT,
        "sha": next(shas), "labels": []})  # fmt: skip
    with pytest.raises(deps.DepsError, match="moved to bbbbbbbbbb"):
        deps.review(cfg, "7")
    assert not github.statuses

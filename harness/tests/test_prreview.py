import pytest

from harness import cli, config, prreview
from harness import gh as gh_mod
from harness.review import Verdict

V = Verdict(head="a" * 40, verdict="approve", round=2, model="sonnet",
            findings=[{"file": "x.py", "line": 3, "severity": "note", "text": "naming"}])  # fmt: skip


def test_default_comment_has_marker_and_findings(tmp_path):
    body = prreview.render(config.load(tmp_path), V, "b" * 40)
    assert body.startswith(prreview.MARKER)
    meta = prreview.MARKER_RE.match(body)
    assert meta and '"pr_head":"' + "b" * 40 in meta.group(1)
    assert "**Review: approve**" in body and "`x.py:3` naming" in body


def test_project_template_custom_style(tmp_path):
    (tmp_path / "harness.toml").write_text(
        '[review]\npr_comment = "reviewed_by: {reviewer}/{model}\\nreviewed_head_sha: {head}"\n',
        encoding="utf-8",
    )
    body = prreview.render(config.load(tmp_path), V, "b" * 40)
    assert "reviewed_by: reviewer/sonnet\nreviewed_head_sha: " + "a" * 40 in body


def test_bad_template_is_a_clear_error(tmp_path):
    (tmp_path / "harness.toml").write_text(
        '[review]\npr_comment = "{nope}"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="bad placeholder"):
        prreview.render(config.load(tmp_path), V, "b")


def test_publish_creates_then_updates_one_comment(tmp_path, github):
    cfg = config.load(tmp_path)
    assert prreview.publish(cfg, "https://x/pull/7", V, "b" * 40) == "created"
    v2 = Verdict(head="c" * 40, verdict="approve", round=3, model="sonnet")
    assert prreview.publish(cfg, "https://x/pull/7", v2, "d" * 40) == "updated"
    assert len(github.comments[7]) == 1 and "c" * 40 in github.comments[7][0]["body"]


def test_verify_pr(tmp_path, github, capsys):
    cfg = config.load(tmp_path)
    github.pr_heads[7] = "b" * 40
    assert (
        cli.main(["-C", str(tmp_path), "review", "verify-pr", "7"]) == 1
    )  # no comment yet
    prreview.publish(cfg, "7", V, "b" * 40)
    assert cli.main(["-C", str(tmp_path), "review", "verify-pr", "7"]) == 0
    github.pr_heads[7] = "e" * 40  # a new push after the review
    ok, line = prreview.verify(cfg, "7")
    assert not ok and "review is for" in line


def test_verify_reads_only_the_pr_authors_comment(tmp_path, github):
    cfg = config.load(tmp_path)
    github.pr_heads[7] = "b" * 40
    forged = prreview.render(cfg, V, "b" * 40)
    github.comment(7, forged, user="mallory")
    ok, line = prreview.verify(cfg, "7")
    assert not ok and "no harness review comment by the PR author (me)" in line
    # publish leaves the stranger's marker comment alone and writes its own
    assert prreview.publish(cfg, "7", V, "b" * 40) == "created"
    assert github.comments[7][0]["body"] == forged
    assert prreview.verify(cfg, "7")[0]


def test_verify_survives_a_broken_marker_and_rejects_changes(tmp_path, github):
    cfg = config.load(tmp_path)
    github.pr_heads[7] = "b" * 40
    github.comment(7, prreview.MARKER + "{not json} -->\nhi")
    assert prreview.verify(cfg, "7") == (
        False,
        "PR #7: no harness review comment by the PR author (me)",
    )
    no = Verdict(head="a" * 40, verdict="changes-requested", round=1, model="sonnet")
    prreview.publish(cfg, "7", no, "b" * 40)
    assert prreview.verify(cfg, "7") == (False, "PR #7: verdict is changes-requested")


def test_required_contexts(tmp_path, github):
    github.required = ["required", "harness/review"]
    assert gh_mod.required_contexts(tmp_path, "main") == ["required", "harness/review"]


@pytest.mark.parametrize(
    ("required", "want"),
    [
        ("404", []),
        (["required", "app:harness/review"], ["required", "app:harness/review"]),
    ],
)
def test_required_contexts_shapes(tmp_path, github, required, want):
    github.required = required
    assert gh_mod.required_contexts(tmp_path, "main") == want


def test_required_contexts_error_is_not_empty(tmp_path, github):
    github.required = "error"
    with pytest.raises(gh_mod.GhError, match="boom"):
        gh_mod.required_contexts(tmp_path, "main")


@pytest.fixture
def github_repo(tmp_path, monkeypatch):
    from conftest import git

    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "remote", "add", "origin", "https://github.com/me/proj.git")
    monkeypatch.setattr("harness.cli.tools.usable", lambda t: True)
    return tmp_path


@pytest.mark.parametrize(
    ("required", "fails"),
    [([], True), (["required"], True), (["required", "harness/review"], False)],
)
def test_doctor_requires_the_review_check(github_repo, github, capsys, required, fails):
    github.required = required
    cli.main(["-C", str(github_repo), "doctor"])
    out = capsys.readouterr().out
    assert ("does not require the check `harness/review`" in out) is fails


def test_doctor_reports_an_unreadable_protection(github_repo, github, capsys):
    github.required = "error"
    cli.main(["-C", str(github_repo), "doctor"])
    assert "branch protection not checked" in capsys.readouterr().err

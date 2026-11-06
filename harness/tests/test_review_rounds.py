"""Rounds after a rejection: the reviewer sees the fix, not the whole branch
again; a rewritten branch gets the full diff; later rounds need a stronger model."""

import json
from pathlib import Path

import pytest
from conftest import git
from test_review import write_verdict
from test_review import wt as _wt

from harness import config, review

wt = _wt  # the task worktree fixture from test_review
LIB = "py/poly-lib/src/poly_lib/__init__.py"
BUG = {"file": LIB, "line": 1, "severity": "blocker", "text": "the change is wrong"}


def commit(wt, text, msg="fix: address review"):
    p = wt / LIB
    p.write_text(p.read_text(encoding="utf-8") + text, encoding="utf-8")
    git(wt, "commit", "-qam", msg)


def reject(wt):
    review.prepare(config.load(wt), wt)
    write_verdict(wt, "changes-requested", findings=[BUG])


def diff_part(text):
    return text.split("```diff\n", 1)[1]


def test_second_round_shows_only_the_fix_and_the_findings(wt):
    cfg = config.load(wt)
    reject(wt)
    commit(wt, "# fixed\n")
    pkg, rnd, _ = review.prepare(cfg, wt)
    text = pkg.read_text(encoding="utf-8")
    assert rnd == 2 and "Fix diff" in text and "Diff stat (whole branch)" in text
    assert "the change is wrong" in text  # previous findings to verify
    assert "+# fixed" in diff_part(text) and "+# change" not in diff_part(text)
    assert "Model: sonnet" in text
    write_verdict(wt, "approve", rnd=2)
    assert review.gate(cfg, wt) is not None


def test_rewritten_branch_gets_the_full_diff(wt):
    reject(wt)
    p = wt / LIB
    p.write_text(p.read_text(encoding="utf-8") + "# fixed\n", encoding="utf-8")
    git(wt, "commit", "-q", "-a", "--amend", "--no-edit")
    pkg, rnd, _ = review.prepare(config.load(wt), wt)
    text = pkg.read_text(encoding="utf-8")
    assert rnd == 2 and "Fix diff" not in text
    assert "+# change" in diff_part(text) and "+# fixed" in diff_part(text)


def test_without_origin_anchor_the_round_gets_the_full_diff(wt):
    """A verdict whose head origin did not record as the last package (an old counter, a
    hand-made verdict) gives no fix diff: nothing hides behind a base inside the branch."""
    reject(wt)
    commit(wt, "# fixed\n")
    write_verdict(wt, "changes-requested", head=review._head(wt), findings=[BUG])
    commit(wt, "# more\n")
    text = review.prepare(config.load(wt), wt)[0].read_text(encoding="utf-8")
    assert "Fix diff" not in text and "+# change" in diff_part(text)


def test_forged_fix_base_is_refused(wt):
    cfg = config.load(wt)
    reject(wt)
    commit(wt, "# one\n")
    commit(wt, "# two\n")
    pkg, _, _ = review.prepare(cfg, wt)
    state = json.loads((wt / review.STATE).read_text(encoding="utf-8"))
    inside = review._git(wt, "rev-parse", "HEAD~1").strip()
    body = review._package_body(wt)
    assert body is not None
    short = review._git(wt, "diff", inside, "HEAD")
    body = body.split("```diff\n")[0] + f"```diff\n{short}\n```\n"
    pkg.write_bytes(
        (review.PACKAGE_ID.format(review._sha(body)) + body).encode("utf-8")
    )
    forged = {**state, "package": review._sha(body), "base": inside}
    (wt / review.STATE).write_text(json.dumps(forged), encoding="utf-8")
    write_verdict(wt, "approve", rnd=2)
    with pytest.raises(review.ReviewError, match="does not show the approved commits"):
        review.gate(cfg, wt)


def test_third_round_needs_opus(wt, capsys):
    from harness import cli

    cfg = config.load(wt)
    reject(wt)
    commit(wt, "# one\n")
    reject(wt)
    commit(wt, "# two\n")
    assert cli.main(["-C", str(wt), "review", "prepare"]) == 0
    out = capsys.readouterr().out
    assert "round 3 of 3, fix diff" in out and "model: opus" in out
    write_verdict(wt, "approve", rnd=3)  # written by sonnet
    with pytest.raises(review.ReviewError, match="needs a review by opus"):
        review.gate(cfg, wt)
    write_verdict(wt, "approve", rnd=3, model="claude-opus-5-5")
    assert review.gate(cfg, wt) is not None


def test_review_models_from_config():
    cfg = config.Config(root=Path("."), name="x")
    assert [review.model_for(cfg, r) for r in (1, 2, 3, 4)] == [
        "sonnet", "sonnet", "opus", "opus"
    ]  # fmt: skip
    assert config._review_models({"models": ["Haiku"]}) == ["haiku"]
    for bad in ([], "opus", [""], [1]):
        with pytest.raises(config.ConfigError, match="review.models"):
            config._review_models({"models": bad})


@pytest.mark.parametrize(
    ("author", "rnd", "expected"),
    [
        (set(), 1, "sonnet"),  # no trailer: by round
        ({"sonnet"}, 1, "opus"),  # never the author's model
        ({"sonnet"}, 3, "opus"),
        ({"opus"}, 3, "sonnet"),
        ({"sonnet", "opus"}, 1, "opus"),  # every configured model wrote it: not sonnet
        ({"haiku"}, 1, "sonnet"),
    ],
)
def test_reviewer_model_is_not_the_authors(author, rnd, expected):
    """a branch written on sonnet is not reviewed by sonnet."""
    cfg = config.Config(root=Path("."), name="x")
    assert review.model_for(cfg, rnd, author) == expected


def test_author_from_trailers_switches_the_model_and_the_gate(wt, capsys):
    from harness import cli

    trailer = "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
    commit(wt, "# by sonnet\n", f"feat: x\n\n{trailer}")
    cfg = config.load(wt)
    assert review.author_models(
        wt, review._git(wt, "merge-base", "main", "HEAD").strip()
    ) == {"sonnet"}
    assert cli.main(["-C", str(wt), "review", "prepare"]) == 0
    out = capsys.readouterr().out
    assert "model: opus (author: sonnet)" in out
    write_verdict(wt, "approve", rnd=1)  # written by sonnet
    with pytest.raises(review.ReviewError, match="needs a review by opus"):
        review.gate(cfg, wt)
    write_verdict(wt, "approve", rnd=1, model="claude-opus-5-5")
    assert review.gate(cfg, wt) is not None

"""The review survives a rebase: conflicts are refused before a round, a clean
rebase keeps the approval, a hand-resolved one gets a delta review with its own limit."""

import json
import re
import subprocess

import pytest
from conftest import git
from test_review import write_verdict
from test_review import wt as _wt
from test_ship import FakeGh

from harness import config, review, ship

wt = _wt  # the task worktree fixture from test_review
LIB = "py/poly-lib/src/poly_lib/__init__.py"


def advance_main(world, rel, text):
    """Someone else merges into main: a commit pushed from the second clone."""
    _, _, b = world
    git(b, "pull", "-q", "origin", "main")
    p = b / rel
    p.write_text(p.read_text(encoding="utf-8") + text, encoding="utf-8")
    git(b, "commit", "-qam", f"main: {rel}")
    git(b, "push", "-q", "origin", "main")


def approve(wt):
    review.prepare(config.load(wt), wt)
    write_verdict(wt, "approve")
    return review._head(wt)


def rebase_resolving(wt):
    """Rebase onto origin/main; on a conflict keep both sides by hand."""
    git(wt, "fetch", "-q", "origin")
    r = subprocess.run(["git", "-C", str(wt), "rebase", "-q", "origin/main"],
                       capture_output=True, check=False)  # fmt: skip
    if r.returncode != 0:
        (wt / LIB).write_text(
            (wt / LIB).read_text(encoding="utf-8").replace("<<<<<<< ", "#<").replace(
                "=======", "#=").replace(">>>>>>> ", "#>"),
            encoding="utf-8",
        )  # fmt: skip
        git(wt, "add", LIB)
        git(wt, "-c", "core.editor=true", "rebase", "--continue")


def origin_counters(wt):
    return review._origin_state(config.load(wt), "gh-5")[1]


def test_conflicting_branch_is_refused_before_a_round(world, wt):
    advance_main(world, LIB, "# main\n")
    with pytest.raises(review.ReviewError, match="rebase first") as e:
        review.prepare(config.load(wt), wt)
    assert LIB in str(e.value)
    assert origin_counters(wt).get("rounds", 0) == 0


def test_clean_rebase_keeps_the_approval(world, wt):
    approved = approve(wt)
    advance_main(world, "crates/poly-core/src/lib.rs", "// main\n")
    rebase_resolving(wt)
    cfg = config.load(wt)
    assert review._head(wt) != approved
    v = review.gate(cfg, wt)
    assert v is not None and v.carried and v.head == approved
    with pytest.raises(review.NothingToReview, match="covers HEAD"):
        review.prepare(cfg, wt)


def test_new_commits_after_approval_still_need_review(world, wt):
    approve(wt)
    # (an empty commit would pass: the gate compares trees, and the code is unchanged)
    (wt / LIB).write_text(
        (wt / LIB).read_text(encoding="utf-8") + "X = 2\n", encoding="utf-8"
    )
    git(wt, "commit", "-qam", "feat: more")
    with pytest.raises(review.ReviewError, match="review the new commits"):
        review.gate(config.load(wt), wt)


def test_hand_resolved_rebase_gets_a_delta_review(world, wt):
    approved = approve(wt)
    advance_main(world, LIB, "# main\n")
    rebase_resolving(wt)
    cfg = config.load(wt)
    with pytest.raises(review.ReviewError, match="review is for"):
        review.gate(cfg, wt)  # hand edits: the old approval does not carry over
    pkg, rnd, _ = review.prepare(cfg, wt)
    text = pkg.read_text(encoding="utf-8")
    assert "delta review 1 of 2" in text and "## Range-diff" in text
    assert approved[:10] in text
    counters = origin_counters(wt)
    assert rnd == 1 and (counters["rounds"], counters["delta"]) == (1, 1)
    assert counters["approved"] == approved and counters["approved_base"]
    write_verdict(wt, "approve")
    v = review.gate(cfg, wt)
    assert v is not None and v.head == review._head(wt) and not v.carried
    # new work after the delta: a full round, and it resets the delta reviews
    (wt / LIB).write_text(
        (wt / LIB).read_text(encoding="utf-8") + "X = 3\n", encoding="utf-8"
    )
    git(wt, "commit", "-qam", "feat: more")
    _, rnd, _ = review.prepare(cfg, wt)
    assert rnd == 1 and origin_counters(wt)["delta"] == 0  # no attempt spent


def test_a_merge_instead_of_a_rebase_gets_a_full_round(world, wt):
    """range-diff skips merge commits, so a branch with one never gets a delta review."""
    approve(wt)
    advance_main(world, LIB, "# main\n")
    git(wt, "fetch", "-q", "origin")
    r = subprocess.run(["git", "-C", str(wt), "merge", "-q", "origin/main"],
                       capture_output=True, check=False)  # fmt: skip
    assert r.returncode != 0  # the conflict, resolved by hand with an extra edit
    text = (wt / LIB).read_text(encoding="utf-8")
    for mark in ("<<<<<<< HEAD\n", "=======\n"):
        text = text.replace(mark, "")
    text = text.split(">>>>>>>")[0] + "HIDDEN = 1\n"
    (wt / LIB).write_text(text, encoding="utf-8")
    git(wt, "commit", "-qam", "merge main")
    cfg = config.load(wt)
    pkg, rnd, _ = review.prepare(cfg, wt)
    body = pkg.read_text(encoding="utf-8")
    # after the approve: a full round that spends no attempt
    assert rnd == 1 and "delta review" not in body and "+HIDDEN = 1" in body
    assert "Re-review after an approve" in body
    assert not review._linear(wt, review._state(wt)["base"], review._head(wt))


def test_delta_review_must_stay_anchored_to_the_approval(world, wt):
    approve(wt)
    advance_main(world, LIB, "# main\n")
    rebase_resolving(wt)
    cfg = config.load(wt)
    review.prepare(cfg, wt)
    write_verdict(wt, "approve")
    sha, counters = review._origin_state(cfg, "gh-5")
    review._record(cfg, "gh-5", sha, {**counters, "approved": "0" * 40})
    with pytest.raises(review.ReviewError, match="not anchored"):
        review.gate(cfg, wt)


def test_delta_reviews_have_their_own_limit(world, wt):
    approved = approve(wt)
    advance_main(world, LIB, "# main\n")
    rebase_resolving(wt)
    cfg = config.load(wt)
    sha, _ = review._origin_state(cfg, "gh-5")
    review._record(cfg, "gh-5", sha, {"rounds": 1, "delta": 2, "approved": approved})
    with pytest.raises(review.ReviewError, match="2 delta reviews after rebases"):
        review.prepare(cfg, wt)


def test_ship_rebases_an_approved_branch_and_keeps_the_approval(world, wt, monkeypatch):
    """main moved after the review; ship rebases by itself, no new round."""
    approved = approve(wt)
    advance_main(world, "crates/poly-core/src/lib.rs", "// main\n")
    monkeypatch.setattr(ship, "gh", FakeGh())
    monkeypatch.setattr(ship.tools, "usable", lambda t: True)
    r = ship.ship(config.load(wt), wt)
    assert review._head(wt) != approved
    assert "rebased onto origin/main (+1 commit(s))" in r.notes
    assert f"approval of {approved[:10]} carried over a clean rebase" in r.notes


CORE = "crates/poly-core/src/lib.rs"
BUG = {"file": LIB, "line": 1, "severity": "blocker", "text": "the change is wrong"}


def approve_after_a_fix(wt):
    """reject → fix → the fix round approved: its package's base is the rejected head."""
    cfg = config.load(wt)
    review.prepare(cfg, wt)
    write_verdict(wt, "changes-requested", findings=[BUG])
    (wt / CORE).write_text(
        (wt / CORE).read_text(encoding="utf-8") + "// fixed\n", encoding="utf-8"
    )
    git(wt, "commit", "-qam", "fix: address review")
    review.prepare(cfg, wt)
    assert review._state(wt).get("fix")
    write_verdict(wt, "approve", rnd=2)
    return review._head(wt)


def test_clean_rebase_after_an_approved_fix_round_ships(world, wt):
    """the approval of a fix round carries over a clean rebase."""
    approved = approve_after_a_fix(wt)
    advance_main(world, "ts/lib/src/index.ts", "// main\n")
    rebase_resolving(wt)
    cfg = config.load(wt)
    v = review.gate(cfg, wt)
    assert v is not None and v.carried and v.head == approved
    with pytest.raises(review.NothingToReview, match="covers HEAD"):
        review.prepare(cfg, wt)


def test_hand_resolved_rebase_after_a_fix_round_gets_a_delta_and_ships(world, wt):
    """the delta is anchored on origin/main, not on the fix base inside
    the branch; after its approve ship passes, also after one more clean rebase."""
    approve_after_a_fix(wt)
    advance_main(world, LIB, "# main\n")
    rebase_resolving(wt)
    cfg = config.load(wt)
    pkg, _, _ = review.prepare(cfg, wt)
    text = pkg.read_text(encoding="utf-8")
    assert "delta review 1 of 2" in text
    # the approved side is the whole approved branch, not only the fix: the commits
    # before the fix round are not shown as new
    rd = text.split("## Range-diff", 1)[1]
    assert len(re.findall(r"^\d+:", rd, re.MULTILINE)) == 2, rd
    assert "feat: change" in rd and "fix: address review" in rd
    write_verdict(wt, "approve")
    v = review.gate(cfg, wt)
    assert v is not None and v.head == review._head(wt)
    # main moves again, a clean rebase: prepare and the gate agree (B3)
    advance_main(world, "ts/lib/src/index.ts", "// main\n")
    rebase_resolving(wt)
    with pytest.raises(review.NothingToReview, match="covers HEAD"):
        review.prepare(cfg, wt)
    v = review.gate(cfg, wt)
    assert v is not None and v.carried


def test_nothing_to_review_only_when_the_gate_takes_the_package(world, wt):
    """a package the gate refuses (an old one anchored inside the branch)
    gets a new round from prepare, not "nothing to review"."""
    approve_after_a_fix(wt)
    fix_base = review._state(wt)["base"]  # the rejected head, inside the branch
    advance_main(world, LIB, "# main\n")
    rebase_resolving(wt)
    cfg = config.load(wt)
    review.prepare(cfg, wt)
    write_verdict(wt, "approve")
    state = review._state(wt)  # what an older h wrote
    (wt / review.STATE).write_text(json.dumps({**state, "prev_base": fix_base}),
                                   encoding="utf-8")  # fmt: skip
    sha, counters = review._origin_state(cfg, "gh-5")
    review._record(cfg, "gh-5", sha, {**counters, "approved_base": fix_base})
    advance_main(world, "ts/lib/src/index.ts", "// main\n")
    rebase_resolving(wt)
    with pytest.raises(review.ReviewError, match="does not show the approved commits"):
        review.gate(cfg, wt)
    pkg, _, _ = review.prepare(cfg, wt)  # no NothingToReview
    assert "delta review 2 of 2" in pkg.read_text(encoding="utf-8")
    write_verdict(wt, "approve")
    assert review.gate(cfg, wt) is not None

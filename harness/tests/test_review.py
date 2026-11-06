import io
import json
import subprocess
import sys

import pytest
from conftest import git
from test_ship import FakeGh

from harness import cards, cli, config, gh, init, review, ship, task

PY = sys.executable.replace("\\", "/")
CHECKS = f"""
[stacks.rust]
enabled = false
[stacks.ts]
enabled = false
[stacks.python.commands]
fmt = "{PY} -c pass"
fmt_write = "{PY} -c pass"
lint = "{PY} -c pass"
typecheck = "{PY} -c pass"
test = "{PY} -c pass"
"""
TRIVIAL = cards.render({"Problem": "p", "Assumptions": "none", "Verify": "h check",
                        "Size": "S", "Trivial": "yes", "Trivial reason": "t"})  # fmt: skip
# The same card with its Problem rewritten after start.
EDITED = TRIVIAL.replace("\n\np\n\n", "\n\nrewritten\n\n", 1)


@pytest.fixture
def wt(world, github, monkeypatch):
    _, a, _ = world
    with (a / "harness.toml").open("a", encoding="utf-8") as f:
        f.write(CHECKS)
    git(a, "commit", "-qam", "checks")
    git(a, "push", "-q", "origin", "main")
    github.add_issue(TRIVIAL, ["ac-confirmed"], number=5)
    r = task.start(config.load(a), "gh-5")
    f = r.path / "py" / "poly-lib" / "src" / "poly_lib" / "__init__.py"
    f.write_text(f.read_text(encoding="utf-8") + "# change\n", encoding="utf-8")
    git(r.path, "commit", "-qam", "feat: change")
    return r.path


def write_verdict(
    wt, verdict, head=None, rnd=1, findings=(), package=None, model="sonnet"
):
    """What the reviewer writes: it copies the package id from the package it read."""
    head = head or review._head(wt)
    package = review._state(wt).get("package", "") if package is None else package
    (wt / review.DIR).mkdir(parents=True, exist_ok=True)
    (wt / review.VERDICT).write_text(json.dumps({"head": head, "verdict": verdict, "round": rnd,
                                                 "model": model, "findings": list(findings),
                                                 "package": package}),
                                     encoding="utf-8")  # fmt: skip


def test_prepare_writes_package_with_card_and_diff(wt):
    pkg, rnd, _ = review.prepare(config.load(wt), wt)
    text = pkg.read_text(encoding="utf-8")
    assert rnd == 1 and "round 1 of 3" in text
    assert "# Login with Google (#5)" in text  # the card
    assert "+# change" in text  # the diff
    assert "feat: change" in text


def test_prepare_reads_the_card_from_the_issue_now(wt, github):
    """B1: a card edited after start reaches the reviewer, and brief.md follows."""
    github.issues[5]["body"] = EDITED
    pkg, _, _ = review.prepare(config.load(wt), wt)
    text = pkg.read_text(encoding="utf-8")
    assert "rewritten" in text and "may be out of date" not in text
    brief = (wt / ".artifacts" / "task" / "brief.md").read_text(encoding="utf-8")
    assert "rewritten" in brief


def test_prepare_falls_back_to_the_brief_when_github_is_down(wt, github, monkeypatch):
    """B2: no GitHub — the saved brief, with a note that it may be stale."""
    github.issues[5]["body"] = EDITED

    def down(cwd, *argv):
        if list(argv[:2]) == ["issue", "view"]:
            return subprocess.CompletedProcess(
                argv, 1, "", "error connecting to api.github.com"
            )
        return github(cwd, *argv)

    monkeypatch.setattr(gh, "run", down)
    pkg, _, _ = review.prepare(config.load(wt), wt)
    text = pkg.read_text(encoding="utf-8")
    assert "card may be out of date" in text and "error connecting" in text
    assert "# Login with Google (#5)" in text and "rewritten" not in text


def test_gate(wt):
    cfg = config.load(wt)
    with pytest.raises(review.ReviewError, match="no review"):
        review.gate(cfg, wt)
    review.prepare(cfg, wt)
    write_verdict(
        wt,
        "changes-requested",
        findings=[{"file": "a.py", "line": 3, "severity": "blocker", "text": "bug"}],
    )
    with pytest.raises(review.ReviewError, match="a.py:3 bug"):
        review.gate(cfg, wt)
    write_verdict(wt, "approve", head="0" * 40)
    with pytest.raises(review.ReviewError, match="review the new commits"):
        review.gate(cfg, wt)
    write_verdict(wt, "approve")
    v = review.gate(cfg, wt)
    assert v is not None and v.verdict == "approve"


def test_fourth_round_needs_the_user(wt):
    cfg = config.load(wt)
    for _ in range(3):
        review.prepare(cfg, wt)
        write_verdict(wt, "changes-requested")
    with pytest.raises(review.ReviewError, match="ask the user"):
        review.prepare(cfg, wt)


def test_ship_refuses_without_review_and_reports_it_in_pr(wt, monkeypatch):
    cfg = config.load(wt)
    fake = FakeGh()
    monkeypatch.setattr(ship, "gh", fake)
    monkeypatch.setattr(ship.tools, "usable", lambda t: True)
    with pytest.raises(ship.ShipRefused, match="no review"):
        ship.ship(cfg, wt)
    assert fake.calls == []
    review.prepare(cfg, wt)
    write_verdict(
        wt,
        "approve",
        findings=[{"file": "b.py", "line": 7, "severity": "note", "text": "naming"}],
    )
    ship.ship(cfg, wt)
    create = next(c for c in fake.calls if c[:2] == ["pr", "create"])
    body = create[create.index("--body") + 1]
    assert "## Review" in body and "approved by `reviewer` (sonnet)" in body
    assert "`b.py:7` naming" in body


@pytest.mark.parametrize(
    ("agent", "code"), [(None, 2), ("general-purpose", 2), ("reviewer", 0)]
)
def test_only_reviewer_may_write_the_verdict(monkeypatch, agent, code):
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "/x/.artifacts/review/verdict.json"},
    }
    if agent:
        payload["agent_type"] = agent
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["hook", "pre-write"]) == code


def test_pre_write_ignores_other_files(monkeypatch):
    payload = {"tool_input": {"file_path": "/x/src/main.rs"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["hook", "pre-write"]) == 0


def test_init_installs_reviewer_hook_and_deny(tmp_path):
    init.run(config.load(tmp_path))
    s = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "Bash(*verdict.json*)" in s["permissions"]["deny"]
    [group] = s["hooks"]["PreToolUse"]
    assert group["hooks"] == [init.PRE_HOOK]
    assert (tmp_path / ".claude" / "agents" / "reviewer.md").is_file()
    assert (tmp_path / ".agents" / "skills" / "review-gate" / "SKILL.md").is_file()


# A real, deterministic formatter for these tests: drops trailing blank lines.
STRIP = [PY, "-c", ("import sys, pathlib; p = pathlib.Path(sys.argv[1]); "
         "p.write_bytes(p.read_bytes().rstrip(b'\\r\\n') + b'\\n')")]  # fmt: skip


@pytest.fixture
def unformatted(wt, monkeypatch):
    """The reviewed commit leaves blank lines that the formatter will remove."""
    monkeypatch.setattr(
        review.fmt, "_commands", lambda cfg, stack, f: [[*STRIP, str(f)]]
    )
    f = wt / "py" / "poly-lib" / "src" / "poly_lib" / "__init__.py"
    f.write_bytes(f.read_bytes() + b"\n\n")  # bytes: no CRLF translation on Windows
    git(wt, "commit", "-qam", "feat: blank lines")
    review.prepare(config.load(wt), wt)
    write_verdict(wt, "approve")
    return f


def test_style_commit_on_top_of_reviewed_head_still_passes(wt, unformatted):
    cfg = config.load(wt)
    assert review.fmt.format_file(cfg, unformatted).status == "formatted"
    git(wt, "commit", "-qam", ship.STYLE_MESSAGE)
    assert review.gate(cfg, wt) is not None
    assert not list(unformatted.parent.glob(".h-fmtcheck-*"))
    git(wt, "commit", "-q", "--allow-empty", "-m", "feat: more")
    with pytest.raises(review.ReviewError, match="review the new commits"):
        review.gate(cfg, wt)


def test_style_subject_with_code_changes_needs_review(wt, unformatted):
    cfg = config.load(wt)
    review.fmt.format_file(cfg, unformatted)
    unformatted.write_text(
        unformatted.read_text(encoding="utf-8") + "X = 1\n", encoding="utf-8"
    )
    git(wt, "commit", "-qam", ship.STYLE_MESSAGE)
    with pytest.raises(review.ReviewError, match="review the new commits"):
        review.gate(cfg, wt)


def test_style_subject_on_an_unformattable_file_needs_review(wt, unformatted):
    (wt / "NOTES.md").write_text("not formatting\n", encoding="utf-8")
    git(wt, "add", "NOTES.md")
    git(wt, "commit", "-qm", ship.STYLE_MESSAGE)
    with pytest.raises(review.ReviewError, match="review the new commits"):
        review.gate(config.load(wt), wt)


def test_style_commit_without_a_formatter_needs_review(wt, unformatted, monkeypatch):
    cfg = config.load(wt)
    review.fmt.format_file(cfg, unformatted)
    git(wt, "commit", "-qam", ship.STYLE_MESSAGE)
    monkeypatch.setattr(review.fmt, "_commands", lambda cfg, stack, f: None)
    with pytest.raises(review.ReviewError, match="review the new commits"):
        review.gate(cfg, wt)


def test_corrupt_state_is_a_clear_error(wt):
    (wt / review.DIR).mkdir(parents=True, exist_ok=True)
    (wt / review.STATE).write_text("{oops", encoding="utf-8")
    with pytest.raises(review.ReviewError, match="corrupt"):
        review.prepare(config.load(wt), wt)


def test_style_subject_matches_ship():
    assert review.STYLE_SUBJECT == ship.STYLE_MESSAGE


def test_package_edited_after_prepare_is_refused(wt):
    cfg = config.load(wt)
    pkg, _, _ = review.prepare(cfg, wt)
    write_verdict(wt, "approve")
    assert review.gate(cfg, wt) is not None
    pkg.write_text(
        pkg.read_text(encoding="utf-8").replace("+# change", "+"), encoding="utf-8"
    )
    with pytest.raises(review.ReviewError, match="changed after `h review prepare`"):
        review.gate(cfg, wt)


def test_verdict_must_name_the_package_it_read(wt):
    cfg = config.load(wt)
    review.prepare(cfg, wt)
    write_verdict(wt, "approve", package="")
    with pytest.raises(review.ReviewError, match="not for the current review package"):
        review.gate(cfg, wt)


def test_verdict_without_a_package_does_not_count(wt):
    write_verdict(wt, "approve")
    with pytest.raises(review.ReviewError, match="no review package"):
        review.gate(config.load(wt), wt)


def test_forged_state_cannot_hide_commits(wt):
    """Recomputing the id and state does not help: the diff is checked against git, and
    a base inside the branch is refused."""
    cfg = config.load(wt)
    git(wt, "commit", "-q", "--allow-empty", "-m", "feat: hidden")
    pkg, _, _ = review.prepare(cfg, wt)
    state = json.loads((wt / review.STATE).read_text(encoding="utf-8"))

    def forge(body, base):
        pkg.write_bytes(
            (review.PACKAGE_ID.format(review._sha(body)) + body).encode("utf-8")
        )
        forged = {**state, "package": review._sha(body), "base": base}
        (wt / review.STATE).write_text(json.dumps(forged), encoding="utf-8")
        write_verdict(wt, "approve")

    body = review._package_body(wt)
    assert body is not None
    forge(body.replace("+# change", "+# looks harmless"), state["base"])
    with pytest.raises(review.ReviewError, match="does not show the approved commits"):
        review.gate(cfg, wt)
    inside = review._git(wt, "rev-parse", "HEAD~1").strip()
    short = review._git(wt, "diff", inside, "HEAD")
    forge(body.split("```diff\n")[0] + f"```diff\n{short}\n```\n", inside)
    with pytest.raises(review.ReviewError, match="does not show the approved commits"):
        review.gate(cfg, wt)


@pytest.mark.parametrize(
    ("path", "hit"),
    [
        ("/x/.artifacts/review/Verdict.JSON", True),
        (r"C:\x\.ARTIFACTS\Review\verdict.json", True),
        ("/x/foo/../.artifacts/review/verdict.json", True),
        (".artifacts/review/verdict.json", True),
        ("/x/.artifacts/review/verdict.json.", True),
        ("/x/.artifacts/review/verdict.json ", True),
        ("/x/.artifacts/review/verdict.json::$DATA", True),
        ("/x/.artifacts./review/verdict.json", True),
        ("/x/.artifacts/review/verdict.json.bak", False),
        ("/x/.artifacts/review/../verdict.json", False),
        ("/x/src/verdict.json", False),
    ],
)
def test_verdict_path_in_any_spelling(path, hit):
    assert review.is_verdict_path(path) is hit


def test_pre_write_refuses_another_case_and_an_unreadable_payload(monkeypatch):
    payload = {"tool_input": {"file_path": "C:/x/.Artifacts/Review/VERDICT.json"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["hook", "pre-write"]) == 2
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    assert cli.main(["hook", "pre-write"]) == 2
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    assert cli.main(["hook", "post-edit"]) == 0  # the formatter hook stays lenient


def test_deleting_the_review_dir_does_not_reset_the_rounds(wt):
    import shutil

    cfg = config.load(wt)
    for _ in range(3):
        review.prepare(cfg, wt)
        write_verdict(wt, "changes-requested")
    shutil.rmtree(wt / review.DIR)
    with pytest.raises(review.ReviewError, match="ask the user"):
        review.prepare(cfg, wt)
    review.reset_rounds(cfg, "gh-5")  # what done / drop do with the task
    _, rnd, _ = review.prepare(cfg, wt)
    assert rnd == 1

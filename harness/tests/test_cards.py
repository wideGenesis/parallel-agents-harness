import json

import pytest
from conftest import out

from harness import cards, cli, config, epics, gh, task
from harness.task import NotReady

READY_BODY = cards.render(
    {
        "Problem": "Users cannot log in with Google.",
        "Assumptions": "- A1: Google ID tokens only — confirmed",
        "AC-B (business, Given/When/Then)": "- [ ] B1: Given a Google account, When I tap Login, Then I see my dashboard",
        "AC-T (technical, ← AC-B)": "- [ ] T1 (←B1): POST /v1/auth/google verifies the ID token",
        "Verify": "cargo test -p api auth::google",
        "Size": "S",
        "Trivial": "no",
    }
)


def test_render_parse_roundtrip_and_trivial_reason_heading():
    body = cards.render({"Trivial": "yes", "Trivial reason": "docs only"})
    s = cards.parse(body)
    assert s["Trivial"] == "yes" and s["Trivial reason"] == "docs only"


def test_ready_needs_user_confirmation():
    v = cards.check_ready(READY_BODY, [])
    assert not v.ready and v.problems == [
        "not confirmed by the user: business criteria need the user's label 'ac-confirmed'"
    ]
    assert cards.state_label(v) == "needs-clarification"
    assert cards.check_ready(READY_BODY, [cards.AC_CONFIRMED]).ready


TRIVIAL_BODY = cards.render({"Problem": "p", "Assumptions": "none", "Verify": "h check",
                             "Size": "S", "Trivial": "yes", "Trivial reason": "docs only"})  # fmt: skip


def test_trivial_claim_needs_the_user_too():
    v = cards.check_ready(TRIVIAL_BODY, [])
    assert not v.ready and "the trivial claim needs" in v.problems[0]
    assert cards.check_ready(TRIVIAL_BODY, [cards.AC_CONFIRMED]).ready


def test_editing_criteria_after_confirmation_voids_it():
    edited = READY_BODY.replace("I see my dashboard", "I see anything")
    v = cards.check_ready(edited, [cards.AC_CONFIRMED], confirmed_body=READY_BODY)
    assert not v.ready and "changed after the user confirmed" in v.problems[0]
    assert cards.state_label(v) == "needs-clarification"
    claimed = TRIVIAL_BODY.replace("docs only", "tiny")
    assert not cards.check_ready(claimed, [cards.AC_CONFIRMED], TRIVIAL_BODY).ready


def test_ticks_and_other_sections_keep_the_confirmation():
    ticked = READY_BODY.replace("- [ ] B1", "- [x] B1").replace(
        "verifies the ID token", "verifies the ID token and nonce"
    )
    assert cards.check_ready(
        ticked, [cards.AC_CONFIRMED], confirmed_body=READY_BODY
    ).ready


def test_stale_confirmation_blocks_start_ready_and_next(world, github, capsys):
    _, a, _ = world
    n = github.add_issue(READY_BODY.replace("dashboard", "settings"),
                         [cards.AC_CONFIRMED, "ready", "agent-ok"])  # fmt: skip
    github.issues[n]["confirmed_body"] = READY_BODY
    assert cli.main(["-C", str(a), "task", "next"]) == 1
    assert cli.main(["-C", str(a), "task", "ready", f"gh-{n}"]) == 1
    assert "changed after the user confirmed" in capsys.readouterr().out
    assert "needs-clarification" in github.issues[n]["labels"]
    with pytest.raises(NotReady, match="changed after the user confirmed"):
        task.start(config.load(a), f"gh-{n}")
    github.issues[n]["confirmed_body"] = github.issues[n][
        "body"
    ]  # the user re-confirmed
    assert cli.main(["-C", str(a), "task", "ready", f"gh-{n}"]) == 0


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (
            lambda b: b.replace(
                "Given a Google account, When I tap Login, Then", "Login works,"
            ),
            "not Given/When/Then",
        ),
        (lambda b: b.replace("(←B1)", ""), "does not trace"),
        (
            lambda b: b.replace("cargo test -p api auth::google", "_No response_"),
            "'Verify' is empty",
        ),
        (
            lambda b: b + "\n[NEEDS CLARIFICATION: which provider?]\n",
            "NEEDS CLARIFICATION",
        ),
        (lambda b: b.replace("### Size\n\nS", "### Size\n\nXL"), "Size must be"),
    ],
)
def test_ready_rules(mutate, problem):
    v = cards.check_ready(mutate(READY_BODY), [cards.AC_CONFIRMED])
    assert not v.ready and any(problem in p for p in v.problems), v.problems


def test_trivial_card_skips_ac_but_needs_reason():
    body = cards.render(
        {
            "Problem": "x",
            "Assumptions": "none",
            "Verify": "h check",
            "Size": "S",
            "Trivial": "yes",
        }
    )
    v = cards.check_ready(body, [cards.AC_CONFIRMED])
    assert not v.ready and "needs a reason" in v.problems[0]
    ok = cards.check_ready(
        body.replace(
            "### Trivial reason\n\n_No response_", "### Trivial reason\n\nrename only"
        ),
        [cards.AC_CONFIRMED],
    )
    assert ok.ready and ok.trivial


def test_labels_are_derived_not_gates():
    # A stale display label must never keep a confirmed card unready.
    v = cards.check_ready(
        READY_BODY, [cards.AC_CONFIRMED, "needs-clarification", "draft"]
    )
    assert v.ready and cards.state_label(v) == "ready"
    assert cards.state_label(cards.check_ready(READY_BODY, [])) == "needs-clarification"
    marked = READY_BODY + "\n[NEEDS CLARIFICATION: x]\n"
    assert cards.state_label(cards.check_ready(marked, [])) == "needs-clarification"
    assert cards.state_label(cards.check_ready("### Problem\n\nx\n", [])) == "draft"


def test_cli_ready_moves_labels(world, github, capsys):
    _, a, _ = world
    n = github.add_issue(READY_BODY, ["needs-clarification", cards.AC_CONFIRMED])
    assert cli.main(["-C", str(a), "task", "ready", f"gh-{n}"]) == 0
    assert "ready" in github.issues[n]["labels"]
    assert "needs-clarification" not in github.issues[n]["labels"]


# --- integration through a fake GitHub ------------------------------------------------


def test_start_refuses_unready_issue_and_leaves_nothing(world, github):
    _, a, _ = world
    n = github.add_issue(READY_BODY, [])
    with pytest.raises(NotReady, match="not confirmed"):
        task.start(config.load(a), f"gh-{n}")
    assert not (a / ".worktrees" / f"gh-{n}").exists()


def test_start_ready_issue_writes_brief_and_moves_labels(world, github):
    _, a, _ = world
    n = github.add_issue(READY_BODY, [cards.AC_CONFIRMED, "ready"])
    r = task.start(config.load(a), f"gh-{n}")
    brief = (r.path / ".artifacts" / "task" / "brief.md").read_text(encoding="utf-8")
    assert brief.startswith(f"# Login with Google (#{n})") and "B1: Given" in brief
    assert "in-progress" in github.issues[n]["labels"]
    assert "ready" not in github.issues[n]["labels"]


def test_start_refuses_closed_issue(world, github):
    _, a, _ = world
    n = github.add_issue(READY_BODY, [cards.AC_CONFIRMED], state="CLOSED")
    with pytest.raises(Exception, match="closed"):
        task.start(config.load(a), f"gh-{n}")


def test_milestone_past_the_first_page_is_found_not_duplicated(world, github):
    _, a, _ = world
    github.milestones += [f"e-{k}" for k in range(150)]
    assert epics.exists(config.load(a), "e-149")
    assert gh.ensure_milestone(a, "e-149", "d") is False
    assert github.milestones.count("e-149") == 1


def test_cli_epic_and_task_new(world, github, tmp_path, capsys):
    _, a, _ = world
    assert cli.main(["-C", str(a), "epic", "new", "auth", "--title", "Login"]) == 0
    # Published through an auto-merged PR, not left untracked in the checkout.
    assert not (a / epics.EPICS_DIR / "auth.md").exists()
    shown = out(a, "show", "origin/chore/epic-auth-new:docs/epics/auth.md")
    assert shown.count(epics.START) == 1
    assert github.prs[-1]["auto"] is True
    assert github.milestones == ["auth"]
    card = tmp_path / "card.md"
    card.write_text(READY_BODY, encoding="utf-8")
    assert cli.main(["-C", str(a), "task", "new", "--title", "Google login", "--epic", "auth",
                     "--body", str(card)]) == 0  # fmt: skip
    printed = capsys.readouterr().out
    assert "task gh-1: created" in printed and "NOT READY" in printed
    issue = github.issues[1]
    assert issue["milestone"] == {"title": "auth"}
    assert set(issue["labels"]) == {"needs-clarification", "size:S"}
    assert cli.main(["-C", str(a), "task", "ready", "gh-1"]) == 1
    issue["labels"] = [cards.AC_CONFIRMED]
    assert cli.main(["-C", str(a), "task", "ready", "gh-1"]) == 0


def test_task_new_rejects_unknown_epic(world, github):
    _, a, _ = world
    assert (
        cli.main(["-C", str(a), "task", "new", "--title", "x", "--epic", "nope"]) == 2
    )


def test_init_github_creates_labels_and_deny_rule(world, github):
    _, a, _ = world
    assert cli.main(["-C", str(a), "init", "--github"]) == 0
    assert cards.AC_CONFIRMED in github.labels and "size:L" in github.labels
    settings = json.loads((a / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "Bash(gh *ac-confirmed*)" in settings["permissions"]["deny"]
    assert (a / ".github" / "ISSUE_TEMPLATE" / "task.yml").is_file()


def test_issue_labels_removes_only_present_labels(github):
    from pathlib import Path

    n = github.add_issue(READY_BODY, ["review"])
    seen = []
    real = github.__call__

    def strict(cwd, *argv):
        if argv[:2] == ("issue", "edit"):
            seen.append(list(argv))
        return real(cwd, *argv)

    gh.run = strict  # github fixture restores gh.run afterwards
    gh.issue_labels(Path("."), n, add=["merged"], remove=["review", "ci-blocked"])
    assert "ci-blocked" not in seen[-1]
    assert github.issues[n]["labels"] == ["merged"]


def test_confirm_is_the_users_command(world, github, monkeypatch, capsys):
    _, a, _ = world
    n = github.add_issue(READY_BODY, [])
    monkeypatch.setenv("CLAUDECODE", "1")
    assert cli.main(["-C", str(a), "task", "confirm", f"gh-{n}"]) == 2
    assert cards.AC_CONFIRMED not in github.issues[n]["labels"]
    monkeypatch.delenv("CLAUDECODE")
    assert cli.main(["-C", str(a), "task", "confirm", f"gh-{n}"]) == 0
    assert github.issues[n]["labels"].count(cards.AC_CONFIRMED) == 1
    assert cli.main(["-C", str(a), "task", "confirm", f"gh-{n}"]) == 0  # re-confirm
    assert github.issues[n]["labels"].count(cards.AC_CONFIRMED) == 1


def test_tick_after_confirmation_keeps_the_card_ready(world, github):
    _, a, _ = world
    ticked = READY_BODY.replace("- [ ] B1", "- [x] B1")
    n = github.add_issue(ticked, [cards.AC_CONFIRMED])
    github.issues[n]["confirmed_body"] = READY_BODY  # the original, before the tick
    assert cli.main(["-C", str(a), "task", "ready", f"gh-{n}"]) == 0


@pytest.mark.parametrize(
    ("history", "msg"),
    [
        ([{"editedAt": "2026-01-03T00:00:00Z", "diff": "a summary, not the body"},
          {"editedAt": "2026-01-01T00:00:00Z", "diff": "x"}],
         "does not end with the current body"),
        ([{"editedAt": "2026-01-03T00:00:00Z", "diff": READY_BODY}],
         "does not reach back to the confirmation"),
    ],
    ids=["diff-is-not-a-body", "history-too-short"],
)  # fmt: skip
def test_unexpected_history_is_an_error_not_a_guess(
    world, github, capsys, history, msg
):
    _, a, _ = world
    n = github.add_issue(READY_BODY, [cards.AC_CONFIRMED, "ready", "agent-ok"])
    github.issues[n]["history"] = history
    assert cli.main(["-C", str(a), "task", "ready", f"gh-{n}"]) == 2
    assert msg in capsys.readouterr().err
    assert cli.main(["-C", str(a), "task", "next"]) == 1  # skipped, listing survives
    assert f"skipped gh-{n}" in capsys.readouterr().err


ONLY_NOCARD = """#!/bin/sh
# a project's pre-push that knows nothing about h
while read -r _ _ remote _; do
  case "$remote" in refs/heads/nocard/*) ;; *) echo "refusing $remote" >&2; exit 1 ;; esac
done
"""


def test_epic_new_follows_the_project_branch_rule(world, github):
    # h-made branches take the project's form through [branches] auto.
    _, a, _ = world
    hook = a / ".git" / "hooks" / "pre-push"
    hook.write_text(ONLY_NOCARD, encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    # the hook bites: h's own branch name is refused
    assert cli.main(["-C", str(a), "epic", "new", "pay", "--title", "Pay"]) != 0
    with (a / "harness.toml").open("a", encoding="utf-8") as f:
        f.write('\n[branches]\nauto = "nocard/{name}"\n')
    assert cli.main(["-C", str(a), "epic", "new", "auth", "--title", "Login"]) == 0
    shown = out(a, "show", "origin/nocard/epic-auth-new:docs/epics/auth.md")
    assert shown.count(epics.START) == 1
    assert github.prs[-1]["headRefName"] == "nocard/epic-auth-new"


@pytest.mark.parametrize(
    "auto", ["nocard/{slug}", "nocard", "{name}/{name}", "a b/{name}", "../{name}",
             "x/{name}.lock", "/{name}"],
)  # fmt: skip
def test_auto_branch_template_is_validated(tmp_path, auto):
    (tmp_path / "harness.toml").write_text(
        f'[branches]\nauto = "{auto}"\n', encoding="utf-8"
    )
    with pytest.raises(config.ConfigError, match="branches.auto"):
        config.load(tmp_path)


def test_labels_only_touches_nothing_in_the_tree(world, github, capsys):
    _, a, _ = world
    before = out(a, "status", "--porcelain")
    assert cli.main(["-C", str(a), "init", "--github", "--labels-only"]) == 0
    assert out(a, "status", "--porcelain") == before
    printed = capsys.readouterr().out
    assert "label ac-confirmed" in printed and "skills" not in printed
    assert cli.main(["-C", str(a), "init", "--labels-only"]) != 0  # needs --github


def test_epic_publish_with_auto_merge_off_says_merge_by_hand(world, github, capsys):
    # a project had auto-merge off — the PR was created, the command failed.
    _, a, _ = world
    github.auto_merge_error = (
        "GraphQL: Auto merge is not allowed for this repository "
        "(enablePullRequestAutoMerge)"
    )
    assert cli.main(["-C", str(a), "epic", "new", "auth", "--title", "Login"]) == 0
    assert github.prs[-1]["headRefName"] == "chore/epic-auth-new"
    assert "auto-merge off — merge it yourself" in capsys.readouterr().err


def test_epic_publish_other_merge_errors_still_fail(world, github):
    _, a, _ = world
    github.auto_merge_error = "HTTP 502: Bad Gateway"
    assert cli.main(["-C", str(a), "epic", "new", "auth", "--title", "Login"]) != 0
    # same mutation, another cause: not "auto-merge off"
    github.auto_merge_error = (
        "Pull request is in clean status (enablePullRequestAutoMerge)"
    )
    assert cli.main(["-C", str(a), "epic", "new", "pay", "--title", "Pay"]) != 0


def test_a_wrapped_item_is_one_item():
    """an AC-B wrapped onto an indented line used to be judged by its first line."""
    text = (
        "- [ ] B1: Given an agent is about to change the ledger, When it\n"
        "  opens `docs/epics/billing.md`, Then it finds the rule there.\n"
        "- [ ] B2: second\n"
        "\n"
        "  not part of B2 after a blank line\n"
    )
    items = cards._items(text)
    assert len(items) == 2
    assert items[0].endswith("Then it finds the rule there.")
    assert cards.GIVEN_WHEN_THEN.search(items[0])
    assert items[1] == "- [ ] B2: second"
    assert cards.TRACE.search(cards._items("- [ ] T1 (\n  ← B1): x")[0])


@pytest.mark.parametrize(
    ("item", "ok"),
    [
        ("Given a login form, When the password is wrong, authentication fails", False),
        ("Given x, When it runs, the then-current rule applies", False),
        ("given a card, when ready runs, then it passes", True),
        ("Given a card, When ready runs, Then it passes", True),
    ],
)
def test_keywords_match_whole_words(item, ok):
    """`then` inside `authentication` or `then-current` is not a Then."""
    assert bool(cards.GIVEN_WHEN_THEN.search(item)) is ok

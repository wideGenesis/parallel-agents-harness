import json
import subprocess
import sys

import pytest
from conftest import git, out

from harness import cards, check, claims, cli, config, installed, ship, task
from harness.claims import ClaimError

PY = sys.executable.replace("\\", "/")
# Checks that cost nothing: these tests are about shipping, not about ruff or cargo.
FAST_CHECKS = f"""
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
[ship]
footer = "-- shipped by harness"
[review]
required = false
"""


class FakeGh:
    """Stands in for the `gh` CLI; records calls, keeps one PR per branch."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.prs: dict[str, str] = {}
        self.states: dict[str, str] = {}  # branch → MERGED / CLOSED; OPEN if absent
        self.titles: dict[str, str] = {}
        self.edit_error = ""  # stderr of a failed `pr edit --title`
        self.view_error = ""  # stderr of a failed `pr view` that is not "no PR"

    def __call__(self, cwd, *argv):
        self.calls.append(list(argv))
        if argv[:2] == ("pr", "view"):
            if self.view_error:
                return subprocess.CompletedProcess(argv, 1, "", self.view_error)
            url = self.prs.get(argv[2])
            if not url:  # what real gh prints
                return subprocess.CompletedProcess(
                    argv, 1, "", f'no pull requests found for branch "{argv[2]}"\n'
                )
            state = self.states.get(argv[2], "OPEN")
            title = self.titles.get(argv[2], "")
            data = {"url": url, "state": state, "number": 1, "title": title}
            return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")
        if argv[:2] == ("pr", "create"):
            branch = argv[argv.index("--head") + 1]
            self.states.pop(branch, None)
            self.prs[branch] = f"https://example/pr/{len(self.prs) + 1}"
            self.titles[branch] = argv[argv.index("--title") + 1]
            return subprocess.CompletedProcess(argv, 0, self.prs[branch] + "\n", "")
        if argv[:2] == ("pr", "edit") and "--title" in argv:
            if self.edit_error:
                return subprocess.CompletedProcess(argv, 1, "", self.edit_error)
            self.titles[argv[2]] = argv[argv.index("--title") + 1]
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def shipping(world, github, monkeypatch):
    origin, a, _ = world
    # gh-42 is an issue-backed task: give it a ready (trivial) card in the fake GitHub.
    trivial = cards.render({"Problem": "p", "Assumptions": "none", "Verify": "h check",
                            "Size": "S", "Trivial": "yes", "Trivial reason": "test"})  # fmt: skip
    github.add_issue(trivial, ["ac-confirmed"], number=42)
    with (a / "harness.toml").open("a", encoding="utf-8") as f:
        f.write(FAST_CHECKS)
    git(a, "-c", "user.name=a", "-c", "user.email=a@a", "commit", "-qam", "fast checks")
    git(a, "push", "-q", "origin", "main")
    fake = FakeGh()
    monkeypatch.setattr(ship, "gh", fake)
    monkeypatch.setattr(ship.tools, "usable", lambda t: True)
    started = task.start(config.load(a), "gh-42", slug="feature")
    return started.path, fake, origin


def commit(wt, rel, text):
    p = wt / rel
    p.write_text(p.read_text(encoding="utf-8") + text, encoding="utf-8")
    git(wt, "add", rel)
    git(
        wt,
        "-c",
        "user.name=a",
        "-c",
        "user.email=a@a",
        "commit",
        "-qm",
        f"feat: touch {rel}",
    )


def test_ship_pushes_and_opens_pr_with_evidence(shipping):
    wt, fake, origin = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# change\n")
    r = ship.ship(config.load(wt), wt)
    assert r.created and r.url == "https://example/pr/1"
    remote = subprocess.run(
        ["git", "ls-remote", str(origin), "refs/heads/task/gh-42-feature"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert remote.strip()
    create = next(c for c in fake.calls if c[:2] == ["pr", "create"])
    body = create[create.index("--body") + 1]
    title = create[create.index("--title") + 1]
    assert title == "feat: touch py/poly-lib/src/poly_lib/__init__.py"
    assert "Task: `gh-42`" in body and "Refs #42" in body and "Closes" not in body
    assert "| python | test | ✅ ok |" in body
    assert "Affected: python: poly-app, poly-lib" in body
    assert body.rstrip().endswith("-- shipped by harness")


def test_second_ship_updates_the_same_pr(shipping):
    wt, fake, _ = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# one\n")
    ship.ship(config.load(wt), wt)
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# two\n")
    r = ship.ship(config.load(wt), wt)
    assert not r.created
    assert sum(c[:2] == ["pr", "create"] for c in fake.calls) == 1
    assert any(c[:2] == ["pr", "edit"] for c in fake.calls)


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_ship_after_the_pr_was_merged_or_closed_opens_a_new_one(shipping, state):
    # editing a merged PR starts no CI; the fix for a red main got lost that way.
    wt, fake, _ = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# one\n")
    ship.ship(config.load(wt), wt)
    fake.states["task/gh-42-feature"] = state
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# fix after merge\n")
    r = ship.ship(config.load(wt), wt)
    assert r.created and r.url == "https://example/pr/2"
    assert sum(c[:2] == ["pr", "create"] for c in fake.calls) == 2
    assert not any(c[:2] == ["pr", "edit"] for c in fake.calls)
    assert any(f"PR #1 of this branch is {state.lower()}" in n for n in r.notes)


def test_failed_pr_lookup_refuses_instead_of_creating(shipping):
    # GitHub unreachable is not "no PR" — create would fail or duplicate.
    wt, fake, _ = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# one\n")
    fake.view_error = "error connecting to api.github.com\n"
    with pytest.raises(ship.ShipRefused, match="error connecting to api.github.com"):
        ship.ship(config.load(wt), wt)
    assert not any(c[:2] in (["pr", "create"], ["pr", "edit"]) for c in fake.calls)


def test_refuses_dirty_tree(shipping):
    wt, fake, _ = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# one\n")
    (wt / "py" / "poly-lib" / "notes.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ship.ShipRefused, match="uncommitted"):
        ship.ship(config.load(wt), wt)
    assert fake.calls == []


def test_refuses_without_commits(shipping):
    wt, _, _ = shipping
    with pytest.raises(ship.ShipRefused, match="nothing to ship"):
        ship.ship(config.load(wt), wt)


def test_refuses_on_failing_check_and_pushes_nothing(shipping, monkeypatch):
    wt, fake, _ = shipping
    toml = wt / "harness.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            f'test = "{PY} -c pass"', f"test = \"{PY} -c 'import sys; sys.exit(3)'\""
        ),
        encoding="utf-8",
    )
    git(wt, "add", "harness.toml")
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# change\n")
    # Editing harness.toml always needs review; approve it to reach the check.
    from harness import review

    # This test is about the check, not the package: prepare would refuse a failing check.
    monkeypatch.setattr(review, "_package_problem", lambda *a: None)
    (wt / review.DIR).mkdir(parents=True, exist_ok=True)
    head = review._head(wt)
    (wt / review.VERDICT).write_text(
        json.dumps({"head": head, "verdict": "approve", "round": 1, "findings": []}),
        encoding="utf-8",
    )
    with pytest.raises(ship.ShipRefused, match="h check failed") as e:
        ship.ship(config.load(wt), wt)
    assert any(r.status == "fail" for r in e.value.results)
    assert fake.calls == []


def test_refuses_when_task_was_taken_over(shipping):
    wt, fake, _ = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# change\n")
    cfg = config.load(wt)
    sha, held = claims.read(cfg, "gh-42")  # type: ignore[misc]
    held.owner = "other-host|/elsewhere"
    assert claims.replace(cfg, sha, held)
    with pytest.raises(ClaimError, match="held by"):
        ship.ship(cfg, wt)
    assert fake.calls == []


def test_refuses_without_origin_but_does_not_call_it_a_takeover(shipping, monkeypatch):
    # a push that cannot reach origin is not "taken over".
    wt, fake, _ = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# change\n")

    def unreachable(*_a, **_k):
        raise claims.PushFailed("push refs/claims/gh-42 failed: could not resolve host")

    monkeypatch.setattr(claims, "replace", unreachable)
    with pytest.raises(ship.ShipRefused, match="no connection to origin"):
        ship.ship(config.load(wt), wt)
    assert fake.calls == []


def test_cli_ship_outside_worktree(world, capsys):
    _, a, _ = world
    assert cli.main(["-C", str(a), "ship"]) == 1
    assert "not inside a task worktree" in capsys.readouterr().out


def test_ship_commits_formatting_before_checking(shipping, monkeypatch):
    wt, fake, _ = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "x=1\n")

    def fake_format(cfg, path):
        path.write_text(
            path.read_text(encoding="utf-8").replace("x=1", "x = 1"), encoding="utf-8"
        )
        return ship.fmt.Result(path.name, "formatted")

    monkeypatch.setattr(ship.fmt, "format_file", fake_format)
    r = ship.ship(config.load(wt), wt)
    log = subprocess.run(
        ["git", "log", "--format=%s", "-2"],
        cwd=wt,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert log[0] == ship.STYLE_MESSAGE
    create = next(c for c in fake.calls if c[:2] == ["pr", "create"])
    assert ship.STYLE_MESSAGE in create[create.index("--body") + 1]
    assert r.created


def test_ship_without_format_changes_adds_no_commit(shipping, monkeypatch):
    wt, _, _ = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# fine\n")
    monkeypatch.setattr(
        ship.fmt, "format_file", lambda cfg, p: ship.fmt.Result(p.name, "formatted")
    )
    ship.ship(config.load(wt), wt)
    last = subprocess.run(
        ["git", "log", "--format=%s", "-1"],
        cwd=wt,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert last != ship.STYLE_MESSAGE


def test_ship_publishes_the_verdict_to_the_pr(shipping, github, monkeypatch):
    from harness.review import Verdict

    wt, _, _ = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# change\n")
    v = Verdict(head="a" * 40, verdict="approve", round=1, model="sonnet")
    monkeypatch.setattr(ship.review, "gate", lambda *a: v)
    r = ship.ship(config.load(wt), wt)
    assert "review verdict created in the PR" in r.notes
    pushed = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()  # fmt: skip
    assert f'"pr_head":"{pushed}"' in github.comments[1][0]["body"]
    again = ship.ship(config.load(wt), wt)
    assert "review verdict updated in the PR" in again.notes
    assert len(github.comments[1]) == 1


def test_ship_warns_when_the_verdict_is_not_published(shipping, github, monkeypatch):
    from harness import gh
    from harness.review import Verdict

    wt, _, _ = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# change\n")
    v = Verdict(head="a" * 40, verdict="approve", round=1, model="sonnet")
    monkeypatch.setattr(ship.review, "gate", lambda *a: v)

    def down(*a):
        raise gh.GhError("api down")

    monkeypatch.setattr(ship.prreview, "publish", down)
    r = ship.ship(config.load(wt), wt)
    assert r.url and "warning: review verdict not published (api down)" in r.notes


def _remote_sha(origin, ref="refs/heads/task/gh-42-feature"):
    out = subprocess.run(["git", "ls-remote", str(origin), ref],
                         capture_output=True, text=True, check=True).stdout  # fmt: skip
    return out.split()[0] if out.strip() else ""


def advance_main(world, rel, text):
    """Another task merged meanwhile: a commit on origin/main from the main checkout."""
    _, a, _ = world
    commit(a, rel, text)
    git(a, "push", "-q", "origin", "main")


def test_rebases_a_branch_behind_base_that_merges_cleanly(shipping, world):
    wt, _, origin = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# mine\n")
    advance_main(world, ".gitignore", "# another task merged meanwhile\n")
    r = ship.ship(config.load(wt), wt)
    assert r.created and "rebased onto origin/main (+1 commit(s))" in r.notes
    assert git_ok(wt, "merge-base", "--is-ancestor", "origin/main", "HEAD")
    assert _remote_sha(origin) == head_of(wt)


def test_refuses_a_branch_that_conflicts_with_base(shipping, world):
    wt, fake, origin = shipping
    lib = "py/poly-lib/src/poly_lib/__init__.py"
    commit(wt, lib, "# mine\n")
    before = head_of(wt)
    advance_main(world, lib, "# theirs\n")
    with pytest.raises(ship.ShipRefused, match="conflicts with it") as e:
        ship.ship(config.load(wt), wt)
    assert lib in str(e.value) and "git rebase origin/main" in str(e.value)
    assert head_of(wt) == before
    assert fake.calls == [] and _remote_sha(origin) == ""


def test_refuses_to_rebase_a_branch_with_merge_commits(shipping, world):
    wt, _fake, origin = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# mine\n")
    git(wt, "switch", "-q", "-c", "side", "HEAD~1")
    commit(wt, "crates/poly-core/src/lib.rs", "// side\n")
    git(wt, "switch", "-q", "task/gh-42-feature")
    git(
        wt,
        "-c",
        "user.name=a",
        "-c",
        "user.email=a@a",
        "merge",
        "-q",
        "--no-edit",
        "side",
    )
    before = head_of(wt)
    advance_main(world, ".gitignore", "# another task merged meanwhile\n")
    with pytest.raises(ship.ShipRefused, match="merge commits"):
        ship.ship(config.load(wt), wt)
    assert head_of(wt) == before and _remote_sha(origin) == ""


def test_a_failed_rebase_is_aborted(shipping, world, monkeypatch):
    wt, _, origin = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# mine\n")
    before = head_of(wt)
    advance_main(world, ".gitignore", "# another task merged meanwhile\n")
    real = ship._git

    def failing(cwd, *argv):
        if argv[:1] == ("rebase",) and "--abort" not in argv:
            return subprocess.CompletedProcess(argv, 1, "", "boom")
        return real(cwd, *argv)

    monkeypatch.setattr(ship, "_git", failing)
    with pytest.raises(ship.ShipRefused, match="branch left as it was: boom"):
        ship.ship(config.load(wt), wt)
    assert head_of(wt) == before and _remote_sha(origin) == ""


def test_own_rebase_of_a_pushed_branch_is_pushed(shipping):
    wt, _, origin = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# one\n")
    ship.ship(config.load(wt), wt)
    git(wt, "commit", "-q", "--amend", "-m", "feat: reworded")
    ship.ship(config.load(wt), wt)
    head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()  # fmt: skip
    assert _remote_sha(origin) == head


def test_push_never_clobbers_a_commit_pushed_by_another_session(shipping, world):
    wt, _, origin = shipping
    _, _, b = world
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# one\n")
    ship.ship(config.load(wt), wt)
    git(b, "fetch", "-q", "origin")
    git(b, "switch", "-q", "task/gh-42-feature")
    commit(b, ".gitignore", "# pushed by another session\n")
    git(b, "push", "-q", "origin", "task/gh-42-feature")
    theirs = _remote_sha(origin)

    git(wt, "commit", "-q", "--amend", "-m", "feat: rewritten without their commit")
    with pytest.raises(ship.ShipRefused, match="git push failed"):
        ship.ship(config.load(wt), wt)
    assert _remote_sha(origin) == theirs


def head_of(wt):
    return subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()  # fmt: skip


def git_ok(wt, *argv):
    return (
        subprocess.run(
            ["git", "-C", str(wt), *argv], capture_output=True, check=False
        ).returncode
        == 0
    )


def test_ship_sets_the_required_review_check(shipping, github, monkeypatch):
    """the gate's outcome is the commit status harness/review on the pushed sha."""
    from harness import prreview
    from harness.review import Verdict

    wt, _, _ = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# change\n")
    ship.ship(config.load(wt), wt)  # [review] required = false in this fixture
    st = github.statuses[head_of(wt)]
    assert st["context"] == prreview.STATUS_CONTEXT and st["state"] == "success"
    assert "not required" in st["description"] and st["target_url"].endswith("/pr/1")

    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# more\n")
    assert head_of(wt) not in github.statuses  # a new push: the check is pending again
    v = Verdict(head="a" * 40, verdict="approve", round=2, model="sonnet")
    monkeypatch.setattr(ship.review, "gate", lambda *a: v)
    ship.ship(config.load(wt), wt)
    assert (
        github.statuses[head_of(wt)]["description"] == "approved by reviewer, round 2"
    )


def test_a_step_that_did_not_run_blocks_ship(shipping, monkeypatch):
    """a missing tool is not a passed check."""
    wt, fake, _ = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# change\n")
    real_plan = ship.check.plan

    def without_pyright(*a, **kw):
        steps = [s for s in real_plan(*a, **kw) if s.kind != "typecheck"]
        return [
            *steps,
            ship.check.Step("python", "typecheck", None, "pyright not installed"),
        ]

    monkeypatch.setattr(ship.check, "plan", without_pyright)
    with pytest.raises(ship.ShipRefused, match="did not run") as e:
        ship.ship(config.load(wt), wt)
    assert "python typecheck: pyright not installed" in str(e.value)
    assert '[stacks.python] skip = ["typecheck"]' in str(e.value)
    assert fake.calls == []  # nothing pushed, no PR


def test_a_disabled_step_ships_and_shows_in_the_pr(shipping, monkeypatch):
    wt, fake, _ = shipping
    toml = wt / "harness.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace(
        "[stacks.python.commands]", '[stacks.python]\nskip = ["typecheck"]\n\n'
        "[stacks.python.commands]"), encoding="utf-8")  # fmt: skip
    git(wt, "add", "harness.toml")
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# change\n")
    monkeypatch.setattr(
        ship.review, "gate", lambda *a: None
    )  # harness.toml edit: reviewed
    ship.ship(config.load(wt), wt)
    create = next(c for c in fake.calls if c[:2] == ["pr", "create"])
    body = create[create.index("--body") + 1]
    assert "| python | typecheck | 🚫 disabled in harness.toml |" in body


def test_a_covered_step_does_not_block_ship(shipping, monkeypatch):
    """Rust typecheck is part of clippy: a skip with nothing to run is not a missed check."""
    wt, _fake, _ = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# change\n")
    real_plan = ship.check.plan
    covered = ship.check._rust("typecheck", [], False)
    assert covered.cmd is None and covered.covered
    monkeypatch.setattr(
        ship.check, "plan", lambda *a, **kw: [*real_plan(*a, **kw), covered]
    )
    assert ship.ship(config.load(wt), wt).created


# the gates are only as good as the h that runs them.
def test_ship_refuses_a_stale_h_before_pushing(shipping, monkeypatch):
    wt, fake, _origin = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# change\n")
    monkeypatch.setattr(
        installed,
        "check",
        lambda: installed.Status("behind", "origin/main (cli.py)", "git -C /m pull"),
    )
    with pytest.raises(ship.ShipRefused, match="installed h is behind"):
        ship.ship(config.load(wt), wt)
    assert fake.calls == []


def test_ship_is_not_blocked_when_h_cannot_be_checked(shipping, monkeypatch):
    wt, _, _ = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# change\n")
    monkeypatch.setattr(installed, "check", lambda: installed.Status("unknown", "why"))
    assert ship.ship(config.load(wt), wt).created


def _no_format(monkeypatch):
    monkeypatch.setattr(
        ship.fmt, "format_file", lambda cfg, p: ship.fmt.Result(p.name, "formatted")
    )


def _no_run(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("h check ran again")

    monkeypatch.setattr(ship.check, "run", refuse)


def test_ship_reuses_a_green_full_run_on_the_same_head(shipping, monkeypatch):
    """review prepare just ran the check green on HEAD — ship does not rerun it."""
    wt, fake, _ = shipping
    _no_format(monkeypatch)
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# change\n")
    ship.ship(config.load(wt), wt)  # records a full green run on HEAD
    _no_run(monkeypatch)
    r = ship.ship(config.load(wt), wt)
    assert any("reused the green run" in n for n in r.notes)
    edit = [c for c in fake.calls if c[:2] == ["pr", "edit"]][-1]
    assert "h check: reused the green run of" in edit[edit.index("--body") + 1]


@pytest.mark.parametrize("change", ["commit", "dirty", "version", "env", "selected"])
def test_ship_reruns_the_check_when_anything_changed(shipping, monkeypatch, change):
    wt, _, _ = shipping
    _no_format(monkeypatch)
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# change\n")
    ship.ship(config.load(wt), wt)
    if change == "commit":
        commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# more\n")
    elif change == "dirty":
        (wt / "py" / "poly-lib" / "new.txt").write_text("x", encoding="utf-8")
    elif change == "version":
        monkeypatch.setattr(ship.check, "__version__", "0.0.0")
    elif change == "env":
        pass  # set on the loaded config below
    else:  # the local loop's run: tests selected by testmon
        rec = json.loads((wt / check.LAST).read_text(encoding="utf-8"))
        (wt / check.LAST).write_text(
            json.dumps({**rec, "full": False}), encoding="utf-8"
        )
    cfg = config.load(wt)
    if change == "env":
        cfg.check_env = {"X": "1"}
    found = ship.stacks.detect(cfg)
    files = ship.affected.changed_files(cfg, ship.affected.resolve_base(cfg, None))
    steps = check.plan(cfg, found, ship.affected.compute(cfg, found, files),
                       check.FAST + check.SLOW)  # fmt: skip
    assert check.reusable(cfg, steps) is None


def test_old_last_json_is_never_reused(shipping, monkeypatch):
    wt, _, _ = shipping
    _no_format(monkeypatch)
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# change\n")
    ship.ship(config.load(wt), wt)
    rec = json.loads((wt / check.LAST).read_text(encoding="utf-8"))
    old = {
        k: v for k, v in rec.items() if k not in ("full", "plan", "steps", "h", "env")
    }
    (wt / check.LAST).write_text(json.dumps(old), encoding="utf-8")
    cfg = config.load(wt)
    assert check.reusable(cfg, []) is None


@pytest.mark.parametrize("title", ["fix(x): the right title", None])
def test_ship_title_retitles_an_open_pr(shipping, monkeypatch, title):
    """--title on an open PR used to be ignored without a word."""
    wt, fake, _ = shipping
    _no_format(monkeypatch)
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# change\n")
    ship.ship(config.load(wt), wt)  # opens the PR
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# more\n")
    r = ship.ship(config.load(wt), wt, title=title)
    edits = [c for c in fake.calls if c[:2] == ["pr", "edit"]]
    if title:
        assert title_edits(fake) == [title]
        assert f"PR retitled: {title}" in r.notes
    else:
        assert not any("--title" in e for e in edits)
        assert not any(n.startswith("PR retitled") for n in r.notes)


# a title taken from the first commit follows it, before the push: a project's
# guards read the title from the event of the push and does not listen to `edited`.

BRANCH = "task/gh-42-feature"


def title_edits(fake):
    return [c[c.index("--title") + 1] for c in fake.calls
            if c[:2] == ["pr", "edit"] and "--title" in c]  # fmt: skip


def reword(wt, subject):
    git(wt, "-c", "user.name=a", "-c", "user.email=a@a", "commit", "-q", "--amend",
        "-m", subject)  # fmt: skip


def test_reworded_first_commit_retitles_the_pr(shipping):
    wt, fake, _ = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# one\n")
    ship.ship(config.load(wt), wt)
    reword(wt, "fix: the new subject")
    r = ship.ship(config.load(wt), wt)
    assert fake.titles[BRANCH] == "fix: the new subject"
    assert "PR retitled: fix: the new subject" in r.notes


def test_a_chosen_title_stays_and_ship_says_so(shipping):
    wt, fake, _ = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# one\n")
    ship.ship(config.load(wt), wt, title="chosen by hand")
    reword(wt, "fix: the new subject")
    r = ship.ship(config.load(wt), wt)
    assert fake.titles[BRANCH] == "chosen by hand"
    assert not title_edits(fake)
    assert any("PR title kept" in n and "chosen by hand" in n for n in r.notes)


def test_an_unchanged_first_commit_edits_no_title(shipping):
    wt, fake, _ = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# one\n")
    ship.ship(config.load(wt), wt)
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# two\n")
    r = ship.ship(config.load(wt), wt)
    assert not title_edits(fake)
    assert not any("PR title" in n for n in r.notes)


@pytest.mark.parametrize("title", [None, "fix: from the flag"])
def test_the_title_changes_before_the_push(shipping, monkeypatch, title):
    wt, fake, _ = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# one\n")
    ship.ship(config.load(wt), wt)
    reword(wt, "fix: the new subject")
    real = ship._git

    def spy(where, *argv):
        if argv[0] == "push":
            fake.calls.append(["git", "push"])
        return real(where, *argv)

    monkeypatch.setattr(ship, "_git", spy)
    ship.ship(config.load(wt), wt, title=title)
    names = [c[:2] for c in fake.calls]
    edit = next(i for i, c in enumerate(fake.calls)
                if c[:2] == ["pr", "edit"] and "--title" in c)  # fmt: skip
    assert edit < names.index(["git", "push"])


def test_a_failed_retitle_pushes_nothing(shipping):
    wt, fake, origin = shipping
    commit(wt, "py/poly-lib/src/poly_lib/__init__.py", "# one\n")
    ship.ship(config.load(wt), wt)
    pushed = out(origin, "rev-parse", BRANCH)
    reword(wt, "fix: the new subject")
    fake.edit_error = "GraphQL: not allowed"
    with pytest.raises(ship.ShipRefused, match="nothing pushed.*not allowed"):
        ship.ship(config.load(wt), wt)
    assert out(origin, "rev-parse", BRANCH) == pushed

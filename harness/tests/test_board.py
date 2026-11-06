import json
import subprocess

import pytest
from conftest import FakeGitHub

from harness import board, cards, cli, config, gh, task


class FakeBoardGitHub(FakeGitHub):
    """FakeGitHub plus a Projects v2 board: Status options, items and their stage."""

    def __init__(self):
        super().__init__()
        self.projects: dict[int, dict] = {}
        self.items: dict[str, str] = {}  # issue url -> stage name
        self.fail_project_calls = False
        self.calls: list[list[str]] = []
        self.owner_kind = "users"  # "orgs" for an organisation's repo

    def __call__(self, cwd, *argv):
        a = list(argv)
        self.calls.append(a)
        ok = lambda out="": subprocess.CompletedProcess(a, 0, out, "")
        if a[0] == "project" and self.fail_project_calls:
            return subprocess.CompletedProcess(a, 1, "", "HTTP 502: bad gateway")
        if a[:2] == ["repo", "view"]:
            return ok(json.dumps({"owner": {"login": "me"}, "name": "repo"}))
        if a[:2] == ["project", "create"]:
            n = len(self.projects) + 1
            opts = [
                {"id": f"o-{x}", "name": x} for x in ("Todo", "In Progress", "Done")
            ]
            self.projects[n] = {"id": f"P{n}", "options": opts}
            return ok(json.dumps({"number": n, "id": f"P{n}"}))
        if a[:2] in (["project", "link"],):
            return ok()
        if a[:2] == ["project", "view"]:
            n = int(a[2])
            url = f"https://github.com/{self.owner_kind}/me/projects/{n}"
            return ok(json.dumps({"id": self.projects[n]["id"], "url": url}))
        if a[:2] == ["project", "field-list"]:
            p = self.projects[int(a[2])]
            return ok(
                json.dumps(
                    {
                        "fields": [
                            {"name": "Status", "id": "F1", "options": p["options"]}
                        ]
                    }
                )
            )
        if a[:2] == ["api", "graphql"] and not any("LabeledEvent" in x for x in a):
            names = [s for s, _ in board.STAGES]
            for p in self.projects.values():
                p["options"] = [{"id": f"o-{x}", "name": x} for x in names]
            return ok("{}")
        if a[:2] == ["project", "item-add"]:
            url = a[a.index("--url") + 1]
            self.items.setdefault(url, "")
            return ok(json.dumps({"id": f"I:{url}"}))
        if a[:2] == ["project", "item-edit"]:
            url = a[a.index("--id") + 1][2:]
            self.items[url] = a[a.index("--single-select-option-id") + 1][2:]
            return ok()
        if a[0] == "api" and any(
            x.startswith("repos/{owner}/{repo}/issues?") for x in a
        ):
            # --paginate --jq: one compact object per line; without --paginate one page.
            rows = [{"url": i["url"], "state": i["state"].lower(), "labels": i["labels"]}
                    for i in self.issues.values()]  # fmt: skip
            rows = rows if "--paginate" in a else rows[:100]
            return ok("".join(json.dumps(r) + "\n" for r in rows))
        return super().__call__(cwd, *argv)


@pytest.fixture
def ghb(monkeypatch):
    fake = FakeBoardGitHub()
    monkeypatch.setattr(gh, "run", fake)
    return fake


READY = cards.render({
    "Problem": "p", "Assumptions": "none", "Verify": "h check", "Size": "S",
    "Trivial": "yes", "Trivial reason": "test",
})  # fmt: skip


def test_init_creates_board_columns_config_and_places_open_issues(world, ghb):
    _, a, _ = world
    n = ghb.add_issue(READY, ["ac-confirmed", "ready"])
    url, _changes = board.init(config.load(a))
    assert url == "https://github.com/users/me/projects/1"
    assert [o["name"] for o in ghb.projects[1]["options"]] == [
        s for s, _ in board.STAGES
    ]
    assert '[board]\nowner = "me"\nnumber = 1' in (a / "harness.toml").read_text(
        encoding="utf-8"
    )
    assert ghb.items[ghb.issues[n]["url"]] == "Ready"
    # idempotent: no second project, no second [board]
    _, again = board.init(config.load(a))
    assert len(ghb.projects) == 1 and not any("created" in c for c in again)
    assert (a / "harness.toml").read_text(encoding="utf-8").count("[board]") == 1


def test_init_places_every_issue_and_links_an_org_board_from_the_api(world, ghb):
    _, a, _ = world
    ghb.owner_kind = "orgs"
    for _ in range(250):
        ghb.add_issue(READY, ["ac-confirmed", "ready"])
    url, changes = board.init(config.load(a))
    assert url == "https://github.com/orgs/me/projects/1"
    assert "250 issues placed" in changes
    assert len(ghb.items) == 250


def test_lifecycle_moves_the_card(world, ghb, tmp_path):
    _, a, _ = world
    board.init(config.load(a))
    card = tmp_path / "card.md"
    card.write_text(READY, encoding="utf-8")
    assert (
        cli.main(["-C", str(a), "task", "new", "--title", "t", "--body", str(card)])
        == 0
    )
    n = max(ghb.issues)
    issue_url = ghb.issues[n]["url"]
    # the trivial claim waits for the user, then the card is ready
    assert ghb.items[issue_url] == board.LABEL_STAGE["needs-clarification"]
    ghb.issues[n]["labels"].append(cards.AC_CONFIRMED)
    assert cli.main(["-C", str(a), "task", "ready", f"gh-{n}"]) == 0
    assert ghb.items[issue_url] == "Ready"
    task.start(config.load(a), f"gh-{n}")
    assert ghb.items[issue_url] == "In progress"


def test_ready_leaves_a_closed_issue_alone(world, ghb, capsys):
    # a done task must not jump back to Ready when its card is re-checked
    _, a, _ = world
    board.init(config.load(a))
    n = ghb.add_issue(READY, ["ac-confirmed"], state="CLOSED")
    url = ghb.issues[n]["url"]
    ghb.items[url] = "Done"
    capsys.readouterr()
    assert cli.main(["-C", str(a), "task", "ready", f"gh-{n}"]) == 0
    out = capsys.readouterr().out
    assert "READY" in out and "closed: labels and board left as is" in out
    assert ghb.items[url] == "Done"
    assert ghb.issues[n]["labels"] == ["ac-confirmed"]


def test_board_failure_is_a_warning_not_an_error(world, ghb, capsys):
    _, a, _ = world
    board.init(config.load(a))
    n = ghb.add_issue(READY, ["ac-confirmed"])
    ghb.fail_project_calls = True
    board._cache_path(config.load(a)).unlink(missing_ok=True)  # force the API path
    r = task.start(config.load(a), f"gh-{n}")
    assert any(note.startswith("board: not updated") for note in r.notes)
    assert (r.path / ".git").is_file()  # the command itself succeeded


def test_not_configured_is_one_line_and_no_calls(world, ghb):
    _, a, _ = world
    before = len(ghb.calls)
    warn = board.move(config.load(a), "https://x/issues/1", "Ready")
    assert warn == "board: not configured (h board init) — skipped"
    assert len(ghb.calls) == before


def test_stage_of_prefers_the_latest_lifecycle_label():
    assert board.stage_of(["ready", "in-progress"]) == "In progress"
    assert board.stage_of(["review", "in-progress"]) == "Review"
    assert board.stage_of([]) == "Draft"
    assert board.stage_of(["ready"], closed=True) == "Done"

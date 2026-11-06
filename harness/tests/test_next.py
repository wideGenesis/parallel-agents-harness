import json

import pytest

from harness import cards, cli, config, task

TRIVIAL = cards.render({"Problem": "p", "Assumptions": "none", "Verify": "h check",
                        "Size": "S", "Trivial": "yes", "Trivial reason": "t"})  # fmt: skip


def test_next_lists_only_ready_agent_ok_unclaimed(world, github, capsys):
    _, a, _ = world
    ok1 = github.add_issue(
        TRIVIAL, ["ac-confirmed", "ready", "agent-ok"], title="take me"
    )
    github.add_issue(TRIVIAL, ["ac-confirmed", "ready"], title="needs the user's word")
    github.add_issue(
        TRIVIAL, ["ac-confirmed", "needs-clarification", "agent-ok"], title="not ready"
    )
    ok2 = github.add_issue(
        TRIVIAL, ["ac-confirmed", "ready", "agent-ok"], title="claimed"
    )
    task.start(config.load(a), f"gh-{ok2}")
    assert cli.main(["-C", str(a), "task", "next", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [x["id"] for x in listed] == [f"gh-{ok1}"]


def test_next_empty_exits_1(world, github, capsys):
    _, a, _ = world
    assert cli.main(["-C", str(a), "task", "next"]) == 1
    assert "nothing for an agent" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["robot", 1])
def test_ship_merge_is_validated(tmp_path, value):
    (tmp_path / "harness.toml").write_text(
        f"[ship]\nmerge = {json.dumps(value)}\n", encoding="utf-8"
    )
    with pytest.raises(config.ConfigError, match="ship.merge"):
        config.load(tmp_path)


def test_ship_merge_default_is_user(tmp_path):
    assert config.load(tmp_path).ship_merge == "user"


def test_task_skill_installed_and_short(tmp_path):
    from harness import init

    init.run(config.load(tmp_path))
    skill = (tmp_path / ".agents" / "skills" / "task" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert len(skill.splitlines()) <= 100
    assert "h task next" in skill and "main checkout" in skill


@pytest.mark.parametrize(("mode", "line"), [
    ("agent", "merge: agent — after green CI"),
    ("user", "merge: user — PR ready, waiting"),
])  # fmt: skip
def test_ship_prints_who_merges(world, github, monkeypatch, capsys, mode, line):
    from conftest import git
    from test_ship import FAST_CHECKS, FakeGh

    from harness import ship

    _, a, _ = world
    toml = FAST_CHECKS  # already has [review] required = false
    toml = toml.replace(
        'footer = "-- shipped by harness"', f'footer = "x"\nmerge = "{mode}"'
    )
    with (a / "harness.toml").open("a", encoding="utf-8") as f:
        f.write(toml)
    git(a, "commit", "-qam", "cfg")
    git(a, "push", "-q", "origin", "main")
    github.add_issue(TRIVIAL, ["ac-confirmed"], number=7)
    r = task.start(config.load(a), "gh-7")
    p = r.path / "py" / "poly-lib" / "src" / "poly_lib" / "__init__.py"
    p.write_text(p.read_text(encoding="utf-8") + "# x\n", encoding="utf-8")
    git(r.path, "commit", "-qam", "feat: x")
    monkeypatch.setattr(ship, "gh", FakeGh())
    monkeypatch.setattr(ship.tools, "usable", lambda t: True)
    assert cli.main(["-C", str(r.path), "ship"]) == 0
    assert line in capsys.readouterr().out


def test_next_json_empty_exits_1(world, github, capsys):
    _, a, _ = world
    assert cli.main(["-C", str(a), "task", "next", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == []

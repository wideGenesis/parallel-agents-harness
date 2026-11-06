import io
import json
import shutil

import pytest

from harness import cli, config, skills


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "harness.toml").write_text(
        '[project]\nname = "demo"\n', encoding="utf-8"
    )
    return tmp_path


def write_skill(root, name, body="---\nname: x\ndescription: y\n---\nbody\n"):
    d = root / ".agents" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    return d


def test_install_copies_templates_once(proj):
    cfg = config.load(proj)
    changes = skills.install(cfg)
    assert "create .agents/skills/bug-fix/" in changes
    assert "create .claude/agents/locator.md" in changes
    skill = proj / ".agents" / "skills" / "bug-fix" / "SKILL.md"
    assert skills.install(cfg) == []
    skill.write_text("project's own edit\n", encoding="utf-8")
    # never overwrites what the project edited; quiet while the template has not moved
    assert skills.install(cfg) == []
    assert skill.read_text(encoding="utf-8") == "project's own edit\n"


def test_a_hand_edited_manifest_never_points_outside(proj, tpl):
    cfg = config.load(proj)
    skills.install(cfg, tpl=tpl)
    outside = proj / "keep.md"
    outside.write_text("x\n", encoding="utf-8")
    digest = skills._item_hash(outside)
    (proj / skills.INSTALLED).write_text(
        json.dumps(
            {"agents/../../keep.md": digest, "noslash": digest, "skills/..": digest}
        ),
        encoding="utf-8",
    )
    skills.install(cfg, tpl=tpl)
    assert outside.exists()
    (proj / skills.INSTALLED).write_text("[]", encoding="utf-8")
    assert skills.install(cfg, tpl=tpl) == []  # not a dict: as if absent


@pytest.fixture
def tpl(tmp_path):
    """A stand-in for the package templates: skill `task`, agent `reviewer.md`."""
    root = tmp_path / "templates"
    write_skill(root / "x", "task", "v1\n")
    (root / "skills").mkdir()
    (root / "x" / ".agents" / "skills" / "task").rename(root / "skills" / "task")
    (root / "agents").mkdir()
    (root / "agents" / "reviewer.md").write_text("r1\n", encoding="utf-8")
    return root


def _skill(proj):
    return proj / ".agents" / "skills" / "task" / "SKILL.md"


def _agent(proj):
    return proj / ".claude" / "agents" / "reviewer.md"


def test_init_updates_an_untouched_skill_and_its_mirror(proj, tpl):
    cfg = config.load(proj)
    skills.install(cfg, tpl=tpl)
    skills.sync(cfg)
    (tpl / "skills" / "task" / "SKILL.md").write_text("v2\n", encoding="utf-8")
    assert skills.install(cfg, tpl=tpl) == ["update .agents/skills/task/"]
    assert skills.sync(cfg) == (["mirror task"], [])
    assert _skill(proj).read_text(encoding="utf-8") == "v2\n"
    mirror = proj / ".claude" / "skills" / "task" / "SKILL.md"
    assert mirror.read_text(encoding="utf-8") == "v2\n"
    assert skills.install(cfg, tpl=tpl) == []  # a second run changes nothing


def test_init_reports_an_edited_agent_and_keeps_it(proj, tpl):
    cfg = config.load(proj)
    skills.install(cfg, tpl=tpl)
    _agent(proj).write_text("ours\n", encoding="utf-8")
    (tpl / "agents" / "reviewer.md").write_text("r2\n", encoding="utf-8")
    [msg] = skills.install(cfg, tpl=tpl)
    assert msg.startswith("MODIFIED .claude/agents/reviewer.md: differs from the h ")
    assert str(tpl / "agents" / "reviewer.md") in msg
    assert _agent(proj).read_text(encoding="utf-8") == "ours\n"


def test_dry_run_lists_updates_and_writes_nothing(proj, tpl):
    cfg = config.load(proj)
    skills.install(cfg, tpl=tpl)
    before = (proj / skills.INSTALLED).read_text(encoding="utf-8")
    (tpl / "agents" / "reviewer.md").write_text("r2\n", encoding="utf-8")
    assert skills.install(cfg, dry_run=True, tpl=tpl) == [
        "update .claude/agents/reviewer.md"
    ]
    assert _agent(proj).read_text(encoding="utf-8") == "r1\n"
    assert (proj / skills.INSTALLED).read_text(encoding="utf-8") == before


def test_a_crlf_copy_counts_as_untouched(proj, tpl):
    cfg = config.load(proj)
    skills.install(cfg, tpl=tpl)
    _agent(proj).write_bytes(b"r1\r\n")
    (tpl / "agents" / "reviewer.md").write_text("r2\n", encoding="utf-8")
    assert skills.install(cfg, tpl=tpl) == ["update .claude/agents/reviewer.md"]


def test_a_project_without_hashes_adopts_matching_copies_only(proj, tpl):
    """installed by an older h, no record of what it put in place."""
    cfg = config.load(proj)
    skills.install(cfg, tpl=tpl)
    (proj / skills.INSTALLED).unlink()
    _skill(proj).write_text("ours\n", encoding="utf-8")
    [msg] = skills.install(cfg, tpl=tpl)
    assert msg.startswith("MODIFIED .agents/skills/task/")
    recorded = json.loads((proj / skills.INSTALLED).read_text(encoding="utf-8"))
    assert list(recorded) == ["agents/reviewer.md"]  # the matching copy is adopted
    (tpl / "agents" / "reviewer.md").write_text("r2\n", encoding="utf-8")
    assert "update .claude/agents/reviewer.md" in skills.install(cfg, tpl=tpl)


def test_a_skill_the_project_deleted_is_not_reinstalled(proj, tpl):
    cfg = config.load(proj)
    skills.install(cfg, tpl=tpl)
    _agent(proj).unlink()
    (tpl / "agents" / "reviewer.md").write_text("r2\n", encoding="utf-8")
    assert skills.install(cfg, tpl=tpl) == []
    assert not _agent(proj).exists()


def test_a_template_dropped_from_h_is_removed_unless_edited(proj, tpl):
    import shutil

    cfg = config.load(proj)
    skills.install(cfg, tpl=tpl)
    skills.sync(cfg)
    _agent(proj).write_text("ours\n", encoding="utf-8")
    shutil.rmtree(tpl / "skills" / "task")
    (tpl / "agents" / "reviewer.md").unlink()
    changes = skills.install(cfg, tpl=tpl)
    assert "remove .agents/skills/task/ (no longer in h templates)" in changes
    assert any(c.startswith("MODIFIED .claude/agents/reviewer.md: edited by the project")
               for c in changes)  # fmt: skip
    assert not _skill(proj).parent.exists() and _agent(proj).exists()
    assert skills.sync(cfg) == (["remove task"], [])
    assert not (proj / ".claude" / "skills" / "task").exists()
    recorded = json.loads((proj / skills.INSTALLED).read_text(encoding="utf-8"))
    assert recorded == {}  # the kept copy is the project's own now: said once
    assert skills.install(cfg, tpl=tpl) == []


def test_agent_templates_have_required_frontmatter():
    from importlib import resources

    for f in resources.files("harness").joinpath("templates", "agents").iterdir():
        head = f.read_text(encoding="utf-8").split("---")[1]
        assert "name:" in head and "description:" in head and "model:" in head, f.name


def test_sync_mirrors_updates_and_removes(proj):
    cfg = config.load(proj)
    src = write_skill(proj, "alpha")
    assert skills.sync(cfg) == (["mirror alpha"], [])
    assert (proj / ".claude" / "skills" / "alpha" / "SKILL.md").is_file()
    assert skills.drift(cfg) == []
    assert skills.sync(cfg) == ([], [])  # idempotent

    (src / "SKILL.md").write_text("changed\n", encoding="utf-8")
    assert skills.drift(cfg) == ["alpha: copy differs from .agents/skills/alpha"]
    assert skills.sync(cfg)[0] == ["mirror alpha"]

    (src / "SKILL.md").unlink()
    src.rmdir()
    assert skills.sync(cfg)[0] == ["remove alpha"]
    assert not (proj / ".claude" / "skills" / "alpha").exists()


def test_sync_never_touches_unmanaged_claude_skill(proj):
    cfg = config.load(proj)
    own = proj / ".claude" / "skills" / "mine"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("claude-only\n", encoding="utf-8")
    write_skill(proj, "mine")  # same name in source → conflict, not overwrite
    changes, conflicts = skills.sync(cfg)
    assert changes == [] and len(conflicts) == 1
    assert (own / "SKILL.md").read_text(encoding="utf-8") == "claude-only\n"


def test_crlf_checkout_is_not_drift(proj):
    cfg = config.load(proj)
    src = write_skill(proj, "alpha") / "SKILL.md"
    src.write_bytes(b"line one\r\nline two\r\n")
    skills.sync(cfg)
    copy = proj / ".claude" / "skills" / "alpha" / "SKILL.md"
    copy.write_bytes(b"line one\nline two\n")
    assert skills.drift(cfg) == []


def test_hook_on_skill_source_syncs(proj, monkeypatch):
    src = write_skill(proj, "alpha")
    payload = {"tool_input": {"file_path": str(src / "SKILL.md")}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["hook", "post-edit"]) == 0
    assert (proj / ".claude" / "skills" / "alpha" / "SKILL.md").is_file()


def test_skills_check_fails_on_drift(proj, capsys):
    write_skill(proj, "alpha")
    assert cli.main(["-C", str(proj), "skills", "sync", "--check"]) == 1
    assert cli.main(["-C", str(proj), "skills", "sync"]) == 0
    assert cli.main(["-C", str(proj), "skills", "sync", "--check"]) == 0


def test_init_json_lists_modified_copies(proj, capsys):
    cfg = config.load(proj)
    skills.install(cfg)
    agent = proj / ".claude" / "agents" / "locator.md"
    agent.write_text("ours\n", encoding="utf-8")
    (proj / skills.INSTALLED).unlink()  # installed by an older h: edits are reported
    assert cli.main(["-C", str(proj), "init", "--dry-run", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] and len(out["modified"]) == 1
    assert out["modified"][0].startswith("MODIFIED .claude/agents/locator.md")


def _harness_repo(proj):
    """`proj` laid out like the harness repo: templates equal to the installed copies."""
    tpl = proj / skills.TEMPLATES
    (tpl / "skills" / "task").mkdir(parents=True)
    (tpl / "skills" / "task" / "SKILL.md").write_text("v1\n", encoding="utf-8")
    (tpl / "agents").mkdir()
    (tpl / "agents" / "reviewer.md").write_text("agent\n", encoding="utf-8")
    write_skill(proj, "task", "v1\n")
    (proj / ".claude" / "agents").mkdir(parents=True)
    (proj / ".claude" / "agents" / "reviewer.md").write_text(
        "agent\n", encoding="utf-8"
    )
    skills.sync(config.load(proj))
    return tpl


def test_harness_repo_copies_must_match_templates(proj, capsys):
    _harness_repo(proj)
    cfg = config.load(proj)
    assert skills.template_drift(cfg) == []
    # B2: CRLF against LF is not drift
    (proj / ".claude" / "agents" / "reviewer.md").write_bytes(b"agent\r\n")
    assert skills.template_drift(cfg) == []
    # B1: a skill edited only in .agents/skills
    write_skill(proj, "task", "v2\n")
    skills.sync(cfg)
    assert cli.main(["-C", str(proj), "skills", "sync", "--check"]) == 1
    out = capsys.readouterr().out
    assert (
        ".agents/skills/task/SKILL.md: differs from "
        "harness/src/harness/templates/skills/task/SKILL.md"
    ) in out
    assert "edit there" in out


def test_harness_repo_extra_or_missing_file_is_drift(proj):
    tpl = _harness_repo(proj)
    cfg = config.load(proj)
    # B3: a skill only this repo has
    write_skill(proj, "local")
    skills.sync(cfg)
    assert skills.template_drift(cfg) == [
        (
            ".agents/skills/local/SKILL.md: not in harness/src/harness/templates/skills/"
            "local/SKILL.md — add it there (source of truth: templates — edit there, "
            "then copy over and run `h skills sync`)"
        )
    ]
    (proj / ".agents" / "skills" / "local" / "SKILL.md").unlink()
    (tpl / "agents" / "locator.md").write_text("new\n", encoding="utf-8")
    [problem] = skills.template_drift(cfg)
    assert problem.startswith(".claude/agents/locator.md: missing")


def test_template_check_only_in_harness_repo(proj):
    # B4: a project without the harness templates tree keeps the old behaviour
    write_skill(proj, "local")
    cfg = config.load(proj)
    skills.sync(cfg)
    assert skills.template_drift(cfg) == []
    assert cli.main(["-C", str(proj), "skills", "sync", "--check"]) == 0


@pytest.mark.parametrize("edited", [False, True])
def test_a_renamed_skill_is_replaced_unless_edited(proj, tpl, edited):
    """code-review → review-gate; the old name was a Claude Code command."""
    old = tpl / "skills" / "code-review"
    shutil.copytree(tpl / "skills" / "task", old)
    cfg = config.load(proj)
    skills.install(cfg, tpl=tpl)
    skills.sync(cfg)
    mine = proj / ".agents" / "skills" / "code-review" / "SKILL.md"
    if edited:
        mine.write_text("ours\n", encoding="utf-8")
    old.rename(tpl / "skills" / "review-gate")
    changes = skills.install(cfg, tpl=tpl)
    assert "create .agents/skills/review-gate/" in changes
    if edited:
        assert any(c.startswith("MODIFIED .agents/skills/code-review/: edited by the "
                                "project, renamed to review-gate") for c in changes)  # fmt: skip
        assert mine.read_text(encoding="utf-8") == "ours\n"
    else:
        assert any(c.startswith("remove .agents/skills/code-review/ (renamed to "
                                "review-gate") for c in changes)  # fmt: skip
        assert not mine.exists()
        assert skills.sync(cfg) == (["mirror review-gate", "remove code-review"], [])

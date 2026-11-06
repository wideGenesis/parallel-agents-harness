"""`[review] paths`: a skill checklist the reviewer gets for matching files and the
verdict must name."""

import json

import pytest
from conftest import git
from test_review import CHECKS, TRIVIAL, write_verdict

from harness import cli, config, init, review, task

SKILL = """---
name: audit
description: x
---

# Audit

For the author: write it well.

## For the reviewer

- a token in a workflow log is a blocker

### Red flags

- `pull_request_target` with a checkout of the PR head

## Later

Not for the reviewer.
"""


def _start(world, github, paths: str, skill: bool = True):
    _, a, _ = world
    with (a / "harness.toml").open("a", encoding="utf-8") as f:
        f.write(CHECKS + f"\n[review]\npaths = {paths}\n")
    if skill:
        d = a / ".claude" / "skills" / "audit"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(SKILL, encoding="utf-8")
        git(a, "add", ".claude")
    git(a, "commit", "-qam", "review paths")
    git(a, "push", "-q", "origin", "main")
    github.add_issue(TRIVIAL, ["ac-confirmed"], number=5)
    r = task.start(config.load(a), "gh-5")
    f = r.path / "py" / "poly-lib" / "src" / "poly_lib" / "__init__.py"
    f.write_text(f.read_text(encoding="utf-8") + "# change\n", encoding="utf-8")
    git(r.path, "commit", "-qam", "feat: change")
    return r.path


def test_matching_path_adds_the_checklist_and_the_verdict_must_name_it(world, github):
    wt = _start(world, github, '{ "py/**" = "audit" }')
    cfg = config.load(wt)
    pkg, _, _ = review.prepare(cfg, wt)
    text = pkg.read_text(encoding="utf-8")
    assert "## Additional checklists" in text and "### audit" in text
    assert "a token in a workflow log is a blocker" in text
    assert "`pull_request_target`" in text  # a subsection stays in
    assert "For the author" not in text and "Not for the reviewer" not in text
    assert review._state(wt)["checklists"] == ["audit"]

    write_verdict(wt, "approve")  # no "checklists": as if there were no review
    with pytest.raises(review.ReviewError, match="does not cover checklist audit"):
        review.gate(cfg, wt)
    v = json.loads((wt / review.VERDICT).read_text(encoding="utf-8"))
    (wt / review.VERDICT).write_text(json.dumps({**v, "checklists": ["audit"]}),
                                     encoding="utf-8")  # fmt: skip
    got = review.gate(cfg, wt)
    assert got is not None and got.checklists == ["audit"]
    assert "Checklists: `audit`" in review.pr_section(got)


def test_no_matching_path_leaves_the_package_as_it_was(world, github):
    wt = _start(world, github, '{ "migrations/**" = "audit" }')
    cfg = config.load(wt)
    pkg, _, _ = review.prepare(cfg, wt)
    assert "Additional checklists" not in pkg.read_text(encoding="utf-8")
    assert "checklists" not in review._state(wt)
    write_verdict(wt, "approve")
    assert review.gate(cfg, wt) is not None


def test_unknown_skill_is_named(world, github):
    wt = _start(world, github, '{ "py/**" = "nope" }', skill=False)
    with pytest.raises(
        review.ReviewError, match="skill `nope` .*py/\\*\\*.* not installed"
    ):
        review.prepare(config.load(wt), wt)


def test_old_skill_name_in_paths_finds_the_renamed_skill(tmp_path):
    # harness.toml from an older h names security-review; h init now installs
    # security-rules. A project that kept its own security-review keeps getting it.
    (tmp_path / "harness.toml").write_text(
        '[review]\npaths = { ".github/workflows/**" = "security-review" }\n',
        encoding="utf-8",
    )
    d = tmp_path / ".claude" / "skills" / "security-rules"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL, encoding="utf-8")
    cfg = config.load(tmp_path)
    files = [".github/workflows/ci.yml"]
    [(name, text)] = review.checklists(cfg, tmp_path, files)
    assert name == "security-rules" and "a token in a workflow log" in text

    own = tmp_path / ".agents" / "skills" / "security-review"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text(SKILL.replace("a token", "OWN"), encoding="utf-8")
    [(name, text)] = review.checklists(cfg, tmp_path, files)
    assert name == "security-review" and "OWN" in text


def test_init_tips_an_old_skill_name_in_paths(tmp_path):
    (tmp_path / "harness.toml").write_text(
        '[review]\npaths = { ".github/workflows/**" = "security-review" }\n',
        encoding="utf-8",
    )
    changes = init.run(config.load(tmp_path), dry_run=True)
    assert any('h renamed that skill to "security-rules"' in c for c in changes)


def test_a_branch_cannot_drop_its_checklist(world, github):
    """`[review]` is a gate rule: the base branch's paths apply to the task branch."""
    wt = _start(world, github, '{ "py/**" = "audit" }')
    toml = wt / "harness.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace('"py/**" = "audit"', ""),
                    encoding="utf-8")  # fmt: skip
    git(wt, "commit", "-qam", "drop the checklist")
    assert config.load(wt).review_paths == {"py/**": "audit"}


@pytest.mark.parametrize(
    "paths", ['"audit"', '{ "py/**" = "../x" }', '{ "py/**" = 1 }']
)
def test_bad_paths_are_a_config_error(tmp_path, paths):
    (tmp_path / "harness.toml").write_text(
        f"[review]\npaths = {paths}\n", encoding="utf-8"
    )
    with pytest.raises(config.ConfigError, match="review.paths"):
        config.load(tmp_path)


def test_whole_skill_without_a_reviewer_section():
    assert (
        review._reviewer_section("---\nname: x\n---\n# X\n\n- check\n")
        == "# X\n\n- check"
    )
    assert review._reviewer_section("## For the Reviewer\n\n- item\n") == "- item"


def test_init_proposes_workflows_and_config_shows_it(tmp_path, capsys):
    init.run(config.load(tmp_path), dry_run=False)
    text = (tmp_path / "harness.toml").read_text(encoding="utf-8")
    assert '"security-rules"' in text
    cfg = config.load(tmp_path)  # the line is valid TOML either way
    expected = {} if "# paths" in text else {".github/workflows/**": "security-rules"}
    assert cfg.review_paths == expected
    (tmp_path / "harness.toml").write_text(
        '[review]\npaths = { ".github/workflows/**" = "security-rules" }\n',
        encoding="utf-8",
    )
    cli.main(["-C", str(tmp_path), "config", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["review_paths"] == {".github/workflows/**": "security-rules"}

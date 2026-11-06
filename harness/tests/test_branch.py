import re

import pytest
from conftest import git, out

from harness import claims, config, task

# a project's pre-push accepts only this shape.
PROJECT_BRANCH = re.compile(r"^task/[A-Z][A-Z0-9]*-[0-9]{2,}-[a-z0-9-]+$")


def with_tasks(clone, body):
    toml = clone / "harness.toml"  # the fixture already has a [tasks] table
    text = toml.read_text(encoding="utf-8")
    toml.write_text(text.replace("[tasks]\n", "[tasks]\n" + body, 1), encoding="utf-8")
    git(clone, "commit", "-qam", "tasks cfg")
    git(clone, "push", "-q", "origin", "main")


def test_default_branch_keeps_slug_optional(world):
    _, a, _ = world
    cfg = config.load(a)
    assert task.branch_name(cfg, "T-1", "") == "task/T-1"
    assert task.branch_name(cfg, "T-1", "x") == "task/T-1-x"
    assert task.branch_globs(cfg, "T-1") == ["task/T-1", "task/T-1-*"]


def test_template_and_external_card(world):
    _, a, _ = world
    card = a / "docs" / "backlog" / "epic_9_search" / "cards" / "SEARCH-99.md"
    card.parent.mkdir(parents=True)
    card.write_text("### TASK-SEARCH-99 — refetch\n\nProblem: x\n", encoding="utf-8")
    git(a, "add", "docs")
    with_tasks(
        a, 'branch = "task/{id}-{slug}"\ncard_path = "docs/backlog/*/cards/{id}.md"\n'
    )
    cfg = config.load(a)
    r = task.start(cfg, "SEARCH-99", slug="refetch")
    assert PROJECT_BRANCH.match(r.branch), r.branch
    brief = (r.path / ".artifacts" / "task" / "brief.md").read_text(encoding="utf-8")
    assert brief.startswith("### TASK-SEARCH-99")
    assert claims.read(cfg, "SEARCH-99") is not None  # the claim guards a file card too


def test_template_with_slug_requires_one(world):
    _, a, _ = world
    with_tasks(a, 'branch = "task/{id}-{slug}"\n')
    with pytest.raises(claims.ClaimError, match="needs --slug"):
        task.start(config.load(a), "SEARCH-98")


def test_missing_card_is_refused(world):
    _, a, _ = world
    with_tasks(a, 'card_path = "docs/backlog/*/cards/{id}.md"\n')
    with pytest.raises(claims.ClaimError, match="no card for NOPE-01"):
        task.start(config.load(a), "NOPE-01", slug="x")


def test_custom_separator_is_found_by_cleanup(world):
    _, a, _ = world
    with_tasks(a, 'branch = "feat/{id}_{slug}"\n')
    cfg = config.load(a)
    r = task.start(cfg, "AB-12", slug="thing")
    assert r.branch == "feat/AB-12_thing"
    assert task.branch_globs(cfg, "AB-12") == ["feat/AB-12", "feat/AB-12_*"]
    assert (
        out(a, "branch", "--list", *task.branch_globs(cfg, "AB-12"))
        .strip()
        .endswith("feat/AB-12_thing")
    )


@pytest.mark.parametrize(("body", "msg"), [
    ('[tasks]\nbranch = "task/{idd}-{slug}"\n', "unknown"),
    ('[tasks]\nbranch = "task/fixed"\n', "must contain"),
    ('[tasks]\nbranch = "{slug}/{id}"\n', "must come before"),
    ('[tasks]\ncard_path = "cards/{name}.md"\n', "unknown"),
])  # fmt: skip
def test_bad_templates_are_config_errors(tmp_path, body, msg):
    (tmp_path / "harness.toml").write_text(body, encoding="utf-8")
    with pytest.raises(config.ConfigError, match=msg):
        config.load(tmp_path)

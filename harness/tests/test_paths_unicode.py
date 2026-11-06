"""Non-ASCII file names reach affected, policy, fmt --changed and the dirty-tree guard
unquoted: git escapes them as "\321\202..." unless asked for -z output."""

import json

from conftest import git
from test_policy import TIERS

from harness import affected, cli, config, policy, stacks, task

NAME = "py/poly-lib/src/poly_lib/テスト.py"


def test_non_ascii_file_affects_its_package(repo):
    (repo / NAME).write_text("X = 1\n", encoding="utf-8")
    cfg = config.load(repo)
    files = affected.changed_files(cfg, affected.resolve_base(cfg, None))
    assert NAME in files
    impact = affected.compute(cfg, stacks.detect(cfg), files)
    assert impact["python"].affected == {"poly-app", "poly-lib"}
    git(repo, "add", NAME)
    git(repo, "commit", "-qm", "add")
    assert NAME in affected.changed_files(cfg, affected.resolve_base(cfg, None))


def test_policy_matches_a_non_ascii_migration(repo):
    git(repo, "switch", "-q", "main")
    (repo / "harness.toml").write_text(TIERS, encoding="utf-8")
    git(repo, "add", "harness.toml")
    git(repo, "commit", "-qm", "rules")
    git(repo, "switch", "-q", "-C", "work")
    (repo / "migrations").mkdir()
    (repo / "migrations" / "マイグレーション.sql").write_text(
        "-- up\n", encoding="utf-8"
    )
    d = policy.for_worktree(config.load(repo), repo)
    assert (d.review, d.merge) == (True, "user")
    assert "migrations/マイグレーション.sql" in d.reason


def test_fmt_changed_formats_a_non_ascii_file(repo, capsys):
    (repo / NAME).write_text("X = 1\n", encoding="utf-8")
    assert cli.main(["-C", str(repo), "fmt", "--changed", "--json"]) == 0
    results = json.loads(capsys.readouterr().out)
    assert [r["file"] for r in results] == [NAME]


def test_dirty_tree_lists_non_ascii_paths_readably(repo):
    (repo / NAME).write_text("X = 1\n", encoding="utf-8")
    assert task._user_changes(config.load(repo), repo) == [f"?? {NAME}"]

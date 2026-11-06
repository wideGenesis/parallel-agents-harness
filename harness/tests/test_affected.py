import shutil

import pytest
from conftest import IGNORE, POLYGON, git, make_repo

from harness import affected, cli, config, stacks


def impact_of(proj):
    cfg = config.load(proj)
    found = stacks.detect(cfg)
    files = affected.changed_files(cfg, affected.resolve_base(cfg, None))
    return {
        name: ("ALL" if imp.all else sorted(imp.affected))
        for name, imp in affected.compute(cfg, found, files).items()
    }


def touch(proj, rel, text="// changed\n"):
    p = proj / rel
    p.write_text(
        p.read_text(encoding="utf-8") + text if p.exists() else text, encoding="utf-8"
    )


def test_graph_reads_workspace_deps():
    found = {st.name: st.by_name() for st in stacks.detect(config.load(POLYGON))}
    assert found["rust"]["poly-cli"].deps == ["poly-core"]
    assert found["python"]["poly-app"].deps == ["poly-lib"]
    assert found["ts"]["@poly/app"].deps == ["@poly/lib"]
    assert found["rust"]["poly-core"].deps == []


def test_no_changes_affects_nothing(repo):
    assert impact_of(repo) == {"rust": [], "python": [], "ts": []}


def test_lib_change_pulls_in_dependents(repo):
    touch(repo, "crates/poly-core/src/lib.rs")
    touch(repo, "py/poly-lib/src/poly_lib/__init__.py", "# changed\n")
    assert impact_of(repo) == {
        "rust": ["poly-cli", "poly-core"],
        "python": ["poly-app", "poly-lib"],
        "ts": [],
    }


def test_leaf_change_stays_local(repo):
    touch(repo, "ts/app/src/main.ts")
    assert impact_of(repo)["ts"] == ["@poly/app"]


def test_committed_and_untracked_both_count(repo):
    touch(repo, "crates/poly-cli/src/main.rs")
    git(repo, "commit", "-q", "-am", "edit")
    (repo / "ts" / "lib" / "src" / "new.ts").write_text(
        "export {};\n", encoding="utf-8"
    )
    impact = impact_of(repo)
    assert impact["rust"] == ["poly-cli"]
    assert impact["ts"] == ["@poly/app", "@poly/lib"]


def test_lockfile_affects_whole_stack(repo):
    touch(repo, "Cargo.lock", "# changed\n")
    assert impact_of(repo) == {"rust": "ALL", "python": [], "ts": []}


def test_unowned_file_affects_nothing(repo):
    (repo / "README.md").write_text("docs\n", encoding="utf-8")
    assert impact_of(repo) == {"rust": [], "python": [], "ts": []}


def test_project_in_repo_subdirectory(tmp_path):
    # harness.toml below the git top level, like polygon/ inside this repo.
    proj = make_repo(tmp_path, subdir="polygon")
    (tmp_path / "repo" / "outside.rs").write_text("fn x() {}\n", encoding="utf-8")
    touch(proj, "crates/poly-core/src/lib.rs")
    assert impact_of(proj)["rust"] == ["poly-cli", "poly-core"]


def test_cli_affected_text(repo, capsys):
    touch(repo, "crates/poly-core/src/lib.rs")
    assert cli.main(["-C", str(repo), "affected"]) == 0
    out = capsys.readouterr().out
    assert "rust: poly-core + dependents poly-cli" in out


def test_cli_reports_missing_base(tmp_path, capsys):
    proj = tmp_path / "p"
    shutil.copytree(POLYGON, proj, ignore=IGNORE)
    git(proj, "init", "-q", "-b", "trunk")
    git(proj, "add", ".")
    git(proj, "commit", "-q", "-m", "init")
    assert cli.main(["-C", str(proj), "affected"]) == 2
    assert "no base ref found" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("rel", "stack"),
    [
        (".ruff.toml", "python"), ("conftest.py", "python"), ("pytest.ini", "python"),
        ("setup.cfg", "python"), ("tox.ini", "python"),
        ("biome.jsonc", "ts"), ("eslint.config.mjs", "ts"), (".eslintrc.json", "ts"),
        (".prettierrc", "ts"), ("prettier.config.js", "ts"),
        (".config/nextest.toml", "rust"), ("deny.toml", "rust"),
    ],
)  # fmt: skip
def test_stack_wide_config_affects_the_whole_stack(repo, rel, stack):
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text("# config\n", encoding="utf-8")
    impact = impact_of(repo)
    assert impact[stack] == "ALL"
    assert all(v == [] for name, v in impact.items() if name != stack)


def test_project_global_files_from_harness_toml(repo):
    toml = repo / "harness.toml"
    text = toml.read_text(encoding="utf-8")
    toml.write_text(text.replace("[stacks.python]\n",
                                 '[stacks.python]\nglobal_files = ["tools/lint/*.toml"]\n'),
                    encoding="utf-8")  # fmt: skip
    git(repo, "commit", "-qam", "global files")
    (repo / "tools" / "lint").mkdir(parents=True)
    (repo / "tools" / "lint" / "strict.toml").write_text("x = 1\n", encoding="utf-8")
    assert impact_of(repo)["python"] == "ALL"


def test_docs_outside_packages_affect_nothing(repo):
    (repo / "README.md").write_text("# readme\n", encoding="utf-8")
    assert impact_of(repo) == {"rust": [], "python": [], "ts": []}


def test_bad_global_files_is_a_config_error(tmp_path):
    (tmp_path / "harness.toml").write_text(
        '[stacks.python]\nglobal_files = "*.toml"\n', encoding="utf-8"
    )
    with pytest.raises(config.ConfigError, match="global_files must be a list"):
        config.load(tmp_path)


# --- CI: docs-only PRs and project-wide files ---


def affected_json(proj, capsys, *args):
    import json

    assert cli.main(["-C", str(proj), "affected", "--json", *args]) == 0
    return json.loads(capsys.readouterr().out)


def test_docs_only_change_skips_ci(repo, capsys):
    (repo / "docs").mkdir()
    (repo / "docs" / "plan.md").write_text("plan\n", encoding="utf-8")
    touch(repo, "README.md", "more\n")
    out = affected_json(repo, capsys)
    assert out["docs_only"] is True
    assert all(not s["affected"] and not s["all"] for s in out["stacks"].values())


def test_one_package_is_not_docs_only(repo, capsys):
    touch(repo, "README.md", "more\n")
    touch(repo, "ts/app/src/main.ts")
    out = affected_json(repo, capsys)
    assert out["docs_only"] is False
    assert out["stacks"]["ts"]["affected"] == ["@poly/app"]
    assert out["stacks"]["rust"]["affected"] == []


def test_markdown_below_the_root_is_not_docs(repo, capsys):
    # `*.md` is root-only: skill sources and the like are read by tests.
    skill = repo / ".agents" / "skills" / "task"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# task\n", encoding="utf-8")
    assert affected_json(repo, capsys)["docs_only"] is False


def test_docs_globs_come_from_harness_toml(repo, capsys):
    (repo / "notes.txt").write_text("x\n", encoding="utf-8")
    assert affected_json(repo, capsys)["docs_only"] is False
    toml = repo / "harness.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8") + '\n[check]\ndocs = ["*.txt"]\n',
        encoding="utf-8",
    )
    git(repo, "commit", "-q", "-am", "docs globs")
    # A gate rule: the branch's own globs do not apply (rules come from the base) ...
    assert affected_json(repo, capsys, "--base", "HEAD")["docs_only"] is False
    git(repo, "branch", "-f", "main", "HEAD")  # ... merged, they do
    assert affected_json(repo, capsys, "--base", "HEAD")["docs_only"] is True


def test_no_change_is_not_docs_only(repo, capsys):
    assert affected_json(repo, capsys)["docs_only"] is False


@pytest.mark.parametrize(
    "rel",
    [".github/workflows/ci.yml", ".github/actions/setup/action.yml", "harness.toml"],
)
def test_project_wide_files_affect_everything(repo, rel):
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    touch(repo, rel, "# changed\n")
    assert impact_of(repo) == {"rust": "ALL", "python": "ALL", "ts": "ALL"}


@pytest.mark.parametrize(
    ("path", "pattern", "hit"),
    [
        ("README.md", "*.md", True),
        ("py/README.md", "*.md", False),
        ("docs/a/b.md", "docs/**", True),
        ("docsx/a.md", "docs/**", False),
        ("a/b/c.md", "**/*.md", True),
        ("c.md", "**/*.md", True),
        ("a.mdx", "*.md", False),
    ],
)
def test_glob_match(path, pattern, hit):
    assert affected.glob_match(path, pattern) is hit

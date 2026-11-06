"""Language skills follow the project's stacks; sql is detected without `.sql`."""

import json

import pytest
from conftest import POLYGON, git

from harness import cli, config, skills, stacks
from harness.config import ConfigError


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "harness.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    return root


@pytest.fixture
def tpl(tmp_path):
    """Templates with the language skills rust, python, sql and the always-on `task`."""
    root = tmp_path / "templates"
    for name in ("rust", "python", "sql", "task"):
        d = root / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"{name} v1\n", encoding="utf-8")
    (root / "agents").mkdir()
    return root


def python_only(root):
    (root / "pyproject.toml").write_text('[project]\nname = "p"\n', encoding="utf-8")


def installed(root):
    base = root / skills.SOURCE
    return sorted(p.name for p in base.iterdir()) if base.is_dir() else []


def test_python_project_gets_python_skill_not_rust(proj, tpl):
    """B1."""
    python_only(proj)
    changes = skills.install(config.load(proj), tpl=tpl)
    assert installed(proj) == ["python", "task"]
    assert "create .agents/skills/python/" in changes
    assert not any("rust" in c for c in changes)  # an absent stack is not news


def test_polygon_gets_all_three_language_skills(tpl):
    """B4: dry run, the polygon is not touched."""
    changes = skills.install(config.load(POLYGON), dry_run=True, tpl=tpl)
    for name in ("rust", "python"):
        assert f"create .agents/skills/{name}/" in changes
    assert stacks.present(config.load(POLYGON)) >= {"rust", "python", "ts"}


def test_a_gone_stack_drops_an_untouched_skill_and_keeps_an_edited_one(proj, tpl):
    """B3, the same INSTALLED logic as a skill dropped from the templates."""
    cfg = config.load(proj)
    skills.install(cfg, tpl=tpl, have_stacks={"rust", "python"})
    (proj / skills.SOURCE / "python" / "SKILL.md").write_text(
        "ours\n", encoding="utf-8"
    )
    changes = skills.install(cfg, tpl=tpl, have_stacks=set())
    assert "remove .agents/skills/rust/ (the project has no rust stack)" in changes
    assert ("MODIFIED .agents/skills/python/: edited by the project, the project has "
            "no python stack — kept") in changes  # fmt: skip
    assert installed(proj) == ["python", "task"]
    assert skills.install(cfg, tpl=tpl, have_stacks=set()) == []  # said once
    # the stack is back: the removed skill returns, the kept one stays the project's
    changes = skills.install(cfg, tpl=tpl, have_stacks={"rust", "python"})
    assert "create .agents/skills/rust/" in changes
    assert (proj / skills.SOURCE / "python" / "SKILL.md").read_text("utf-8") == "ours\n"


def test_a_project_skill_named_like_a_language_is_left_alone(proj, tpl):
    own = proj / skills.SOURCE / "sql"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("ours\n", encoding="utf-8")
    assert skills.install(config.load(proj), tpl=tpl, have_stacks=set()) == [
        "create .agents/skills/task/"
    ]
    assert (own / "SKILL.md").is_file()


def repo(root):
    git(root, "init", "-q")
    return config.load(root)


@pytest.mark.parametrize(
    "files",
    [
        {"db/schema.sql": ""},
        {"alembic.ini": "[alembic]\n"},
        {"app/alembic/env.py": ""},
        {"migrations/0001_init.py": ""},
        {"crates/db/.sqlx/query-1.json": "{}"},
        {"crates/db/Cargo.toml": '[dependencies]\nsqlx = "0.8"\n'},
        {"Cargo.toml": '[workspace.dependencies]\nsqlx = { version = "0.8" }\n'},
        {"Cargo.toml": "[target.'cfg(unix)'.dependencies]\nsqlx = \"0.8\"\n"},
    ],
)
def test_sql_is_detected_without_a_single_sql_file_too(proj, files):
    """B5: alembic, migrations, sqlx."""
    for rel, text in files.items():
        (proj / rel).parent.mkdir(parents=True, exist_ok=True)
        (proj / rel).write_text(text, encoding="utf-8")
    assert stacks.has_sql(repo(proj))


def test_no_sql_signs_no_sql(proj):
    python_only(proj)
    (proj / "Cargo.toml").write_text('[dependencies]\nserde = "1"\n', encoding="utf-8")
    (proj / ".gitignore").write_text("build/\n", encoding="utf-8")
    (proj / "build").mkdir()
    (proj / "build" / "dump.sql").write_text("", encoding="utf-8")  # ignored
    assert not stacks.has_sql(repo(proj))


def test_python_with_alembic_gets_the_sql_skill(proj, tpl):
    """B5 end to end."""
    python_only(proj)
    (proj / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    skills.install(repo(proj), tpl=tpl)
    assert installed(proj) == ["python", "sql", "task"]


def test_harness_toml_overrides_sql_detection(proj):
    (proj / "alembic.ini").write_text("", encoding="utf-8")
    toml = proj / "harness.toml"
    toml.write_text(toml.read_text("utf-8") + "[stacks]\nsql = false\n", "utf-8")
    assert not stacks.has_sql(repo(proj))
    toml.write_text('[project]\nname = "demo"\n[stacks]\nsql = true\n', "utf-8")
    assert stacks.has_sql(config.load(proj))  # no repo, no signs: the setting decides
    toml.write_text('[project]\nname = "demo"\n[stacks]\nsql = "yes"\n', "utf-8")
    with pytest.raises(ConfigError, match="stacks.sql must be true or false"):
        config.load(proj)


def test_doctor_suggests_init_for_a_new_stack(proj, tpl, monkeypatch, capsys):
    """B2: Rust added after `h init`."""
    monkeypatch.setattr(skills, "_templates", lambda: tpl)
    python_only(proj)
    skills.install(config.load(proj))
    (proj / "Cargo.toml").write_text('[package]\nname = "r"\n', encoding="utf-8")
    cli.main(["-C", str(proj), "doctor", "--json"])
    out = json.loads(capsys.readouterr().out)
    [tip] = [t for t in out["tips"] if "language skill" in t]
    assert "rust" in tip and "`h init`" in tip and "python" not in tip
    assert not any("language skill" in p for p in out["problems"])
    # deleted on purpose after install: no nagging
    skills.install(config.load(proj))
    (proj / skills.SOURCE / "rust" / "SKILL.md").unlink()
    (proj / skills.SOURCE / "rust").rmdir()
    assert skills.missing(config.load(proj), {"rust", "python"}) == []


def test_security_review_is_installed_without_any_stack(proj):
    """not a language skill — every project gets it from the real templates."""
    changes = skills.install(config.load(proj), have_stacks=set())
    assert "create .agents/skills/security-rules/" in changes
    assert (
        proj / skills.SOURCE / "security-rules" / "references" / "patterns.md"
    ).is_file()
    assert "security-rules" not in skills.LANGUAGE

"""ADRs in the brief, the review package and the PR body."""

import json
from pathlib import Path

import pytest
from conftest import git
from test_cards import READY_BODY
from test_review import CHECKS

from harness import adr, cards, config, progress, review, ship, task

PROJECT_FORMAT = """# ADR-0013: review is a merge gate

- status: accepted
- date: 2026-09-20

## Decision

Nothing merges without the reviewer's approve. UNIQUE-0013-BODY
"""
HARNESS = """# ADR-0002 — Task claims on origin

Status: accepted · 2026-10-20 · Basis: the plan

## Decision

- [ ] T9 (←B1): a checkbox inside an ADR is not the card's AC-T
"""
ODD = "# Some draft\n\nNo status line here.\n"


def write_adrs(root, rel="docs/adr"):
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "0013-review-is-a-merge-gate.md").write_text(PROJECT_FORMAT, encoding="utf-8")
    (d / "0002-task-claims-on-origin.md").write_text(HARNESS, encoding="utf-8")
    (d / "0007-draft.md").write_text(ODD, encoding="utf-8")
    (d / "README.md").write_text("# template\n", encoding="utf-8")


def test_index_reads_number_title_and_status_of_both_formats(tmp_path):
    write_adrs(tmp_path)
    cfg = config.load(tmp_path)
    got = [(a.ref, a.status, a.title) for a in adr.index(cfg, tmp_path)]
    assert got == [
        ("ADR-0002", "accepted", "Task claims on origin"),
        ("ADR-0007", "unknown", "Some draft"),
        ("ADR-0013", "accepted", "review is a merge gate"),
    ]


def test_no_directory_or_switched_off_means_no_section(tmp_path):
    cfg = config.load(tmp_path)
    assert adr.index(cfg, tmp_path) == []
    assert adr.with_section(cfg, tmp_path, "# card\n") == "# card\n"
    write_adrs(tmp_path)
    (tmp_path / "harness.toml").write_text('[adr]\ndir = ""\n', encoding="utf-8")
    cfg = config.load(tmp_path)
    assert adr.with_section(cfg, tmp_path, "# card ADR-0013\n") == "# card ADR-0013\n"


def test_section_has_the_index_and_full_text_of_named_adrs_only(tmp_path):
    write_adrs(tmp_path, "decisions")
    (tmp_path / "harness.toml").write_text(
        '[adr]\ndir = "decisions"\n', encoding="utf-8"
    )
    cfg = config.load(tmp_path)
    text = adr.with_section(cfg, tmp_path, "# card\n\nKeep ADR-0013; see ADR-0099.\n")
    assert text.startswith("# card\n")
    assert (
        "- ADR-0013 [accepted] review is a merge gate — `decisions/0013-review" in text
    )
    assert "- ADR-0007 [unknown] Some draft" in text
    assert "UNIQUE-0013-BODY" in text  # named → full text
    assert "T9 (←B1)" not in text  # not named → index line only
    assert "Named in the card but not found: ADR-0099" in text
    # Rebuilding replaces the block instead of stacking a second one.
    again = adr.with_section(cfg, tmp_path, text)
    assert again == text and again.count(adr.HEADING) == 1


def commit_adrs(clone):
    write_adrs(clone)
    git(clone, "add", "docs")
    git(clone, "commit", "-qm", "docs: adrs")
    git(clone, "push", "-q", "origin", "main")


def test_start_puts_index_and_named_adr_into_the_brief(world, github):
    _, a, _ = world
    commit_adrs(a)
    body = READY_BODY.replace("Users cannot", "Per ADR-0002, users cannot")
    n = github.add_issue(body, [cards.AC_CONFIRMED, "ready"])
    r = task.start(config.load(a), f"gh-{n}")
    brief = (r.path / ".artifacts" / "task" / "brief.md").read_text(encoding="utf-8")
    assert brief.startswith(f"# Login with Google (#{n})")
    assert "- ADR-0013 [accepted] review is a merge gate" in brief
    assert "### ADR-0002 (named in the card)" in brief and "Status: accepted" in brief
    assert "UNIQUE-0013-BODY" not in brief
    # The ADR's own checkbox is not a card item for `h progress done`.
    assert [i.id for i in progress.card_items(r.path)] == ["T1"]


def test_start_without_adrs_writes_the_card_as_before(world, github):
    _, a, _ = world
    n = github.add_issue(READY_BODY, [cards.AC_CONFIRMED, "ready"])
    r = task.start(config.load(a), f"gh-{n}")
    brief = (r.path / ".artifacts" / "task" / "brief.md").read_text(encoding="utf-8")
    assert adr.HEADING not in brief and "ADR-" not in brief


def test_review_package_lists_adrs_as_the_branch_has_them(world, github):
    _, a, _ = world
    with (a / "harness.toml").open("a", encoding="utf-8") as f:
        f.write(CHECKS)
    git(a, "commit", "-qam", "checks")
    commit_adrs(a)
    n = github.add_issue(READY_BODY.replace("Users", "ADR-0013: users"),
                         [cards.AC_CONFIRMED, "ready"])  # fmt: skip
    r = task.start(config.load(a), f"gh-{n}")
    # The branch adds an ADR after the start: the reviewer must see it.
    (r.path / "docs" / "adr" / "0044-new.md").write_text(
        "# ADR-0044: new\n\n- status: proposed\n", encoding="utf-8"
    )
    f = r.path / "py" / "poly-lib" / "src" / "poly_lib" / "__init__.py"
    f.write_text(f.read_text(encoding="utf-8") + "# change\n", encoding="utf-8")
    git(r.path, "add", "docs", "py")
    git(r.path, "commit", "-qm", "feat: change")
    pkg, _, _ = review.prepare(config.load(r.path), r.path)
    text = pkg.read_text(encoding="utf-8")
    assert text.count(adr.HEADING) == 1
    assert "- ADR-0044 [proposed] new" in text
    assert "UNIQUE-0013-BODY" in text


def test_adr_notes_become_candidates_in_the_pr_body(world, github):
    _, a, _ = world
    n = github.add_issue(READY_BODY, [cards.AC_CONFIRMED, "ready"])
    r = task.start(config.load(a), f"gh-{n}")
    cfg = config.load(r.path)
    progress.note(cfg, r.path, f"gh-{n}", "cache in memory, not redis", kind="adr")
    for i in range(progress.NOTES_MAX + 3):  # the cap must not drop the candidate
        progress.note(cfg, r.path, f"gh-{n}", f"step {i}")
    progress.note(cfg, r.path, f"gh-{n}", "plain decision", kind="decision")
    got = progress.adr_candidates(r.path)
    assert got == ["cache in memory, not redis"]
    body = ship.body(cfg, f"gh-{n}", ["c1"], "proof", adr_candidates=got)
    assert "## ADR candidates" in body and "- cache in memory, not redis" in body
    assert body.index("## ADR candidates") < body.index("## Commits")
    assert "## ADR candidates" not in ship.body(cfg, f"gh-{n}", ["c1"], "proof")


@pytest.mark.parametrize("line", ["**Status:** Accepted", "> status: superseded by 3"])
def test_status_variants(tmp_path, line):
    p = tmp_path / "0001-x.md"
    p.write_text(f"# ADR-0001: x\n\n{line}\n", encoding="utf-8")
    assert adr.parse(p, 1).status in {"accepted", "superseded"}


@pytest.mark.parametrize(
    "bad", ["../x", "docs/../../x", "/etc", "C:/adr", r"C:\adr", r"..\x"]
)
def test_adr_dir_outside_the_project_is_a_config_error(tmp_path, bad):
    toml = f"[adr]\ndir = {json.dumps(bad)}\n"
    (tmp_path / "harness.toml").write_text(toml, encoding="utf-8")
    with pytest.raises(config.ConfigError, match="adr.dir"):
        config.load(tmp_path)


def test_adr_dir_through_a_symlink_out_is_a_config_error(tmp_path, monkeypatch):
    proj, outside = tmp_path / "proj", tmp_path / "outside"
    proj.mkdir()
    (proj / "harness.toml").write_text('[adr]\ndir = "adr"\n', encoding="utf-8")
    # A link `adr` → ../outside, without symlink rights (Windows): resolve() is what follows it.
    real = Path.resolve

    def resolve(self, strict=False):
        return outside if self.name == "adr" else real(self, strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(config.ConfigError, match="outside the project"):
        config.load(proj)


def test_named_adr_with_long_backtick_runs_stays_in_one_block(tmp_path):
    d = tmp_path / "docs" / "adr"
    d.mkdir(parents=True)
    body = "# ADR-0001: fences\n\n- status: accepted\n\n`````text\ninner\n`````\n\nTAIL-LINE\n"
    (d / "0001-fences.md").write_text(body, encoding="utf-8")
    text = adr.with_section(config.load(tmp_path), tmp_path, "# card ADR-0001\n")
    fence = "`" * 6
    start = text.index(fence + "markdown\n")
    end = text.index("\n" + fence, start + 1)
    assert "TAIL-LINE" in text[start:end]  # the whole ADR inside the one block
    assert text[end + 1 :].strip() == fence  # nothing of the ADR after it

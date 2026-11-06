"""`h doctor` suggests faster Rust builds and never edits the project."""

import json

import pytest

from harness import cli, config, init, rustbuild, stacks

CARGO = '[workspace]\nmembers = ["crates/*"]\n'


def rust_project(tmp_path, cargo=CARGO):
    (tmp_path / "Cargo.toml").write_text(cargo, encoding="utf-8")
    crate = tmp_path / "crates" / "a"
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text('[package]\nname = "a"\n', encoding="utf-8")
    (crate / "src" / "lib.rs").write_text("", encoding="utf-8")
    return tmp_path


def tips(proj):
    cfg = config.load(proj)
    [st] = [s for s in stacks.detect(cfg) if s.name == "rust"]
    return rustbuild.tips(cfg, st)


def test_a_default_profile_gets_all_tips(tmp_path):
    proj = rust_project(tmp_path)
    flow = proj / ".github" / "workflows" / "ci.yml"
    flow.parent.mkdir(parents=True)
    flow.write_text("jobs: {}\n", encoding="utf-8")
    out = tips(proj)
    assert any('debug = "line-tables-only"' in t for t in out)
    [linker] = [t for t in out if "fast linker" in t]
    assert "mold on Linux, rust-lld on Windows" in linker
    assert 'linker = "rust-lld.exe"' in linker  # the template is shown
    assert any("CARGO_INCREMENTAL: 0" in t for t in out)


@pytest.mark.parametrize(
    ("debug", "tip"),
    [('"line-tables-only"', False), ("0", False), ("false", False), ("1", False),
     ("true", True), ("2", True), ('"full"', True)],
)  # fmt: skip
def test_dev_debug_level(tmp_path, debug, tip):
    proj = rust_project(tmp_path, CARGO + f"\n[profile.dev]\ndebug = {debug}\n")
    assert any("line-tables-only" in t for t in tips(proj)) is tip


LINTS = (
    "\n[workspace.lints.clippy]\n"
    'unwrap_used = "deny"\nexpect_used = { level = "warn" }\n'
    'undocumented_unsafe_blocks = "deny"\n'
)
CLIPPY = "allow-unwrap-in-tests = true\nallow-expect-in-tests = true\n"


def test_configured_project_gets_no_tips(tmp_path):
    proj = rust_project(
        tmp_path, CARGO + '\n[profile.dev]\ndebug = "line-tables-only"\n' + LINTS
    )
    (proj / "clippy.toml").write_text(CLIPPY, encoding="utf-8")
    (proj / ".cargo").mkdir()
    (proj / ".cargo" / "config.toml").write_text(
        rustbuild.config_template(), encoding="utf-8"
    )
    flow = proj / ".github" / "workflows" / "harness.yml"
    flow.parent.mkdir(parents=True)
    flow.write_text(
        f"jobs:\n  check:\n    uses: {init.HARNESS_REPO}/.github/workflows/check.yml@abc\n",
        encoding="utf-8",
    )
    before = (proj / "Cargo.toml").read_text(encoding="utf-8")
    assert tips(proj) == []
    assert (proj / "Cargo.toml").read_text(encoding="utf-8") == before


def test_one_linker_missing_is_named(tmp_path):
    proj = rust_project(tmp_path)
    (proj / ".cargo").mkdir()
    (proj / ".cargo" / "config.toml").write_text(
        '[target.x86_64-unknown-linux-gnu]\nrustflags = ["-C", "link-arg=-fuse-ld=mold"]\n',
        encoding="utf-8",
    )
    [linker] = [t for t in tips(proj) if "fast linker" in t]
    assert "(rust-lld on Windows)" in linker


def test_no_workflows_no_ci_tip(tmp_path):
    assert not any("CARGO_INCREMENTAL" in t for t in tips(rust_project(tmp_path)))


def test_doctor_prints_the_tips_and_does_not_fail_on_them(tmp_path, capsys):
    proj = rust_project(tmp_path)
    cli.main(["-C", str(proj), "doctor", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert any("line-tables-only" in t for t in out["tips"])
    assert not any("line-tables-only" in p for p in out["problems"])


def lint_tip(proj) -> str:
    [tip] = [t for t in tips(proj) if "rust skill" in t]
    return tip


def test_missing_skill_lints_are_listed_in_the_workspace_section(tmp_path):
    tip = lint_tip(rust_project(tmp_path))
    assert "[workspace.lints.clippy]" in tip
    for name in ("unwrap_used", "expect_used", "undocumented_unsafe_blocks"):
        assert f'{name} = "deny"' in tip
    assert "allow-unwrap-in-tests = true" in tip


def test_allow_counts_as_off_and_a_single_crate_gets_its_own_section(tmp_path):
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "a"\n\n[lints.clippy]\nunwrap_used = "allow"\n'
        'expect_used = "deny"\nundocumented_unsafe_blocks = "deny"\n',
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("", encoding="utf-8")
    tip = lint_tip(tmp_path)
    assert "[lints.clippy]" in tip and "[workspace" not in tip
    assert 'unwrap_used = "deny"' in tip and "expect_used" not in tip


def test_lints_on_without_test_allowance_asks_only_for_clippy_toml(tmp_path):
    tip = lint_tip(rust_project(tmp_path, CARGO + LINTS))
    assert "[workspace.lints.clippy]" not in tip
    assert "allow-expect-in-tests = true" in tip

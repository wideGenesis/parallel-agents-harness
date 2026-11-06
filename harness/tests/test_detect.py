import json
from pathlib import Path

import pytest

from harness import cli, config, stacks

POLYGON = Path(__file__).resolve().parents[2] / "polygon"


def by_name(found):
    return {st.name: st for st in found}


def test_polygon_detects_all_stacks():
    cfg = config.load(POLYGON)
    assert cfg.source == POLYGON / "harness.toml"
    assert cfg.heavy_job_limit() == 3
    found = by_name(stacks.detect(cfg))
    assert {n: sorted(p.name for p in st.packages) for n, st in found.items()} == {
        "rust": ["poly-cli", "poly-core"],
        "python": ["poly-app", "poly-lib"],
        "ts": ["@poly/app", "@poly/lib"],
    }


def test_load_from_subdirectory_finds_root():
    assert config.load(POLYGON / "crates" / "poly-core" / "src").root == POLYGON


def test_owner_picks_package_of_file():
    found = stacks.detect(config.load(POLYGON))
    hit = stacks.owner(
        found, POLYGON / "py" / "poly-lib" / "src" / "poly_lib" / "__init__.py"
    )
    assert hit is not None
    st, pkg = hit
    assert (st.name, pkg.name) == ("python", "poly-lib")
    assert stacks.owner(found, POLYGON / "harness.toml") is None


def test_no_config_means_autodetect(tmp_path):
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "solo"\n', encoding="utf-8")
    cfg = config.load(tmp_path)
    assert cfg.source is None
    [st] = stacks.detect(cfg)
    assert (st.name, [p.name for p in st.packages]) == ("rust", ["solo"])


def test_disabled_stack_is_skipped(tmp_path):
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "solo"\n', encoding="utf-8")
    (tmp_path / "harness.toml").write_text(
        "[stacks.rust]\nenabled = false\n", encoding="utf-8"
    )
    assert stacks.detect(config.load(tmp_path)) == []


def test_enabled_stack_without_manifest_fails(tmp_path):
    (tmp_path / "harness.toml").write_text(
        "[stacks.ts]\nenabled = true\n", encoding="utf-8"
    )
    with pytest.raises(FileNotFoundError):
        stacks.detect(config.load(tmp_path))


@pytest.mark.parametrize(
    "body",
    ["[stacks.go]\n", '[stacks.rust.commands]\nbuild = "x"\n', "not toml ="],
)
def test_bad_config_is_rejected(tmp_path, body):
    (tmp_path / "harness.toml").write_text(body, encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load(tmp_path)


def test_cli_config_json(capsys):
    assert cli.main(["-C", str(POLYGON), "config", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["stacks"]["rust"]["packages"]["poly-core"] == "crates/poly-core"


def test_cli_reports_config_error_with_exit_2(tmp_path, capsys):
    (tmp_path / "harness.toml").write_text("[stacks.go]\n", encoding="utf-8")
    assert cli.main(["-C", str(tmp_path), "config"]) == 2
    assert "unknown stacks" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("yaml", "want"),
    [
        ("packages:\n  - 'ts/*'\n  - \"lib/*\"\n", ["ts/*", "lib/*"]),
        ("packages:\n- ts/*\n- '!ts/legacy'\nonlyBuilt: x\n", ["ts/*", "!ts/legacy"]),
        ('packages: ["ts/*", \'lib/*\']\n', ["ts/*", "lib/*"]),
        ("packages: [\n  'ts/*', # apps\n  \"lib/*\"\n]\nother: 1\n", ["ts/*", "lib/*"]),
        ('packages:\n  - "ts/*"  # libs\n  - ts/a#b\n', ["ts/*", "ts/a#b"]),
        ("catalog:\n  - no\npackages:\n  - ts/*\nnext:\n  - no\n", ["ts/*"]),
    ],
    ids=["indented", "column-0", "flow", "flow-multiline", "inline-comment", "between-keys"],
)  # fmt: skip
def test_pnpm_workspace_in_common_yaml_styles(yaml, want):
    assert stacks._pnpm_patterns(yaml) == want


def test_column_0_list_finds_polygon_ts_packages(tmp_path):
    import shutil

    proj = tmp_path / "p"
    shutil.copytree(
        POLYGON, proj, ignore=shutil.ignore_patterns("node_modules", "target", ".venv")
    )
    (proj / "pnpm-workspace.yaml").write_text(
        "packages:\n- ts/*  # all\n", encoding="utf-8"
    )
    ts = by_name(stacks.detect(config.load(proj)))["ts"]
    assert sorted(p.name for p in ts.packages) == ["@poly/app", "@poly/lib"]


def test_zero_packages_is_a_warning(tmp_path, capsys):
    (tmp_path / "pnpm-workspace.yaml").write_text(
        "packages:\n  - nowhere/*\n", encoding="utf-8"
    )
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    found = stacks.detect(config.load(tmp_path))
    assert any("ts: pnpm-workspace.yaml found but it lists no packages" in w
               for w in stacks.warnings(found))  # fmt: skip
    cli.main(["-C", str(tmp_path), "doctor"])
    assert "lists no packages" in capsys.readouterr().out


def test_tool_only_pyproject_is_not_a_warning(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 100\n", encoding="utf-8"
    )
    assert stacks.warnings(stacks.detect(config.load(tmp_path))) == []

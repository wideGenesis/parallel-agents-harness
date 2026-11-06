"""A top-level section of harness.toml that h does not read is an error with a hint,
not a setting silently ignored."""

import re
from pathlib import Path

import pytest

from harness import cli, config

CONFIG_SRC = Path(config.__file__).read_text(encoding="utf-8")
POLYGON = Path(__file__).resolve().parents[2] / "polygon"


def _write(root, text):
    (root / "harness.toml").write_text(text, encoding="utf-8")
    return root


def test_unknown_section_names_the_closest_one(tmp_path):
    """`[worktree]` was written by an agent that followed the old security-rules."""
    _write(tmp_path, '[worktree]\ninclude = [".env"]\n')
    with pytest.raises(config.ConfigError) as e:
        config.load(tmp_path)
    assert "unknown section [worktree]; did you mean [worktrees]?" in str(e.value)


def test_unknown_section_without_a_close_one_lists_the_known(tmp_path):
    _write(tmp_path, "[zzz]\nx = 1\n")
    with pytest.raises(config.ConfigError, match=r"unknown section \[zzz\]") as e:
        config.load(tmp_path)
    assert "did you mean" not in str(e.value)
    assert "known: adr, board, branches" in str(e.value)


def test_the_cli_stops_on_it(tmp_path, capsys):
    _write(tmp_path, "[worktree]\n")
    assert cli.main(["-C", str(tmp_path), "config"]) != 0
    assert "did you mean [worktrees]?" in capsys.readouterr().err


def test_subtables_and_arrays_count_by_their_top_level_name(tmp_path):
    """`[stacks.x]`, `[check.env]`, `[[check.generated]]`, `[[policy]]`."""
    _write(
        tmp_path,
        "[stacks.rust]\nenabled = false\n"
        '[check.env]\nX = "1"\n'
        '[[check.generated]]\npath = "a.ts"\ncommand = "gen"\ninputs = ["src/**"]\n'
        '[[policy]]\npaths = ["docs/**"]\nreview = false\n',
    )
    cfg = config.load(tmp_path)
    assert cfg.check_env == {"X": "1"}
    assert [p["paths"] for p in cfg.policies if p["label"] == "policy #1"] == [
        ["docs/**"]
    ]


def test_polygon_loads():
    config.load(POLYGON)


def test_a_project_for_a_newer_h_hears_about_the_version(tmp_path, capsys):
    """The section may come from that newer h; the version message is what helps."""
    _write(tmp_path, '[harness]\nversion = ">=99.0"\n[future]\nx = 1\n')
    assert config.load(tmp_path).harness_version == ">=99.0"
    assert cli.main(["-C", str(tmp_path), "check"]) == 2
    err = capsys.readouterr().err
    assert "requires h >=99.0" in err and "unknown section" not in err


def test_sections_are_what_load_reads():
    """SECTIONS stays the list of top-level names config.py reads, both ways."""
    read = set(re.findall(r'\b(?:data|base)\.get\("([a-z_-]+)"', CONFIG_SRC))
    assert read == set(config.SECTIONS)

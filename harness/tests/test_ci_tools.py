"""CI installs exact tool versions — a new release of nextest, machete or deny must
not turn a project red with no change in it — and the sqlx-cli of the project's sqlx."""

import json
import re
from pathlib import Path

import pytest
from test_sqlx import make_project, rust_stack

from harness import cli, sqlx

REPO = Path(__file__).resolve().parents[2]


def install_tools() -> str:
    text = (REPO / ".github/workflows/check.yml").read_text(encoding="utf-8")
    m = re.search(r"uses: taiki-e/install-action@[0-9a-f]{40}.*\n(?:        .*\n)*?          tool: (.+)\n",
                  text)  # fmt: skip
    assert m, "the install-action step lost its tool list"
    return m.group(1)


def test_every_tool_ci_installs_has_an_exact_version():
    line = install_tools()
    fixed, _, sqlx_part = line.partition("${{")
    tools = [t for t in fixed.split(",") if t]
    assert {t.split("@")[0] for t in tools} == {
        "cargo-machete",
        "cargo-deny",
        "cargo-nextest",
    }
    for t in tools:
        assert re.fullmatch(r"[a-z-]+@\d+\.\d+\.\d+", t), f"{t}: not an exact version"
    # sqlx-cli only with the version the project's Cargo.lock names
    assert "format(',sqlx-cli@{0}', steps.rust.outputs.sqlx)" in sqlx_part


def test_the_rust_step_reads_the_sqlx_version_from_h():
    text = (REPO / ".github/workflows/check.yml").read_text(encoding="utf-8")
    assert "h config --json | jq -r '.stacks.rust.sqlx // empty'" in text


LOCK = """version = 4

[[package]]
name = "sqlx"
version = "{v}"

[[package]]
name = "sqlx-core"
version = "{v}"
"""


@pytest.mark.parametrize("v", ["0.8.6", "0.9.0"])
def test_sqlx_cli_follows_the_projects_sqlx(tmp_path, v):
    proj = make_project(tmp_path, {"1_init.sql": "select 1;"})
    (proj / "Cargo.lock").write_text(LOCK.format(v=v), encoding="utf-8")
    assert sqlx.cli_version(rust_stack(proj)) == v


def test_two_sqlx_versions_take_the_newer(tmp_path):
    proj = make_project(tmp_path, {"1_init.sql": "select 1;"})
    (proj / "Cargo.lock").write_text(LOCK.format(v="0.7.4") + LOCK.format(v="0.8.6")[12:],
                                     encoding="utf-8")  # fmt: skip
    assert sqlx.cli_version(rust_stack(proj)) == "0.8.6"


def test_no_sqlx_in_the_lock_or_no_sql_step_installs_none(tmp_path, repo):
    proj = make_project(tmp_path, {"1_init.sql": "select 1;"})
    (proj / "Cargo.lock").write_text('version = 4\n[[package]]\nname = "serde"\nversion = "1.0.0"\n',
                                     encoding="utf-8")  # fmt: skip
    assert sqlx.cli_version(rust_stack(proj)) is None
    assert sqlx.cli_version(rust_stack(repo)) is None  # polygon: no sqlx at all


def test_h_config_json_names_it(tmp_path, capsys):
    proj = make_project(tmp_path, {"1_init.sql": "select 1;"})
    (proj / "Cargo.lock").write_text(LOCK.format(v="0.9.0"), encoding="utf-8")
    assert cli.main(["-C", str(proj), "config", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["stacks"]["rust"]["sqlx"] == "0.9.0"

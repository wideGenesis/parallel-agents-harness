import io
import json
import subprocess

import pytest

from harness import cli, config, fmt, init, tools

needs_rustfmt = pytest.mark.skipif(
    not tools.usable("rustfmt"), reason="rustfmt not usable"
)


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "harness.toml").write_text(
        '[project]\nname = "demo"\n[generated]\npaths = ["src/gen/**", "**/*.gen.ts"]\n',
        encoding="utf-8",
    )
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nedition = "2024"\n', encoding="utf-8"
    )
    (tmp_path / "src" / "gen").mkdir(parents=True)
    return tmp_path


UGLY_RS = 'fn main(){let x=1;println!("{}",x);}\n'


@needs_rustfmt
def test_formats_rust_file(proj):
    f = proj / "src" / "main.rs"
    f.write_text(UGLY_RS, encoding="utf-8")
    r = fmt.format_file(config.load(proj), f)
    assert r.status == "formatted"
    assert "let x = 1;" in f.read_text(encoding="utf-8")


@pytest.mark.skipif(not tools.usable("ruff"), reason="ruff not usable")
def test_formats_python_file_and_autofixes(proj):
    f = proj / "mod.py"
    f.write_text("import os\nimport sys\nx=[1,2]\nprint(sys.argv)\n", encoding="utf-8")
    r = fmt.format_file(config.load(proj), f)
    assert r.status == "formatted"
    text = f.read_text(encoding="utf-8")
    assert "x = [1, 2]" in text
    assert (
        "import os" in text
    )  # F401 is never auto-removed mid-edit (h check reports it)


def test_generated_file_is_never_touched(proj):
    f = proj / "src" / "gen" / "api.rs"
    f.write_text(UGLY_RS, encoding="utf-8")
    assert fmt.format_file(config.load(proj), f).status == "skipped"
    assert f.read_text(encoding="utf-8") == UGLY_RS


def test_unknown_extension_is_skipped(proj):
    f = proj / "notes.txt"
    f.write_text("x", encoding="utf-8")
    assert fmt.format_file(config.load(proj), f).detail == "no formatter for this file"


def run_hook(monkeypatch, payload) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    return cli.main(["hook", "post-edit"])


@needs_rustfmt
def test_hook_formats_silently(proj, monkeypatch, capsys):
    f = proj / "src" / "main.rs"
    f.write_text(UGLY_RS, encoding="utf-8")
    assert (
        run_hook(
            monkeypatch, {"tool_name": "Edit", "tool_input": {"file_path": str(f)}}
        )
        == 0
    )
    assert capsys.readouterr().out == ""
    assert "let x = 1;" in f.read_text(encoding="utf-8")


@needs_rustfmt
def test_hook_reports_syntax_error_with_exit_2(proj, monkeypatch, capsys):
    f = proj / "src" / "main.rs"
    f.write_text("fn main( {\n", encoding="utf-8")
    assert run_hook(monkeypatch, {"tool_input": {"file_path": str(f)}}) == 2
    assert "rejected by formatter" in capsys.readouterr().err


def test_hook_ignores_garbage_input(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert cli.main(["hook", "post-edit"]) == 0
    assert run_hook(monkeypatch, {"tool_name": "Bash"}) == 0


def test_init_creates_files_and_merges_settings(proj):
    settings = proj / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps({"permissions": {"allow": ["Bash(ls)"]}}), encoding="utf-8"
    )

    changes = init.run(config.load(proj))
    assert "create AGENTS.md" in changes and "create CLAUDE.md" in changes
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["permissions"]["allow"] == ["Bash(ls)"]  # project's own rules survive
    assert data["permissions"]["deny"][:2] == [
        "Edit(/src/gen/**)",
        "Edit(/**/*.gen.ts)",
    ]
    [group] = data["hooks"]["PostToolUse"]
    assert group["hooks"] == [init.HOOK]
    assert (proj / "AGENTS.md").read_text(encoding="utf-8").startswith("# demo")

    again = init.run(config.load(proj))  # idempotent; the version tip is advice
    assert [c for c in again if not c.startswith("tip:")] == []


def test_init_dry_run_writes_nothing(proj):
    assert init.run(config.load(proj), dry_run=True)
    assert not (proj / "AGENTS.md").exists()
    assert not (proj / ".claude").exists()


def test_init_keeps_existing_agents_md(proj):
    (proj / "AGENTS.md").write_text("mine\n", encoding="utf-8")
    init.run(config.load(proj))
    assert (proj / "AGENTS.md").read_text(encoding="utf-8") == "mine\n"


def test_fmt_changed_formats_branch_files(repo, capsys):
    f = repo / "py" / "poly-lib" / "src" / "poly_lib" / "__init__.py"
    f.write_text(f.read_text(encoding="utf-8") + "x=[1,2]\n", encoding="utf-8")
    if not tools.usable("ruff"):
        pytest.skip("ruff not usable")
    assert cli.main(["-C", str(repo), "fmt", "--changed"]) == 0
    assert "x = [1, 2]" in f.read_text(encoding="utf-8")


@pytest.mark.skipif(not tools.usable("ruff"), reason="ruff not usable")
def test_hook_keeps_an_import_added_before_its_use(proj):
    f = proj / "mod.py"
    f.write_text("from pathlib import Path\n", encoding="utf-8")
    assert fmt.format_file(config.load(proj), f).status == "formatted"
    assert "from pathlib import Path" in f.read_text(encoding="utf-8")


def test_fmt_is_the_check_and_fmt_write_writes(tmp_path, monkeypatch):
    """`fmt = "... --check"` must not turn the edit hook into a rejection."""
    import sys

    from harness import config, fmt

    py = sys.executable.replace("\\", "/")
    check_only = f'{py} -c "import sys; sys.exit(1)"'  # a check that fails on any input
    (tmp_path / "harness.toml").write_text(
        f"[stacks.python.commands]\nfmt = '{check_only}'\n", encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "p"\n', encoding="utf-8"
    )
    f = tmp_path / "a.py"
    f.write_text("x=1\n", encoding="utf-8")
    calls = []
    cfg = config.load(tmp_path)
    monkeypatch.setattr(fmt.tools, "ruff", lambda root: ["ruff-builtin"])
    monkeypatch.setattr(fmt.proc, "run", lambda cmd, cwd, **kw: calls.append(cmd)
                        or subprocess.CompletedProcess(cmd, 0, "", ""))  # fmt: skip
    assert fmt.format_file(cfg, f).status == "formatted"
    assert calls[0][0] == "ruff-builtin"  # the check command is not used to write

    writer = f"{py} -c pass {{paths}} --write"
    (tmp_path / "harness.toml").write_text(
        f"[stacks.python.commands]\nfmt = '{check_only}'\nfmt_write = '{writer}'\n",
        encoding="utf-8",
    )
    calls.clear()
    fmt.format_file(config.load(tmp_path), f)
    assert calls[0][-2:] == [str(f.resolve()), "--write"]  # {paths} is the file


@pytest.mark.parametrize("os_name", ["nt", "posix"])
def test_node_bin_takes_the_cmd_shim_on_windows(tmp_path, monkeypatch, os_name):
    """pnpm on Windows writes `prettier` (a shell shim) and `prettier.cmd`; only
    the .cmd starts there."""
    (tmp_path / "harness.toml").write_text('[project]\nname = "d"\n', encoding="utf-8")
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    for name in ("prettier", "prettier.cmd"):
        (bin_dir / name).write_text("", encoding="utf-8")
    monkeypatch.setattr(fmt, "_WINDOWS", os_name == "nt")  # not os.name: global
    got = fmt._node_bin(config.load(tmp_path), tmp_path / "a.ts", "prettier")
    assert got == str(bin_dir / ("prettier.cmd" if os_name == "nt" else "prettier"))


def test_formatter_that_cannot_start_is_a_failed_file(tmp_path, monkeypatch):
    """OSError from the formatter is a `failed` result, not a traceback in ship."""
    (tmp_path / "harness.toml").write_text('[project]\nname = "d"\n', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "p"\n', encoding="utf-8"
    )
    f = tmp_path / "a.py"
    f.write_text("x=1\n", encoding="utf-8")

    def cannot_start(cmd, cwd, **kw):
        raise OSError(193, "%1 is not a valid Win32 application")

    monkeypatch.setattr(fmt.tools, "ruff", lambda root: ["ruff-shim"])
    monkeypatch.setattr(fmt.proc, "run", cannot_start)
    r = fmt.format_file(config.load(tmp_path), f)
    assert r.status == "failed"
    assert "ruff-shim" in r.detail and "Win32" in r.detail


def test_fmt_write_names_without_manifest_skips(tmp_path):
    """`{names}` needs stack detection; a manifest-less stack skips."""
    from harness import config, fmt

    (tmp_path / "harness.toml").write_text(
        "[stacks.rust]\nenabled = true\n"
        "[stacks.rust.commands]\nfmt_write = 'cargo fmt -p {names}'\n",
        encoding="utf-8",
    )
    f = tmp_path / "a.rs"
    f.write_text("fn main(){}\n", encoding="utf-8")
    assert fmt.format_file(config.load(tmp_path), f).status == "skipped"

"""an agent session cannot rewrite shared history or commit to the base branch."""

import re
import subprocess
from types import SimpleNamespace

import pytest
from conftest import out

from harness import config, fmt, init, ship


def _matches(rule: str, command: str) -> bool:
    """Claude Code's Bash rule matching (docs: permissions, wildcard patterns): `*` is any
    text; a trailing ` *` that is the rule's only wildcard also matches the bare command."""
    pattern = rule.removeprefix("Bash(").removesuffix(")")
    if pattern.endswith(" *") and pattern.count("*") == 1 and command == pattern[:-2]:
        return True
    regex = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(regex, command, re.DOTALL) is not None


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    """Plain `git commit` here, as the agent would run it; CI has no global identity."""
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "t")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "t@t")


RULES = init._git_rules("main")

DENIED = [
    "git push --force",
    "git push --force origin task/gh-1-x",
    "git push -f origin x",
    "git push origin x --force",
    "git push origin main",
    "git push -u origin main",
    "git push origin HEAD:main",
    "git reset --hard",
    "git reset --hard origin/main",
    "git branch -D task/gh-1-x",
    "git clean -fdx",
    "git clean -fd",
]
ALLOWED = [
    "git push --force-with-lease",
    "git push --force-with-lease origin task/gh-135-x",
    "git push origin task/gh-135-x --force-with-lease",
    "git push -u origin task/gh-135-x",
    "git rebase origin/main",
    "git commit --amend --no-edit",
    "git reset --soft HEAD~1",
    "git branch -d done-branch",
    "git clean -n",
]


@pytest.mark.parametrize("command", DENIED)
def test_destructive_git_is_denied(command):
    assert any(_matches(r, command) for r in RULES), command


@pytest.mark.parametrize("command", ALLOWED)
def test_task_branch_history_stays_rewritable(command):
    assert not any(_matches(r, command) for r in RULES), command


def test_rules_use_configured_base(repo):
    (repo / "harness.toml").write_text(
        '[project]\nname = "p"\nbase = "trunk"\n', encoding="utf-8"
    )
    settings: dict = {}
    init._merge_settings(settings, config.load(repo))
    deny = settings["permissions"]["deny"]
    assert "Bash(git push *origin trunk*)" in deny
    assert "Bash(git push *:trunk*)" in deny


def _commit(cwd, env_agent: bool, monkeypatch, *extra):
    if env_agent:
        monkeypatch.setenv("CLAUDECODE", "1")
    else:
        monkeypatch.delenv("CLAUDECODE", raising=False)
    (cwd / "f.txt").write_text(str(len(list(cwd.iterdir()))), encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=cwd, check=True)
    return subprocess.run(
        ["git", "commit", "-q", "-m", "x", *extra],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_hook_refuses_agent_commit_on_base(repo, monkeypatch):
    changes = init.run(config.load(repo))
    assert any("git hook pre-commit" in c for c in changes)
    out(repo, "switch", "-q", "main")

    r = _commit(repo, True, monkeypatch)
    assert r.returncode == 1
    assert "h task start <ID>" in r.stderr
    assert _commit(repo, False, monkeypatch).returncode == 0  # the user's terminal


def test_hook_lets_agent_commit_on_task_branch(repo, monkeypatch):
    init.run(config.load(repo))
    assert out(repo, "branch", "--show-current") == "work"
    assert _commit(repo, True, monkeypatch).returncode == 0
    assert _commit(repo, True, monkeypatch, "--amend", "--no-edit").returncode == 0
    # the epic publish commits on its own branch in a linked worktree, like this one
    wt = repo.parent / "pub"
    out(repo, "worktree", "add", "-q", "-b", "epic/x", str(wt), "main")
    assert _commit(wt, True, monkeypatch).returncode == 0


def test_ship_style_commit_skips_hook(repo, monkeypatch):
    init.run(config.load(repo))
    out(repo, "switch", "-q", "main")
    monkeypatch.setenv("CLAUDECODE", "1")
    target = repo / "harness.toml"

    def fake_format(cfg, path):
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        return SimpleNamespace(status="ok")

    monkeypatch.setattr(ship.fmt, "format_file", fake_format)
    assert ship._autoformat(config.load(repo), repo, [target.name])


def test_hook_install_is_idempotent_and_keeps_foreign_hook(repo):
    cfg = config.load(repo)
    init.run(cfg)
    again = init.run(cfg)
    assert not any("pre-commit" in c for c in again)

    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    tips = [c for c in init.run(cfg) if "pre-commit" in c]
    assert len(tips) == 1 and tips[0].startswith("tip:") and init.HOOK_MARK in tips[0]
    assert hook.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"

    hook.write_text("#!/bin/sh\necho mine\n" + init._pre_commit_body("main"),
                    encoding="utf-8")  # fmt: skip
    assert not any("pre-commit" in c for c in init.run(cfg))


def _deny(repo) -> list[str]:
    settings: dict = {}
    init._merge_settings(settings, config.load(repo))
    return settings["permissions"]["deny"]


USER_ONLY_DENIED = [
    "h task start gh-5 --steal",
    "h -C ../p task start gh-5 --steal",
    "h task release gh-5 --force",
    "h -C D:/p task release gh-5 --force",
    "h task drop gh-5 --reason x --discard",
    "h task drop --discard gh-5 --reason x",
    "h deps approve 12",
    "h -C ../p deps approve 12",
    "h ci reset gh-5",
    "h -C ../p review reset gh-5",
    "h -C ../p task confirm gh-5",
]
USER_ONLY_ALLOWED = [
    "h task start gh-5",
    "h task start gh-5 --resume",
    "h task release gh-5",
    "h task drop gh-5 --reason x",
    "h deps review 12",
    "h init",
    "h -C ../p init --dry-run",
    "h check",
]


@pytest.mark.parametrize("command", USER_ONLY_DENIED)
def test_user_only_h_commands_are_denied(repo, command):
    # taking another session's task, losing work, approving a dependency.
    assert any(_matches(r, command) for r in _deny(repo)), command


@pytest.mark.parametrize("command", USER_ONLY_ALLOWED)
def test_the_agent_keeps_its_own_h_commands(repo, command):
    assert not any(_matches(r, command) for r in _deny(repo)), command


def test_every_h_rule_has_its_dash_c_form(repo):
    plain = [
        r
        for r in _deny(repo)
        if r.startswith("Bash(h ") and not r.startswith("Bash(h * ")
    ]
    assert plain
    for rule in plain:
        assert rule.replace("Bash(h ", "Bash(h * ", 1) in _deny(repo), rule


# json.dumps puts each array element on its own line, a project's prettier does not.


def fake_formatter(monkeypatch, status="formatted", detail="prettier"):
    calls = []

    def format_file(cfg, file):
        calls.append(file)
        return fmt.Result(file.name, status, detail)

    monkeypatch.setattr(init.fmt, "format_file", format_file)
    return calls


def test_written_settings_go_through_the_project_formatter(repo, monkeypatch):
    calls = fake_formatter(monkeypatch)
    changes = init.run(config.load(repo))
    assert calls == [repo / ".claude" / "settings.json"]
    assert not any(
        "settings.json" in c and c.startswith(("tip", "warning")) for c in changes
    )


def test_settings_with_nothing_to_add_are_not_touched(repo, monkeypatch):
    calls = fake_formatter(monkeypatch)
    cfg = config.load(repo)
    init.run(cfg)
    sp = repo / ".claude" / "settings.json"
    before = sp.read_bytes()
    calls.clear()
    init.run(cfg)
    assert not calls and sp.read_bytes() == before


def test_dry_run_formats_nothing(repo, monkeypatch):
    calls = fake_formatter(monkeypatch)
    init.run(config.load(repo), dry_run=True)
    assert not calls


def test_formatter_not_installed_says_how_to_format_later(repo, monkeypatch):
    fake_formatter(monkeypatch, "skipped", "ts formatter not installed")
    changes = init.run(config.load(repo))
    assert any(
        c.startswith("tip:") and "h fmt .claude/settings.json" in c for c in changes
    )


def test_no_package_json_no_tip(repo, monkeypatch):
    fake_formatter(monkeypatch, "skipped", "ts formatter not installed")
    (repo / "package.json").unlink()
    changes = init.run(config.load(repo))
    assert not any("h fmt" in c for c in changes)


def test_generated_settings_get_no_tip(repo, monkeypatch):
    fake_formatter(monkeypatch, "skipped", "generated")
    changes = init.run(config.load(repo))
    assert not any("h fmt" in c for c in changes)


def test_rejected_settings_stay_written_and_warn(repo, monkeypatch):
    fake_formatter(monkeypatch, "failed", "SyntaxError")
    changes = init.run(config.load(repo))
    assert any(c.startswith("warning:") and "SyntaxError" in c for c in changes)
    assert (repo / ".claude" / "settings.json").is_file()


def test_fmt_write_of_the_project_formats_the_settings(repo, monkeypatch):
    """Through the real format_file: the project's own command wins (no mock of it)."""
    toml = repo / "harness.toml"
    command = '[stacks.ts]\n[stacks.ts.commands]\nfmt_write = "myfmt"'
    text = toml.read_text(encoding="utf-8").replace("[stacks.ts]", command, 1)
    toml.write_text(text, encoding="utf-8")
    ran = []

    def run(argv, cwd, **kw):
        ran.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(fmt.proc, "run", run)
    init.run(config.load(repo))
    assert any(a[0] == "myfmt" and a[-1].endswith("settings.json") for a in ran)


# a formatter pass makes h read harness.yml as hand-edited, and skill sync overwrites
# the rest, so the project's prettier must not look at the files h owns.

PRETTIER_IGNORE = ".prettierignore"


def init_in(path):
    return init.run(config.load(path))


@pytest.mark.parametrize(
    ("name", "text"),
    [
        (".prettierrc.json", "{}"),
        (".prettierrc", "{}"),
        ("prettier.config.mjs", "export default {}"),
        (PRETTIER_IGNORE, "dist/\n"),
        ("package.json", '{"devDependencies": {"prettier": "3"}}'),
        ("package.json", '{"dependencies": {"prettier": "3"}}'),
        ("package.json", '{"prettier": "@acme/style"}'),
    ],
)
def test_a_prettier_project_ignores_what_h_owns(tmp_path, name, text):
    (tmp_path / name).write_text(text, encoding="utf-8")
    changes = init_in(tmp_path)
    lines = (tmp_path / PRETTIER_IGNORE).read_text(encoding="utf-8").splitlines()
    assert set(init.H_OWNED) <= set(lines)
    assert any(c.startswith(f"{PRETTIER_IGNORE}: add") for c in changes)


@pytest.mark.parametrize("package", [None, '{"name": "x"}', "not json", "[]"])
def test_no_prettier_no_ignore_file(tmp_path, package):
    if package is not None:
        (tmp_path / "package.json").write_text(package, encoding="utf-8")
    init_in(tmp_path)
    assert not (tmp_path / PRETTIER_IGNORE).exists()


def test_an_existing_ignore_file_only_gains_what_is_missing(tmp_path):
    path = tmp_path / PRETTIER_IGNORE
    path.write_text(f"dist/\n# ours\n{init.CI_WORKFLOW}\n", encoding="utf-8")
    init_in(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[:3] == ["dist/", "# ours", init.CI_WORKFLOW]
    assert lines.count(init.CI_WORKFLOW) == 1
    assert set(init.H_OWNED) <= set(lines)


def test_a_second_init_leaves_the_ignore_file_alone(tmp_path):
    (tmp_path / ".prettierrc").write_text("{}", encoding="utf-8")
    init_in(tmp_path)
    before = (tmp_path / PRETTIER_IGNORE).read_bytes()
    changes = init_in(tmp_path)
    assert (tmp_path / PRETTIER_IGNORE).read_bytes() == before
    assert not any(PRETTIER_IGNORE in c for c in changes)


def test_dry_run_writes_no_ignore_file(tmp_path):
    (tmp_path / ".prettierrc").write_text("{}", encoding="utf-8")
    changes = init.run(config.load(tmp_path), dry_run=True)
    assert any(c.startswith(f"{PRETTIER_IGNORE}: add") for c in changes)
    assert not (tmp_path / PRETTIER_IGNORE).exists()

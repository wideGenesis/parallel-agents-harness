"""Project rules as tests: a rule that code can check lives here, not only in prose.

Each failure names the rule, where it is written down, and what to do instead.
"""

import argparse
import ast
import re
import sys
import tomllib
from pathlib import Path

from harness import affected, check, cli, config, stacks

HARNESS = Path(__file__).resolve().parents[1]
REPO = HARNESS.parent
SRC = HARNESS / "src" / "harness"

# Commands that still lack --json. The list may only shrink: adding --json to one of them
# fails test_json_exceptions_are_current until the entry is removed here.
NO_JSON = {
    "ship": "prints the PR url and next step; --json not yet added",
    "skills": "--json not yet added",
    "hook": "machine entry point: its stdout/exit code is the hook protocol",
    "board": "--json not yet added",
    "epic": "--json not yet added",
}

SHELL_SUFFIXES = {".sh", ".bash", ".ps1", ".psm1", ".cmd", ".bat"}


def _subcommands() -> dict[str, argparse.ArgumentParser]:
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return dict(sub.choices)


def _has_json(p: argparse.ArgumentParser) -> bool:
    return any("--json" in a.option_strings for a in p._actions)


def test_harness_imports_only_stdlib():
    allowed = set(sys.stdlib_module_names) | {"harness"}
    bad = []
    for f in sorted(SRC.rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                if name.split(".")[0] not in allowed:
                    bad.append(f"{f.relative_to(REPO).as_posix()}:{node.lineno} {name}")
    assert not bad, (
        "harness is stdlib only (ADR-0001, AGENTS.md): it is installed with "
        "`uv tool install` into any project and must not pull dependencies. "
        "Use the stdlib or shell out to the tool instead:\n  " + "\n  ".join(bad)
    )


def test_harness_declares_no_runtime_dependencies():
    meta = tomllib.loads((HARNESS / "pyproject.toml").read_text(encoding="utf-8"))
    assert meta["project"]["dependencies"] == [], (
        "harness is stdlib only (ADR-0001): runtime dependencies stay empty; "
        "dev tools go to [dependency-groups] dev"
    )


def test_every_command_has_json():
    missing = sorted(
        name
        for name, p in _subcommands().items()
        if not _has_json(p) and name not in NO_JSON
    )
    assert not missing, (
        "h output is for agents, --json always (AGENTS.md): add "
        f"`p.add_argument('--json', action='store_true')` to: {', '.join(missing)}"
    )


def test_json_exceptions_are_current():
    commands = _subcommands()
    stale = sorted(
        name for name in NO_JSON if name not in commands or _has_json(commands[name])
    )
    assert not stale, (
        f"remove from NO_JSON in tests/test_rules.py (they have --json or are gone): "
        f"{', '.join(stale)}"
    )


def test_no_shell_scripts_in_templates_or_skills():
    roots = [SRC / "templates", REPO / ".agents" / "skills"]
    bad = sorted(
        f.relative_to(REPO).as_posix()
        for root in roots
        if root.is_dir()
        for f in root.rglob("*")
        if f.is_file() and f.suffix.lower() in SHELL_SUFFIXES
    )
    assert not bad, (
        "logic lives in `h` (Python), not in shell (AGENTS.md): Git Bash rewrites path "
        "arguments (MSYS path conversion). Move it into an `h` command:\n  "
        + "\n  ".join(bad)
    )


def test_harness_change_runs_the_whole_suite_locally():
    # ADR-0003: Windows is checked only by the local `h check` that `ship` runs, so a change
    # under harness/ must select every harness test, not a subset.
    cfg = config.load(REPO)
    found = stacks.detect(cfg)
    impact = affected.compute(cfg, found, ["harness/src/harness/cli.py"])
    [step] = [
        s for s in check.plan(cfg, found, impact, ("test",)) if s.stack == "python"
    ]
    assert step.cmd is not None and step.cwd is not None, step.reason
    targets = step.cmd[step.cmd.index("-q") + 1 :]
    assert [(step.cwd / t).resolve() for t in targets] == [HARNESS], (
        "a harness/ change must run pytest on the whole harness package (ADR-0003); "
        f"got {targets}"
    )


def test_package_version_is_one_number():
    """projects pin `[harness] version`; pyproject and harness.__version__ agree,
    and this repository runs with the h it builds."""
    from harness import __version__, version

    meta = tomllib.loads((HARNESS / "pyproject.toml").read_text(encoding="utf-8"))
    assert meta["project"]["version"] == __version__, (
        "bump harness/pyproject.toml and harness/src/harness/__init__.py together"
    )
    for toml in (REPO / "harness.toml", REPO / "polygon" / "harness.toml"):
        assert version.mismatch(config.load(toml.parent).harness_version) is None, toml


def test_command_output_is_decoded_only_by_proc():
    """a stray non-UTF-8 byte from gh, git or a check must not crash h; text-mode
    subprocess calls go through proc.run / proc.TEXT (errors="replace")."""
    text_kw = {"text", "encoding", "errors", "universal_newlines"}
    bad = []
    for f in sorted(SRC.rglob("*.py")):
        if f.name == "proc.py":
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("run", "Popen", "check_output")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and text_kw & {k.arg for k in node.keywords}
            ):
                bad.append(f"{f.relative_to(REPO).as_posix()}:{node.lineno}")
    assert not bad, (
        "decode command output with proc.run or **proc.TEXT:\n  " + "\n  ".join(bad)
    )


USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)", re.MULTILINE)
PINNED = re.compile(
    r"^(?:[\w.-]+/[\w.-]+|\{repo\})(?:/[\w./-]+)?@(?:[0-9a-f]{40}|\{sha\})$"
)


def test_workflow_uses_are_pinned_or_self():
    """every action and reusable workflow is either this repository at the running
    commit (`$/`, GitHub's self-repository reference; `./` for the caller's checkout) or
    another repository at a full SHA (SUMMARY #13). `{repo}@{sha}` is filled in by `h init`."""
    files = [*(REPO / ".github").rglob("*.yml"),
             REPO / "harness/src/harness/templates/github/harness.yml"]  # fmt: skip
    bad = []
    for f in files:
        for ref in USES.findall(f.read_text(encoding="utf-8")):
            if not (ref.startswith(("$/", "./")) or PINNED.match(ref)):
                bad.append(f"{f.relative_to(REPO).as_posix()}: {ref}")
    assert not bad, "pin `uses:` by full SHA or use `$/`:\n  " + "\n  ".join(bad)


def test_check_workflow_installs_node_before_pnpm():
    """pnpm/action-setup runs the first npm on PATH — on a self-hosted runner the
    runner's own, broken since runner 2.338.0. Node must be set up first."""
    text = (REPO / ".github" / "workflows" / "check.yml").read_text(encoding="utf-8")
    assert text.index("actions/setup-node@") < text.index("pnpm/action-setup@")


def _steps(text: str) -> list[str]:
    """Each `- uses:` / `- name:` step of a workflow as text, up to the next step."""
    return re.split(r"\n\s*- (?=uses:|name:|run:)", text)


def test_ci_caches_are_written_only_by_main():
    """a PR restores main's caches and never saves its own — a branch
    cannot poison them. Every cache step says so; the combined actions/cache is not used."""
    files = [*(REPO / ".github").rglob("*.yml")]
    bad = []
    for f in files:
        for step in _steps(f.read_text(encoding="utf-8")):
            where = f"{f.relative_to(REPO).as_posix()}: {step.split(chr(10))[0]}"
            if (
                step.startswith("uses: astral-sh/setup-uv")
                and "save-cache:" not in step
            ):
                bad.append(where + " — no save-cache")
            if step.startswith("uses: Swatinem/rust-cache") and "save-if:" not in step:
                bad.append(where + " — no save-if")
            if re.match(r"uses: actions/cache@", step):
                bad.append(where + " — use actions/cache/restore + save on main")
            if step.startswith("uses: actions/setup-node") and "cache:" in step:
                bad.append(where + " — setup-node saves its cache on every run")
    assert not bad, "caches must be saved by main only:\n  " + "\n  ".join(bad)


def test_rust_cache_keeps_the_runner_toolchain():
    """rust-cache's default cache-bin deletes from ~/.cargo/bin everything present
    before it ran — rustup and cargo on a self-hosted runner, so the next job fails with
    `rustup: command not found`. Every rust-cache step turns it off."""
    bad = [
        f"{f.relative_to(REPO).as_posix()}: {step.split(chr(10))[0]}"
        for f in (REPO / ".github").rglob("*.yml")
        for step in _steps(f.read_text(encoding="utf-8"))
        if step.startswith("uses: Swatinem/rust-cache")
        and 'cache-bin: "false"' not in step
    ]
    assert not bad, 'rust-cache needs cache-bin: "false":\n  ' + "\n  ".join(bad)


def test_setup_h_installs_into_the_jobs_own_dirs():
    """uv's shared tool dirs keep state between jobs on a persistent runner — a
    leftover `h` there made `uv tool install` fail. Every `uv tool install` under .github/
    goes to runner.temp, which the runner empties per job."""
    bad = [
        f"{f.relative_to(REPO).as_posix()}: {step.split(chr(10))[0]}"
        for f in (REPO / ".github").rglob("*.yml")
        for step in _steps(f.read_text(encoding="utf-8"))
        if "uv tool install" in step
        and not all(
            f"{var}: ${{{{ runner.temp }}}}/" in step
            for var in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR")
        )
    ]
    assert not bad, (
        "uv tool install needs UV_TOOL_DIR/UV_TOOL_BIN_DIR under runner.temp:\n  "
        + ("\n  ".join(bad))
    )


RUN_KEY = re.compile(r"^(\s*)(?:-\s+)?run:\s*(.*)$")


def _run_scripts(text: str) -> list[tuple[int, str]]:
    """(line, script) of every step `run:`; `outputs: run:` and `defaults: run:` are not
    scripts."""
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        m = RUN_KEY.match(line)
        if not m:
            continue
        indent = len(m[1])
        parent = next((p.strip() for p in reversed(lines[:i])
                       if p.strip() and len(p) - len(p.lstrip()) < indent), "")  # fmt: skip
        if parent in ("outputs:", "defaults:"):
            continue
        body = [m[2]]
        for nxt in lines[i + 1 :]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                break
            body.append(nxt)
        out.append((i + 1, "\n".join(body)))
    return out


def test_workflows_follow_security_review():
    """Rules 3–7 of the `security-rules` skill on the workflows of this repo and
    the `harness.yml` that `h init` installs: no trigger that runs PR code with secrets,
    top-level `permissions`, checkout without a persisted token, and no `${{ }}` pasted
    into a `run:` script — values go through `env:`."""
    workflows = [*(REPO / ".github" / "workflows").glob("*.yml"),
                 REPO / "harness/src/harness/templates/github/harness.yml"]  # fmt: skip
    bad = []
    for f in [*workflows, *(REPO / ".github" / "actions").rglob("*.yml")]:
        text = f.read_text(encoding="utf-8")
        rel = f.relative_to(REPO).as_posix()
        if f in workflows:
            if re.search(
                r"^\s*(pull_request_target|workflow_run)\b", text, re.MULTILINE
            ):
                bad.append(f"{rel}: pull_request_target / workflow_run (rule 3)")
            if not re.search(r"^permissions:", text, re.MULTILINE):
                bad.append(f"{rel}: no top-level permissions (rule 4)")
        for step in _steps(text):
            if step.startswith("uses: actions/checkout@") and (
                "persist-credentials: false" not in step
            ):
                bad.append(
                    f"{rel}: checkout without persist-credentials: false (rule 6)"
                )
        for line, script in _run_scripts(text):
            if "${{" in script:
                bad.append(f"{rel}:{line}: ${{{{ }}}} inside run: — use env: (rule 7)")
    assert not bad, "workflow security (skill security-rules):\n  " + "\n  ".join(bad)


def test_run_scripts_skip_job_outputs():
    text = (
        "jobs:\n  a:\n    defaults:\n      run:\n        working-directory: ${{ d }}\n"
        "    outputs:\n      run: ${{ steps.x.outputs.run }}\n"
        "    steps:\n      - run: echo ${{ github.event.issue.title }}\n"
        "      - name: b\n        run: |\n          echo ok\n          echo ${{ x }}\n"
        "      - run: echo done\n"
    )
    found = _run_scripts(text)
    assert [n for n, _ in found] == [9, 11, 14]
    assert "${{" in found[1][1] and "${{" not in found[2][1]


# agent files are instructions that load into every context. Too long, a broken
# frontmatter or a dead link breaks them silently: Claude does not see a skill without
# `name`/`description`. Copies instead of symlinks and CLAUDE.md = `@AGENTS.md` are ours.
TEMPLATES = SRC / "templates"
LIMITS = {"AGENTS.md": 100, "SKILL.md": 150, "agent": 80}
FENCE = re.compile(r"^\s*(```|~~~)")
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")


def _agents_md(root: Path, templates: Path) -> list[Path]:
    return [p for p in (root / "AGENTS.md", templates / "AGENTS.md") if p.is_file()]


def _skills(root: Path, templates: Path) -> list[Path]:
    return sorted(
        [*(root / ".agents" / "skills").glob("*/SKILL.md"),
         *(templates / "skills").glob("*/SKILL.md")]
    )  # fmt: skip


def _subagents(root: Path, templates: Path) -> list[Path]:
    return sorted(
        [
            *(root / ".claude" / "agents").glob("*.md"),
            *(templates / "agents").glob("*.md"),
        ]
    )


def _prose(text: str, inline_code: bool = False) -> list[str]:
    """Lines outside fenced code blocks; inline code blanked unless `inline_code`
    (it holds no links, but is part of a heading's anchor)."""
    out, fenced = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            out.append("")
        else:
            out.append(line if inline_code else re.sub(r"`[^`]*`", "", line))
    return out


def _slug(heading: str) -> str:
    """GitHub's heading anchor: lowercase, punctuation dropped, spaces to dashes."""
    text = re.sub(r"`|\*\*|__", "", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


def _anchors(md: Path) -> set[str]:
    """Anchors of a markdown file; a repeated heading gets `-1`, `-2`… as on GitHub."""
    seen: dict[str, int] = {}
    anchors = set()
    for line in _prose(md.read_text(encoding="utf-8"), inline_code=True):
        if m := HEADING.match(line):
            slug = _slug(m.group(1))
            n = seen.get(slug, 0)
            seen[slug] = n + 1
            anchors.add(f"{slug}-{n}" if n else slug)
    return anchors


def _frontmatter(text: str) -> dict[str, str] | None:
    """Top-level `key: value` pairs of the leading `---` block (stdlib has no YAML);
    a folded `key: >` value is its indented lines."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    fields: dict[str, str] = {}
    key = None
    for line in lines[1:end]:
        if m := re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line):
            key, value = m.group(1), m.group(2).strip()
            fields[key] = "" if value in (">", "|", ">-", "|-") else value.strip("'\"")
        elif key and line.startswith((" ", "\t")):
            fields[key] = (fields[key] + " " + line.strip()).strip()
    return fields


def length_problems(root: Path, templates: Path) -> list[str]:
    files = [
        *((f, LIMITS["AGENTS.md"]) for f in _agents_md(root, templates)),
        *((f, LIMITS["SKILL.md"]) for f in _skills(root, templates)),
        *((f, LIMITS["agent"]) for f in _subagents(root, templates)),
    ]
    bad = []
    for f, limit in files:
        n = len(f.read_text(encoding="utf-8").splitlines())
        if n > limit:
            bad.append(f"{f.relative_to(root).as_posix()}: {n} lines > {limit}")
    return bad


def frontmatter_problems(root: Path, templates: Path) -> list[str]:
    bad = []
    for f in [*_skills(root, templates), *_subagents(root, templates)]:
        where = f.relative_to(root).as_posix()
        fields = _frontmatter(f.read_text(encoding="utf-8"))
        if fields is None:
            bad.append(f"{where}: no `---` frontmatter block at the top")
            continue
        bad += [f"{where}: empty or missing `{k}`" for k in ("name", "description")
                if not fields.get(k)]  # fmt: skip
        if f.name == "SKILL.md" and fields.get("name", f.parent.name) != f.parent.name:
            bad.append(
                f"{where}: name `{fields['name']}` != directory `{f.parent.name}`"
            )
    return bad


def link_problems(root: Path, templates: Path) -> list[str]:
    # The template AGENTS.md links into the project it is installed in, not into harness.
    files = [
        root / "AGENTS.md",
        *(f for s in _skills(root, templates) for f in sorted(s.parent.rglob("*.md"))),
        *_subagents(root, templates),
    ]
    bad = []
    for f in (f for f in files if f.is_file()):
        for line in _prose(f.read_text(encoding="utf-8")):
            for target in LINK.findall(line):
                if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE):
                    continue  # URL, mailto:
                path, _, anchor = target.partition("#")
                dest = (root / path.lstrip("/")) if path.startswith("/") else (
                    f.parent / path if path else f
                )  # fmt: skip
                where = f"{f.relative_to(root).as_posix()}: ({target})"
                if not dest.exists():
                    bad.append(f"{where} — no such file")
                elif anchor and dest.suffix == ".md" and anchor not in _anchors(dest):
                    bad.append(f"{where} — no heading #{anchor}")
    return bad


# `[check] heavy`, `[stacks.ts] evidence`, `[[check.generated]]`: a harness.toml section an
# agent file names. Not a Rust attribute (`#[allow]`, `#![deny]`), a link (`[x](…)`), a
# checkbox or a `[T2: …]` provenance mark (one letter, uppercase).
SECTION = re.compile(r"(?<![\w#!\]\[])\[\[?([a-z][a-z_-]+)(?:\.[\w<>.-]+)?\]\]?(?!\()")
# Brackets in agent files that are not harness.toml sections.
NOT_SECTIONS = {
    "severity": "the reviewer's output format, `[severity] path:line — text`",
    "id": "a JavaScript array in a security-rules code sample",
}


def section_problems(root: Path, templates: Path) -> list[str]:
    """every harness.toml section an agent file names is one h reads. The
    security-rules skill said `[worktree] include`; h reads `[worktrees]` and ignored it."""
    files = [
        *_agents_md(root, templates),
        *(f for s in _skills(root, templates) for f in sorted(s.parent.rglob("*.md"))),
        *_subagents(root, templates),
        *sorted((templates / "github").glob("*.yml")),
    ]
    bad = []
    for f in (f for f in files if f.is_file()):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for name in SECTION.findall(line):
                if name not in config.SECTIONS and name not in NOT_SECTIONS:
                    bad.append(f"{f.relative_to(root).as_posix()}:{n}: [{name}]")
    return bad


def claude_md_problems(root: Path) -> list[str]:
    f = root / "CLAUDE.md"
    if not f.is_file():
        return ["CLAUDE.md: missing, must be `@AGENTS.md`"]
    if f.read_text(encoding="utf-8").strip() != "@AGENTS.md":
        return [f"{f.relative_to(root).as_posix()}: must be exactly `@AGENTS.md`"]
    return []


def test_agent_files_fit_their_limits():
    bad = length_problems(REPO, TEMPLATES)
    assert not bad, (
        "agent files load into every context: AGENTS.md <= 100 lines, "
        "SKILL.md <= 150, a subagent <= 80. Move details into the skill's `references/` "
        "(not limited) and link to them:\n  " + "\n  ".join(bad)
    )


# Claude Code's own slash commands and bundled skills (2.1.x). A harness skill with one of
# these names makes `/<name>` ambiguous: the user or agent may reach the built-in, which
# writes no review verdict. Add a name here when Claude Code ships a new one.
CLAUDE_CODE_COMMANDS = {
    "add-dir", "agents", "bug", "claude-api", "clear", "code-review", "compact", "config",
    "context", "cost", "doctor", "exit", "export", "fewer-permission-prompts", "help",
    "hooks", "ide", "init", "install-github-app", "keybindings-help", "login", "logout",
    "loop", "mcp", "memory", "model", "output-style", "permissions", "plugin",
    "pr-comments", "release-notes", "resume", "review", "rewind", "run", "schedule",
    "security-review", "simplify", "skills", "status", "statusline", "terminal-setup",
    "todos", "ultrareview", "update-config", "upgrade", "usage", "vim",
}  # fmt: skip


def test_skill_names_are_not_claude_code_commands():
    """a harness skill never shares a name with a Claude Code command."""
    names = {p.name for p in (SRC / "templates" / "skills").iterdir() if p.is_dir()}
    clash = sorted(names & CLAUDE_CODE_COMMANDS)
    assert not clash, f"skills named like Claude Code commands: {clash} — rename them"


def test_skills_and_subagents_have_frontmatter():
    bad = frontmatter_problems(REPO, TEMPLATES)
    assert not bad, (
        "Claude does not see a skill or subagent without frontmatter `name` and "
        "`description`; a skill's name is its directory:\n  " + "\n  ".join(bad)
    )


def test_agent_file_links_resolve():
    bad = link_problems(REPO, TEMPLATES)
    assert not bad, (
        "a link in an agent file points nowhere: fix the path or the anchor "
        "(GitHub heading slug):\n  " + "\n  ".join(bad)
    )


def test_agent_files_name_only_sections_h_reads():
    bad = section_problems(REPO, TEMPLATES)
    assert not bad, (
        "an agent file names a harness.toml section h does not read, so an agent "
        "following it writes a setting that does nothing. Use a name from "
        "config.SECTIONS, or list a bracket that is not a section in NOT_SECTIONS:\n  "
        + "\n  ".join(bad)
    )


def test_claude_md_is_agents_md(tmp_path):
    from harness import init

    (tmp_path / "harness.toml").write_text(
        '[project]\nname = "demo"\n', encoding="utf-8"
    )
    init.run(config.load(tmp_path))
    bad = claude_md_problems(REPO) + claude_md_problems(tmp_path)
    assert not bad, (
        "CLAUDE.md is one line `@AGENTS.md`: rules live in AGENTS.md, "
        "read by every agent:\n  " + "\n  ".join(bad)
    )


def _agent_tree(root: Path) -> Path:
    """A minimal repository with one valid file of each kind; returns its templates dir."""
    skill = root / ".agents" / "skills" / "demo"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: >\n  Use when testing.\n---\n\n# Demo\n\n"
        "## Two words\n\n## The `h check` command\n\n"
        "See [ref](references/more.md#details) and [up](#two-words) "
        "and [cmd](#the-h-check-command).\n"
        "`[not a link](nowhere.md)` and [web](https://example.com/x.md).\n",
        encoding="utf-8",
    )
    (skill / "references" / "more.md").write_text(
        "# More\n\n## Details\n\n" + "line\n" * 400, encoding="utf-8"
    )
    (root / ".claude" / "agents").mkdir(parents=True)
    (root / ".claude" / "agents" / "bot.md").write_text(
        "---\nname: bot\ndescription: Finds things.\ntools: Read\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (root / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
    return root / "templates"


def test_agent_file_checks_pass_a_valid_tree(tmp_path):
    t = _agent_tree(tmp_path)
    assert length_problems(tmp_path, t) == []  # references/ are not limited
    assert frontmatter_problems(tmp_path, t) == []
    assert link_problems(tmp_path, t) == []
    assert section_problems(tmp_path, t) == []
    assert claude_md_problems(tmp_path) == []


def test_agent_file_checks_catch_breakage(tmp_path):
    t = _agent_tree(tmp_path)
    skill = tmp_path / ".agents" / "skills" / "demo" / "SKILL.md"
    agent = tmp_path / ".claude" / "agents" / "bot.md"

    (tmp_path / "AGENTS.md").write_text("x\n" * 101, encoding="utf-8")
    agent.write_text(
        "---\nname: bot\ndescription: d\n---\n" + "x\n" * 80, encoding="utf-8"
    )
    assert length_problems(tmp_path, t) == [
        "AGENTS.md: 101 lines > 100",
        ".claude/agents/bot.md: 84 lines > 80",
    ]

    agent.write_text("---\nname: bot\n---\n", encoding="utf-8")
    skill.write_text("---\nname: other\ndescription: d\n---\n", encoding="utf-8")
    assert frontmatter_problems(tmp_path, t) == [
        ".agents/skills/demo/SKILL.md: name `other` != directory `demo`",
        ".claude/agents/bot.md: empty or missing `description`",
    ]
    skill.write_text("# no frontmatter\n", encoding="utf-8")
    assert frontmatter_problems(tmp_path, t)[0].endswith(
        "no `---` frontmatter block at the top"
    )

    skill.write_text(
        "# S\n\n[a](references/gone.md) [b](references/more.md#nope) [c](#s)\n",
        encoding="utf-8",
    )
    assert link_problems(tmp_path, t) == [
        ".agents/skills/demo/SKILL.md: (references/gone.md) — no such file",
        ".agents/skills/demo/SKILL.md: (references/more.md#nope) — no heading #nope",
    ]

    skill.write_text(
        "Copy `.env` with `[worktree] include`; `[worktrees] include`, `[stacks.ts] x`,\n"
        "`[[check.generated]]`, `#[allow(dead_code)]`, `#![deny(x)]`, [link](a.md),\n"
        "- [x] done, `[T2: url]`, `[severity] path:line`\n",
        encoding="utf-8",
    )
    assert section_problems(tmp_path, t) == [
        ".agents/skills/demo/SKILL.md:1: [worktree]"
    ]

    (tmp_path / "CLAUDE.md").write_text("@AGENTS.md\nAlso do X.\n", encoding="utf-8")
    assert claude_md_problems(tmp_path) == ["CLAUDE.md: must be exactly `@AGENTS.md`"]
    (tmp_path / "CLAUDE.md").unlink()
    assert claude_md_problems(tmp_path) == ["CLAUDE.md: missing, must be `@AGENTS.md`"]


def test_heading_slugs_follow_github():
    assert _slug("Regeln für `h check`!") == "regeln-für-h-check"
    assert _slug("AC-B (business, Given/When/Then)") == "ac-b-business-givenwhenthen"


def test_push_runs_get_a_concurrency_group_per_commit():
    """one group for every push to main let the next push replace a pending run,
    so that commit was never checked or deployed. A push run's group holds its sha."""
    files = [*(REPO / ".github" / "workflows").glob("*.yml"),
             REPO / "harness/src/harness/templates/github/harness.yml"]  # fmt: skip
    bad = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        if "concurrency:" not in text or not re.search(
            r"^\s+push:", text, re.MULTILINE
        ):
            continue
        group = re.search(r"^\s+group:\s*(.+)$", text, re.MULTILINE)
        if not group or "github.sha" not in group.group(1):
            bad.append(f.relative_to(REPO).as_posix())
    assert not bad, "push runs share a concurrency group (use github.sha):\n  " + (
        "\n  ".join(bad)
    )

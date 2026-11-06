"""`h init` — install the context layer into a project (Stage 2).

Idempotent and non-destructive: existing AGENTS.md / CLAUDE.md are left alone, and
`.claude/settings.json` is merged — harness-owned entries are added once, everything the
project already has stays. The CI workflow calls the harness reusable workflow by
full SHA; an untouched harness.yml is re-pinned to this h, a workflow the project edited
or wrote is never written over. Rerun after editing `[generated]` in harness.toml.
"""

from __future__ import annotations

import json
import os
import re
from importlib import resources
from pathlib import Path

from . import (
    __version__,
    affected,
    cards,
    fmt,
    gh,
    git,
    installed,
    ratchet,
    skills,
    task,
)
from .config import CI_NODE_VERSION, CI_TIMEOUT_MINUTES, CONFIG_NAME, Config

HOOK = {"type": "command", "command": "h", "args": ["hook", "post-edit"]}
PRE_HOOK = {"type": "command", "command": "h", "args": ["hook", "pre-write"]}
EDIT_MATCHER = "Write|Edit|MultiEdit"
# Progress ledger: refreshed before compaction, printed back into context after it.
# SessionStart `startup` too: a new session opened in a task worktree after a crash.
LEDGER_HOOKS = (
    ("PreCompact", None, ["hook", "pre-compact"]),
    ("SessionStart", "startup|resume|compact", ["hook", "session-start"]),
)


# The repository whose reusable workflow the project's CI calls.
HARNESS_REPO = "wideGenesis/parallel-agents-harness"
CI_WORKFLOW = ".github/workflows/harness.yml"


def harness_sha() -> str | None:
    """The harness commit a new project's CI pins: origin/<base> of the checkout `h` was
    installed from (offline, and on origin by definition), else the remote's main."""
    if src := installed._source():
        root = installed._run(src[0], "rev-parse", "--show-toplevel")
        if root.returncode == 0:
            top = Path(root.stdout.strip())
            r = installed._run(top, "rev-parse", f"origin/{installed._base(top)}")
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
    r = gh.run(Path.cwd(), "api", f"repos/{HARNESS_REPO}/commits/main", "--jq", ".sha")
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


TEMPLATE_PATH = "harness/src/harness/templates/github/harness.yml"


def _template_at(sha: str) -> str | None:
    """harness.yml's template as of the harness commit `sha`, from the checkout h was
    installed from: what h init wrote back then. None when that history is not here."""
    if not (src := installed._source()):
        return None
    top = installed._run(src[0], "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return None
    r = installed._run(Path(top.stdout.strip()), "show", f"{sha}:{TEMPLATE_PATH}")
    return r.stdout.replace("\r\n", "\n") if r.returncode == 0 else None


def _render(
    cfg: Config,
    sha: str,
    version: str,
    ci: tuple[str, str] | None = None,
    template: str | None = None,
) -> str:
    """The template filled in; `ci` = (runs-on, with-block) as text, else from [ci]."""
    runs_on, with_ = ci or (_runs_on(cfg), _with(cfg))
    text = template if template is not None else _template("github/harness.yml")
    return (text.replace("{repo}", HARNESS_REPO)
            .replace("{sha}", sha).replace("{version}", version)
            .replace("{base}", cfg.base_branch)
            .replace("{runs_on}", runs_on).replace("{with}", with_))  # fmt: skip


PIN = re.compile(r"check\.yml@([0-9a-f]{40}) # harness (\S+)")
# What [ci] wrote into a file: the jobs' runs-on and the check job's `with:` block.
_RUNS_ON = re.compile(r"^    runs-on: (.*)$", re.MULTILINE)
_WITH = re.compile(r"\n    with:(?:\n      .*)+")


def _ci(cfg: Config, dry_run: bool) -> list[str]:
    """`.github/workflows/harness.yml`: created once; re-pinned to this h and redrawn with
    the current [ci] while the project has not edited it. A workflow the project
    wrote itself is never touched."""
    uses = f"{HARNESS_REPO}/.github/workflows/check.yml@"
    path = cfg.root / CI_WORKFLOW
    flows = path.parent
    own = sorted([*flows.glob("*.yml"), *flows.glob("*.yaml")])
    if path.exists() and uses in (text := path.read_text(encoding="utf-8")):
        return _repin(cfg, path, text.replace("\r\n", "\n"), dry_run)
    if any(uses in p.read_text(encoding="utf-8") for p in own):
        return []  # wired by hand in a workflow of the project's own
    if path.exists():
        return [(f"tip: {CI_WORKFLOW} exists and is kept; to run h check in CI add a job "
                f"`uses: {uses}<sha>` and one aggregate job for branch protection "
                "(`if: always()`, every `needs` result success or skipped)")]  # fmt: skip
    sha = harness_sha()
    if not sha:
        return [(f"tip: {CI_WORKFLOW} not created — harness commit unknown (no origin "
                "in h's source checkout, gh api failed); rerun `h init` online")]  # fmt: skip
    if not dry_run:
        flows.mkdir(parents=True, exist_ok=True)
        path.write_text(_render(cfg, sha, __version__), encoding="utf-8", newline="\n")
    out = [(f"create {CI_WORKFLOW} (harness {sha[:12]}); branch protection: require "
           "the check `required`")]  # fmt: skip
    if own:
        names = ", ".join(p.name for p in own)
        out.append(
            f"tip: kept your workflows ({names}); drop the checks h check repeats"
        )
    return out


def _repin(cfg: Config, path: Path, text: str, dry_run: bool) -> list[str]:
    m = PIN.search(text)
    sha = harness_sha()
    if m is None or not sha:
        return []
    old_sha, old_version = m.groups()
    # Untouched = exactly what h init wrote: the template with this file's own pin and its
    # own [ci] values. Jobs with different runs-on, or any other edit, render differently.
    found = _RUNS_ON.search(text)
    block = _WITH.search(text)
    old_ci = (found.group(1) if found else "", block.group(0) if block else "")
    then = _template_at(old_sha)  # the template that wrote it, if h's history has it
    untouched = text == _render(cfg, old_sha, old_version, old_ci, then)
    want = _render(cfg, sha, __version__)
    if text == want:
        return []
    if not untouched:
        if old_sha == sha:
            return []  # current pin; the project's own edits are its business
        line = f"uses: {HARNESS_REPO}/.github/workflows/check.yml@{sha} # harness {__version__}"
        return [(f"MODIFIED {CI_WORKFLOW}: edited by the project, pin {old_sha[:12]} kept "
                 f"— to run h {__version__} in CI, set\n    {line}")]  # fmt: skip
    if not dry_run:
        path.write_text(want, encoding="utf-8", newline="\n")
    runner = (
        "" if old_ci[0] == _runs_on(cfg) else f"; runs-on {old_ci[0]} → {_runs_on(cfg)}"
    )
    return [(f"update {CI_WORKFLOW} (harness {old_sha[:12]} {old_version} → "
             f"{sha[:12]} {__version__}{runner})")]  # fmt: skip


def _runs_on(cfg: Config) -> str:
    """[ci] runs-on as a YAML value; labels are plain words (config._LABEL)."""
    labels = cfg.ci_runs_on
    return labels[0] if len(labels) == 1 else "[" + ", ".join(labels) + "]"


def _with(cfg: Config) -> str:
    """The `with:` of the reusable check job: its runs-on input is a string, a
    label list goes as JSON. Nothing when everything is the default."""
    inputs = {}
    if cfg.ci_runs_on != ["ubuntu-latest"]:
        labels = cfg.ci_runs_on
        inputs["runs-on"] = labels[0] if len(labels) == 1 else f"'{json.dumps(labels)}'"
    if cfg.ci_postgres_image:
        inputs["postgres-image"] = cfg.ci_postgres_image
    if cfg.ci_setup:
        inputs["setup"] = cfg.ci_setup
    if cfg.ci_timeout_minutes != CI_TIMEOUT_MINUTES:
        inputs["timeout-minutes"] = cfg.ci_timeout_minutes
    if cfg.ci_node_version != CI_NODE_VERSION:
        inputs["node-version"] = (
            f"'{cfg.ci_node_version}'"  # quoted: 26 is a string input
        )
    if not cfg.ci_upload_logs:
        inputs["upload-logs"] = "false"
    if not inputs:
        return ""
    return "\n    with:" + "".join(f"\n      {k}: {v}" for k, v in inputs.items())


def _template(name: str) -> str:
    return (
        resources.files("harness")
        .joinpath("templates", name)
        .read_text(encoding="utf-8")
    )


# h commands that are the user's decision, refused to the agent at the tool level. A word
# inside an argument (a task title with "--force") is refused too: the user runs it then.
USER_ONLY = (
    "ci reset",  # lifting the CI round limit, like confirming business criteria
    "review reset",  # the review round limit too
    "task confirm",  # the tool refuses too
    "deps approve",  # a Dependabot update h would not approve itself
    "task*--steal",  # taking a task from another session
    "task*--force",  # release: dropping another session's claim
    "task*--discard",  # drop: losing unpushed work
)


def _merge_settings(settings: dict, cfg: Config) -> list[str]:
    changes: list[str] = []

    deny = settings.setdefault("permissions", {}).setdefault("deny", [])
    # Only the user confirms business criteria ([I-ready]); any gh call that mentions the
    # label — `issue edit --add-label`, `api …/labels` — is refused at the tool level.
    rules = [f"Edit(/{pat.lstrip('/')})" for pat in cfg.generated]
    rules.append(f"Bash(gh *{cards.AC_CONFIRMED}*)")
    # Only the reviewer subagent writes the verdict (PreToolUse hook); no shell detour.
    rules.append("Bash(*verdict.json*)")
    for cmd in USER_ONLY:  # each also as `h -C <dir> …`
        rules += [f"Bash(h {cmd}*)", f"Bash(h * {cmd}*)"]
    # The review check is set by h ship only, not by hand through the API.
    rules.append("Bash(gh api*statuses*)")
    rules += _git_rules(cfg.base_branch)
    for rule in rules:
        if rule not in deny:
            deny.append(rule)
            changes.append(f"deny {rule}")

    post = settings.setdefault("hooks", {}).setdefault("PostToolUse", [])
    present = any(
        h.get("command") == HOOK["command"] and h.get("args") == HOOK["args"]
        for group in post
        for h in group.get("hooks", [])
    )
    if not present:
        post.append({"matcher": EDIT_MATCHER, "hooks": [dict(HOOK)]})
        changes.append("hook PostToolUse → h hook post-edit")

    pre = settings["hooks"].setdefault("PreToolUse", [])
    if not any(
        h.get("args") == PRE_HOOK["args"] for g in pre for h in g.get("hooks", [])
    ):
        pre.append({"matcher": EDIT_MATCHER, "hooks": [dict(PRE_HOOK)]})
        changes.append("hook PreToolUse → h hook pre-write")

    for event, matcher, argv in LEDGER_HOOKS:
        groups = settings["hooks"].setdefault(event, [])
        if any(h.get("args") == argv for g in groups for h in g.get("hooks", [])):
            continue
        group: dict = {"hooks": [{"type": "command", "command": "h", "args": argv}]}
        if matcher:
            group = {"matcher": matcher, **group}
        groups.append(group)
        changes.append(f"hook {event} → h {' '.join(argv)}")
    return changes


def _git_rules(base: str) -> list[str]:
    """Shared history and the base branch are out of an agent's reach. A guard
    against accidents, like the rest: `bash -c` slips past. `--force-with-lease` matches
    none of these, so a task branch can still be rebased and re-pushed. h's own forced
    pushes (claims, epic publish) run as subprocesses, not through the Bash tool."""
    return [
        "Bash(git push --force *)",
        "Bash(git push -f *)",
        "Bash(git push * --force)",
        f"Bash(git push *origin {base}*)",
        f"Bash(git push *:{base}*)",
        "Bash(git reset --hard*)",
        "Bash(git branch -D*)",
        "Bash(git clean -f*)",
    ]


HOOK_MARK = "# harness (h init): an agent session does not commit to the base"


def _pre_commit_body(base: str) -> str:
    # Only inside a Claude Code session, the same signal as policy.user_only: the user's
    # own terminal is untouched. h's style commit uses --no-verify; the epic publish
    # commits on its own branch.
    return (
        f"{HOOK_MARK}\n"
        'if [ -n "$CLAUDECODE" ] && '
        f'[ "$(git symbolic-ref --short -q HEAD)" = "{base}" ]; then\n'
        f'  echo "h: no commits to {base} in an agent session; '
        'take a task: h task start <ID>" >&2\n'
        "  exit 1\n"
        "fi\n"
    )


def _pre_commit(cfg: Config, dry_run: bool) -> list[str]:
    r = git.run(cfg.root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if r.returncode != 0:
        return []  # not a git repo yet
    body = _pre_commit_body(cfg.base_branch)
    hooks_path = git.run(cfg.root, "config", "--get", "core.hooksPath").stdout.strip()
    if hooks_path:
        return [f"tip: core.hooksPath = {hooks_path}; add to its pre-commit:\n{body}"]
    hook = Path(r.stdout.strip()) / "hooks" / "pre-commit"
    ours = "#!/bin/sh\n" + body
    if hook.is_file():
        text = hook.read_text(encoding="utf-8", errors="replace")
        if text == ours:
            return []
        if not text.startswith("#!/bin/sh\n" + HOOK_MARK):
            if HOOK_MARK in text:
                return []  # the snippet was pasted into the project's own hook
            return [f"tip: {hook} exists, left as is; add to it:\n{body}"]
    if not dry_run:
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(ours, encoding="utf-8", newline="\n")
        os.chmod(hook, 0o755)
    return [f"create git hook pre-commit (no agent commits to {cfg.base_branch})"]


def github(cfg: Config, dry_run: bool = False) -> list[str]:
    """Labels on the GitHub repo — outward-facing, so only with `h init --github`."""
    changes = []
    for name, (color, description) in cards.LABELS.items():
        if dry_run:
            changes.append(f"label {name}")
        elif gh.ensure_label(cfg.root, name, color, description):
            changes.append(f"label {name} created")
    return changes


# `[review] paths`: workflows get the security checklist. Commented out while h
# ships no such skill — a path whose skill is missing makes `h review prepare` refuse.
REVIEW_PATHS = (
    '[review]\n# An extra reviewer checklist by path: { "<glob>" = "<skill>" }.\n'
)
REVIEW_PATHS_DEFAULT = 'paths = { ".github/workflows/**" = "security-rules" }\n'


def _review_paths_default() -> str:
    ok = skills._templates().joinpath("skills", "security-rules", "SKILL.md").is_file()
    return "\n" + REVIEW_PATHS + ("" if ok else "# ") + REVIEW_PATHS_DEFAULT


def _format_settings(cfg: Config, path: Path) -> list[str]:
    """json.dumps puts each array element on its own line; a project's prettier or biome
    collapses short arrays and its fmt step fails on our file. The file is valid
    and written either way, so a formatter's trouble is a line, not a failed init."""
    r = fmt.format_file(cfg, path)
    if r.status == "failed":
        return [f"warning: formatter rejected {r.file}, left as written:\n{r.detail}"]
    if (
        r.detail == "ts formatter not installed"
        and (cfg.root / "package.json").is_file()
    ):
        # The next `h init` has nothing to add, so it will not come back to this file.
        tip = ("tip: after `pnpm install` run `h fmt .claude/settings.json` — the fmt "
               "step of `h check` may reject it as written")  # fmt: skip
        return [tip]
    return []


def _add_lines(path: Path, wanted: list[str], dry_run: bool) -> list[str]:
    """The lines of an ignore file h needs, added once under its header."""
    have = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    missing = [line for line in wanted if line not in have]
    if not missing:
        return []
    if not dry_run:
        body = "\n".join(have).rstrip("\n")
        block = "\n".join(["# harness (h init)", *missing])
        path.write_text(
            (body + "\n\n" if body else "") + block + "\n",
            encoding="utf-8",
            newline="\n",
        )
    return [f"{path.name}: add " + ", ".join(missing)]


# Files h owns: a formatter pass makes `h init` read harness.yml as hand-edited, and skill
# sync overwrites the rest. Biome formats neither Markdown nor YAML.
PRETTIER_IGNORE = ".prettierignore"
H_OWNED = [
    CI_WORKFLOW,
    ".github/ISSUE_TEMPLATE/task.yml",
    ".agents/skills/",
    ".claude/skills/",
    ".claude/agents/",
]


def _uses_prettier(root: Path) -> bool:
    if any(
        any(root.glob(p))
        for p in (".prettierrc*", "prettier.config.*", PRETTIER_IGNORE)
    ):
        return True
    try:
        pj = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(pj, dict) and "prettier" in {
        **pj, **pj.get("dependencies", {}), **pj.get("devDependencies", {})
    }  # fmt: skip


def run(cfg: Config, dry_run: bool = False) -> list[str]:

    changes: list[str] = []

    # the h that set the project up is the oldest that may run it. An existing
    # harness.toml is never rewritten — tomllib cannot write it back with its comments.
    toml = cfg.root / CONFIG_NAME
    pin = f'version = ">={__version__}"'
    if not toml.exists():
        changes.append(f"create {CONFIG_NAME} ([harness] {pin})")
        if not dry_run:
            toml.write_text(f"[harness]\n# The oldest h this project runs with.\n{pin}\n"
                            + _review_paths_default(),
                            encoding="utf-8", newline="\n")  # fmt: skip
    elif not cfg.harness_version:
        changes.append(f"tip: add `[harness] {pin}` to {CONFIG_NAME} — an older h "
                       "then refuses instead of running old rules")  # fmt: skip

    form = cfg.root / ".github" / "ISSUE_TEMPLATE" / "task.yml"
    if not form.exists():
        changes.append("create .github/ISSUE_TEMPLATE/task.yml")
        if not dry_run:
            form.parent.mkdir(parents=True, exist_ok=True)
            form.write_text(
                _template("github/task.yml"), encoding="utf-8", newline="\n"
            )

    for glob, name in cfg.review_paths.items():
        if new := skills.RENAMED.get(name):
            changes.append(f'tip: {CONFIG_NAME} [review] paths "{glob}" = "{name}" — '
                           f'h renamed that skill to "{new}"; review prepare follows the '
                           "rename, update the name when you next edit the file")  # fmt: skip

    changes += _ci(cfg, dry_run)

    for name, body in (
        ("AGENTS.md", _template("AGENTS.md").replace("{name}", cfg.name)),
        ("CLAUDE.md", "@AGENTS.md\n"),
    ):
        path = cfg.root / name
        if not path.exists():
            changes.append(f"create {name}")
            if not dry_run:
                path.write_text(body, encoding="utf-8", newline="\n")

    sp = cfg.root / ".claude" / "settings.json"
    try:
        settings = json.loads(sp.read_text(encoding="utf-8")) if sp.is_file() else {}
    except json.JSONDecodeError as e:
        raise ValueError(f"{sp}: not valid JSON ({e}); fix it before `h init`") from e
    merged = _merge_settings(settings, cfg)
    if merged:
        changes += [f".claude/settings.json: {c}" for c in merged]
        if not dry_run:
            sp.parent.mkdir(exist_ok=True)
            sp.write_text(
                json.dumps(settings, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
            changes += _format_settings(cfg, sp)

    wanted = [
        f"/{cfg.worktree_dir.strip('/')}/",
        "/.artifacts/",
        task.TASK_FILE,
        task.ENV_FILE,
    ]
    changes += _add_lines(cfg.root / ".gitignore", wanted, dry_run)
    if _uses_prettier(cfg.root):
        changes += _add_lines(cfg.root / PRETTIER_IGNORE, H_OWNED, dry_run)

    # The first ratchet base is today's state: existing debt never blocks.
    if not (cfg.root / ratchet.BASE).exists():
        try:
            counts = ratchet.count(ratchet.scan(cfg))
        except affected.GitError:
            counts = None  # not a git repo yet: nothing tracked to count
        if counts is not None:
            changes.append(
                f"create {ratchet.BASE.as_posix()} ({sum(counts.values())} suppressions)"
            )
            if not dry_run:
                ratchet.write_base(cfg, counts)

    changes += _pre_commit(cfg, dry_run)
    changes += skills.install(cfg, dry_run)
    mirrored, conflicts = skills.sync(cfg, dry_run)
    changes += [f".claude/skills: {c}" for c in mirrored]
    changes += [f"CONFLICT {c}" for c in conflicts]
    return changes

"""Loads `harness.toml` and fills in defaults.

The file is optional: a project without it still gets autodetected stacks, so `h doctor`
works on day one of adoption.
"""

from __future__ import annotations

import difflib
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import git as gitrun
from . import version

CONFIG_NAME = "harness.toml"
# Every top-level section `load` reads; any other is a typo h would ignore in
# silence — `[worktree] include` copied nothing. `[stacks.ts]`, `[[check.generated]]`
# count by their first name. A test keeps this list equal to the reads below.
SECTIONS = (
    "adr", "board", "branches", "check", "ci", "deploy", "generated", "git", "harness",
    "limits", "policy", "project", "review", "ship", "stacks", "tasks", "worktrees",
)  # fmt: skip
STACK_NAMES = ("rust", "python", "ts")
CHECK_KINDS = (
    "fmt",
    "lint",
    "deadcode",
    "deps",
    "typecheck",
    "test",
    "sql",
    "generated",
)
COMMAND_KINDS = (
    "fmt",
    "fmt_write",
    "lint",
    "deadcode",
    "deps",
    "typecheck",
    "test",
    "sql",
)
REVIEW_MODELS = ("sonnet", "sonnet", "opus")
CI_TIMEOUT_MINUTES = 30  # the default of the check job in check.yml
CI_NODE_VERSION = "24"  # check.yml's default: a pinned major, not lts/*
STEP_TIMEOUT_MINUTES = 30  # slowest green step 21 min, job limit 45
# A PR touching only these skips CI's checks; `*` stays within a directory, so
# `*.md` is the root's README/AGENTS — skills and package docs still run the checks.
DOCS = ["docs/**", "*.md"]
# Documents the reviewer judges by document criteria: decisions and instructions
# to agents, where a wrong claim or a contradiction costs more than a typo.
REVIEW_DOCS = [
    "docs/adr/**",
    "AGENTS.md",
    "CLAUDE.md",
    ".claude/rules/**",
    ".agents/skills/**",
]


class ConfigError(Exception):
    pass


@dataclass
class StackConfig:
    name: str
    enabled: bool | None = None  # None = autodetect from manifest
    root: str = "."
    commands: dict[str, str] = field(default_factory=dict)  # kind -> override
    skip: list[str] = field(default_factory=list)  # check steps disabled on purpose
    global_files: list[str] = field(default_factory=list)  # extra stack-wide globs
    deadcode_whitelist: str = ""  # "" = .harness/deadcode/<stack>.txt
    # What a failed test step leaves behind, kept in the run dir; None = default
    evidence: list[str] | None = None


@dataclass
class Config:
    root: Path
    name: str
    base_branch: str = "main"
    worktree_dir: str = ".worktrees"
    worktree_include: list[str] = field(default_factory=list)
    # Commands run once in a freshly created task worktree.
    worktree_setup: list[str] = field(default_factory=list)
    # Names of ports to allocate per task (written to .env.harness as PORT_<NAME>).
    worktree_ports: list[str] = field(default_factory=list)
    task_branch: str = ""  # branch template with {id} {slug}; "" = task/{id}[-{slug}]
    # [branches] auto: the branches h makes itself — epic PRs, reverts; "" = their
    # own names (chore/epic-<slug>, revert/<sha>)
    auto_branch_template: str = ""
    card_path: str = ""  # glob with {id} for file-based task cards (external ids)
    # ADRs indexed in the brief and the review package; "" = off
    adr_dir: str = "docs/adr"
    claim_ttl_hours: float = 4.0  # a claim without heartbeat this long may be stolen
    ship_footer: str = ""  # appended to every PR body `h ship` writes
    ship_merge: str = "user"  # who merges a green PR: "user" | "agent"
    board_owner: str = ""  # GitHub Projects v2 board of this repo (`h board init`)
    board_number: int = 0
    deploy_workflow: str = (
        ""  # workflow file whose success on the merge commit = deployed
    )
    deploy_timeout_min: float = 30.0
    # A git call running longer is killed with its process tree.
    git_timeout_local_s: float = 60.0
    git_timeout_network_s: float = 300.0  # fetch / push / pull
    review_required: bool = True  # ship needs the reviewer's approval for HEAD
    review_delta_rounds: int = 2  # delta reviews after manual rebase fixes
    # The reviewer model of round 1, 2, 3…; the last one serves later rounds.
    review_models: list[str] = field(default_factory=lambda: list(REVIEW_MODELS))
    review_pr_comment: str = (
        ""  # template of the verdict comment in the PR ("" = default)
    )
    # [review] paths: {glob: skill} — an extra checklist the verdict must cover
    review_paths: dict[str, str] = field(default_factory=dict)
    # [review] docs: globs of documents reviewed by document criteria
    review_docs: list[str] = field(default_factory=lambda: list(REVIEW_DOCS))
    # [review] provenance: the project's provenance scheme for document review
    review_provenance: str = ""
    # Variables for check steps that beat .env and the process: where tests may connect.
    check_env: dict[str, str] = field(default_factory=dict)
    # Test files no check step runs on purpose: {glob: reason} (tether.py)
    untethered: dict[str, str] = field(default_factory=dict)
    # [[check.generated]]: committed files generated from other paths
    generators: list[Generated] = field(default_factory=list)
    # Paths a PR may change and still skip CI's checks (`h affected` docs_only)
    docs: list[str] = field(default_factory=lambda: list(DOCS))
    # Check steps a PR skips — "kind" or "stack.kind"; push to main and `--tier full` run
    # them
    heavy: list[str] = field(default_factory=list)
    # Check steps that must not share the machine — run last, holding every heavy slot
    # (timed e2e tests); same "kind" | "stack.kind" form as heavy
    exclusive: list[str] = field(default_factory=list)
    # [check] postgres: a throwaway Postgres per run from [ci] postgres-image
    check_postgres: bool = True
    # [check] step-timeout-minutes: a hung step fails by name, not the job
    step_timeout_minutes: float = STEP_TIMEOUT_MINUTES  # float: tests use seconds
    # [check.deadcode]: which deadcode findings fail the step — deps | code → block | report
    deadcode: dict[str, str] = field(
        default_factory=lambda: {"deps": "block", "code": "report"}
    )
    # [[policy]] rules: {paths: [glob], review?: bool, merge?: user|agent} (policy.py)
    policies: list[dict] = field(default_factory=list)
    rules_from: str = "worktree"  # "base" when gate rules were read from origin/<base>
    rules_changed: bool = False  # the branch edits gate rules; they apply after merge
    heavy_jobs: int = 0  # 0 = derive from CPU count
    # [ci]: what `h init` writes into harness.yml — the runner labels of every job
    # and the Postgres image CI starts for the tests ("" = none)
    ci_runs_on: list[str] = field(default_factory=lambda: ["ubuntu-latest"])
    ci_postgres_image: str = ""
    ci_setup: str = ""  # a project script CI runs before h check
    ci_timeout_minutes: int = CI_TIMEOUT_MINUTES  # the check job's limit
    ci_node_version: str = CI_NODE_VERSION  # Node of the CI check job
    ci_upload_logs: bool = True  # a failed check job uploads its step logs
    generated: list[str] = field(default_factory=list)  # globs agents must not edit
    stacks: dict[str, StackConfig] = field(default_factory=dict)
    sql: bool | None = None  # `[stacks] sql`: None = detect (stacks.has_sql)
    source: Path | None = None  # None = no harness.toml, all defaults
    harness_version: str = ""  # `[harness] version`, ">=X.Y[.Z]"; "" = any h

    def auto_branch(self, name: str, default: str) -> str:
        """The branch for an h-made PR: `name` through [branches] auto, else `default`."""
        t = self.auto_branch_template
        return t.replace("{name}", name) if t else default

    def heavy_job_limit(self) -> int:
        # The machine budget: its heavy steps at once, from ~/.harness/machine.toml.
        from . import machine

        return machine.budget(self.heavy_jobs).slots


def find_root(start: Path) -> Path:
    """Nearest ancestor with harness.toml, else the git top level, else `start`."""
    start = start.resolve()
    for d in (start, *start.parents):
        if (d / CONFIG_NAME).is_file():
            return d
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return start


def load(start: Path | None = None) -> Config:
    root = find_root(start or Path.cwd())
    path = root / CONFIG_NAME
    data: dict = {}
    if path.is_file():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}") from e

    project = data.get("project", {})
    worktrees = data.get("worktrees", {})
    limits = data.get("limits", {})
    cfg = Config(
        root=root,
        name=project.get("name", root.name),
        base_branch=project.get("base", "main"),
        worktree_dir=worktrees.get("dir", ".worktrees"),
        worktree_include=list(worktrees.get("include", [])),
        worktree_setup=list(worktrees.get("setup", [])),
        worktree_ports=list(worktrees.get("ports", [])),
        claim_ttl_hours=float(data.get("tasks", {}).get("claim_ttl_hours", 4.0)),
        task_branch=_template(
            data.get("tasks", {}).get("branch", ""), {"id", "slug"}, "tasks.branch"
        ),
        auto_branch_template=_auto_branch(data.get("branches", {}).get("auto", "")),
        card_path=_template(
            data.get("tasks", {}).get("card_path", ""), {"id"}, "tasks.card_path"
        ),
        adr_dir=_adr_dir(data.get("adr", {}).get("dir", "docs/adr"), root),
        ship_footer=str(data.get("ship", {}).get("footer", "")),
        ship_merge=_choice(
            data.get("ship", {}).get("merge", "user"), ("user", "agent"), "ship.merge"
        ),
        board_owner=str(data.get("board", {}).get("owner", "")),
        board_number=int(data.get("board", {}).get("number", 0)),
        deploy_workflow=str(data.get("deploy", {}).get("workflow", "")),
        deploy_timeout_min=float(data.get("deploy", {}).get("timeout_min", 30)),
        git_timeout_local_s=_positive(
            data.get("git", {}).get("timeout_local_s", 60.0), "git.timeout_local_s"
        ),
        git_timeout_network_s=_positive(
            data.get("git", {}).get("timeout_network_s", 300.0), "git.timeout_network_s"
        ),
        review_required=bool(data.get("review", {}).get("required", True)),
        review_delta_rounds=int(data.get("review", {}).get("delta_rounds", 2)),
        review_models=_review_models(data.get("review", {})),
        review_pr_comment=str(data.get("review", {}).get("pr_comment", "")),
        review_paths=_review_paths(data.get("review", {})),
        review_docs=_review_docs(data.get("review", {})),
        review_provenance=_review_provenance(data.get("review", {})),
        check_env={
            str(k): str(v) for k, v in data.get("check", {}).get("env", {}).items()
        },
        untethered=_untethered(data),
        generators=_generated(data),
        docs=_docs(data),
        heavy=_heavy(data),
        exclusive=_steps(data, "exclusive"),
        check_postgres=_flag(data.get("check", {}), "postgres", True, path),
        step_timeout_minutes=_minutes(
            data.get("check", {}), "step-timeout-minutes", STEP_TIMEOUT_MINUTES, path
        ),
        deadcode=_deadcode(data),
        heavy_jobs=int(limits.get("heavy_jobs", 0)),
        ci_runs_on=_runs_on(data.get("ci", {}), path),
        ci_postgres_image=_postgres_image(data.get("ci", {}), path),
        ci_setup=_setup(data.get("ci", {}), path),
        ci_timeout_minutes=_timeout_minutes(data.get("ci", {}), path),
        ci_node_version=_node_version(data.get("ci", {}), path),
        ci_upload_logs=_upload_logs(data.get("ci", {}), path),
        generated=list(data.get("generated", {}).get("paths", [])),
        source=path if path.is_file() else None,
        harness_version=_harness_version(data, path),
    )
    _unknown_sections(data, path, cfg.harness_version)
    gitrun.TIMEOUTS.update(
        local=cfg.git_timeout_local_s, network=cfg.git_timeout_network_s
    )
    cfg.policies = [_policy(r, i) for i, r in enumerate(data.get("policy", []))]
    _apply_base_rules(cfg, data)

    raw_stacks = data.get("stacks", {})
    # sql has no packages to check, only a language skill to install
    sql = raw_stacks.get("sql")
    if sql is not None and not isinstance(sql, bool):
        raise ConfigError(f"{path}: stacks.sql must be true or false")
    cfg.sql = sql
    unknown = set(raw_stacks) - set(STACK_NAMES) - {"sql"}
    if unknown:
        raise ConfigError(
            f"{path}: unknown stacks {sorted(unknown)}; known: {STACK_NAMES}"
        )
    for name in STACK_NAMES:
        raw = raw_stacks.get(name, {})
        commands = raw.get("commands", {})
        bad = set(commands) - set(COMMAND_KINDS)
        if bad:
            raise ConfigError(f"{path}: stacks.{name}.commands: unknown {sorted(bad)}")
        extra = raw.get("global_files", [])
        if not isinstance(extra, list) or not all(
            isinstance(g, str) and g for g in extra
        ):
            raise ConfigError(
                f"{path}: stacks.{name}.global_files must be a list of globs"
            )
        skip = raw.get("skip", [])
        if not isinstance(skip, list) or set(skip) - set(CHECK_KINDS):
            raise ConfigError(
                f"{path}: stacks.{name}.skip must list steps of {list(CHECK_KINDS)}"
            )
        whitelist = raw.get("deadcode_whitelist", "")
        if not isinstance(whitelist, str):
            raise ConfigError(
                f"{path}: stacks.{name}.deadcode_whitelist must be a path"
            )
        evidence = raw.get("evidence")
        if evidence is not None and (
            not isinstance(evidence, list)
            or not all(isinstance(e, str) and e for e in evidence)
            or any(e.startswith(("/", "\\")) or ":" in e
                   or ".." in e.replace("\\", "/").split("/") for e in evidence)
        ):  # fmt: skip
            raise ConfigError(
                f"{path}: stacks.{name}.evidence must list directories under the stack root"
            )
        cfg.stacks[name] = StackConfig(
            name=name,
            enabled=raw.get("enabled"),
            root=raw.get("root", "."),
            commands=dict(commands),
            skip=list(skip),
            global_files=list(extra),
            deadcode_whitelist=whitelist,
            evidence=None if evidence is None else list(evidence),
        )
    return cfg


GATE_KEYS = ("review", "policy", "ship.merge", "check.env", "check.untethered",
             "check.docs", "check.heavy", "check.deadcode")  # fmt: skip
# Editing the rules is itself the most sensitive change: always the user's review and merge.
BUILTIN_POLICY = {
    "paths": [CONFIG_NAME],
    "review": True,
    "merge": "user",
    "label": "built-in",
}


def _gate_view(data: dict) -> tuple:
    check = data.get("check", {})
    return (data.get("review", {}), data.get("policy", []),
            data.get("ship", {}).get("merge"), check.get("env", {}),
            check.get("untethered", {}), check.get("docs", DOCS),
            check.get("heavy", []), check.get("deadcode", {}),
            check.get("generated", []))  # fmt: skip


def _base_data(cfg: Config) -> dict | None:
    """harness.toml as committed on origin/<base> (or <base>); None without git or base."""
    import subprocess

    def git(*argv: str) -> subprocess.CompletedProcess:
        return gitrun.run(cfg.root, *argv)

    prefix = git("rev-parse", "--show-prefix")
    if prefix.returncode != 0:
        return None
    for ref in (f"origin/{cfg.base_branch}", cfg.base_branch):
        r = git("show", f"{ref}:{prefix.stdout.strip()}{CONFIG_NAME}")
        if r.returncode == 0:
            try:
                return tomllib.loads(r.stdout)
            except tomllib.TOMLDecodeError:
                return None
        if git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0:
            return {}  # base exists but has no harness.toml: defaults, not the branch's
    return None


def _review_models(review: dict) -> list[str]:
    models = review.get("models", list(REVIEW_MODELS))
    if (not isinstance(models, list) or not models
            or not all(isinstance(m, str) and m.strip() for m in models)):  # fmt: skip
        raise ConfigError("review.models must be a non-empty list of model names")
    return [m.strip().lower() for m in models]


SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")


def _review_paths(review: dict) -> dict[str, str]:
    """`[review] paths = { "<glob>" = "<skill>" }`. The name is a directory under
    .claude/skills, so only a plain name — no path into another folder."""
    raw = review.get("paths", {})
    example = 'review.paths: expected { ".github/workflows/**" = "security-rules" }'
    if not isinstance(raw, dict):
        raise ConfigError(example)
    for glob, skill in raw.items():
        if not glob or not isinstance(skill, str) or not SKILL_NAME.fullmatch(skill):
            raise ConfigError(f"{example}; {glob!r} = {skill!r} is not a skill name")
    return {str(k): str(v) for k, v in raw.items()}


def _review_docs(review: dict) -> list[str]:
    raw = review.get("docs", REVIEW_DOCS)
    if not isinstance(raw, list) or not all(isinstance(g, str) and g for g in raw):
        raise ConfigError('review.docs: expected a list of globs, e.g. ["docs/adr/**"]')
    return list(raw)


def _review_provenance(review: dict) -> str:
    raw = review.get("provenance", "")
    if not isinstance(raw, str):
        raw = "/"
    parts = raw.replace("\\", "/").split("/")
    if raw and (raw[:1] in "/\\" or ":" in raw or ".." in parts):
        raise ConfigError(
            'review.provenance: a path in the project, e.g. "docs/provenance.md"'
        )
    return raw


def _apply_base_rules(cfg: Config, local: dict) -> None:
    """Gate rules (review, policy, ship.merge, check.env) come from the base branch, so a
    task branch cannot relax them for itself (the gate runs from
    the base). A branch's edits to them apply after merge. Everything else — stacks,
    commands, ports — stays the branch's: changing it weakens no gate."""
    base = _base_data(cfg)
    if base is not None:
        cfg.rules_from = "base"
        cfg.rules_changed = _gate_view(base) != _gate_view(local)
        cfg.review_required = bool(base.get("review", {}).get("required", True))
        cfg.review_delta_rounds = int(base.get("review", {}).get("delta_rounds", 2))
        cfg.review_models = _review_models(base.get("review", {}))
        cfg.review_paths = _review_paths(base.get("review", {}))
        cfg.review_docs = _review_docs(base.get("review", {}))
        cfg.review_provenance = _review_provenance(base.get("review", {}))
        cfg.ship_merge = _choice(base.get("ship", {}).get("merge", "user"), ("user", "agent"),
                                 "ship.merge")  # fmt: skip
        cfg.check_env = {
            str(k): str(v) for k, v in base.get("check", {}).get("env", {}).items()
        }
        cfg.policies = [_policy(r, i) for i, r in enumerate(base.get("policy", []))]
        cfg.untethered = _untethered(base)
        cfg.generators = _generated(base)
        cfg.docs = _docs(base)
        cfg.heavy = _heavy(base)
        cfg.deadcode = _deadcode(base)
    cfg.policies = [dict(BUILTIN_POLICY), *cfg.policies]


def _docs(data: dict) -> list[str]:
    raw = data.get("check", {}).get("docs", DOCS)
    if not isinstance(raw, list) or not all(isinstance(g, str) for g in raw):
        raise ConfigError(
            'check.docs: expected a list of globs, e.g. ["docs/**", "*.md"]'
        )
    return list(raw)


def _heavy(data: dict) -> list[str]:
    return _steps(data, "heavy")


def _steps(data: dict, key: str) -> list[str]:
    """`[check] <key>`: check steps as "<kind>" or "<stack>.<kind>"."""
    raw = data.get("check", {}).get(key, [])
    ok = isinstance(raw, list) and all(isinstance(e, str) for e in raw)
    for entry in raw if ok else []:
        stack, _, kind = entry.rpartition(".")
        ok = ok and kind in CHECK_KINDS and stack in ("", *STACK_NAMES)
    if not ok:
        raise ConfigError(
            f'check.{key}: expected ["<kind>" or "<stack>.<kind>"], e.g. ["rust.test"]; '
            f"kinds {', '.join(CHECK_KINDS)}, stacks {', '.join(STACK_NAMES)}"
        )
    return list(raw)


# Both go verbatim into a generated workflow: a narrow alphabet keeps YAML and the shell
# step that runs the image free of quoting.
_LABEL = re.compile(r"[A-Za-z0-9._-]+")
# Not starting with "-": the image is the last argument of `docker run`.
_IMAGE = re.compile(r"[A-Za-z0-9._/:@][A-Za-z0-9._/:@-]*")
_SETUP = re.compile(r"[A-Za-z0-9._/-]+")


def _runs_on(ci: dict, path: Path) -> list[str]:
    raw = ci.get("runs-on", ["ubuntu-latest"])
    labels = [raw] if isinstance(raw, str) else raw
    if not (
        isinstance(labels, list)
        and labels
        and all(isinstance(x, str) and _LABEL.fullmatch(x) for x in labels)
    ):
        raise ConfigError(
            f"{path}: ci.runs-on: a runner label or a list of them, e.g. "
            f'["self-hosted", "linux"]; letters, digits, ".", "_", "-"'
        )
    return list(labels)


def _setup(ci: dict, path: Path) -> str:
    """A path, not a command: CI runs `bash <it>`, so no shell text travels from
    harness.toml into a workflow. Relative, inside the project, plain characters."""
    raw = ci.get("setup", "")
    if raw == "":
        return ""
    if (
        not isinstance(raw, str)
        or not _SETUP.fullmatch(raw)
        or raw.startswith(("/", "-"))
        or ".." in raw.split("/")
    ):
        raise ConfigError(
            f'{path}: ci.setup: a script path inside the project, e.g. "scripts/ci-setup.sh"'
        )
    return raw


def _flag(table: dict, key: str, default: bool, path: Path) -> bool:
    raw = table.get(key, default)
    if not isinstance(raw, bool):
        raise ConfigError(f"{path}: check.{key} must be true or false")
    return raw


def _postgres_image(ci: dict, path: Path) -> str:
    raw = ci.get("postgres-image", "")
    if not isinstance(raw, str) or (raw and not _IMAGE.fullmatch(raw)):
        raise ConfigError(
            f'{path}: ci.postgres-image: an image reference, e.g. "postgres:18.6"'
        )
    return raw


def _minutes(table: dict, key: str, default: int, path: Path) -> int:
    raw = table.get(key, default)
    if not isinstance(raw, int) or isinstance(raw, bool) or not 1 <= raw <= 1440:
        raise ConfigError(f"{path}: check.{key}: whole minutes from 1 to 1440, e.g. 30")
    return raw


_NODE = re.compile(r"[0-9A-Za-z.*/+-]{1,40}")


def _node_version(ci: dict, path: Path) -> str:
    raw = ci.get("node-version", CI_NODE_VERSION)
    if not isinstance(raw, str) or not _NODE.fullmatch(raw):
        raise ConfigError(
            f'{path}: ci.node-version: a Node version, e.g. "24" or "26.13"'
        )
    return raw


def _upload_logs(ci: dict, path: Path) -> bool:
    raw = ci.get("upload-logs", True)
    if not isinstance(raw, bool):
        raise ConfigError(f"{path}: ci.upload-logs must be true or false")
    return raw


def _timeout_minutes(ci: dict, path: Path) -> int:
    raw = ci.get("timeout-minutes", CI_TIMEOUT_MINUTES)
    if not isinstance(raw, int) or isinstance(raw, bool) or not 1 <= raw <= 1440:
        raise ConfigError(
            f"{path}: ci.timeout-minutes: whole minutes from 1 to 1440, e.g. 45"
        )
    return raw


def _deadcode(data: dict) -> dict[str, str]:
    """deps block and code only reports by default (agreed 2026-10-23): code turns blocking
    once the project has a baseline."""
    raw = data.get("check", {}).get("deadcode", {})
    modes = {"deps": "block", "code": "report"}
    if not isinstance(raw, dict) or set(raw) - set(modes):
        raise ConfigError(
            'check.deadcode: expected { deps = "block", code = "report" }'
        )
    for kind, mode in raw.items():
        modes[kind] = _choice(mode, ("block", "report"), f"check.deadcode.{kind}")
    return modes


@dataclass(frozen=True)
class Generated:
    """A committed file `command` writes from `inputs`, e.g. an API client from
    the Rust contract. h check regenerates it when an input or the file changes."""

    path: str
    command: str
    inputs: tuple[str, ...]


def _generated(data: dict) -> list[Generated]:
    raw = data.get("check", {}).get("generated", [])
    example = ('check.generated: expected [[check.generated]] path = "<file>", '
               'command = "<command>", inputs = ["<glob>", …]')  # fmt: skip
    if not isinstance(raw, list):
        raise ConfigError(example)
    out = []
    for i, e in enumerate(raw):
        ok = (isinstance(e, dict) and isinstance(e.get("path"), str) and e["path"]
              and isinstance(e.get("command"), str) and e["command"].strip()
              and isinstance(e.get("inputs"), list) and e["inputs"]
              and all(isinstance(g, str) and g for g in e["inputs"]))  # fmt: skip
        if not ok:
            raise ConfigError(f"{example} (entry {i + 1})")
        path = e["path"].replace("\\", "/")
        if path.startswith("/") or ":" in path or ".." in path.split("/"):
            raise ConfigError(f"check.generated: {path!r} is not a path in the project")
        out.append(Generated(path, e["command"], tuple(e["inputs"])))
    return out


def _untethered(data: dict) -> dict[str, str]:
    raw = data.get("check", {}).get("untethered", {})
    if not isinstance(raw, dict):
        raise ConfigError(
            'check.untethered: expected a table { "<glob>" = "<reason>" }'
        )
    for glob, reason in raw.items():
        if not isinstance(reason, str) or not reason.strip():
            raise ConfigError(
                f"check.untethered: {glob!r} needs a reason — why no step runs it"
            )
    return {str(k): str(v) for k, v in raw.items()}


def _policy(raw: object, i: int) -> dict:
    key = f"policy #{i + 1}"
    if not isinstance(raw, dict):
        raise ConfigError(f"{key}: expected a table ([[policy]])")
    unknown = set(raw) - {"paths", "review", "merge"}
    if unknown:
        raise ConfigError(f"{key}: unknown keys {sorted(unknown)}")
    paths = raw.get("paths")
    if (
        not isinstance(paths, list)
        or not paths
        or not all(isinstance(p, str) for p in paths)
    ):
        raise ConfigError(f"{key}: paths must be a non-empty list of globs")
    rule: dict = {"paths": paths, "label": f"policy #{i + 1}"}
    if "review" in raw:
        if not isinstance(raw["review"], bool):
            raise ConfigError(f"{key}: review must be true or false")
        rule["review"] = raw["review"]
    if "merge" in raw:
        rule["merge"] = _choice(raw["merge"], ("user", "agent"), f"{key}.merge")
    if len(rule) == 2:
        raise ConfigError(f"{key}: set review and/or merge")
    return rule


def _adr_dir(value: object, root: Path) -> str:
    """`[adr] dir` relative to the project and inside it: its files go into the brief and
    the review package, so a path outside would carry foreign text there."""
    text = str(value).strip().replace("\\", "/")
    if not text:
        return ""
    bad = PurePosixPath(text).is_absolute() or PureWindowsPath(text).drive
    if bad or ".." in PurePosixPath(text).parts:
        raise ConfigError(f"adr.dir = {text!r}: must be a path inside the project")
    try:
        (root / text).resolve().relative_to(root.resolve())
    except ValueError:  # a symlink pointing out of the project
        raise ConfigError(f"adr.dir = {text!r}: resolves outside the project") from None
    return text.strip("/")


def _unknown_sections(data: dict, path: Path, required: str) -> None:
    """A top-level section h does not read stops the command, with the closest known name.
    Not when the project requires a newer h: the section may be that h's, and the version
    gate says what to do (update h)."""
    unknown = sorted(set(data) - set(SECTIONS))
    if not unknown or version.mismatch(required) is not None:
        return
    lines = []
    for name in unknown:
        close = difflib.get_close_matches(name, SECTIONS, n=1)
        hint = f"; did you mean [{close[0]}]?" if close else ""
        lines.append(f"unknown section [{name}]{hint}")
    known = ", ".join(sorted(SECTIONS))
    raise ConfigError(f"{path}: " + "\n  ".join(lines) + f"\n  known: {known}")


def _harness_version(data: dict, path: Path) -> str:
    spec = data.get("harness", {}).get("version", "")
    if spec == "":
        return ""
    if not isinstance(spec, str):
        raise ConfigError(
            f'{path}: harness.version: expected ">=X.Y[.Z]", got {spec!r}'
        )
    try:
        version.parse_spec(spec)
    except ValueError as e:
        raise ConfigError(f"{path}: {e}") from e
    return spec


def _choice(value: object, allowed: tuple[str, ...], key: str) -> str:
    if value not in allowed:
        raise ConfigError(f"{key} = {value!r}: expected one of {allowed}")
    return str(value)


def _positive(value: object, key: str) -> float:
    try:
        n = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = 0.0
    if n <= 0:
        raise ConfigError(f"{key} = {value!r}: expected a positive number of seconds")
    return n


def _auto_branch(value: object) -> str:
    text = _template(value, {"name"}, "branches.auto")
    sample = text.replace("{name}", "epic-x")
    if text and (
        text.count("{name}") != 1
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", sample)
        or ".." in sample
        or sample.startswith(("/", "-"))
        or sample.endswith(("/", ".", ".lock"))
        or "//" in sample
    ):
        raise ConfigError(
            f"branches.auto = {text!r}: a branch name with one {{name}}, "
            'e.g. "nocard/{name}"'
        )
    return text


def _template(value: object, allowed: set[str], key: str) -> str:
    """A {name} template may use only `allowed` names — a typo would otherwise surface as a
    raw KeyError deep inside `h task start`."""
    text = str(value)
    names = {m.group(1) for m in re.finditer(r"\{([^{}]*)\}", text)}
    if names - allowed:
        raise ConfigError(
            f"{key} = {text!r}: unknown {sorted(names - allowed)}; use {sorted(allowed)}"
        )
    if key == "tasks.branch" and text and "{id}" not in text:
        raise ConfigError(f"{key} = {text!r}: must contain {{id}}")
    # Cleanup finds a task's branches by the part before {slug}; with {slug} first that
    # part would not identify the task and could match other branches.
    if (
        key == "tasks.branch"
        and "{slug}" in text
        and text.index("{slug}") < text.index("{id}")
    ):
        raise ConfigError(f"{key} = {text!r}: {{id}} must come before {{slug}}")
    return text


def expand_command(
    template: str, names: list[str], paths: list[str]
) -> tuple[list[str], bool]:
    """A [stacks.*.commands] template → argv: `{names}` → package names, `{paths}` (or
    `{file}`) → paths. Shared by h check and the formatter. The flag says whether
    any placeholder was there — without one, the caller appends its own arguments."""
    out: list[str] = []
    placed = False
    for tok in shlex.split(template):
        if tok == "{names}":
            out += names
        elif tok in ("{paths}", "{file}"):
            out += paths
        else:
            out.append(tok)
            continue
        placed = True
    return out, placed

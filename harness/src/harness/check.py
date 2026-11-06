"""`h check` — the one gate an agent runs before shipping (SUMMARY #5).

Two waves, as in prek: fast read-only checks (fmt, lint, deadcode, deps) for all stacks in
parallel, then the slow ones (typecheck, test). Both waves always run so one failure does
not hide another.
Output is for an agent: one line per step, and for a failure only the log tail; full logs
go to `.artifacts/check/`.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import sys
import threading
import time
import tomllib
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import (
    __version__,
    affected,
    collisions,
    deadcode,
    git,
    lock,
    machine,
    pg,
    phases,
    proc,
    ratchet,
    sqlx,
    task,
    tether,
    tools,
)
from .affected import StackImpact
from .config import STACK_NAMES as STACKS
from .config import Config, Generated, expand_command
from .phases import Phases
from .stacks import Package, Stack

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
FAST = ("fmt", "lint", "deadcode", "deps")
SLOW = ("typecheck", "test", "sql", "generated")
ARTIFACTS = Path(".artifacts") / "check"
LAST = ARTIFACTS / "last.json"
TAIL = 15

# Colour codes in a log cost tokens and break grep; every tool honours one of these.
QUIET_ENV = {
    "NO_COLOR": "1",
    "FORCE_COLOR": "0",
    "CARGO_TERM_COLOR": "never",
    "PY_COLORS": "0",
    # pip-installed pyright prints an "update available" banner on every run.
    "PYRIGHT_PYTHON_IGNORE_WARNINGS": "1",
}


@dataclass
class Step:
    stack: str
    kind: str
    cmd: list[str] | None  # None = skipped
    reason: str = ""  # why skipped
    ok_codes: tuple[int, ...] = (0,)
    hint: str = ""  # added to the tail of an exit-5 failure: what it means here
    # The stack root: cargo, uv and pnpm find their workspace from the working directory,
    # and it is not the project root when a stack lives in a subdirectory.
    cwd: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    note: str = ""  # shown with the result, e.g. why testmon selected everything
    testmon: bool = False  # the output says how many tests testmon selected
    disabled: bool = False  # skipped on purpose: [stacks.<s>] skip in harness.toml
    heavy: bool = False  # [check] heavy, deferred: runs after merge or with --tier full
    covered: bool = False  # nothing to run: another step does this job (rust typecheck)
    # Run in-process instead of cmd: (ok, log text, warning lines). For checks that only
    # read the file list, where a subprocess costs more than the check.
    call: Callable[[], tuple[bool, str, list[str]]] | None = None
    # Directories a failed run leaves its evidence in (traces, screenshots): copied into
    # the run directory on failure, before the next run deletes them.
    evidence: list[Path] = field(default_factory=list)
    # Run after cmd passed, into the same log and within the same time limit; the step
    # fails if it fails. nextest runs no doctests: `cargo test --doc` follows it.
    then: list[str] | None = None


DISABLED = "disabled in harness.toml"
HEAVY = "heavy — runs after merge, on label full-ci, or now with `h check --tier full`"
# heavy: only the [check] heavy steps — main reused the PR's check of its tree.
TIERS = ("pr", "full", "heavy")


def exclusive(cfg: Config, step: Step) -> bool:
    """[check] exclusive: the step runs after the others, alone on the machine."""
    return step.kind in cfg.exclusive or f"{step.stack}.{step.kind}" in cfg.exclusive


def deferred(cfg: Config, stack: str, kind: str) -> bool:
    """[check] heavy: the PR tier leaves this step to the post-merge run."""
    return kind in cfg.heavy or f"{stack}.{kind}" in cfg.heavy


@dataclass
class StepResult:
    step: Step
    status: str  # ok | fail | skip
    seconds: float = 0.0
    log: str = ""
    tail: str = ""
    waited: float = 0.0  # seconds spent queueing for a heavy-job slot
    started: float = 0.0  # seconds from the start of the check to this step's run
    note: str = ""
    warnings: list[str] = field(default_factory=list)  # ok, but worth the agent's eye
    phases: Phases | None = None  # rust test: build, tests, the rest

    @property
    def blocks_ship(self) -> bool:
        """A step that did not run is not a step that passed, unless the user
        disabled it in harness.toml or another step does its job."""
        step = self.step
        return self.status == "skip" and not (
            step.disabled or step.covered or step.heavy
        )

    @property
    def disable_line(self) -> str:
        return f'[stacks.{self.step.stack}] skip = ["{self.step.kind}"]'


def _rel(base: Path, p: Path) -> str:
    return p.resolve().relative_to(base.resolve()).as_posix() or "."


def _override(
    cfg: Config, st: Stack, kind: str, pkgs: list[Package]
) -> list[str] | None:
    tpl = cfg.stacks[st.name].commands.get(kind)
    if tpl is None:
        return None
    names = [p.name for p in pkgs]
    return expand_command(tpl, names, [_rel(st.root, p.path) for p in pkgs])[0]


NO_NEXTEST = ("cargo-nextest not installed — test binaries ran one at a time; "
              "`cargo install cargo-nextest --locked` runs them in one pool")  # fmt: skip


def has_lib(path: Path) -> bool:
    """The package has a library target — the only kind `cargo test --doc` accepts."""
    if (path / "src" / "lib.rs").is_file():
        return True
    try:
        manifest = tomllib.loads((path / "Cargo.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return "lib" in manifest


def _rust(kind: str, pkgs: list[Package], everything: bool) -> Step:
    sel = ["--workspace"] if everything else [a for p in pkgs for a in ("-p", p.name)]
    if kind == "fmt":
        return Step(
            "rust",
            kind,
            ["cargo", "fmt", "--check"] + (["--all"] if everything else sel),
        )
    if kind == "lint":
        return Step(
            "rust",
            kind,
            ["cargo", "clippy", *sel, "--all-targets", "--", "-D", "warnings"],
        )
    if kind == "test":
        # Both stop at the first failing test binary by default: the rest never run and
        # the summary shows one failure out of many.
        if tools.usable("cargo-nextest"):
            # nextest runs every test binary in one pool, but no doctests: they
            # follow, for the packages with a lib — a bin-only package is an error there.
            libs = [a for p in pkgs if has_lib(p.path) for a in ("-p", p.name)]
            # exit 4 = no tests to run in the selection — not a failure.
            return Step(
                "rust",
                kind,
                ["cargo", "nextest", "run", "--no-fail-fast", *sel],
                ok_codes=(0, 4),
                then=["cargo", "test", "--doc", "--no-fail-fast", *libs]
                if libs
                else None,
            )
        return Step("rust", kind, ["cargo", "test", "--no-fail-fast", *sel],
                    note=NO_NEXTEST)  # fmt: skip
    if kind == "deps":
        # The policy is repo-wide (deny.toml), so no package selection.
        if tools.usable("cargo-deny"):
            return Step("rust", kind, ["cargo", "deny", "check"])
        # A red step that says what to do, not cargo's "no such command: `deny`".
        why = "cargo-deny is not installed: cargo install --locked cargo-deny"
        return Step(
            "rust", kind, [sys.executable, "-c", f"import sys; sys.exit({why!r})"]
        )
    return Step("rust", kind, None, "rust typecheck is part of clippy", covered=True)


def _sql(cfg: Config, st: Stack, base: str | None) -> Step:
    """In-process: a throwaway database, migrations on two paths, the .sqlx/ cache.
    Without a base from the caller (ship, review) the step finds it as `h affected` does."""

    def call() -> tuple[bool, str, list[str]]:
        found = base
        if found is None:
            try:
                found = affected.resolve_base(cfg, None)
            except git.GitError:
                found = None  # no base branch: one path only
        return sqlx.run(cfg.root, st, step_env(cfg, st.name), found)

    return Step(st.name, "sql", None, call=call)


def _generated_step(cfg: Config, gen: Generated) -> Step:
    """Regenerate `gen.path` and fail when it differs from the file as it was when the check
    was planned; that file is put back either way, so the check leaves the tree as it found
    it. Planned, not run: a stack step of the waves may regenerate the file in
    place (a project's `pnpm typecheck`), and comparing with that would always pass."""
    target = cfg.root / gen.path
    before = target.read_bytes() if target.is_file() else None

    def call() -> tuple[bool, str, list[str]]:
        # POSIX quoting on every OS: quotes group an argument, they do not stay in it.
        argv = shlex.split(gen.command)
        exe = shutil.which(argv[0]) if argv else None
        if exe is None:
            return False, f"{gen.path}: command not found: {gen.command}\n", []
        try:
            r = proc.run([exe, *argv[1:]], cfg.root, env=step_env(cfg, "generated"))
            after = target.read_bytes() if target.is_file() else None
        finally:
            if before is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(before)
        log = f"$ {gen.command}\n{r.stdout}{r.stderr}"
        if r.returncode != 0:
            why = f"{gen.path}: the generator failed (exit {r.returncode})\n"
            return False, log + why, []
        if after != before:
            why = (f"{gen.path} is stale: `{gen.command}` writes a different file — "
                   "run it and commit the result\n")  # fmt: skip
            return False, log + why, []
        return True, log + f"{gen.path} is up to date\n", []

    return Step("generated", "generated", None, cwd=cfg.root, call=call, note=gen.path)


def _deny_policy(st: Stack) -> bool:
    """cargo deny runs where the project wrote its policy."""
    return st.name == "rust" and (st.root / "deny.toml").is_file()


# Test selection: a change in any of these can change what every test sees, so
# testmon still runs and refreshes its base, but selects nothing out.
TESTMON_RESET = (
    "conftest.py", "pyproject.toml", "uv.lock", "setup.py", "setup.cfg",
    "pytest.ini", "tox.ini", "requirements*.txt",
)  # fmt: skip
TESTMON_DIR = Path(".artifacts") / "testmon"


def _testmon(
    cfg: Config, st: Stack, imp: StackImpact
) -> tuple[list[str], dict[str, str], str]:
    """Flags, env and note for a selected pytest run. The base lives in .artifacts: never
    committed, gone with the worktree. Without one testmon runs everything and builds it."""
    data = cfg.root / TESTMON_DIR / f"{st.name}.testmondata"
    data.parent.mkdir(parents=True, exist_ok=True)
    env = {"TESTMON_DATAFILE": str(data)}
    reset = sorted(
        f for f in imp.files
        if any(fnmatch.fnmatchcase(Path(f).name, g) for g in TESTMON_RESET)
    )  # fmt: skip
    if imp.all or reset:
        why = imp.reason if imp.all else reset[0]
        return ["--testmon-noselect"], env, f"full run: {why} changed (testmon)"
    if not data.is_file():
        return ["--testmon"], env, "full run: no testmon base yet, building it"
    return ["--testmon"], env, ""


def _python(
    cfg: Config, st: Stack, kind: str, pkgs: list[Package], imp: StackImpact | None
) -> Step:
    paths = [_rel(st.root, p.path) for p in pkgs]
    if kind in ("fmt", "lint"):
        ruff = tools.ruff(st.root)
        if ruff is None:
            return Step("python", kind, None, "ruff not installed")
        if kind == "fmt":
            return Step("python", kind, [*ruff, "format", "--check", *paths])
        return Step("python", kind, [*ruff, "check", *paths])
    if kind == "typecheck":
        # The project's own checker first, then a global one. Under `uv run` a
        # global checker sees the project venv (otherwise it reports every workspace import
        # as missing), and a fresh worktree gets its venv synced first.
        args = {"pyright": paths, "ty": ["check", *paths]}
        for checker, argv in args.items():
            if (cmd := tools.project_tool(st.root, checker)) is not None:
                return Step("python", kind, [*cmd, *argv])
        for checker, argv in args.items():
            if tools.usable(checker):
                cmd = ["uv", "run", "--quiet", checker, *argv]
                return Step("python", kind, cmd)
        return Step("python", kind, None, "neither pyright nor ty installed")
    # exit 5 = no tests collected: fine for a package without tests, a failure when its
    # test files exist but pytest did not collect them.
    cmd = ["uv", "run", "--quiet", "pytest", "-q", *paths]
    if tether.has_tests(cfg, st, pkgs):
        hint = (
            "exit 5 = pytest collected no tests, but the package has test files — "
            "check python_files / testpaths / collect_ignore"
        )
        step = Step("python", kind, cmd, hint=hint)
    else:
        step = Step("python", kind, cmd, ok_codes=(0, 5))
    # Selection is for the agent's own loop only: --full, CI, review and ship pass imp=None
    # and run everything, so a test testmon misses is still caught before merge.
    if imp is not None and tools.pinned_in(
        st.root / "pyproject.toml", "pytest-testmon"
    ):
        flags, step.env, step.note = _testmon(cfg, st, imp)
        step.cmd = ["uv", "run", "--quiet", "pytest", "-q", *flags, *paths]
        step.testmon = True
        if not step.note:  # selecting: no base to build, no shared file changed
            # Exit 5 is then testmon finding nothing affected, not a package whose tests
            # never run: a full run (new base, --testmon-noselect) keeps that rule.
            step.ok_codes = (0, 5)
    return step


def _scripts(manifest: Path) -> dict:
    try:
        scripts = json.loads(manifest.read_text(encoding="utf-8")).get("scripts")
    except (OSError, ValueError, AttributeError):
        return {}
    return scripts if isinstance(scripts, dict) else {}


_RECURSE = re.compile(r"^pnpm\b.*\s(?:-r|--recursive|--filter)(?:\s|=|$)")


def _recursion(command: str) -> str:
    """How a root script relates to the members: "none", "only" — it just runs the script
    in the packages (`pnpm -r run test`), "mixed" — that plus work of its own."""
    parts = [p.strip() for p in re.split(r"&&|\|\||;", command) if p.strip()]
    recursive = [bool(_RECURSE.match(p)) for p in parts]
    if not any(recursive):
        return "none"
    return "only" if all(recursive) else "mixed"


def _ts(st: Stack, kind: str, pkgs: list[Package]) -> Step:
    # TS has no single toolchain, so the adapter runs the projects' own scripts by convention:
    # `format:check`, `lint`, `typecheck`, `test` — in each affected package that defines it
    # and in the root package.json, where biome/eslint usually live (`-r` skips the root).
    # Who has the script is read here, not left to `--if-present`: that reported a package
    # without the script as ok.
    script = {"fmt": "format:check"}.get(kind, kind)
    root = st.root.resolve()
    have = [
        p.name
        for p in pkgs
        if p.path.resolve() != root and script in _scripts(p.path / "package.json")
    ]
    root_cmd = _scripts(st.root / "package.json").get(script)
    in_root = root_cmd is not None
    # A root script that walks the packages would run each selected one a second time,
    # concurrently (two Playwright on one port): one that only walks adds nothing
    # to the filter; one that also does its own work runs alone, covering every package.
    recursion = _recursion(root_cmd) if isinstance(root_cmd, str) else "none"
    if recursion == "only":
        in_root = False
    elif recursion == "mixed":
        have = []
    if not have and not in_root:
        names = ", ".join(p.name for p in pkgs)
        where = "the root package.json" + (
            " (its script only runs the packages')" if recursion == "only" else ""
        )
        return Step("ts", kind, None, f"no `{script}` script in {names} or {where}")
    if not tools.usable("pnpm"):
        return Step("ts", kind, None, "pnpm not installed")
    sel = [a for n in have for a in ("--filter", n)]
    return Step(
        "ts",
        kind,
        ["pnpm", *sel, *(["--workspace-root"] if in_root else []), "run", script],
    )


def _deadcode(cfg: Config, st: Stack, pkgs: list[Package], everything: bool) -> Step:
    """In-process: the step parses the tools' output to apply the whitelist and the
    block/report mode per finding (deadcode.py)."""

    def call() -> tuple[bool, str, list[str]]:
        return deadcode.run(cfg, st, pkgs, everything, step_env(cfg, st.name))

    return Step(st.name, "deadcode", None, call=call)


def plan(
    cfg: Config,
    stacks: list[Stack],
    impact: dict[str, StackImpact] | None,
    kinds: tuple[str, ...],
    select_tests: bool = False,
    tier: str = "pr",
    base: str | None = None,
    files: list[str] | None = None,
) -> list[Step]:
    """impact=None means --full: every package of every stack. select_tests lets the python
    test step run only the tests testmon ties to the change (the local `h check` loop).
    tier "pr" defers [check] heavy steps to after the merge; "full" runs them."""
    steps: list[Step] = []
    for st in stacks:
        if impact is None:
            pkgs, everything = st.packages, True
        else:
            imp = impact[st.name]
            everything = imp.all
            names = imp.affected
            pkgs = [p for p in st.packages if p.name in names]
        if not pkgs:
            continue
        for kind in kinds:
            if kind == "generated":
                continue  # not a stack step: one per [[check.generated]] entry, below
            override = _override(cfg, st, kind, pkgs)
            if kind == "deps" and override is None and not _deny_policy(st):
                continue  # no policy, no step: nothing to report as skipped
            if kind == "sql" and override is None and not sqlx.planned(st):
                continue  # not a sqlx project with migrations or a query cache
            if kind in cfg.stacks[st.name].skip:
                step = Step(st.name, kind, None, DISABLED, disabled=True)
            elif tier == "pr" and deferred(cfg, st.name, kind):
                step = Step(st.name, kind, None, HEAVY, heavy=True)
            elif override is not None:
                step = Step(st.name, kind, override)
            elif kind == "deadcode":
                step = _deadcode(cfg, st, pkgs, everything)
            elif kind == "sql":
                step = _sql(cfg, st, base)
            elif st.name == "rust":
                step = _rust(kind, pkgs, everything)
            elif st.name == "python":
                select = (
                    impact[st.name] if impact is not None and select_tests else None
                )
                step = _python(cfg, st, kind, pkgs, select)
            else:
                step = _ts(st, kind, pkgs)
            step.cwd = st.root
            if kind == "test":
                step.evidence = _evidence_dirs(cfg, st, pkgs)
            steps.append(step)
    # A committed file generated from other paths: `files` None means --full.
    if "generated" in kinds:
        for gen in cfg.generators:
            touched = files is None or any(
                affected.glob_match(f, g)
                for f in files
                for g in (gen.path, *gen.inputs)
            )
            if not touched:
                continue
            if tier == "pr" and deferred(cfg, "generated", "generated"):
                steps.append(Step("generated", "generated", None, HEAVY, heavy=True))
                continue
            steps.append(_generated_step(cfg, gen))
    if tier == "heavy":
        # The PR checked this tree green; only what its tier left for the merge runs.
        return [s for s in steps if deferred(cfg, s.stack, s.kind)]
    # A test file no step runs is found from the file list alone, so it is cheap; it runs
    # with any stack step, not on a change that affects nothing.
    if "lint" in kinds and steps:
        cmd = [sys.executable, "-m", "harness.cli", "-C", str(cfg.root), "tether"]
        steps.append(Step("tether", "lint", cmd, cwd=cfg.root))
    # Suppression counts are repo-wide, so the ratchet runs whatever is affected.
    if "lint" in kinds and (cfg.root / ratchet.BASE).is_file():
        cmd = [sys.executable, "-m", "harness.cli", "-C", str(cfg.root), "ratchet"]
        steps.append(Step("ratchet", "lint", cmd, cwd=cfg.root))
    # Repo-wide and stack-free like the ratchet: a docs-only change can collide.
    if "lint" in kinds:

        def case() -> tuple[bool, str, list[str]]:
            return collisions.report(collisions.scan(cfg))

        steps.append(Step(collisions.STEP, "lint", None, cwd=cfg.root, call=case))
    return steps


KEEP_RUNS = 10  # run directories kept under .artifacts/check


def new_run_dir(cfg: Config) -> Path:
    """A directory of its own per `h check` run: two runs in one checkout (the edit hook
    and ship, two agent commands) must not overwrite each other's logs. `latest`
    names the newest — a file, since symlinks need privileges on Windows. Old runs beyond
    KEEP_RUNS are removed."""
    root = cfg.root / ARTIFACTS
    root.mkdir(parents=True, exist_ok=True)
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    run_dir = root / name
    run_dir.mkdir()
    (root / "latest").write_text(_rel(cfg.root, run_dir) + "\n", encoding="utf-8")
    runs = sorted(
        d for d in root.iterdir() if d.is_dir() and d != run_dir
    )  # by start time
    for old in runs[: max(0, len(runs) - (KEEP_RUNS - 1))]:  # never this run
        shutil.rmtree(old, ignore_errors=True)
    return run_dir


def _call(cfg: Config, step: Step, log: Path) -> StepResult:
    assert step.call is not None
    start = time.monotonic()
    try:
        ok, output, warnings = step.call()
    except git.GitError as e:
        ok, output, warnings = False, f"{e}\n", []
    seconds = time.monotonic() - start
    log.write_text(output, encoding="utf-8")
    rel_log = _rel(cfg.root, log)
    if ok:
        note = f"{len(warnings)} warning(s)" if warnings else ""
        return StepResult(step, "ok", seconds, rel_log, note=note, warnings=warnings)
    tail = "\n".join(focus_tail([ln for ln in output.splitlines() if ln.strip()]))
    return StepResult(step, "fail", seconds, rel_log, tail, warnings=warnings)


def _run(cfg: Config, step: Step, run_dir: Path | None = None) -> StepResult:
    """`started` is the monotonic clock here, after any slot wait; `run` makes it
    relative to the start of the check."""
    started = time.monotonic()
    _PROGRESS.begin(step, "")
    try:
        result = _run_step(cfg, step, run_dir)
    finally:
        _PROGRESS.end(step)
    result.started = started
    return result


TIMED_OUT = -9  # exit code of a step killed at [check] step-timeout-minutes
DB_DEAD = -10  # killed because the tests' Postgres stopped answering
DB_KINDS = ("test", "sql")
# The probe of this run's database, set by `run` while its steps go.
_WATCH: pg.Watch | None = None
PROGRESS_S = 300.0  # how often a long check says what it is doing


class _Progress:
    """What the steps of this run are doing: a job cancelled at its limit leaves a CI log
    that names the step that hung, and a wait says who holds the slots."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._now: dict[
            str, tuple[str, float]
        ] = {}  # step → (what it waits for, since)

    def begin(self, step: Step, waits_for: str) -> None:
        with self._lock:
            self._now[f"{step.stack} {step.kind}"] = (waits_for, time.monotonic())

    def end(self, step: Step) -> None:
        with self._lock:
            self._now.pop(f"{step.stack} {step.kind}", None)

    def running(self, kinds: tuple[str, ...]) -> str:
        """The running (not waiting) steps of these kinds, e.g. "rust test"."""
        with self._lock:
            names = [k for k, (w, _) in self._now.items()
                     if not w and k.rsplit(" ", 1)[-1] in kinds]  # fmt: skip
        return ", ".join(sorted(names))

    def line(self) -> str | None:
        now = time.monotonic()
        with self._lock:
            items = sorted(self._now.items(), key=lambda kv: kv[1][1])
        if not items:
            return None
        running = [f"{k} {_mins(now - t)}" for k, (w, t) in items if not w]
        waiting = [f"{k} for {w} {_mins(now - t)}" for k, (w, t) in items if w]
        parts = []
        if running:
            parts.append("running: " + ", ".join(running))
        if waiting:
            load = machine.load()
            parts.append("waiting: " + ", ".join(waiting)
                         + (f" (load {load:.0%})" if load is not None else ""))  # fmt: skip
        return "h check: " + "; ".join(parts)


def _mins(seconds: float) -> str:
    return f"{int(seconds // 60)}m"


_PROGRESS = _Progress()


@contextmanager
def _reporting(every: float) -> Iterator[None]:
    """A line on stderr every `every` seconds while the steps run."""
    stop = threading.Event()

    def report() -> None:
        while not stop.wait(every):
            if line := _PROGRESS.line():
                print(line, file=sys.stderr, flush=True)

    t = threading.Thread(target=report, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join()


def _run_step(cfg: Config, step: Step, run_dir: Path | None = None) -> StepResult:
    # The tests' database died: its steps do not start, the running ones stop.
    watch = _WATCH if step.kind in DB_KINDS and (step.cmd or step.call) else None
    if watch is not None and watch.dead.is_set():
        return StepResult(step, "fail", 0.0, "", f"{watch.cause} — not started")
    if step.call is not None:
        # deadcode keeps its old log name; other in-process stack steps add their kind.
        keep = step.stack in STACKS and step.kind == "deadcode"
        name = step.stack if keep else f"{step.stack}-{step.kind}"
        r = _call(cfg, step, (run_dir or new_run_dir(cfg)) / f"{name}.log")
        if watch is not None and watch.dead.is_set() and r.status == "fail":
            r.tail = f"{watch.cause}\n{r.tail}"
        return r
    if step.cmd is None:
        return StepResult(step, "skip")
    stop = watch.dead if watch is not None else None
    log_dir = run_dir or new_run_dir(cfg)
    log = log_dir / f"{step.stack}-{step.kind}.log"
    env = {**step_env(cfg, step.stack), **step.env}
    header = f"$ {shlex.join(step.cmd)}\n"
    limit = cfg.step_timeout_minutes
    start = time.monotonic()
    try:
        # One stream in order (noise cannot bury the cause), into the log as it comes: a
        # cancelled job keeps what the step printed.
        code, output = proc.run_logged(step.cmd, step.cwd or cfg.root, log, header,
                                       env=env, timeout_s=limit * 60, stop=stop)  # fmt: skip
    except FileNotFoundError as e:
        output, code = f"{e}\n", 127
    main = output  # the doctests of `then` count as the rest of rust test
    if code in step.ok_codes and step.then:
        # the second command's output follows the first's in the same log
        first = f"{header}{output}$ {shlex.join(step.then)}\n"
        left = max(1.0, limit * 60 - (time.monotonic() - start))
        try:
            code, more = proc.run_logged(step.then, step.cwd or cfg.root, log, first,
                                         env=env, timeout_s=left, stop=stop)  # fmt: skip
        except FileNotFoundError as e:
            more, code = f"{e}\n", 127
        output = f"{output}$ {shlex.join(step.then)}\n{more}"
        if code is not None and code != 0:
            code = code if code not in step.ok_codes else 1  # the doctests failed
    stopped = code is None and watch is not None and watch.dead.is_set()
    if stopped:
        assert watch is not None
        output += f"h check: {watch.cause} — killed with its child processes\n"
        code = DB_DEAD
    elif code is None:
        output += (f"h check: timed out after {limit} min — killed with its child "
                   "processes ([check] step-timeout-minutes)\n")  # fmt: skip
        code = TIMED_OUT
    seconds = time.monotonic() - start
    log.write_text(header + ANSI.sub("", output), encoding="utf-8")
    rel_log = _rel(cfg.root, log)
    note = step.note
    if step.testmon and not note:
        note = selected(output, known_tests(step.env.get("TESTMON_DATAFILE", "")))
    split = None
    if step.stack == "rust" and step.kind == "test":
        split = phases.rust_test(ANSI.sub("", main), seconds)
    if code in step.ok_codes:
        return StepResult(step, "ok", seconds, rel_log, note=note, phases=split)
    if stopped:
        # one cause, not the hundreds of connection failures in the log
        assert watch is not None
        return StepResult(step, "fail", seconds, rel_log, f"{watch.cause} — stopped",
                          note=note, phases=split)  # fmt: skip
    base = step.cwd or cfg.root
    lines = [_shorten(base, ln) for ln in output.splitlines() if ln.strip()]
    if step.hint and code == 5:
        lines.append(step.hint)
    tail = "\n".join(focus_tail(lines))
    if kept := keep_evidence(cfg, step, log_dir):
        tail += f"\n{kept}"
    return StepResult(step, "fail", seconds, rel_log, tail, note=note, phases=split)


EVIDENCE_LIMIT = 200 * 1024 * 1024  # bytes copied per failed step


def _evidence_dirs(cfg: Config, st: Stack, pkgs: list[Package]) -> list[Path]:
    """`[stacks.<s>] evidence` under the stack root; by default Playwright's
    `test-results/` of each ts package the step covered."""
    named = cfg.stacks[st.name].evidence
    if named is not None:
        return [st.root / e for e in named]
    if st.name == "ts":
        found = {p.path / "test-results" for p in pkgs} | {st.root / "test-results"}
        return sorted(found)
    return []


def keep_evidence(cfg: Config, step: Step, run_dir: Path) -> str:
    """Copy what the failed step left in its evidence directories into the run directory,
    up to EVIDENCE_LIMIT; the line for the failure output, "" when there was nothing."""
    dest = run_dir / f"{step.stack}-{step.kind}"
    base = (step.cwd or cfg.root).resolve()
    copied, skipped = 0, 0
    for src in step.evidence:
        if not src.is_dir() or src.is_symlink():
            continue
        # Not through symlinks: a link could pull a file from outside the project in.
        for f in sorted(
            p for p in src.rglob("*") if p.is_file() and not p.is_symlink()
        ):
            size = f.stat().st_size
            if copied + size > EVIDENCE_LIMIT:
                skipped += 1
                continue
            try:
                rel = f.resolve().relative_to(base)
            except ValueError:  # outside the stack root: keep it under its own dir name
                rel = Path(src.name) / f.relative_to(src)
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, out)
            copied += size
    if not copied and not skipped:
        return ""
    limit = EVIDENCE_LIMIT >> 20
    more = f", {skipped} file(s) over the {limit} MB limit left out" if skipped else ""
    return f"evidence kept: {_rel(cfg.root, dest)}{more}"


def step_env(cfg: Config, stack: str) -> dict[str, str]:
    """Project files first, the process environment wins: a worktree's .env carries
    DATABASE_URL for sqlx::test the way the developer's shell does.
    [check.env] wins over everything: a DATABASE_URL in .env or exported in a shell may
    point at a real server, and sqlx::test creates and drops databases there.
    In CI the workflow's own variables win instead: it started the database the tests
    need, on a port only it knows."""
    ci = os.environ.get("CI") == "true"
    return {
        **dotenv(cfg.root),
        **os.environ,
        **QUIET_ENV,
        **cache_env(stack),
        **{k: v for k, v in cfg.check_env.items() if not (ci and k in os.environ)},
    }


OUTCOME = re.compile(
    r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b"
)


def known_tests(datafile: str) -> int | None:
    """How many tests the testmon base knows. testmon does not even collect the test files a
    change cannot reach, so pytest's own summary has no total."""
    try:
        con = sqlite3.connect(f"file:{Path(datafile).as_posix()}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT COUNT(DISTINCT test_name) FROM test_execution"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None  # no base yet, or a testmon that changed its schema
    return int(row[0]) if row else None


def selected(output: str, total: int | None = None) -> str:
    """`selected N of M tests (testmon)`: N from pytest's summary line, M from the base."""
    summary = next(
        (ln for ln in reversed(output.splitlines()) if OUTCOME.search(ln)), ""
    )
    counts: dict[str, int] = {}
    for n, what in OUTCOME.findall(summary):
        counts[what] = counts.get(what, 0) + int(n)
    ran = sum(n for what, n in counts.items() if what != "deselected")
    of = f" of {max(total, ran)}" if total else ""
    if ran == 0:  # testmon compares code, not text: a comment-only edit selects none
        return f"selected 0{of} tests: testmon found none affected by the change"
    return f"selected {ran}{of} tests (testmon)"


# Strong markers only, case-sensitive: on a pilot project a loose `error|failed` matched test
# *names* (`http::error::tests…`, `a_failed_rebuild…`) and filled the tail with green lines.
FAILURE = re.compile(
    r"^error(\[|:)|^\s*\d*\s*error(\[|:)|\.\.\. FAILED$|^test result: FAILED|panicked at"
    r"|^Traceback|^E\s|AssertionError|^FAILED |^FAIL\b|✖|^failures:$|ERR!"
    # A JS/Node uncaught error names itself on one line above its stack (knip's
    # summary was the stack's tail without it). Anchored, a capital, then `Error:` or
    # `Exception:` — `TypeErrorKind::` and `Errors: none` stay unmatched.
    r"|^\s*(Uncaught )?([A-Z]\w*)?(Error|Exception): "
)
PASS_LINE = re.compile(r"\.\.\. ok$")


def focus_tail(lines: list[str], limit: int = TAIL) -> list[str]:
    """The lines that explain a failure: error lines with one line of context, then the
    last lines (the summary). In a big build the plain last lines are compiler progress —
    seen on a pilot project, where the tail showed `Compiling …` and not the failing test."""
    hits = [
        i
        for i, ln in enumerate(lines)
        if FAILURE.search(ln) and not PASS_LINE.search(ln)
    ]
    if not hits:
        return lines[-limit:]
    keep: list[int] = []
    for i in hits:
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(lines) and j not in keep and not PASS_LINE.search(lines[j]):
                keep.append(j)
    summary = [i for i in range(max(0, len(lines) - 3), len(lines)) if i not in keep]
    head = keep[: limit - len(summary)]
    return [lines[i] for i in sorted(head)] + [lines[i] for i in summary]


def dotenv(root: Path) -> dict[str, str]:
    """KEY=VALUE from .env then .env.harness (the later file wins). Comments, blank lines,
    `export ` prefixes and matching quotes are handled; values are never printed."""
    out: dict[str, str] = {}
    for name in (".env", ".env.harness"):
        path = root / name
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.removeprefix("export ").partition("=")
            key, value = key.strip(), value.strip()
            if value[:1] in ("'", '"') and value[0] in value[1:]:
                value = value[
                    1 : value.index(value[0], 1)
                ]  # anything after the quote: comment
            elif " #" in value:
                value = value.split(" #", 1)[0].rstrip()
            if key.isidentifier():
                out[key] = value
    return out


def _shorten(base: Path, line: str) -> str:
    """Strip the step's working directory from paths in tool output, so every path in a
    tail is relative to one place, and an absolute Windows path (~20 tokens) is gone."""
    line = ANSI.sub("", line)  # ruff colours even with NO_COLOR when piped
    root = str(base.resolve())
    for prefix in (root, root.replace("\\", "/")):
        line = re.sub(re.escape(prefix) + r"[\\/]", "", line, flags=re.IGNORECASE)
    return line


def cache_env(stack: str) -> dict[str, str]:
    """Caches shared by every worktree on the machine.

    uv's cache and pnpm's store are machine-wide already. Rust is the gap: each worktree has
    its own target/ (one shared CARGO_TARGET_DIR would serialise all sessions on cargo's
    build lock), so dependencies recompile per worktree. sccache caches rustc outputs across
    them. Incremental compilation stays on: sccache skips the workspace's own incremental
    crates but still serves every registry dependency, which is where the time goes.
    An explicit RUSTC_WRAPPER in the environment (even empty) always wins.
    """
    if stack != "rust" or "RUSTC_WRAPPER" in os.environ:
        return {}
    return {"RUSTC_WRAPPER": "sccache"} if tools.usable("sccache") else {}


def is_heavy(step: Step) -> bool:
    # Anything that compiles or runs a suite. clippy sits in the fast wave but compiles.
    return step.kind in SLOW or (step.stack == "rust" and step.kind == "lint")


def _run_gated(
    cfg: Config,
    step: Step,
    on_wait: Callable[[Step], None] | None,
    run_dir: Path | None,
) -> StepResult:
    if step.cmd is None or not is_heavy(step):
        return _run(cfg, step, run_dir)
    return _heavy(cfg, step, on_wait, run_dir)


LOAD_WAIT_S = (
    600.0  # past this the step starts anyway: a load h does not own can stay up
)


def _heavy(
    cfg: Config,
    step: Step,
    on_wait: Callable[..., None] | None,
    run_dir: Path | None,
    whole: bool = False,
) -> StepResult:
    """A heavy step within the machine budget: it waits while CPU load is above
    max_load, then for a slot (or, `whole`, for every slot), and its tools get its share
    of cores — so the steps of all sessions together stay under max_cores."""
    b = machine.budget(cfg.heavy_jobs)
    start = time.monotonic()
    told = False
    _PROGRESS.begin(step, "the machine load")
    while time.monotonic() - start < LOAD_WAIT_S:
        now = machine.load()
        if now is None or now <= b.max_load:
            break
        if not told and on_wait:
            on_wait(step, f"waits for the machine load ({now:.0%}) to drop below "
                          f"{b.max_load:.0%}")  # fmt: skip
            told = True
        time.sleep(2)
    _PROGRESS.begin(step, "the whole machine" if whole else "a heavy-job slot")
    notify = (lambda: on_wait(step)) if on_wait else None
    gate = (lock.machine(b.slots, on_wait=notify) if whole
            else lock.slot(b.slots, on_wait=notify))  # fmt: skip
    with gate:
        waited = time.monotonic() - start
        share = b.env(b.slots * b.share if whole else None)  # whole: every slot's cores
        # [check.env] set on purpose wins over the share (the user pinned it).
        share = {k: v for k, v in share.items() if k not in cfg.check_env}
        step.env = {**step.env, **share}
        result = _run(cfg, step, run_dir)
    result.waited = waited
    return result


def _git_out(root: Path, *argv: str) -> str | None:
    r = git.run(root, *argv)
    return r.stdout if r.returncode == 0 else None


@dataclass(frozen=True)
class Snapshot:
    head: str
    changes: tuple[str, ...]  # `git status` lines outside harness-owned files


def snapshot(cfg: Config) -> Snapshot | None:
    """The tree a run is about to check; None outside git or before the first commit."""
    head = _git_out(cfg.root, "rev-parse", "HEAD")
    if head is None:
        return None
    return Snapshot(head.strip(), tuple(task._user_changes(cfg, cfg.root)))


def _plan_keys(steps: list[Step]) -> list[str]:
    """Order-free: `run` returns the steps by wave, not in plan order."""
    return sorted(
        f"{s.stack} {s.kind} {'run' if s.cmd or s.call else 'skip'}" for s in steps
    )


def _env_hash(cfg: Config) -> str:
    return hashlib.sha256(
        json.dumps(cfg.check_env, sort_keys=True).encode()
    ).hexdigest()


def wall(results: list[StepResult]) -> float:
    """How long the check took: from its start to the end of its last step (`started` is
    relative to the check's start). The sum of the steps overstates parallel waves: on
    a pilot project 2337 s against a real ~1450 s."""
    return max(
        (r.started + r.seconds for r in results if r.status != "skip"), default=0.0
    )


def record(
    cfg: Config,
    start: Snapshot | None,
    results: list[StepResult],
    partial: bool = False,
    full: bool = False,
) -> None:
    """Remember what the last run saw, keyed by HEAD: `h progress done` trusts a green
    check on this commit, not the agent's word. A dirty tree or an `--only`
    subset or a custom `--base` is recorded as partial — neither proves the commit.
    `start` is taken before the steps: a commit or edit during the run makes the result
    stale, so it is never credited to a tree no step saw."""
    if start is None:
        return
    data = {
        "head": start.head,
        "dirty": bool(start.changes),  # harness output is not dirt
        "stale": snapshot(cfg) != start,
        "partial": partial,
        "ran": sum(r.status != "skip" for r in results),
        "failed": [f"{r.step.stack} {r.step.kind}" for r in results if r.status == "fail"],
        "skipped": [f"{r.step.stack} {r.step.kind}" for r in results if r.status == "skip"],
        "missed": [f"{r.step.stack} {r.step.kind}" for r in results if r.blocks_ship],
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # What `h ship` needs to reuse this run: every test ran (no testmon
        # selection), the plan, h and [check.env] it ran with, and each step's outcome.
        "full": full,
        "wall_s": round(wall(results), 1),
        "plan": _plan_keys([r.step for r in results]),
        "h": __version__,
        "env": _env_hash(cfg),
        "steps": [{"stack": r.step.stack, "kind": r.step.kind, "status": r.status,
                   "seconds": round(r.seconds, 1), "log": r.log} for r in results],
    }  # fmt: skip
    path = cfg.root / LAST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def reusable(cfg: Config, steps: list[Step]) -> tuple[list[StepResult], dict] | None:
    """The last run's results for `steps`, when that run proves the tree now: a
    full run (review prepare or ship) on this HEAD, clean, not stale or partial, green with
    no missed step, the same plan, h and [check.env]. None means run the check."""
    rec = last(cfg.root)
    now = snapshot(cfg)
    if rec is None or now is None or now.changes or rec.get("head") != now.head:
        return None
    if (not rec.get("full") or rec.get("dirty") or rec.get("stale") or rec.get("partial")
            or rec.get("failed") or rec.get("missed")):  # fmt: skip
        return None
    if rec.get("h") != __version__ or rec.get("env") != _env_hash(cfg):
        return None
    done = rec.get("steps")
    if rec.get("plan") != _plan_keys(steps) or not isinstance(done, list):
        return None
    by_key = {(d.get("stack"), d.get("kind")): d for d in done if isinstance(d, dict)}
    if len(by_key) != len(steps) or any((s.stack, s.kind) not in by_key for s in steps):
        return None
    results = []
    for s in steps:
        d = by_key[(s.stack, s.kind)]
        results.append(StepResult(s, str(d.get("status", "")), float(d.get("seconds", 0)),
                                  str(d.get("log", ""))))  # fmt: skip
    if any(r.status not in ("ok", "skip") for r in results):
        return None
    return results, rec


def last(root: Path) -> dict | None:
    """The last recorded run in this worktree, or None (never run, unreadable)."""
    try:
        data = json.loads((root / LAST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and "head" in data else None


def run(
    cfg: Config, steps: list[Step], on_wait: Callable[[Step], None] | None = None
) -> list[StepResult]:
    """The steps, with a throwaway Postgres of this run's own when tests need one
    : parallel sessions no longer share the dev server's test databases."""
    global _WATCH
    with pg.throwaway(cfg, steps) as server:
        pg.say(server.line)
        watch = _watch(cfg, steps, server)
        _WATCH = watch
        try:
            with watch:
                results = _run_all(cfg, steps, on_wait)
        finally:
            _WATCH = None
        _db_evidence(cfg, server, watch, results)
        return results


def _watch(cfg: Config, steps: list[Step], server: pg.Server) -> pg.Watch:
    """A probe of the DATABASE_URL the test and sql steps get — the throwaway locally, the
    workflow's container in CI, [check.env] otherwise; none without such a step."""
    db = [s for s in steps if s.kind in DB_KINDS and (s.cmd or s.call)]
    where = pg.target(step_env(cfg, db[0].stack).get("DATABASE_URL")) if db else None
    label = ("throwaway Postgres" if server.container
             else f"Postgres at {where[0]}:{where[1]}" if where else "")  # fmt: skip
    return pg.Watch(where, label, during=lambda: _PROGRESS.running(DB_KINDS),
                    armed=bool(server.container))  # fmt: skip


def _db_evidence(cfg: Config, server: pg.Server, watch: pg.Watch,
                 results: list[StepResult]) -> None:  # fmt: skip
    """The throwaway's state and logs, kept before it is removed with them, when the
    database died or a step that used it failed; the failures name the file."""
    failed = [r for r in results if r.status == "fail" and r.step.kind in DB_KINDS]
    if not server.container or not (watch.dead.is_set() or failed):
        return
    logs = [r.log for r in results if r.log]
    run_dir = (cfg.root / logs[0]).parent if logs else new_run_dir(cfg)
    path = run_dir / "postgres.log"
    try:
        kept = server.keep_evidence(path)
    except OSError as e:  # evidence is extra: the step results still go out
        print(f"h check: postgres logs not kept ({e})", file=sys.stderr)
        return
    if kept:
        for r in failed:
            r.tail += f"\npostgres: {_rel(cfg.root, path)}"


def _ts_install(steps: list[Step]) -> Step | None:
    """The install a run needs first, or None. `pnpm run` installs by itself, but
    the deadcode step does not go through it and the fast wave starts everything at once.
    Frozen: an install h starts never rewrites the lockfile."""
    root = next(
        (s.cwd for s in steps if s.stack == "ts" and (s.cmd or s.call) and s.cwd), None
    )
    if root is None or not (root / "pnpm-lock.yaml").is_file():
        return None
    knip_absent = deadcode.knip_pinned(root) and not deadcode.knip_bin(root)
    if (root / "node_modules").is_dir() and not knip_absent:
        return None
    if not tools.usable("pnpm"):
        return None
    return Step("ts", "install", ["pnpm", "install", "--frozen-lockfile"], cwd=root)


def _run_all(
    cfg: Config, steps: list[Step], on_wait: Callable[[Step], None] | None = None
) -> list[StepResult]:
    """Both waves run every step in parallel; heavy steps take a machine-wide slot first,
    so seven sessions share `heavy_jobs` builds instead of thrashing the CPU. [check]
    exclusive steps come last, one at a time, each holding the whole machine.
    A needed install comes first, outside the plan: `reusable` compares plans,
    so only its failure is a result, and the other steps still run."""
    with _reporting(PROGRESS_S):
        return _run_waves(cfg, steps, on_wait)


def _run_waves(
    cfg: Config, steps: list[Step], on_wait: Callable[[Step], None] | None
) -> list[StepResult]:
    t0 = time.monotonic()
    results: list[StepResult] = []
    run_dir = new_run_dir(cfg) if any(s.cmd or s.call for s in steps) else None
    if install := _ts_install(steps):
        done = _run(cfg, install, run_dir)
        if done.status == "fail":
            results.append(done)
    alone = [s for s in steps if (s.cmd or s.call) is not None and exclusive(cfg, s)]
    # A generator rewrites a file the wave's typecheck and tests read or regenerate
    # themselves: it runs after the waves, one at a time, in a heavy slot.
    later = [s for s in steps if s.kind == "generated" and s not in alone]
    for wave in (FAST, SLOW):
        batch = [s for s in steps if s.kind in wave and s not in alone + later]
        if not batch:
            continue
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            results += list(
                pool.map(lambda s: _run_gated(cfg, s, on_wait, run_dir), batch)
            )
    for step in later:
        if step.call is None:  # heavy-deferred or disabled: nothing to run
            results.append(_run(cfg, step, run_dir))
            continue
        results.append(_heavy(cfg, step, on_wait, run_dir))
    for step in alone:
        results.append(_heavy(cfg, step, on_wait, run_dir, whole=True))
    for r in results:
        r.started = max(0.0, r.started - t0)
    return results

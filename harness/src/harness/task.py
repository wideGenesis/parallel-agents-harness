"""Task lifecycle: claim on origin → worktree → env/ports/setup → … → clean after merge.

Order matters: the claim is taken before anything local is created, so a session that loses
the race leaves nothing behind. Every step is idempotent — `h task start --resume` on a task
this clone holds continues it (vibe-kanban's ensure-worktree pattern); without --resume it is
refused, since the owner key cannot tell two sessions of one clone apart.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import adr, board, cards, claims, gh, git, lock, proc
from .claims import Claim, ClaimError
from .config import Config


class NotReady(ClaimError):
    """The task's card fails the Definition of Ready."""


class Reconcile(ClaimError):
    """The task branch needs the user's hands before start can go on; the claim stays."""


TASK_FILE = ".harness-task.json"
ENV_FILE = ".env.harness"
GITIGNORE_LINES = [".worktrees/", ".artifacts/", TASK_FILE, ENV_FILE]


def _git(cwd: Path, *argv: str) -> subprocess.CompletedProcess:
    return git.run(cwd, *argv)


def _ok(r: subprocess.CompletedProcess, what: str) -> str:
    if r.returncode != 0:
        raise ClaimError(f"{what}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def common_dir(cfg: Config) -> Path:
    return Path(
        _ok(
            _git(cfg.root, "rev-parse", "--path-format=absolute", "--git-common-dir"),
            "git rev-parse",
        )
    )


def main_root(cfg: Config) -> Path:
    """The main checkout, also when called from inside a task worktree — new worktrees must
    not nest inside the current one (a real incident on a pilot project)."""
    return common_dir(cfg).parent


def worktree_path(cfg: Config, task: str) -> Path:
    return main_root(cfg) / cfg.worktree_dir / task


# --- ports -------------------------------------------------------------------------------


PORT_RANGE = (20000, 30000)  # below the OS ephemeral range: bind(0) ports get reused


def _ports_file() -> Path:
    """One registry per machine, next to the machine locks: worktrees of every clone here
    share the ports, so two clones never hand out the same one."""
    return lock.lock_dir().parent / "ports.json"


def _ports_key(cfg: Config, task: str) -> str:
    return worktree_path(cfg, task).resolve().as_posix().lower()


def _is_ports(entry: object) -> bool:
    return isinstance(entry, dict) and all(
        type(p) is int
        for p in entry.values()  # a bool is an int to isinstance
    )


def _read_registry(path: Path, notes: list[str] | None = None) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            # Valid JSON, wrong shape inside (a hand edit, another h): drop just those
            # entries, so the ports of every other worktree stay where they are.
            good = {k: v for k, v in data.items() if _is_ports(v)}
            if len(good) < len(data) and notes is not None:
                notes.append(f"warning: port registry was partly broken — dropped "
                             f"{len(data) - len(good)} entries of the wrong shape")  # fmt: skip
            return good
    except ValueError:
        pass  # an OSError propagates: a file we cannot open now is not a broken one
    # A crash mid-write once left broken JSON and every later start failed on it.
    path.replace(path.with_suffix(".json.bak"))
    if notes is not None:
        notes.append(f"warning: port registry was unreadable — kept as {path.name}.bak, "
                     "ports are handed out anew")  # fmt: skip
    return {}


def _write_registry(path: Path, registry: dict) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(registry, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)  # atomic: readers see the old file or the new one


def _free_port(taken: set[int]) -> int:
    lo, hi = PORT_RANGE
    for _ in range(200):
        port = random.randrange(lo, hi)
        if port in taken:
            continue
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue  # something outside the registry listens there
        return port
    raise ClaimError(f"could not find a free port in {lo}-{hi - 1}")


def allocate_ports(
    cfg: Config, task: str, notes: list[str] | None = None
) -> dict[str, int]:
    """Stable per task worktree: the machine registry keeps ports of every task worktree,
    so two tasks never get the same port even while their servers are down."""
    if not cfg.worktree_ports:
        return {}
    path = _ports_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = _ports_key(cfg, task)
    with lock.slot(1, name="ports"):
        registry = _read_registry(path, notes)
        mine = registry.get(key, {})
        taken = {p for k, ports in registry.items() if k != key for p in ports.values()}
        for name in cfg.worktree_ports:
            if name not in mine:
                mine[name] = _free_port(taken | set(mine.values()))
        registry[key] = mine
        _write_registry(path, registry)
    return mine


def has_ports(cfg: Config, task: str) -> bool:
    with lock.slot(1, name="ports"):  # never read while another process replaces it
        return _ports_key(cfg, task) in _read_registry(_ports_file())


def free_ports(cfg: Config, task: str) -> bool:
    path = _ports_file()
    if not path.is_file():
        return False
    with lock.slot(1, name="ports"):
        registry = _read_registry(path)
        if registry.pop(_ports_key(cfg, task), None) is None:
            return False
        _write_registry(path, registry)
        return True


# --- start -------------------------------------------------------------------------------


@dataclass
class Started:
    task: str
    path: Path
    branch: str
    resumed: bool
    stolen_from: str = ""
    ports: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    setup_failed: bool = False


def _claim(
    cfg: Config, task: str, branch: str, wt: Path, steal: bool, resume: bool
) -> tuple[Claim, bool, str]:
    """Returns (claim we hold, resumed?, previous owner if stolen)."""
    me = claims.owner_key(wt)
    email = _git(cfg.root, "config", "user.email").stdout.strip() or "unknown"
    now = claims.now_iso()
    current = claims.read(cfg, task)
    if current is None:
        # A claim an older h took under another spelling (GH-5, 5) is the same task.
        if other := [
            t for t in claims.list_all(cfg) if t != task and same_task(t, task)
        ]:
            raise ClaimError(f"{task} is taken as {other[0]}; see `h task status`")
        mine = Claim(task, me, branch, email, now, now)
        if claims.create(cfg, mine):
            return mine, False, ""
        current = claims.read(cfg, task)  # lost the race: report the winner below
        if current is None:
            raise ClaimError(f"{task}: claim vanished while racing; retry")
    sha, held = current
    if held.owner == me:
        # Same clone, same path: also another session of this clone, or the loser of a
        # race with one. Only an explicit --resume steps into that worktree.
        if not resume:
            raise ClaimError(
                f"{task} is already in progress in this clone ({wt.as_posix()}); "
                f"to continue it: h task start {task} --resume"
            )
        held.heartbeat = now
        if not claims.replace(cfg, sha, held):
            raise ClaimError(f"{task}: claim changed under us; run `h task status`")
        return held, True, ""
    age = held.age_hours()
    who = f"{held.by} ({held.owner.split('|')[0]}), heartbeat {age:.1f}h ago"
    if not held.stale(cfg.claim_ttl_hours):
        raise ClaimError(
            f"{task} is taken by {who}; stealable after {cfg.claim_ttl_hours:g}h without heartbeat"
        )
    if not steal:
        raise ClaimError(f"{task} is taken by {who} — stale; taking it over is the user's "
                         f"call: they run `h task start {task} --steal`")  # fmt: skip
    stolen = Claim(task, me, held.branch, email, now, now)
    if not claims.replace(cfg, sha, stolen):
        raise ClaimError(f"{task}: someone else took the stale claim first")
    return stolen, False, who


def _branch_exists(cfg: Config, ref: str) -> bool:
    return _git(cfg.root, "rev-parse", "--verify", "--quiet", ref).returncode == 0


def _ensure_worktree(cfg: Config, wt: Path, branch: str) -> bool:
    """Create the worktree if needed; True if it was created now."""
    if wt.exists():
        if not (wt / ".git").is_file():
            raise ClaimError(
                f"{wt} exists and is not a git worktree — move it away first"
            )
        head = _git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if head != branch:
            raise ClaimError(f"{wt} is on {head!r}, expected {branch!r}")
        return False
    _git(cfg.root, "worktree", "prune")
    wt.parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(cfg, f"refs/heads/{branch}"):
        args = ["worktree", "add", str(wt), branch]
    elif _branch_exists(cfg, f"refs/remotes/{claims.REMOTE}/{branch}"):
        # Taken over from another session that already pushed work.
        args = ["worktree", "add", "-b", branch, str(wt), f"{claims.REMOTE}/{branch}"]
    else:
        args = [
            "worktree",
            "add",
            "-b",
            branch,
            str(wt),
            f"{claims.REMOTE}/{cfg.base_branch}",
        ]
    _ok(_git(cfg.root, *args), "git worktree add")
    return True


def _sync_branch(
    cfg: Config, main: Path, task: str, branch: str, wt: Path | None
) -> str:
    """Fast-forward the local task branch to origin's, in `wt` when the worktree
    is reused, else the bare ref. Refuses on divergence or on uncommitted changes: the
    claim stays ours, the user reconciles and resumes. Returns a note, or ""."""
    head = f"refs/heads/{branch}"
    tracking = f"refs/remotes/{claims.REMOTE}/{branch}"
    r = _git(main, "ls-remote", "--heads", claims.REMOTE, head)
    if not _ok(r, "git ls-remote"):
        return ""
    _ok(
        _git(main, "fetch", "--quiet", claims.REMOTE, f"+{head}:{tracking}"),
        "git fetch",
    )
    if not _branch_exists(cfg, head):
        return ""  # the worktree is made from origin's branch
    counts = _ok(_git(main, "rev-list", "--left-right", "--count", f"{head}...{tracking}"),
                 "git rev-list")  # fmt: skip
    ahead, behind = (int(n) for n in counts.split())
    if not behind:
        return ""
    resume = f"h task start {task} --resume"
    if ahead:

        def listed(rng: str) -> str:
            return _git(main, "log", "--format=  %h %s", rng).stdout.rstrip()

        raise Reconcile(
            f"{branch} has diverged from {claims.REMOTE} — the claim is yours, nothing was "
            f"changed; reconcile the branch, then: {resume}\n"
            f"local only:\n{listed(f'{tracking}..{head}')}\n"
            f"{claims.REMOTE} only:\n{listed(f'{head}..{tracking}')}"
        )
    if wt is None:
        _ok(_git(main, "branch", "-f", branch, tracking), "git branch -f")
    else:
        dirty = _user_changes(cfg, wt)
        if dirty:
            shown = "\n".join("  " + ln for ln in dirty[:5])
            raise Reconcile(
                f"{wt.as_posix()} has uncommitted changes and {claims.REMOTE} has "
                f"{behind} new commit(s) on {branch} — commit or move them, then: "
                f"{resume}\n{shown}"
            )
        _ok(_git(wt, "merge", "--ff-only", "--quiet", tracking), "git merge --ff-only")
    return f"fast-forwarded {branch} to {claims.REMOTE} (+{behind} commit(s))"


DEFAULT_BRANCH = "task/{id}-{slug}"


def branch_name(cfg: Config, task: str, slug: str) -> str:
    """The task branch from `[tasks] branch`. A project template that names
    {slug} requires one — its pre-push hook will reject anything else. The default keeps
    the slug optional: `task/<id>` without it."""
    template = cfg.task_branch or DEFAULT_BRANCH
    if "{slug}" in template and not slug:
        if cfg.task_branch:
            raise ClaimError(f"[tasks] branch = {template!r} needs --slug")
        return branch_prefix(cfg, task)
    return template.format(id=task, slug=slug)


def branch_prefix(cfg: Config, task: str) -> str:
    """The part of the branch that only depends on the task id: what every later command
    uses to find the task's branches and PRs, whatever slug was chosen."""
    template = cfg.task_branch or DEFAULT_BRANCH
    head = template.split("{slug}")[0].format(id=task)
    return head.rstrip("-_/.")


def branch_globs(cfg: Config, task: str) -> list[str]:
    prefix = branch_prefix(cfg, task)
    template = cfg.task_branch or DEFAULT_BRANCH
    before = template.split("{slug}")[0]
    sep = before[len(before.rstrip("-_/.")) :] or "-"
    return [prefix, f"{prefix}{sep}*"]


def existing_branches(cfg: Config, main: Path, task: str) -> list[str]:
    """The task's branches, local and on origin: a released task or a fresh clone
    has no claim, and a start without --slug must resume the branch, not begin anew.
    Branches found only on origin are fetched, so the worktree can be made from them."""
    globs = branch_globs(cfg, task)
    local = _git(main, "for-each-ref", "--format=%(refname:short)",
                 *[f"refs/heads/{g}" for g in globs]).stdout.split()  # fmt: skip
    r = _git(main, "ls-remote", "--heads", claims.REMOTE,
             *[f"refs/heads/{g}" for g in globs])  # fmt: skip
    remote = [ln.split("refs/heads/", 1)[1] for ln in r.stdout.splitlines() if ln]
    for b in remote:
        if b not in local:
            _git(main, "fetch", "--quiet", claims.REMOTE,
                 f"+refs/heads/{b}:refs/remotes/{claims.REMOTE}/{b}")  # fmt: skip
    return sorted(set(local) | set(remote))


def find_card(cfg: Config, root: Path, task: str) -> Path | None:
    """`[tasks] card_path` is a glob with {id}: docs/backlog/*/cards/{id}.md."""
    matches = sorted(root.glob(cfg.card_path.format(id=task)))
    return matches[0] if matches else None


def _card_ids(cfg: Config) -> list[str]:
    """Every external id with a card: the working tree and the base on origin, so a card
    pushed but not yet pulled still counts."""
    parts = re.split(r"(\{id\}|\*\*/|\*|\?)", cfg.card_path)
    rx = re.compile(
        "".join(
            {"{id}": "(?P<id>[^/]+)", "**/": "(?:.*/)?", "*": "[^/]*", "?": "[^/]"}.get(
                p, re.escape(p)
            )
            for p in parts
        )
    )
    paths = [
        p.relative_to(cfg.root).as_posix()
        for p in cfg.root.glob(cfg.card_path.format(id="*"))
    ]
    r = _git(cfg.root, "ls-tree", "-r", "--name-only",
             f"refs/remotes/{claims.REMOTE}/{cfg.base_branch}")  # fmt: skip
    if r.returncode == 0:
        paths += r.stdout.splitlines()
    return sorted({m["id"] for p in paths if (m := rx.fullmatch(p))})


def same_task(a: str, b: str) -> bool:
    """Two spellings of one task: gh-5, GH-5, #5 and 5; ROADMAP and roadmap."""
    na, nb = gh.issue_number(a), gh.issue_number(b)
    if na is not None or nb is not None:
        return na == nb
    return a.casefold() == b.casefold()


def canonical_id(cfg: Config, raw: str, need_card: bool = True) -> str:
    """The one spelling of a task id — every claim, branch, worktree and gh call
    uses it, so `5` and `GH-5` cannot become a second claim on gh-5. An issue is `gh-<N>`;
    an external id (`[tasks] card_path`) takes the spelling of its card file. Only `start`
    needs the card: clean-up of a task whose card is gone takes the spelling of its claim,
    or the id as given."""
    if (number := gh.issue_number(raw)) is not None:
        return f"gh-{number}"
    claims.check_id(raw)
    if not cfg.card_path:
        return raw
    found = [i for i in _card_ids(cfg) if same_task(i, raw)]
    if not found and not need_card:
        return next((t for t in claims.list_all(cfg) if same_task(t, raw)), raw)
    if not found:
        raise ClaimError(f"no card for {raw} at {cfg.card_path}")
    if len(found) > 1:
        raise ClaimError(
            f"{raw}: several cards differ only in case: {', '.join(found)}"
        )
    return found[0]


def card_verdict(root: Path, number: int) -> tuple[dict, cards.Verdict]:
    """The card's readiness, including whether the user's confirmation still covers what
    the card says now."""
    issue = gh.issue_view(root, number)
    body = issue.get("body") or ""
    confirmed = None
    if cards.AC_CONFIRMED in issue["labels"]:
        confirmed = gh.body_when_labeled(root, number, cards.AC_CONFIRMED, body)
    return issue, cards.check_ready(body, issue["labels"], confirmed)


def start(
    cfg: Config, task: str, slug: str = "", steal: bool = False, resume: bool = False
) -> Started:
    task = canonical_id(cfg, task)
    wt = worktree_path(cfg, task)
    main = main_root(cfg)

    # Issue-backed task: the Definition of Ready gates the claim, so a card that is not
    # ready leaves no claim and no worktree behind.
    number = gh.issue_number(task)
    issue = None
    if number is not None:
        issue, verdict = card_verdict(main, number)
        if issue.get("state", "OPEN").upper() != "OPEN":
            raise ClaimError(f"{task} is closed")
        if not verdict.ready:
            listed = "\n".join(f"  - {p}" for p in verdict.problems)
            raise NotReady(
                f"{task} is not ready — clarify with the user first:\n{listed}\n"
                f"(re-check: h task ready {task})"
            )

    _ok(_git(main, "fetch", "--quiet", claims.REMOTE, cfg.base_branch), "git fetch")
    branch = branch_name(cfg, task, slug)
    notes: list[str] = []
    if claims.read(cfg, task) is None:  # a live claim names its branch itself
        found = existing_branches(cfg, main, task)
        if not slug and len(found) > 1:
            listed = "\n".join(f"  {b}" for b in found)
            raise ClaimError(
                f"{task} has several branches — pick one with --slug:\n{listed}"
            )
        if not slug and found:
            branch = found[0]
            notes.append(f"resumed existing branch {branch}")
        elif other := [b for b in found if b != branch]:
            notes.append(f"warning: {task} also has branch(es) {', '.join(other)}")
    held, resumed, stolen_from = _claim(cfg, task, branch, wt, steal, resume)
    made = _Made()
    try:
        result = _prepare(cfg, task, wt, main, held.branch, issue, notes, made)
    except Reconcile:
        raise  # nothing local was made yet; the claim waits for `start --resume`
    except BaseException as e:
        # a claim without a worktree keeps the task off `h task next` for the
        # whole TTL. A resumed task had its claim and worktree before this run: untouched.
        if resumed:
            raise
        undone = _roll_back(cfg, task, wt, held.branch, made)
        if not isinstance(e, Exception):
            raise
        msg = f"{e}\nrolled back: {', '.join(undone) or 'nothing'}"
        raise type(e)(msg) if isinstance(e, ClaimError) else ClaimError(msg) from e
    result.resumed = resumed and not made.worktree
    result.stolen_from = stolen_from
    return result


@dataclass
class _Made:
    """What this run of `start` created, so a failure can remove exactly that."""

    worktree: bool = False
    branch: bool = False


def _roll_back(cfg: Config, task: str, wt: Path, branch: str, made: _Made) -> list[str]:
    """Best effort: every step is tried, each failure only shortens the report."""
    main, undone = main_root(cfg), []
    try:
        current = claims.read(cfg, task)
        if (
            current
            and current[1].owner == claims.owner_key(wt)
            and claims.delete(cfg, task, current[0])  # CAS: only the claim we wrote
        ):
            undone.append("claim")
    except (ClaimError, git.GitError):
        pass
    if made.worktree:
        _git(main, "worktree", "remove", "--force", str(wt))
        if wt.exists():
            _remove_dir(wt)
        _git(main, "worktree", "prune")
        if not wt.exists():
            undone.append("worktree")
    if made.branch and _git(main, "branch", "-D", branch).returncode == 0:
        undone.append(f"branch {branch}")
    if made.worktree and free_ports(cfg, task):  # a reused worktree keeps its ports
        undone.append("ports")
    return undone


def _prepare(
    cfg: Config,
    task: str,
    wt: Path,
    main: Path,
    branch: str,
    issue: dict | None,
    notes: list[str],
    made: _Made,
) -> Started:
    """Everything after the claim; records in `made` what it creates."""
    number = gh.issue_number(task)
    # an old local branch must not ship over work pushed since (ADR-0002 §5).
    reused = wt.exists()
    if reused:
        _ensure_worktree(cfg, wt, branch)
    synced = _sync_branch(cfg, main, task, branch, wt if reused else None)
    if synced:
        notes.append(synced)
    if not reused:
        had_branch = _branch_exists(cfg, f"refs/heads/{branch}")
        made.worktree = _ensure_worktree(cfg, wt, branch)
        made.branch = made.worktree and not had_branch
    result = Started(task, wt, branch, False, notes=notes)
    if issue is None and cfg.card_path:
        # External card: the project's own file is the brief; no DoR, labels or board.
        card = find_card(cfg, wt, task)  # the fresh worktree, not a possibly stale main
        brief_path = wt / ".artifacts" / "task" / "brief.md"
        if card is not None:
            brief_path.parent.mkdir(parents=True, exist_ok=True)
            brief_path.write_text(
                adr.with_section(cfg, wt, card.read_text(encoding="utf-8")),
                encoding="utf-8",
            )
            result.notes.append(
                f"brief: .artifacts/task/brief.md (from {card.relative_to(wt).as_posix()})"
            )
        else:
            result.notes.append(f"warning: no card for {task} at {cfg.card_path}")
    if issue is not None and number is not None:
        brief_path = wt / ".artifacts" / "task" / "brief.md"
        brief_path.parent.mkdir(parents=True, exist_ok=True)
        brief_path.write_text(
            adr.with_section(cfg, wt, cards.brief(issue)), encoding="utf-8"
        )
        result.notes.append("brief: .artifacts/task/brief.md")
        try:  # labels only display state; the claim is the truth
            gh.issue_labels(main, number, add=["in-progress"], remove=["ready"])
        except gh.GhError as e:
            result.notes.append(f"warning: labels not updated ({e})")
        warn = board.move(cfg, issue["url"], "In progress")
        if warn:
            result.notes.append(warn)
    for rel in cfg.worktree_include:
        src, dst = main / rel, wt / rel
        if src.is_file() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            result.notes.append(f"copied {rel}")
    result.ports = allocate_ports(cfg, task, result.notes)
    env_lines = [f"HARNESS_TASK={task}"] + [
        f"PORT_{name.upper()}={port}" for name, port in result.ports.items()
    ]
    (wt / ENV_FILE).write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    (wt / TASK_FILE).write_text(
        json.dumps({"task": task, "branch": branch, "ports": result.ports}, indent=1),
        encoding="utf-8",
    )
    if cfg.worktree_setup and not (wt / SETUP_OK).is_file():
        # Not only on creation: a setup that failed is redone by `start --resume`.
        # A failed setup is not rolled back: the worktree is where its cause is fixed.
        result.notes += run_setup(cfg, wt)
        result.setup_failed = any(n.startswith("setup FAILED") for n in result.notes)
    return result


SETUP_OK = ".artifacts/task/setup.ok"
SETUP_LOG = ".artifacts/task/setup.log"


def setup_argv(cmd: str) -> list[str]:
    """A `[worktrees] setup` line as argv. Windows rules on Windows — POSIX shlex eats the
    backslashes of a path — and the program resolved via PATH/PATHEXT, so `pnpm` finds
    pnpm.cmd, which CreateProcess alone never does."""
    if os.name == "nt":
        argv = [a[1:-1] if len(a) > 1 and a[0] == a[-1] == '"' else a
                for a in shlex.split(cmd, posix=False)]  # fmt: skip
    else:
        argv = shlex.split(cmd)
    if argv and (exe := shutil.which(argv[0])):
        argv[0] = exe
    return argv


def run_setup(cfg: Config, wt: Path) -> list[str]:
    """Run `[worktrees] setup` in order; stop at the first failure. The full output goes to
    .artifacts/task/setup.log; success leaves a mark, so a retry knows to run it again."""
    notes, log = [], [f"# h task start — setup in {wt.as_posix()}"]
    for cmd in cfg.worktree_setup:
        log.append(f"\n$ {cmd}")
        try:
            r = proc.run(setup_argv(cmd), wt)
            out = (r.stdout or "") + (r.stderr or "")
            failed = r.returncode != 0
        except OSError as e:  # program not found, not executable
            out, failed = f"{type(e).__name__}: {e}", True
        log.append(out.rstrip())
        if failed:
            tail = out.strip().splitlines()[-5:]
            notes.append(f"setup FAILED: {cmd}\n" + "\n".join(tail) + f"\n"
                         f"full log: {SETUP_LOG}; fix, then: h task start <id> --resume")  # fmt: skip
            break
        notes.append(f"setup ok: {cmd}")
    (wt / SETUP_LOG).parent.mkdir(parents=True, exist_ok=True)
    (wt / SETUP_LOG).write_text("\n".join(log) + "\n", encoding="utf-8")
    if not any(n.startswith("setup FAILED") for n in notes):
        (wt / SETUP_OK).write_text("", encoding="utf-8")
    return notes


# --- heartbeat / release -------------------------------------------------------------------


def current_task(start_dir: Path) -> tuple[Path, str] | None:
    """The task worktree containing `start_dir`, from its .harness-task.json."""
    for d in (start_dir.resolve(), *start_dir.resolve().parents):
        f = d / TASK_FILE
        if f.is_file():
            return d, json.loads(f.read_text(encoding="utf-8"))["task"]
    return None


class TakenOver(ClaimError):
    """This worktree no longer holds the task — stop working on it."""


def heartbeat(cfg: Config, task: str, wt: Path) -> Claim:
    """Raises TakenOver only when origin says the task is someone else's; a push that
    could not reach origin raises claims.PushFailed."""
    me = claims.owner_key(wt)
    # The hook, `h check` and `h ci watch` of one worktree beat at the same time; in turn,
    # none of them loses the compare-and-swap to another.
    with lock.slot(1, name=f"heartbeat-{task}"):
        for _ in range(3):
            current = claims.read(cfg, task)
            if current is None:
                raise TakenOver(
                    f"{task}: no claim on origin — run `h task start {task}`"
                )
            sha, held = current
            if held.owner != me:
                raise TakenOver(
                    f"{task} is held by {held.by} ({held.owner.split('|')[0]}), "
                    "not this worktree"
                )
            held.heartbeat = claims.now_iso()
            if claims.replace(cfg, sha, held):
                _stamp(cfg, task).touch()
                return held
            # Lost the swap but maybe to ourselves (start --resume, another clone's
            # process of this worktree): read again — the owner decides.
    raise ClaimError(f"{task}: claim keeps changing under us; run `h task status`")


HOOK_INTERVAL_S = 30 * 60


def _stamp(cfg: Config, task: str) -> Path:
    d = common_dir(cfg) / "harness" / "heartbeat"
    d.mkdir(parents=True, exist_ok=True)
    return d / task


def auto_heartbeat(cfg: Config, start: Path, min_interval_s: float = 0) -> str | None:
    """Heartbeat from inside a task worktree as a side effect of normal work.

    Returns a warning line when the network failed — work must go on offline. Raises
    TakenOver when the task belongs to someone else now: continuing would be wasted work.
    Outside a task worktree, or within `min_interval_s` of the last beat, does nothing."""
    found = current_task(start)
    if found is None:
        return None
    wt, task_id = found
    stamp = _stamp(cfg, task_id)
    if (
        min_interval_s
        and stamp.exists()
        and time.time() - stamp.stat().st_mtime < min_interval_s
    ):
        return None
    try:
        heartbeat(cfg, task_id, wt)
    except TakenOver:
        raise
    except ClaimError as e:
        # Stamp the attempt too: offline, the hook would otherwise wait for a network
        # timeout on every single edit (reviewer note).
        stamp.touch()
        return f"heartbeat: not sent ({str(e).splitlines()[0][:100]})"
    return None


def claim_spelling(cfg: Config, task: str) -> str:
    """The id the task's claim is stored under: `task`, or the spelling an older h used
    (GH-5, 5) — clean-up must find that claim too."""
    if claims.read(cfg, task) is not None:
        return task
    return next((t for t in claims.list_all(cfg) if same_task(t, task)), task)


def release(cfg: Config, task: str, force: bool = False) -> str:
    """Drop the claim (abandon the task). Only the holder may, unless `force`. Returns the
    spelling the released claim had, '' if there was nothing to release."""
    ref = claim_spelling(cfg, task)
    current = claims.read(cfg, ref)
    if current is None:
        return ""
    sha, held = current
    if not force and held.owner != claims.owner_key(worktree_path(cfg, ref)):
        raise ClaimError(
            f"{task} is held by {held.by}, not this worktree; dropping it is the user's "
            f"call: they run `h task release {task} --force`"
        )
    if not claims.delete(cfg, ref, sha):
        raise ClaimError(f"{task}: claim changed under us")
    return ref


# --- clean -------------------------------------------------------------------------------


def merge_state(cfg: Config, branch: str) -> str:
    """'merged' | 'squash-merged' | 'empty' | 'unmerged' against origin/<base>
    ('pr-merged' comes from `clean`, see `in_merged_pr`).

    GitHub squash merges leave no ancestry, so besides --is-ancestor we squash the branch
    into one probe commit and ask `git cherry` whether base already has the same patch."""
    main = main_root(cfg)
    base = f"{claims.REMOTE}/{cfg.base_branch}"
    mb = _git(main, "merge-base", base, branch).stdout.strip()
    if not mb:
        return "unmerged"
    tree = _git(main, "rev-parse", f"{branch}^{{tree}}").stdout.strip()
    if tree == _git(main, "rev-parse", f"{mb}^{{tree}}").stdout.strip():
        return "empty"
    if _git(main, "merge-base", "--is-ancestor", branch, base).returncode == 0:
        return "merged"
    # The probe is a dangling object nobody sees; its own identity keeps it working on
    # machines and CI runners without user.name/user.email ("Author identity unknown").
    probe = _ok(
        _git(
            main,
            "-c", "user.name=harness",
            "-c", "user.email=harness@localhost",
            "commit-tree", tree, "-p", mb, "-m", "harness squash probe",
        ),
        "git commit-tree (squash probe)",
    )  # fmt: skip
    cherry = _git(main, "cherry", base, probe).stdout.strip()
    return "squash-merged" if cherry.startswith("-") else "unmerged"


def in_merged_pr(cfg: Config, branch: str, head: str, number: int | None) -> bool:
    """The branch holds nothing beyond the head of its merged PR. A squash merge
    that GitHub combined with a neighbour's edit of the same lines differs from the
    branch, so `merge_state` cannot match the patch; the PR head can."""
    main = main_root(cfg)
    if _git(main, "cat-file", "-e", f"{head}^{{commit}}").returncode and number:
        _git(main, "fetch", "--quiet", claims.REMOTE, f"refs/pull/{number}/head")
    return _git(main, "merge-base", "--is-ancestor", branch, head).returncode == 0


@dataclass
class CleanItem:
    task: str
    path: Path
    branch: str
    state: str  # merge state
    action: str  # removed | kept: <reason> | would remove
    manual: str = ""  # what a human must do when the code could not finish


def _own_files(cfg: Config) -> set[str]:
    """Files `h task start` put into the worktree — never the user's work."""
    return {TASK_FILE, ENV_FILE, *cfg.worktree_include}


OWN_DIRS = (".artifacts/",)  # harness output (check logs) — not the user's work either


def _user_changes(cfg: Config, wt: Path) -> list[str]:
    """`git status` lines ("XY path") outside harness-owned files. `-z` keeps non-ASCII
    paths readable; a rename record carries its source path as an extra field."""
    own = _own_files(cfg)
    fields = _git(wt, "status", "--porcelain", "-z", "--untracked-files=all").stdout
    records, it = [], iter(fields.split("\0"))
    for rec in it:
        if not rec:
            continue
        if rec[0] in "RC":
            next(it, None)
        records.append(rec)
    return [
        rec
        for rec in records
        if (p := rec[3:]) not in own and not p.startswith(OWN_DIRS)
    ]


def _worktrees(cfg: Config) -> list[tuple[Path, str]]:
    out = _git(main_root(cfg), "worktree", "list", "--porcelain").stdout
    items, path = [], None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree ") :])
        elif line.startswith("branch ") and path is not None:
            items.append((path, line[len("branch refs/heads/") :]))
    return items


ORPHAN_GRACE_MINUTES = 10  # `start` between the claim and `worktree add` takes seconds


def _in_progress(cfg: Config, task: str, path: Path, fresh_hours: float) -> str:
    """'kept: in progress (heartbeat Nm ago)' while this clone's claim on the task is
    younger than `fresh_hours`, else ''. A claim origin cannot tell about keeps it too."""
    try:
        got = claims.read(cfg, task)
    except (ClaimError, git.GitError) as e:
        return f"kept: claim not readable ({_first_line(e)})"
    if not got or got[1].owner != claims.owner_key(path):
        return ""
    age = got[1].age_hours()
    if age > fresh_hours:
        return ""
    return f"kept: in progress (heartbeat {age * 60:.0f}m ago)"


def clean(
    cfg: Config,
    only: str | None = None,
    dry_run: bool = False,
    merged_pr: tuple[str, int | None] | None = None,
) -> list[CleanItem]:
    """Remove task worktrees whose work is in base. Never removes one with uncommitted or
    untracked changes, nor anything outside the managed worktree directory. A global clean
    also keeps an untouched worktree whose claim is alive — a session may be reading code
    before its first edit; `clean <ID>` and `done` remove it as before.
    `merged_pr` — (head sha, number) of the task's merged PR, from `done`: a branch within
    that head counts as merged whatever the squash commit looks like."""
    main = main_root(cfg)
    managed = (main / cfg.worktree_dir).resolve()
    _git(main, "fetch", "--quiet", claims.REMOTE, cfg.base_branch)
    _git(main, "worktree", "prune")
    result = []
    for path, branch in _worktrees(cfg):
        if path.resolve().parent != managed:
            continue
        task = path.name
        if only and not same_task(task, only):
            continue
        state = merge_state(cfg, branch)
        if state == "unmerged" and merged_pr and merged_pr[0]:
            if in_merged_pr(cfg, branch, *merged_pr):
                state = "pr-merged"
            else:
                result.append(CleanItem(task, path, branch, state,
                                        "kept: not merged — the branch has commits "
                                        "outside the merged PR"))  # fmt: skip
                continue
        if state == "unmerged":
            result.append(CleanItem(task, path, branch, state, "kept: not merged"))
            continue
        if (
            state == "empty"
            and not only
            and (kept := _in_progress(cfg, task, path, cfg.claim_ttl_hours))
        ):
            result.append(CleanItem(task, path, branch, state, kept))
            continue
        if _user_changes(cfg, path):
            result.append(
                CleanItem(task, path, branch, state, "kept: uncommitted changes")
            )
            continue
        if dry_run:
            result.append(CleanItem(task, path, branch, state, "would remove"))
            continue
        for rel in _own_files(cfg):  # ours, untracked: `worktree remove` would refuse
            f = path / rel
            if (
                f.is_file()
                and _git(path, "ls-files", "--error-unmatch", rel).returncode
            ):
                f.unlink()
        for rel in OWN_DIRS:  # check logs, brief.md — harness output, never the user's
            d = path / rel.rstrip("/")
            if d.is_dir() and not _git(path, "ls-files", rel).stdout.strip():
                shutil.rmtree(d, ignore_errors=True)
        r = _git(main, "worktree", "remove", str(path))
        if r.returncode != 0 and _registered(cfg, path):
            # Windows: a running cargo/node/shell holding the directory.
            reason = (r.stderr or r.stdout).strip().splitlines()[-1:]
            result.append(
                CleanItem(task, path, branch, state, f"kept: {' '.join(reason)}",
                          manual=f"close what uses it, then: h task clean {task}")
            )  # fmt: skip
            continue
        # git has let go — fully, or it unregistered the worktree and left the directory
        # behind (seen 2026-10-22: the session's own cwd). Finish everything else anyway.
        if err := _release(cfg, task, path, branch):
            result.append(CleanItem(task, path, branch, state, f"removed; {err}",
                                    manual=f"h task clean {task} once origin is reachable"))  # fmt: skip
            continue
        if path.exists() and not _remove_dir(path):
            result.append(
                CleanItem(task, path, branch, state, "removed from git; directory left",
                          manual=_manual_rm(path))
            )  # fmt: skip
        else:
            result.append(CleanItem(task, path, branch, state, "removed"))
    result += _orphans(cfg, managed, only, dry_run, {i.task for i in result})
    return result


def _registered(cfg: Config, path: Path) -> bool:
    return any(p.resolve() == path.resolve() for p, _ in _worktrees(cfg))


def _release(cfg: Config, task: str, path: Path, branch: str | None) -> str:
    """Branch, ports and our claim; '' on success, else what was not released — a network
    error must not cut the clean-up report short."""
    main = main_root(cfg)
    if branch:
        _git(main, "branch", "-D", branch)
    free_ports(cfg, task)
    try:
        current = claims.read(cfg, task)
        if current and current[1].owner == claims.owner_key(path):
            claims.delete(cfg, task, current[0])
    except (ClaimError, git.GitError) as e:
        return f"claim not released ({_first_line(e)})"
    return ""


def _first_line(e: Exception) -> str:
    return (str(e).splitlines() or [type(e).__name__])[0][:120]


PRISTINE_MAX = 500  # more files than this is a tree, not the remnant of a removal


def _pristine(cfg: Config, d: Path) -> bool:
    """Only empty directories and files byte-identical (CRLF-tolerant) to the same paths
    on origin/<base>: what a worktree removal leaves when a shell stands in a subdirectory.
    No one's work can be in it, so it may go without asking."""
    files = [p for p in d.rglob("*") if p.is_file() or p.is_symlink()]
    if len(files) > PRISTINE_MAX:
        return False
    if not files:
        return True
    rels = [f.relative_to(d).as_posix() for f in files]
    ref = f"{claims.REMOTE}/{cfg.base_branch}"
    r = _git(main_root(cfg), "ls-tree", "--full-tree", ref, "--", *rels)
    if r.returncode != 0:
        return False
    blobs = {}
    for line in r.stdout.splitlines():
        meta, path = line.split("\t", 1)
        if meta.split()[1] == "blob":
            blobs[path] = meta.split()[2]
    for f, rel in zip(files, rels):
        if f.is_symlink() or rel not in blobs:
            return False
        data = f.read_bytes()
        if blobs[rel] not in (_blob_id(data), _blob_id(data.replace(b"\r\n", b"\n"))):
            return False
    return True


def _blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _remove_dir(path: Path) -> bool:
    def force(func, p, _exc):  # read-only files (node_modules, .git objects) on Windows
        os.chmod(p, stat.S_IWRITE)
        func(p)

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=force)
        else:
            shutil.rmtree(path, onerror=force)
    except OSError:
        return False
    return not path.exists()


def _manual_rm(path: Path) -> str:
    if sys.platform == "win32":
        return f'PowerShell: Remove-Item -Recurse -Force "{path}"  (close editors/terminals using it first)'
    return f'rm -rf "{path}"'


def _orphans(
    cfg: Config, managed: Path, only: str | None, dry_run: bool, handled: set[str]
) -> list[CleanItem]:
    """Leftovers git no longer knows about: directories in the managed dir that are not
    worktrees, and our claims whose worktree is gone. Empty directories and our own claims
    are cleaned; a directory with files is only reported — it might hold someone's work.
    A claim younger than ORPHAN_GRACE_MINUTES is a `start` still creating its worktree."""
    out: list[CleanItem] = []
    grace = ORPHAN_GRACE_MINUTES / 60
    registered = {p.resolve() for p, _ in _worktrees(cfg)}
    dirs = (
        sorted(d for d in managed.iterdir() if d.is_dir()) if managed.is_dir() else []
    )
    for d in dirs:
        task = d.name
        if (
            task in handled
            or d.resolve() in registered
            or (only and not same_task(task, only))
        ):
            continue
        if any(d.iterdir()) and not _pristine(cfg, d):
            out.append(CleanItem(task, d, "", "orphan", "kept: not a worktree, has files",
                                 manual=f"inspect, then {_manual_rm(d)}"))  # fmt: skip
            continue
        if kept := _in_progress(cfg, task, d, grace):
            out.append(CleanItem(task, d, "", "orphan", kept))
            continue
        if dry_run:
            out.append(CleanItem(task, d, "", "orphan", "would remove"))
            continue
        err = _release(cfg, task, d, _merged_branch(cfg, task))
        ok = _remove_dir(d)
        action = ("removed" if ok else "directory left") + (f"; {err}" if err else "")
        manual = "; ".join(m for m in (
            "" if ok else _manual_rm(d), "h task clean once origin is reachable" if err else ""
        ) if m)  # fmt: skip
        out.append(CleanItem(task, d, "", "orphan", action, manual=manual))
    seen = handled | {i.task for i in out}
    retry = "h task clean once origin is reachable"
    try:
        ids = claims.list_all(cfg)
    except (ClaimError, git.GitError) as e:
        out.append(CleanItem("claims", managed, "", "orphan",
                             f"not checked ({_first_line(e)})", manual=retry))  # fmt: skip
        return out
    for tid in ids:
        wt = managed / tid
        if tid in seen or wt.exists() or (only and not same_task(tid, only)):
            continue
        if kept := _in_progress(cfg, tid, wt, grace):
            out.append(CleanItem(tid, wt, "", "orphan", kept))
            continue
        try:
            got = claims.read(cfg, tid)
            if got and got[1].owner == claims.owner_key(wt):
                released = dry_run or claims.delete(cfg, tid, got[0])
                out.append(CleanItem(tid, wt, got[1].branch, "orphan",
                                     "would release claim" if dry_run else
                                     "claim released" if released else "kept: claim changed on origin"))  # fmt: skip
        except (ClaimError, git.GitError) as e:
            out.append(CleanItem(tid, wt, "", "orphan",
                                 f"claim not released ({_first_line(e)})", manual=retry))  # fmt: skip
    return out


def _merged_branch(cfg: Config, task: str) -> str | None:
    """The task's local branch, if its work is already in base (safe to delete)."""
    out = _git(main_root(cfg), "branch", "--list", *branch_globs(cfg, task),
               "--format=%(refname:short)").stdout.split()  # fmt: skip
    for b in out:
        if merge_state(cfg, b) != "unmerged":
            return b
    return None

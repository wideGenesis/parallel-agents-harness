"""`h ci watch` — wait for the PR's CI, triage failures without the log, cap the rounds.

    green  → next step (merge per [ship] merge, then h task done)
    red    → rerun the failed jobs once (flakes); still red → a counted round:
             compressed log in .artifacts/ci/round-N.log for the `ci-triage` subagent
    2 rounds counted → blocked: ship and ci watch refuse, label ci-blocked, ask the user
    not the code (cancelled, awaiting approval, startup failure, a red check from outside
    Actions) → blocked with the reason; no rerun, no round

The round counter lives on origin (`refs/ci-rounds/<ID>`, a JSON blob, compare-and-swap like
claims) — a file in the worktree would vanish with it. `h ci reset` clears it; only the user
runs it (denied to agents by `h init`).
"""

from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import check, claims, gh, task
from .config import Config

PREFIX = "refs/ci-rounds/"
MAX_ROUNDS = 2
ART = Path(".artifacts") / "ci"
ERROR = re.compile(
    r"error|failed|failure|panicked|traceback|assert|exception|^E\s|✖|FAIL",
    re.IGNORECASE,
)
CONTEXT, TAIL, CAP = 3, 30, 200
OK = ("success", "skipped", "neutral")
# The run never tested the code: approval pending, broken workflow file, expired. A rerun
# or a round would be wasted on it.
STUCK = ("action_required", "startup_failure", "stale")
CANCEL_GRACE = (
    300  # s; a cancelled run may be superseded by a newer run of the same commit
)
# `gh run view --log-failed` lines: "<job>\t<step>\t<timestamp> <text>"; job and step names
# contain spaces ("test (windows)"), so the columns are split on tabs only.
STAMP = re.compile(r"^[^\t]*\t[^\t]*\t\d{4}-\d\d-\d\dT[\d:.]+Z ?")


class CiBlocked(task.ClaimError):
    pass


@dataclass
class State:
    rounds: list[str] = field(
        default_factory=list
    )  # head shas whose CI failed for real
    reruns: list[str] = field(default_factory=list)  # head shas already rerun once


def _read(cfg: Config, task_id: str) -> tuple[str | None, State]:
    # An unreadable counter is an error, never an empty one: the next write would wipe the
    # rounds and lift the limit.
    what = "CI round counter unreadable — retry; nothing was reset"
    out = claims._ok(
        claims._git(cfg, "ls-remote", claims.REMOTE, PREFIX + task_id), what
    )
    if not out.strip():
        return None, State()
    sha = out.split()[0]
    if claims._git(cfg, "cat-file", "-e", sha).returncode != 0:
        claims._git(cfg, "fetch", "--quiet", claims.REMOTE, PREFIX + task_id)
    raw = claims._ok(claims._git(cfg, "cat-file", "-p", sha), what)
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise claims.ClaimError(f"{what}: {e}") from None
    return sha, State(list(data.get("rounds", [])), list(data.get("reruns", [])))


def _write(cfg: Config, task_id: str, expect: str | None, state: State) -> None:
    body = json.dumps({"rounds": state.rounds, "reruns": state.reruns}) + "\n"
    blob = claims._ok(
        claims._git(cfg, "hash-object", "-w", "--stdin", stdin=body), "hash-object"
    ).strip()
    ref = PREFIX + task_id
    lease = (
        f"--force-with-lease={ref}:{expect}" if expect else f"--force-with-lease={ref}:"
    )
    r = claims._git(cfg, "push", "--quiet", lease, claims.REMOTE, f"{blob}:{ref}")
    if r.returncode != 0:
        raise task.ClaimError(f"CI round counter changed under us: {r.stderr.strip()}")


def rounds(cfg: Config, task_id: str) -> int:
    return len(_read(cfg, task_id)[1].rounds)


def guard(cfg: Config, task_id: str) -> None:
    n = rounds(cfg, task_id)
    if n >= MAX_ROUNDS:
        raise CiBlocked(
            f"{task_id}: CI failed {n} times — stop and ask the user how to proceed "
            f"(logs in {ART.as_posix()}/; the user clears the block with `h ci reset {task_id}`)"
        )


def reset(cfg: Config, task_id: str) -> bool:
    sha, _ = _read(cfg, task_id)
    if sha is None:
        return False
    r = claims._git(cfg, "push", "--quiet", f"--force-with-lease={PREFIX}{task_id}:{sha}",
                    claims.REMOTE, f":{PREFIX}{task_id}")  # fmt: skip
    if r.returncode != 0:  # "cleared" only when origin agrees
        raise claims.ClaimError(f"CI round counter not cleared: {r.stderr.strip()}")
    return True


def compress(raw: str, cap: int = CAP) -> str:
    """`gh run view --log-failed` → the lines that explain the failure. Deterministic, so the
    main session never sees thousands of runner lines."""
    lines = [STAMP.sub("", ln).rstrip() for ln in raw.splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    keep: set[int] = set()
    for i, ln in enumerate(lines):
        if ERROR.search(ln):
            keep.update(range(max(0, i - CONTEXT), min(len(lines), i + CONTEXT + 1)))
    keep.update(range(max(0, len(lines) - TAIL), len(lines)))
    out, last = [], -2
    for i in sorted(keep):
        if i != last + 1:
            out.append("…")
        out.append(lines[i])
        last = i
    if len(out) > cap:
        out = out[: cap // 2] + ["… (cut) …"] + out[-cap // 2 :]
    return "\n".join(out) + "\n"


@dataclass
class WatchResult:
    status: str  # green | pending | red | blocked | conflict | no-pr | unpushed
    lines: list[str] = field(default_factory=list)
    log: str = ""  # path of the compressed log for a counted round


class _Conflict(Exception):
    pass


def _latest(runs: list[dict], branch: str) -> list[dict]:
    """The newest run per workflow and event on the task's branch: an older run of the same
    commit (cancelled by concurrency, say) is history, and a run of another branch at the
    same commit is not this PR's."""
    seen: dict[tuple, dict] = {}
    for r in sorted(runs, key=lambda r: r["databaseId"], reverse=True):
        if branch and (r.get("headBranch") or branch) != branch:
            continue
        seen.setdefault((r.get("workflowName"), r.get("event")), r)
    return list(seen.values())


def _external(cfg: Config, branch: str) -> list[dict]:
    """The PR's checks that do not come from GitHub Actions (commit statuses, apps)."""
    if not branch:
        return []
    return [c for c in gh.pr_checks(cfg.root, branch) if not c.get("workflow")]


def _wait(
    cfg: Config, sha: str, timeout_s: float, sleep: Callable[[float], None],
    min_attempt: dict[int, int] | None = None, branch: str = "",
) -> tuple[list[dict], list[dict]]:  # fmt: skip
    """Poll until every run of `sha` and every check from outside Actions is done — and,
    after a rerun, until those runs report a newer attempt: right after `gh run rerun`
    GitHub may still show the old failed attempt, which would count a flake as a real round
    (reviewer note). Returns (runs, external checks)."""
    deadline = time.monotonic() + timeout_s
    min_attempt = min_attempt or {}
    cancelled_at: float | None = None
    while True:
        runs = _latest(gh.runs_for_commit(cfg.root, sha), branch)
        # GitHub starts no pull_request run for a PR that conflicts with its base, so
        # "no runs yet" would last forever. UNKNOWN (not computed yet) waits on.
        if not runs and branch and gh.pr_mergeable(cfg.root, branch) == "CONFLICTING":
            raise _Conflict
        if any(
            r["status"] == "waiting" for r in runs
        ):  # an environment awaits approval
            return runs, []
        fresh = all(
            int(r.get("attempt", 1)) >= min_attempt.get(r["databaseId"], 0)
            for r in runs
        )
        checks: list[dict] = []
        if runs and fresh and all(r["status"] == "completed" for r in runs):
            checks = _external(cfg, branch)
            if not any(c.get("bucket") == "pending" for c in checks):
                if not any(r.get("conclusion") == "cancelled" for r in runs):
                    return runs, checks
                cancelled_at = cancelled_at or time.monotonic()
                if time.monotonic() - cancelled_at >= CANCEL_GRACE:
                    return runs, checks
        if time.monotonic() >= deadline:
            return runs, checks
        sleep(20)


def _unpushed(wt: Path, branch: str, sha: str) -> list[str] | None:
    """CI runs only for what origin has: a local HEAD that is not the branch tip there would
    wait out the whole timeout for runs that never start."""
    r = task._git(wt, "ls-remote", claims.REMOTE, f"refs/heads/{branch}")
    if r.returncode != 0:
        raise task.ClaimError(f"cannot read {branch} on origin: {r.stderr.strip()}")
    remote = r.stdout.split()[0] if r.stdout.strip() else ""
    if remote == sha:
        return None
    # Missing on origin, or HEAD is ahead of it: the local commits are just not pushed.
    if (
        not remote
        or task._git(wt, "merge-base", "--is-ancestor", remote, sha).returncode == 0
    ):
        return [f"{sha[:10]} is not on origin/{branch} — no CI will run for it",
                "next: h ship (pushes and updates the PR), then h ci watch"]  # fmt: skip
    return [(f"origin/{branch} is at {remote[:10]}, which HEAD {sha[:10]} lacks — "
            "CI there is not for this HEAD"),
            (f"next: git fetch && git rebase origin/{branch}, then h ship (or h ci watch "
            "if nothing local is left to push)")]  # fmt: skip


def watch(
    cfg: Config, start: Path, timeout_s: float = 45 * 60,
    sleep: Callable[[float], None] = time.sleep,
) -> WatchResult:  # fmt: skip
    found = task.current_task(start)
    if found is None:
        raise task.ClaimError("not inside a task worktree")
    wt, task_id = found
    task.auto_heartbeat(cfg, wt)
    guard(cfg, task_id)
    sha = task._git(wt, "rev-parse", "HEAD").stdout.strip()
    branch = task._git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if (lines := _unpushed(wt, branch, sha)) is not None:
        return WatchResult("unpushed", lines)
    if gh.pr_for_branch(cfg.root, branch) is None:
        # A merged or closed PR runs no CI for new commits: waiting would only time out.
        return WatchResult("no-pr", [
            f"{branch} has no open PR — no CI will run for {sha[:10]}",
            "next: h ship (opens a PR)",
        ])  # fmt: skip

    min_attempt: dict[int, int] = {}
    while True:
        try:
            runs, checks = _wait(cfg, sha, timeout_s, sleep, min_attempt, branch)
        except _Conflict:
            return WatchResult("conflict", [
                f"PR for {branch} conflicts with {cfg.base_branch} — GitHub starts no CI for it",
                (f"next: git fetch && git rebase origin/{cfg.base_branch}, resolve, "
                "then h review prepare → h ship"),
            ])  # fmt: skip
        stuck = [r for r in runs
                 if r["status"] == "waiting" or r.get("conclusion") in STUCK]  # fmt: skip
        if stuck:
            return WatchResult("blocked", [
                f"CI for {sha[:10]} did not test the code — no round counted",
                *(f"{r.get('workflowName', '?')}: {r.get('conclusion') or r['status']} "
                  f"({r['url']})" for r in stuck),
                ("next: action_required/waiting — ask the user to approve the run; "
                "startup_failure — fix the workflow file; stale — rerun it; then h ci watch"),
            ])  # fmt: skip
        pending = any(r["status"] != "completed" for r in runs) or any(
            c.get("bucket") == "pending" for c in checks
        )
        if not runs or pending:
            return WatchResult(
                "pending", [f"CI for {sha[:10]} not finished — run `h ci watch` again"]
            )
        cancelled = [r for r in runs if r.get("conclusion") == "cancelled"]
        if cancelled:
            return WatchResult("blocked", [
                f"CI for {sha[:10]} was cancelled and no newer run came — no round counted",
                *(f"{r.get('workflowName', '?')}: cancelled ({r['url']})" for r in cancelled),
                # a job cut off by its time limit: which step hung is in its logs
                *(f"step logs: gh run download {r['databaseId']} -n "
                  + gh.LOGS_ARTIFACT.format(attempt=int(r.get("attempt", 1)))
                  for r in cancelled),
                "next: " + "; ".join(f"gh run rerun {r['databaseId']}" for r in cancelled)
                + ", then h ci watch",
            ])  # fmt: skip
        failed = [r for r in runs if r.get("conclusion") not in OK]
        if not failed:
            red = [c for c in checks if c.get("bucket") in ("fail", "cancel")]
            if red:
                return WatchResult("blocked", [
                    (f"CI for {sha[:10]}: Actions green, checks from outside Actions red — "
                    "h can neither rerun them nor read their logs; no round counted"),
                    *(f"{c.get('name', '?')}: {c.get('state', '')} ({c.get('link', '')})"
                      for c in red),
                    "STOP: show the user these checks and ask how to proceed",
                ])  # fmt: skip
            return WatchResult("green", [f"CI green for {sha[:10]}"])
        ref_sha, state = _read(cfg, task_id)
        if sha not in state.reruns:
            for r in failed:
                gh.rerun_failed(cfg.root, r["databaseId"])
                min_attempt[r["databaseId"]] = int(r.get("attempt", 1)) + 1
            state.reruns.append(sha)
            _write(cfg, task_id, ref_sha, state)
            continue
        if sha not in state.rounds:
            state.rounds.append(sha)
            _write(cfg, task_id, ref_sha, state)
        n = len(state.rounds)
        log = wt / ART / f"round-{n}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        parts, notes = [], []
        for r in failed:
            parts.append(
                f"## {r.get('workflowName', '?')} — {r.get('name', '')} ({r['url']})\n"
            )
            # The 200-line budget is for the whole round, shared by the failed runs.
            text, note = _failed_log(cfg, wt, r, cap=max(20, CAP // len(failed)))
            parts.append(text)
            if note:
                notes.append(note)
        log.write_text("\n".join(parts), encoding="utf-8")
        rel = log.relative_to(wt).as_posix()
        lines = [f"CI red for {sha[:10]} after one rerun — round {n} of {MAX_ROUNDS}",
                 f"log: {rel} ({len(log.read_text(encoding='utf-8').splitlines())} lines)",
                 *notes]  # fmt: skip
        if n >= MAX_ROUNDS:
            number = gh.issue_number(task_id)
            if number is not None:
                try:
                    gh.issue_labels(cfg.root, number, add=["ci-blocked"])
                except gh.GhError as e:
                    lines.append(f"warning: label not set ({e})")
            lines.append("STOP: two real CI failures — ask the user how to proceed")
            return WatchResult("blocked", lines, rel)
        if len(notes) == len(failed) and all(n.startswith(UNAVAILABLE) for n in notes):
            ids = " ".join(str(r["databaseId"]) for r in failed)
            lines.append(f"next: nothing to triage — `gh run rerun {ids} --failed`, "
                         "then h ci watch; or show the user the run")  # fmt: skip
            return WatchResult("red", lines, rel)
        lines.append(
            f"next: spawn the `ci-triage` subagent with: worktree {wt.as_posix()} log {rel}"
        )
        return WatchResult("red", lines, rel)


UNAVAILABLE = "log unavailable"


def _failed_log(cfg: Config, wt: Path, run: dict, cap: int) -> tuple[str, str]:
    """(compressed log, note) for one failed run. GitHub can lose a job's log (BlobNotFound); then the step logs check.yml uploaded stand in. A note says which."""
    rid = run["databaseId"]
    try:
        text = gh.run_log_failed(cfg.root, rid)
        why = "empty"
    except gh.GhError as e:
        text, why = "", str(e)
    if text.strip():
        return compress(text, cap=cap), ""
    name = gh.LOGS_ARTIFACT.format(attempt=int(run.get("attempt", 1)))
    dest = wt / ART / f"run-{rid}-{name}"
    if dest.exists():
        shutil.rmtree(dest)  # ours, from an earlier watch: gh refuses to overwrite
    try:
        gh.run_download(cfg.root, rid, name, dest)
    except gh.GhError as e:
        return (f"{UNAVAILABLE}: {why}; step logs: {e}\n",
                f"{UNAVAILABLE} for run {rid}: {why}; no step logs artifact {name}")  # fmt: skip
    steps = _step_logs(dest)
    if not steps:
        return (f"{UNAVAILABLE}: {why}; artifact {name} has no step log\n",
                f"{UNAVAILABLE} for run {rid}: {why}; artifact {name} is empty")  # fmt: skip
    body = "\n".join(f"### {p.relative_to(dest).as_posix()}\n"
                     + p.read_text(encoding="utf-8", errors="replace") for p in steps)  # fmt: skip
    return (f"job log unavailable ({why}) — step logs from artifact {name}:\n"
            + compress(body, cap=cap),
            (f"job log unavailable for run {rid} — step logs from artifact {name} "
             f"({dest.relative_to(wt).as_posix()})"))  # fmt: skip


def _step_logs(dest: Path) -> list[Path]:
    """The failed steps' logs by the run's last.json; without one (a cancelled job never
    wrote it) every step log of the newest run."""
    try:
        data = json.loads((dest / "last.json").read_text(encoding="utf-8"))
        logs = [s.get("log", "") for s in data.get("steps", [])
                if isinstance(s, dict) and s.get("status") == "fail"]  # fmt: skip
    except (OSError, ValueError, AttributeError):
        logs = []
    prefix = check.ARTIFACTS.as_posix() + "/"
    # dict: steps of one stack can share a log
    found = list({dest / log.removeprefix(prefix): None for log in logs
                  if log.startswith(prefix)})  # fmt: skip
    # a `..` in a downloaded last.json must not pull a local file into the round log
    found = [p for p in found
             if p.resolve().is_relative_to(dest.resolve()) and p.is_file()]  # fmt: skip
    if found:
        return found
    runs = sorted((d for d in dest.iterdir() if d.is_dir()), key=lambda d: d.name)
    return sorted(runs[-1].glob("*.log")) if runs else []

"""`h task drop` — cancel a card cleanly.

Dropping is a business decision: the issue is closed as "not planned" with the reason and
who decided, never deleted. Local traces go (claim, worktree, local branch, ports, open PR);
the branch on origin stays, so the work can be picked up again.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field

from . import board, ci, claims, done, epics, gh, review, task
from .config import Config


class DropRefused(task.ClaimError):
    pass


@dataclass
class DropReport:
    task: str
    lines: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)


def _unpushed(cfg: Config, task_id: str) -> list[str]:
    """'<branch> <sha> <subject>' for commits of the task's local branches reachable from
    no branch on origin — also when the task branch was never pushed."""
    main = task.main_root(cfg)
    task._git(
        main, "fetch", "--quiet", claims.REMOTE
    )  # offline: stale refs only refuse more
    found = []
    for b in task._git(main, "branch", "--list", *task.branch_globs(cfg, task_id),
                       "--format=%(refname:short)").stdout.split():  # fmt: skip
        if task.merge_state(cfg, b) != "unmerged":
            continue  # its work is in base, even if origin deleted the merged branch
        log = task._ok(task._git(main, "log", "--format=%h %s", b, "--not",
                                 f"--remotes={claims.REMOTE}"),
                       f"git log {b} (checking for unpushed commits)")  # fmt: skip
        found += [f"{b} {line}" for line in log.splitlines()]
    return found


def drop(cfg: Config, task_id: str, reason: str, discard: bool = False) -> DropReport:
    if not reason.strip():
        raise DropRefused('a reason is required: --reason "…"')
    number = gh.issue_number(task_id)
    if number is None:
        raise DropRefused(
            f"{task_id} is not an issue-backed task; use `h task release` / `clean`"
        )
    rep = DropReport(task_id)
    main = task.main_root(cfg)
    try:
        os.chdir(main)  # never stand in the directory we are about to remove
    except OSError:
        pass
    issue = gh.issue_view(cfg.root, number)

    wt = task.worktree_path(cfg, task_id)
    if wt.exists() and (wt / ".git").is_file():
        changes = task._user_changes(cfg, wt)
        if changes and not discard:
            listed = "\n".join("  " + c for c in changes[:10])
            raise DropRefused(f"{task_id} has unsaved changes — they would be lost:\n{listed}\n"
                              "commit and push them first; throwing them away is the user's call (--discard)")  # fmt: skip
    if not discard and (unpushed := _unpushed(cfg, task_id)):
        listed = "\n".join("  " + c for c in unpushed[:10])
        more = f"\n  … and {len(unpushed) - 10} more" if len(unpushed) > 10 else ""
        raise DropRefused(f"{task_id} has commits that are not on origin — `branch -D` would lose them:\n"
                          f"{listed}{more}\npush them first; throwing them away is the user's call (--discard)")  # fmt: skip

    # Everything below is idempotent: a rerun after a manual fix finishes the job.
    for pr in gh.task_prs(cfg.root, task_id, task.branch_globs(cfg, task_id)):
        if pr["state"] == "OPEN":
            gh.pr_close(cfg.root, pr["url"], f"Task {task_id} dropped: {reason}")
            rep.lines.append(f"closed PR {pr['url']} (branch kept on origin)")

    try:
        ref = task.claim_spelling(cfg, task_id)  # GH-5 from an older h
        got = claims.read(cfg, ref)
        if got and claims.delete(cfg, ref, got[0]):
            rep.lines.append(
                "claim released" + (f" (as {ref})" if ref != task_id else "")
            )
        elif got:
            rep.lines.append("claim changed on origin meanwhile — not released")
    except claims.ClaimError as e:
        rep.manual.append(f"claim not released ({e}) — once origin is reachable, the user "
                          f"runs `h task release {task_id} --force`")  # fmt: skip
    ci.reset(cfg, task_id)
    review.reset_rounds(cfg, task_id)

    if wt.exists():
        if (wt / ".git").is_file():
            task._git(main, "worktree", "remove", "--force", str(wt))
        if wt.exists() and not task._remove_dir(wt):
            rep.manual.append(f"directory {wt.as_posix()} — {task._manual_rm(wt)}")
        else:
            rep.lines.append("worktree removed")
    task._git(main, "worktree", "prune")
    for b in task._git(main, "branch", "--list", *task.branch_globs(cfg, task_id),
                       "--format=%(refname:short)").stdout.split():  # fmt: skip
        task._git(main, "branch", "-D", b)
        rep.lines.append(f"local branch {b} deleted")
    task.free_ports(cfg, task_id)

    if issue.get("state", "OPEN").upper() != "CLOSED":
        who = task._git(main, "config", "user.email").stdout.strip() or "unknown"
        when = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
        gh.issue_close(cfg.root, number, f"Dropped: {reason}\n\nBy {who}, {when}, via `h task drop`.",
                       reason="not planned")  # fmt: skip
        rep.lines.append("issue closed as not planned")
    try:
        gh.issue_labels(cfg.root, number, add=["dropped"],
                        remove=[x for x in done.LIFECYCLE if x != "dropped"])  # fmt: skip
    except gh.GhError as e:
        rep.lines.append(f"warning: labels not updated ({e})")
    if warn := board.move(cfg, issue["url"], "Dropped"):
        rep.lines.append(warn)

    slug = (issue.get("milestone") or {}).get("title")
    if slug:
        try:
            url = epics.publish_sync(cfg, slug)
            rep.lines.append(f"epic {slug}: " + (f"updated via {url} (auto-merge)" if url
                                                 else "already up to date"))  # fmt: skip
        except (gh.GhError, task.ClaimError, ValueError) as e:
            rep.lines.append(f"warning: epic {slug} not updated ({e})")
    return rep

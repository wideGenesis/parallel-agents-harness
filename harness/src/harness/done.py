"""`h task done` — close a task only when it is deployed and nothing is left behind.

Gates in order; each one that is not met stops the run, says why, and leaves the issue open:

    merged → deployed → cleaned → verified clean → close issue, board Done, epic [x]

Every step is idempotent, so after a fix (a manual removal, a finished deploy) the same
command continues from where it stopped. Display updates (labels, board) never fail the run.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import board, ci, claims, epics, gh, init, review, task
from .config import Config

LIFECYCLE = ["draft", "needs-clarification", "ready", "in-progress", "review",
             "merged", "deployed", "cleanup-blocked", "ci-blocked"]  # fmt: skip


@dataclass
class DoneReport:
    task: str
    closed: bool = False
    stage: str = (
        ""  # where it stopped: not-merged | deploy-* | cleanup-blocked | closed
    )
    lines: list[str] = field(default_factory=list)  # what happened, one line each
    manual: list[str] = field(default_factory=list)  # what a human must do


def _label(
    cfg: Config, number: int, stage_label: str, rep: DoneReport, url: str
) -> None:
    """stage_label is a lifecycle label, or "done" — which clears them all."""
    add = [] if stage_label == "done" else [stage_label]
    try:
        gh.issue_labels(cfg.root, number, add=add,
                        remove=[x for x in LIFECYCLE if x != stage_label])  # fmt: skip
    except gh.GhError as e:
        rep.lines.append(f"warning: labels not updated ({e})")
    if warn := board.move(cfg, url, board.LABEL_STAGE.get(stage_label, "Done")):
        rep.lines.append(warn)


def _wait_deploy(
    cfg: Config, sha: str, timeout_s: float, sleep: Callable[[float], None]
) -> dict | None:
    deadline = time.monotonic() + timeout_s
    while True:
        run = deploy_run(cfg, sha)
        if (
            run
            and run.get("status") == "completed"
            and run.get("conclusion") != "cancelled"
        ):
            return run
        if time.monotonic() >= deadline:
            return run
        sleep(15)


def deploy_run(cfg: Config, sha: str) -> dict | None:
    """The run that proves `sha` deployed: its own, or — when that one was cancelled
    because newer pushes to the base superseded it (one pending run per concurrency group) —
    the newest base-branch run whose commit contains `sha`. Seen 2026-10-23: with another
    session merging, every intermediate run on main was cancelled and `done` could never
    close."""
    own = gh.workflow_run(cfg.root, cfg.deploy_workflow, sha)
    if own and own.get("conclusion") != "cancelled":
        return own
    if (
        own
        and own.get("databaseId")
        and "failure" in gh.run_job_conclusions(cfg.root, own["databaseId"])
    ):
        # A job hit its time limit (or someone cancelled a running run): the jobs ran
        # and a gate failed, so nothing newer will vouch for it — report it now.
        # A run superseded in the queue never starts a job.
        return {**own, "conclusion": "failure", "stopped": True}
    main = task.main_root(cfg)
    # The later run's commit must be known locally for the ancestry check.
    task._git(main, "fetch", "--quiet", claims.REMOTE, cfg.base_branch)
    for r in gh.workflow_runs_on(cfg.root, cfg.deploy_workflow, cfg.base_branch):
        head = r.get("headSha", "")
        if not head or head.startswith(sha) or r.get("conclusion") == "cancelled":
            continue
        contains = (
            task._git(main, "merge-base", "--is-ancestor", sha, head).returncode == 0
        )
        if contains and not _tested_nothing(cfg, r):
            # The own run's id: rerunning it tests this merge alone.
            return {**r, "superseded": head, "own_id": (own or {}).get("databaseId")}
    return own


def _tested_nothing(cfg: Config, run: dict) -> bool:
    """A run that skipped its check holds the commit but never tested it: it is
    no verdict for an earlier merge. Unreadable jobs vouch for nothing either."""
    rid = run.get("databaseId")
    if not rid:
        return False
    try:
        return gh.run_skipped_check(cfg.root, rid)
    except gh.GhError:
        return True


def leftovers(cfg: Config, task_id: str, branch: str) -> list[str]:
    """Independent re-check after cleanup: trust the state, not the cleanup's report."""
    main = task.main_root(cfg)
    found = []
    wt = task.worktree_path(cfg, task_id)
    if wt.exists():
        found.append(f"directory {wt.as_posix()} — {task._manual_rm(wt)}")
    local = task._git(main, "branch", "--list", *task.branch_globs(cfg, task_id),
                      "--format=%(refname:short)").stdout.split()  # fmt: skip
    found += [f"local branch {b} — git branch -D {b}" for b in local]
    remote = task._git(
        main, "ls-remote", "--heads", claims.REMOTE, branch
    ).stdout.strip()
    if remote:
        found.append(f"remote branch {branch} — git push origin --delete {branch}")
    if claims.read(cfg, task_id) is not None:
        found.append(f"claim refs/claims/{task_id} — the user runs "
                     f"`h task release {task_id} --force`")  # fmt: skip
    if task.has_ports(cfg, task_id):
        found.append(f"port reservation of {task_id} — h task clean {task_id}")
    return found


def done(
    cfg: Config,
    task_id: str,
    timeout_s: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> DoneReport:
    rep = DoneReport(task_id)
    main = task.main_root(cfg)
    # A process cannot delete the directory it stands in (Windows): step out first.
    try:
        os.chdir(main)
    except OSError:
        pass

    number = gh.issue_number(task_id)
    if number is None:  # not issue-backed: nothing to close, only clean up
        items = task.clean(cfg, only=task_id)
        rep.lines += [f"{i.task}: {i.action}" for i in items]
        rep.manual += [i.manual for i in items if i.manual]
        rep.closed, rep.stage = (
            not rep.manual,
            "cleaned" if not rep.manual else "cleanup-blocked",
        )
        return rep

    issue = gh.issue_view(cfg.root, number)
    url = issue["url"]
    if issue.get("state", "OPEN").upper() == "CLOSED":
        rep.closed, rep.stage = True, "closed"
        rep.lines.append("issue already closed")
        return rep

    pr = gh.merged_pr(cfg.root, task_id, task.branch_globs(cfg, task_id))
    if pr is None or pr["state"] != "MERGED":
        rep.stage = "not-merged"
        rep.lines.append(
            f"PR not merged yet: {pr['url'] if pr else 'no PR for task/' + task_id}"
        )
        return rep
    sha = (pr.get("mergeCommit") or {}).get("oid", "")
    branch = pr["headRefName"]
    rep.lines.append(f"merged: {pr['url']} ({sha[:10]})")
    _label(cfg, number, "merged", rep, url)

    if not cfg.deploy_workflow and (cfg.root / init.CI_WORKFLOW).is_file():
        # The CI `h init` installs runs the heavy tier on main: its green run on the merge
        # commit is what "deployed" means without a real deploy.
        cfg.deploy_workflow = Path(init.CI_WORKFLOW).name
    if not cfg.deploy_workflow:
        rep.stage = "deploy-unconfigured"
        rep.manual.append('set [deploy] workflow = "<file>.yml" in harness.toml')
        return rep
    run = _wait_deploy(
        cfg, sha, cfg.deploy_timeout_min * 60 if timeout_s is None else timeout_s, sleep
    )
    if (
        run is None
        or run.get("status") != "completed"
        or run.get("conclusion") == "cancelled"
    ):
        # Cancelled with nothing newer to vouch for it yet: wait, it is not a failure.
        rep.stage = "deploy-pending"
        where = (
            run["url"] if run else f"no {cfg.deploy_workflow} run for {sha[:10]} yet"
        )
        rep.lines.append(
            f"deploy not finished: {where} — run `h task done {task_id}` again"
        )
        return rep
    if run.get("conclusion") != "success":
        rep.stage = "deploy-failed"
        how = (
            " (cancelled after a job failed: time limit?)" if run.get("stopped") else ""
        )
        rep.lines.append(f"deploy {run.get('conclusion')}{how}: {run['url']}")
        rid = run.get("databaseId")
        try:
            lost = bool(rid) and gh.run_lost_runner(cfg.root, rid)
        except gh.GhError:  # no job details: judge the run as before
            lost = False
        if lost:
            # Not the code: a job failed with no failed step.
            rep.lines.append(
                "no step failed — the runner went offline mid-job, not main red"
            )
            rep.manual.append(
                f"`gh run rerun {rid} --failed`, then `h task done {task_id}`"
            )
        elif run.get("stopped"):
            # Cut off by the time limit or by hand: no check judged the commit.
            rep.lines.append("the run was cancelled before a verdict, not main red")
            rep.manual.append(
                f"`gh run rerun {rid} --failed`, then `h task done {task_id}`; timed out "
                "again — raise [ci] timeout-minutes"
            )
        elif run.get("superseded"):
            # A newer run holds later merges too: it does not name this PR.
            rep.lines.append(f"the red run is on {run['superseded'][:10]}, which holds later "
                             "merges — it does not show this PR is the cause")  # fmt: skip
            own = run.get("own_id")
            rep.manual.append(
                f"rerun this merge's own run: `gh run rerun {own}`, then `h task done {task_id}`"
                if own
                else f"find which merge broke main before reverting {sha[:10]}"
            )
        else:
            # Heavy checks run after the merge: red main is reverted first, fixed after.
            rep.manual.append(f"main is red after the merge — revert PR for the user: "
                              f"`h revert {sha[:10]} --run {run['url']}`")  # fmt: skip
        return rep
    via = (
        f" (superseded run on {run['superseded'][:10]} contains it)"
        if run.get("superseded")
        else ""
    )
    rep.lines.append(f"deployed: {run['url']}{via}")
    if not run.get("superseded") and run.get("databaseId"):
        try:
            state = gh.run_check_state(cfg.root, run["databaseId"])
            if state == "skipped":
                rep.lines.append("check skipped: no code changed — main's verdict is "
                                 "the last run that checked code")  # fmt: skip
            elif state == "reused":
                rep.lines.append("check reused: main took the PR's green check of the "
                                 "same tree")  # fmt: skip
        except gh.GhError:
            pass  # only a note: the run is green either way
    _label(cfg, number, "deployed", rep, url)

    items = task.clean(
        cfg, only=task_id, merged_pr=(pr.get("headRefOid", ""), pr.get("number"))
    )
    rep.lines += [f"cleanup {i.task}: {i.action}" for i in items]
    task._git(main, "push", "--quiet", claims.REMOTE, "--delete", branch)  # ok if gone
    ci.reset(cfg, task_id)  # the CI round counter goes with the task
    review.reset_rounds(cfg, task_id)
    left = leftovers(cfg, task_id, branch)
    if left:
        rep.stage = "cleanup-blocked"
        rep.manual += left
        try:
            gh.issue_labels(cfg.root, number, add=["cleanup-blocked"])
        except gh.GhError as e:
            rep.lines.append(f"warning: label not set ({e})")
        return rep

    report = "\n".join([
        "Closed by `h task done`: deployed and clean.", "",
        f"- PR: {pr['url']}", f"- Deploy: {run['url']}",
        f"- Removed: worktree, branch `{branch}` (local + remote), claim, ports",
    ])  # fmt: skip
    gh.issue_close(cfg.root, number, report)
    _label(cfg, number, "done", rep, url)
    rep.closed, rep.stage = True, "closed"
    rep.lines.append("issue closed")

    slug = (issue.get("milestone") or {}).get("title")
    if slug:
        try:
            pr_url = epics.publish_sync(cfg, slug)
            rep.lines.append(f"epic {slug}: " + (f"[x] via {pr_url} (auto-merge)" if pr_url
                                                 else "already up to date"))  # fmt: skip
        except (gh.GhError, task.ClaimError, ValueError) as e:
            rep.lines.append(
                f"warning: epic {slug} not updated ({e}) — `h epic sync {slug} --publish`"
            )
    return rep

"""`h ship` — from a task worktree to an open PR that carries its own evidence.

Every step is deterministic, so the agent spends no judgement on it (SUMMARY #3):
guards → `h check` on affected packages → heartbeat (refuse if the task is no longer ours)
→ push → create or update the PR. The PR body holds the check results, so a reviewer sees
what was run instead of taking the agent's word for it (SUMMARY: "prove, don't claim").
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import (
    affected,
    board,
    check,
    ci,
    claims,
    fmt,
    git,
    installed,
    policy,
    proc,
    progress,
    prreview,
    ratchet,
    review,
    stacks,
    task,
    tools,
)
from . import gh as github
from .claims import ClaimError
from .config import CONFIG_NAME, Config


@dataclass
class Shipped:
    task: str
    branch: str
    url: str
    created: bool
    results: list[check.StepResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    policy: policy.Decision | None = None


class ShipRefused(ClaimError):
    """A guard stopped the ship; `results` is set when the check failed."""

    def __init__(self, msg: str, results: list[check.StepResult] | None = None):
        super().__init__(msg)
        self.results = results or []


def _git(cwd: Path, *argv: str) -> subprocess.CompletedProcess:
    return git.run(cwd, *argv)


def _lease(wt: Path, branch: str) -> list[str]:
    """Force-push flags that allow our own rebase but never clobber a commit someone else
    pushed (a takeover, a second session). The branch's tracking ref is fetched first, and
    `--force-if-includes` then requires its tip in our reflog — so a commit we never had
    refuses the push. A branch not on origin yet must still not exist when we push."""
    tracking = f"refs/remotes/{claims.REMOTE}/{branch}"
    on_origin = _git(wt, "fetch", "--quiet", claims.REMOTE,
                     f"+refs/heads/{branch}:{tracking}").returncode == 0  # fmt: skip
    if not on_origin:
        return [f"--force-with-lease={branch}:"]
    return [f"--force-with-lease={branch}", "--force-if-includes"]


def _commits(wt: Path, base: str, head: str = "HEAD") -> list[str]:
    out = _git(wt, "log", "--reverse", "--format=%s (%h)", f"{base}..{head}").stdout
    return [c for c in out.splitlines() if c]


def _pr_title(commit_line: str) -> str:
    return commit_line.rsplit(" (", 1)[0]


def _sync_title(
    wt: Path, branch: str, base: str, commits: list[str], title: str | None
) -> str | None:
    """Before the push: a check reading the title from the event of that push sees this
    one, and a rerun reads the same event. A title taken from the first commit
    follows it; a chosen one stays until `--title`. Returns the note for the output.
    A failed lookup is left to the one after the push, which reports it."""
    if not tools.usable("gh"):
        return None
    view = gh(wt, "pr", "view", branch, "--json", "title,state")
    if view.returncode != 0:
        return None
    pr = json.loads(view.stdout)
    if pr.get("state", "OPEN") != "OPEN":
        return None
    now, first = pr.get("title", ""), _pr_title(commits[0])
    pushed = _commits(wt, base, f"{claims.REMOTE}/{branch}")
    if title:
        want = title
    elif now == first:
        return None
    elif pushed and now == _pr_title(pushed[0]):
        want = first
    else:
        return (f"PR title kept, it differs from the first commit: {now} — "
                "`h ship --title` changes it")  # fmt: skip
    r = gh(wt, "pr", "edit", branch, "--title", want)
    if r.returncode != 0:
        raise ShipRefused(
            f"PR title not updated, nothing pushed: {(r.stderr or r.stdout).strip()}"
        )
    return f"PR retitled: {want}"


def _rebase_if_clean(cfg: Config, wt: Path, branch: str, base: str) -> int:
    """Bring the branch onto `base` when git can do it alone; returns how many base
    commits came in (0: already up to date). Conflicts, merge commits or a failed rebase
    refuse with the branch as it was — except a conflict only in the ratchet base, which
    has one right answer: the recount."""
    if _git(wt, "merge-base", "--is-ancestor", base, "HEAD").returncode == 0:
        return 0
    behind = int(_git(wt, "rev-list", "--count", f"HEAD..{base}").stdout.strip() or 0)
    how = f"bring it up to date, then ship again:\n  git rebase {base}"
    if _git(wt, "rev-list", "--merges", f"{base}..HEAD").stdout.strip():
        raise ShipRefused(f"{branch} is {behind} commit(s) behind {base} and has merge "
                          f"commits — {how}")  # fmt: skip
    try:
        conflicts = review._conflicts(wt, base)
    except review.ReviewError as e:
        raise ShipRefused(str(e)) from e
    if conflicts and conflicts != [RATCHET]:
        listed = "\n".join(f"  {n}" for n in conflicts[:10])
        raise ShipRefused(f"{branch} is {behind} commit(s) behind {base} and conflicts "
                          f"with it in:\n{listed}\n{how}")  # fmt: skip
    steps = len(_commits(wt, base)) + 1  # each commit stops at most once
    r = _git(wt, "rebase", "--quiet", base)
    for _ in range(steps):
        if r.returncode == 0:
            return behind
        step = _recount_conflict(cfg, wt)
        if step is None:
            break
        r = _git(wt, "rebase", step)
    _git(wt, "rebase", "--abort")
    raise ShipRefused(f"git rebase {base} failed, branch left as it was: "
                      f"{(r.stderr or r.stdout).strip()[:300]}")  # fmt: skip


RATCHET = ratchet.BASE.as_posix()


def _recount_conflict(cfg: Config, wt: Path) -> str | None:
    """A rebase stopped on a conflict only in the ratchet base: write the recount, commit
    the step as the original commit and return how the rebase goes on. None when anything
    else conflicts."""
    unmerged = _git(wt, "diff", "--name-only", "--diff-filter=U").stdout.split()
    if unmerged != [RATCHET]:
        return None

    def side(stage: int) -> dict[str, int]:
        r = _git(wt, "show", f":{stage}:{RATCHET}")
        return ratchet.parse(r.stdout) if r.returncode == 0 else {}

    try:  # stages: 1 the commit's parent, 2 the base we replay onto, 3 the commit
        ratchet.resolve(cfg, side(2), side(1), side(3))
    except ValueError:
        return None
    _git(wt, "add", RATCHET)
    if _git(wt, "diff", "--cached", "--quiet").returncode == 0:
        return (
            "--skip"  # the base already holds the recount: nothing left of the commit
        )
    # Not `rebase --continue` straight away: it may open an editor, and h has no terminal.
    c = _git(wt, "commit", "--quiet", "--no-verify", "-C", "REBASE_HEAD")
    return "--continue" if c.returncode == 0 else None


def gh(cwd: Path, *argv: str) -> subprocess.CompletedProcess:
    """Single seam to GitHub; tests replace it."""
    return proc.run(["gh", *argv], cwd)


STYLE_MESSAGE = "style: h fmt before ship"


def _autoformat(cfg: Config, wt: Path, files: list[str]) -> str | None:
    """The Stripe pattern: a deterministic format pass before push, so formatting never
    costs a CI round or an agent turn. Edits made through a shell skip the PostToolUse
    hook — this is where they get caught. Returns the commit line if one was made.

    The tree is known clean here (guard above), so every change after the pass is ours.
    """
    for rel in files:
        path = wt / rel
        if path.is_file():
            r = fmt.format_file(cfg, path)
            if r.status == "failed":
                raise ShipRefused(f"formatter rejected {r.file}:\n{r.detail}")
    if not _git(wt, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return None
    _git(wt, "add", "--update")
    c = _git(wt, "commit", "--quiet", "--no-verify", "-m", STYLE_MESSAGE)
    if c.returncode != 0:
        raise ShipRefused(
            f"could not commit formatting: {(c.stderr or c.stdout).strip()}"
        )
    sha = _git(wt, "rev-parse", "--short", "HEAD").stdout.strip()
    return f"{STYLE_MESSAGE} ({sha})"


RATCHET_MESSAGE = "chore: lower ratchet base"


def _lower_ratchet(cfg: Config, wt: Path) -> str | None:
    """A counter that went down is committed as the new base right away: left
    uncommitted, the old base lets the next change bring the suppressions back unseen.
    Growth is not ours — `h check` reports it red. Returns the commit line if one was made.
    """
    if not (wt / ratchet.BASE).is_file():
        return None
    before = ratchet.load_base(cfg) or {}
    counts = ratchet.count(ratchet.scan(cfg))
    if counts == before or not ratchet.lowers(before, counts):
        return None
    ratchet.write_base(cfg, counts)
    _git(wt, "add", "--", RATCHET)
    c = _git(wt, "commit", "--quiet", "--no-verify", "-m", RATCHET_MESSAGE)
    if c.returncode != 0:
        raise ShipRefused(
            f"could not commit the ratchet base: {(c.stderr or c.stdout).strip()}"
        )
    sha = _git(wt, "rev-parse", "--short", "HEAD").stdout.strip()
    return f"{RATCHET_MESSAGE} ({sha})"


def evidence(
    results: list[check.StepResult], impact: dict[str, affected.StackImpact]
) -> str:
    rows = ["| stack | step | result | time |", "| --- | --- | --- | --- |"]
    for r in results:
        note = r.step.reason if r.status == "skip" and not r.step.disabled else ""
        status = {"ok": "✅ ok", "fail": "❌ fail", "skip": "⏭ skip"}[r.status]
        if r.step.disabled:
            status = f"🚫 {check.DISABLED}"
        rows.append(
            f"| {r.step.stack} | {r.step.kind} | {status}{' — ' + note if note else ''} "
            f"| {r.seconds:.1f}s |"
        )
    scope = []
    for name, imp in impact.items():
        if imp.all:
            scope.append(f"{name}: all ({imp.reason})")
        elif imp.affected:
            scope.append(f"{name}: {', '.join(sorted(imp.affected))}")
    return "\n".join(rows) + "\n\nAffected: " + ("; ".join(scope) or "nothing") + "\n"


def body(
    cfg: Config,
    task_id: str,
    commits: list[str],
    proof: str,
    review_md: str = "",
    adr_candidates: list[str] | None = None,
) -> str:
    parts = [f"Task: `{task_id}`"]
    if m := github.ISSUE_ID.match(task_id):
        # Not "Closes": a task is done after deploy, and `h task done` closes the issue.
        parts.append(f"Refs #{m.group(1)}")
    parts += ["", "## Evidence — `h check` (affected)", "", proof]
    if review_md:
        parts += [review_md]
    if adr_candidates:  # `h progress note --adr` — decisions with a trade-off
        parts += ["## ADR candidates", "",
                  ("Decisions with a trade-off noted during the task; each may deserve an ADR "
                  "by the project's template."), "",
                  *[f"- {c}" for c in adr_candidates], ""]  # fmt: skip
    parts += ["## Commits", ""]
    parts += [f"- {c}" for c in commits]
    if cfg.ship_footer:
        parts += ["", cfg.ship_footer]
    return "\n".join(parts) + "\n"


def ship(
    cfg: Config, start: Path, title: str | None = None, draft: bool = False
) -> Shipped:
    found = task.current_task(start)
    if found is None:
        raise ShipRefused("not inside a task worktree — `h task start <ID>` first")
    wt, task_id = found
    # the gates below are only as good as the h that runs them; unknown passes.
    own = installed.check()
    if own.stale:
        raise ShipRefused(own.message())
    branch = json.loads((wt / task.TASK_FILE).read_text(encoding="utf-8"))["branch"]
    base = f"{claims.REMOTE}/{cfg.base_branch}"

    dirty = task._user_changes(cfg, wt)
    if dirty:
        listed = "\n".join("  " + ln for ln in dirty[:5])
        raise ShipRefused(f"uncommitted changes — commit them first:\n{listed}")
    fetched = _git(wt, "fetch", "--quiet", claims.REMOTE, cfg.base_branch)
    if fetched.returncode != 0:  # a stale base would pass the up-to-date check below
        raise ShipRefused(f"git fetch {base} failed: {fetched.stderr.strip()}")
    if not _commits(wt, base):
        raise ShipRefused(f"nothing to ship: no commits on {branch} beyond {base}")
    # Checks and review on an old base prove little, and the PR would arrive behind or
    # conflicting. A clean rebase is ship's own: git merges with no hand edits,
    # and the review gate checks the result against the approved commit. A
    # conflict resolution can silently drop edits, so that stays the agent's.
    pulled = _rebase_if_clean(cfg, wt, branch, base)
    commits_list = _commits(wt, base)  # after the rebase: the hashes that get pushed

    # Before autoformat: the format commit is made by a tool and needs no review, but it
    # moves HEAD, so the verdict is matched against the commits the reviewer saw.
    decision = policy.for_worktree(
        cfg, wt
    )  # who reviews and merges, by the files touched
    try:
        ci.guard(
            cfg, task_id
        )  # two real CI failures: the user decides, not another push
        verdict = review.gate(cfg, wt, decision.review)
        if verdict is not None:
            review.record_verdict(cfg, task_id, verdict)
    except (review.ReviewError, ci.CiBlocked) as e:
        why = f" (policy: {decision.review_reason})" if decision.review_reason else ""
        raise ShipRefused(f"{e}{why}") from e

    files = affected.changed_files(cfg, affected.resolve_base(cfg, None))
    style_commit = _autoformat(cfg, wt, files)
    if style_commit:
        commits_list.append(style_commit)
    try:
        ratchet_commit = _lower_ratchet(cfg, wt)
    except ValueError as e:  # an unreadable base: h check below names it
        raise ShipRefused(str(e)) from e
    if ratchet_commit:
        commits_list.append(ratchet_commit)

    found_stacks = stacks.detect(cfg)
    impact = affected.compute(cfg, found_stacks, files)
    steps = check.plan(cfg, found_stacks, impact, check.FAST + check.SLOW, files=files)
    reused = check.reusable(cfg, steps)  # the green run review prepare just made
    if reused is not None:
        results, rec = reused
        reused_line = (f"h check: reused the green run of {rec.get('at')} on "
                       f"`{str(rec.get('head'))[:10]}` — nothing changed since")  # fmt: skip
    else:
        before = check.snapshot(cfg)
        results = check.run(cfg, steps)
        check.record(cfg, before, results, full=True)
        reused_line = ""
    if any(r.status == "fail" for r in results):
        raise ShipRefused("h check failed — fix before shipping", results)
    # The user disables a missed step on purpose in harness.toml, or the tool gets installed.
    missed = [r for r in results if r.blocks_ship]
    if missed:
        listed = "\n".join(
            f"  - {r.step.stack} {r.step.kind}: {r.step.reason} — or, on purpose: "
            f"{r.disable_line}"
            for r in missed
        )
        raise ShipRefused(f"check steps did not run — make them run, or disable "
                          f"the step in harness.toml:\n{listed}", results)  # fmt: skip

    try:
        task.heartbeat(cfg, task_id, wt)
    except task.TakenOver:
        raise  # the task is someone else's now
    except task.ClaimError as e:  # PushFailed, or origin not readable: not a takeover
        raise ShipRefused(
            f"no connection to origin — retry when it is back ({e})"
        ) from e

    lease = _lease(wt, branch)  # fetches the branch: _sync_title reads what is pushed
    title_note = _sync_title(wt, branch, base, commits_list, title)
    push = _git(wt, "push", "--quiet", *lease, "-u", claims.REMOTE,
                f"HEAD:{branch}")  # fmt: skip
    if push.returncode != 0:
        raise ShipRefused(f"git push failed: {push.stderr.strip()}")

    if not tools.usable("gh"):
        raise ShipRefused(
            "branch pushed, but `gh` is not installed — open the PR by hand"
        )
    pr_body = body(
        cfg,
        task_id,
        commits_list,
        (f"{reused_line}\n\n" if reused_line else "") + evidence(results, impact),
        review.pr_section(verdict),
        progress.adr_candidates(wt),
    )
    # Only an open PR takes new commits: a merged or closed one is history, and editing
    # it starts no CI — the branch gets a new PR instead.
    existing = gh(wt, "pr", "view", branch, "--json", "url,state,number")
    err = (existing.stderr or existing.stdout).strip()
    if existing.returncode != 0 and "no pull requests found" not in err:
        # an unreachable GitHub is not "no PR" — create would fail or duplicate.
        raise ShipRefused(f"branch pushed, but the PR lookup failed: {err}")
    pr = json.loads(existing.stdout) if existing.returncode == 0 else {}
    prior = ""
    if pr and pr.get("state", "OPEN") != "OPEN":
        prior = f"PR #{pr.get('number')} of this branch is {pr['state'].lower()}"
    if pr and not prior:
        url = pr["url"]
        r = gh(wt, "pr", "edit", branch, "--body", pr_body)
        created = False
    else:
        pr_title = title or _pr_title(commits_list[0])
        args = ["pr", "create", "--head", branch, "--base", cfg.base_branch,
                "--title", pr_title, "--body", pr_body]  # fmt: skip
        r = gh(wt, *args, *(["--draft"] if draft else []))
        url = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        created = True
    if r.returncode != 0:
        raise ShipRefused(
            f"branch pushed, but gh failed: {(r.stderr or r.stdout).strip()}"
        )
    shipped = Shipped(task_id, branch, url, created, results, policy=decision)
    if reused_line:
        shipped.notes.append(reused_line)
    if prior:
        shipped.notes.append(f"{prior} — opened a new one")
    if title_note:
        shipped.notes.append(title_note)
    if pulled:
        shipped.notes.append(f"rebased onto {base} (+{pulled} commit(s))")
    if verdict is not None and verdict.carried:
        shipped.notes.append(
            f"approval of {verdict.head[:10]} carried over a clean rebase"
        )
    pushed = _git(wt, "rev-parse", "HEAD").stdout.strip()
    if url:
        # the gate's outcome as the required check `harness/review` on this sha.
        why = (f"approved by {review.REVIEWER}, round {verdict.round}" if verdict
               else "review not required (harness.toml)")  # fmt: skip
        try:
            prreview.publish_status(cfg, pushed, why, url)
        except github.GhError as e:
            shipped.notes.append(f"warning: review status not set ({e})")
    if verdict is not None and url:
        # the verdict where people, CI and project gates can see it.
        try:
            how = prreview.publish(cfg, url, verdict, pushed)
            shipped.notes.append(f"review verdict {how} in the PR")
        except (github.GhError, ValueError) as e:
            shipped.notes.append(f"warning: review verdict not published ({e})")
    if CONFIG_NAME in files:  # on any edit of the file
        shipped.notes.append("harness.toml changed: base rules apply")
    if (number := github.issue_number(task_id)) is not None:
        # Display only: label and board follow the PR; failures are warnings.
        try:
            github.issue_labels(wt, number, add=["review"], remove=["in-progress"])
        except github.GhError as e:
            shipped.notes.append(f"warning: labels not updated ({e})")
        repo_url = url.split("/pull/")[0] if "/pull/" in url else ""
        if repo_url:
            warn = board.move(cfg, f"{repo_url}/issues/{number}", "Review")
            if warn:
                shipped.notes.append(warn)
    return shipped

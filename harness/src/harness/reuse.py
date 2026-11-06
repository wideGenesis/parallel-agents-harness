"""`h ci reused` — main takes a PR's green check of the same tree instead of running it
again.

The PR run checks GitHub's merge commit, so when main does not move between that check and
the squash merge, main's new tree is the tree the PR checked. The PR's check job records it
as the artifact `h-checked-<tree>`; the push run looks it up and skips its own check when
all of these hold:

  1. the push commit is the merge commit of a PR (GitHub's list of PRs for a commit);
  2. an artifact for main's tree comes from a `pull_request` run of the same workflow file,
     for that PR's head commit, from this repository (not a fork), whose check job passed —
     bound to the merged head, a branch cannot plant the tree of another PR;
  3. main's last verdict is green: the PR checked only what it touched, the rest is vouched
     for by the last main run that checked code (a docs-only skip is walked past).

[check] heavy steps never ran on the PR, so main still runs those (`h check --tier heavy`).
Anything else — main moved, no artifact, a failed lookup — is a full run, as before.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any

from . import check, gh, stacks, task
from .config import Config

ARTIFACT = "h-checked-{tree}"
WALK = 10  # main commits looked back for the last run that checked code


@dataclass
class Decision:
    reuse: bool
    reason: str
    run: str = ""  # the PR run that vouches for the tree
    pr: int | None = None
    heavy: bool = False  # [check] heavy steps still run on main

    def as_dict(self) -> dict:
        return asdict(self)


def decide(cfg: Config, sha: str, before: str, repo: str = "",
           workflow: str = "") -> Decision:  # fmt: skip
    repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
    workflow = workflow or _workflow_path(
        repo, os.environ.get("GITHUB_WORKFLOW_REF", "")
    )
    if not repo or not workflow:
        return Decision(False, "not in GitHub Actions: checking everything")
    try:
        return _decide(cfg, repo, workflow, sha, before)
    except (gh.GhError, ValueError, KeyError, TypeError) as e:
        # Never a skip on doubt: an unreadable answer checks everything.
        return Decision(False, f"reuse lookup failed, checking everything: {e}")


def _decide(cfg: Config, repo: str, workflow: str, sha: str, before: str) -> Decision:
    tree = _git(cfg, "rev-parse", f"{sha}^{{tree}}")
    pulls = _api(cfg, f"repos/{repo}/commits/{sha}/pulls")
    pr = next((p for p in pulls
               if p.get("merge_commit_sha") == sha and p.get("merged_at")), None)  # fmt: skip
    if pr is None:
        return Decision(
            False, f"{sha[:10]} is not the merge commit of a PR: checking everything"
        )
    number, head = int(pr["number"]), pr["head"]["sha"]
    name = ARTIFACT.format(tree=tree)
    arts = _api(cfg, f"repos/{repo}/actions/artifacts?name={name}&per_page=30")[
        "artifacts"
    ]
    vouch = None
    for a in sorted(arts, key=lambda a: a.get("created_at", ""), reverse=True):
        wr = a.get("workflow_run") or {}
        if a.get("expired") or wr.get("head_sha") != head:
            continue
        if wr.get("head_repository_id") != wr.get("repository_id"):
            continue  # a fork's run: it chose its own workflow file
        run = _api(cfg, f"repos/{repo}/actions/runs/{wr['id']}")
        if run.get("event") != "pull_request" or _bare(run.get("path", "")) != workflow:
            continue
        if _job(cfg, repo, wr["id"], gh.CHECK_JOB) == "success":
            vouch = run
            break
    if vouch is None:
        return Decision(False, f"no green check of PR #{number} on tree {tree[:10]} — main "
                               "moved since, or the PR's check is gone: checking everything",
                        pr=number)  # fmt: skip
    if why := _previous_verdict(cfg, repo, workflow, before):
        return Decision(False, why, pr=number)
    url = vouch.get("html_url", "")
    return Decision(True, f"tree {tree[:10]} was checked green by PR #{number}: {url}",
                    run=url, pr=number, heavy=_heavy(cfg))  # fmt: skip


def _previous_verdict(cfg: Config, repo: str, workflow: str, before: str) -> str:
    """ "" when main's last verdict is green, else why it is not."""
    c = before
    for _ in range(WALK):
        runs = [r for r in _api(cfg, f"repos/{repo}/actions/runs?head_sha={c}"
                                     "&event=push&per_page=20")["workflow_runs"]
                if _bare(r.get("path", "")) == workflow]  # fmt: skip
        if not runs:
            return f"main's previous commit {c[:10]} has no run: checking everything"
        r = max(runs, key=lambda r: r.get("created_at", ""))
        if r.get("status") != "completed":
            return (
                f"main's previous run ({c[:10]}) has not finished: checking everything"
            )
        if r.get("conclusion") != "success":
            return (f"main's last verdict ({c[:10]}) is {r.get('conclusion')}, not green: "
                    "checking everything")  # fmt: skip
        jobs = _api(cfg, f"repos/{repo}/actions/runs/{r['id']}/jobs?per_page=50")[
            "jobs"
        ]
        if gh.check_state(jobs) != "skipped":
            return ""  # checked, or reused from a PR — both green verdicts
        c = _git(cfg, "rev-parse", f"{c}^")  # docs only: its parent holds the verdict
    return (
        f"no main run that checked code in the last {WALK} commits: checking everything"
    )


def _heavy(cfg: Config) -> bool:
    """A [check] heavy step is planned for the project. Not by its command: this runs in the
    `changes` job, which has h but no pnpm or uv tools, and a step whose tool is missing
    has none — counting commands would skip the heavy steps on main."""
    found = stacks.detect(cfg)
    steps = check.plan(cfg, found, None, check.FAST + check.SLOW, tier="heavy")
    return any(not s.disabled for s in steps)


def _job(cfg: Config, repo: str, run_id: int, name: str) -> str:
    jobs = _api(cfg, f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=50")["jobs"]
    return next((j.get("conclusion") or "" for j in jobs if j.get("name") == name), "")


def _workflow_path(repo: str, ref: str) -> str:
    """`.github/workflows/harness.yml` from `owner/repo/.github/workflows/harness.yml@ref`:
    in a called workflow the github context is the caller's."""
    if not repo or not ref.startswith(f"{repo}/"):
        return ""
    return _bare(ref[len(repo) + 1 :])


def _bare(path: str) -> str:
    return path.split("@", 1)[0]


def _api(cfg: Config, path: str) -> Any:
    return json.loads(gh._ok(gh.run(cfg.root, "api", path), f"gh api {path}"))


def _git(cfg: Config, *argv: str) -> str:
    r = task._git(cfg.root, *argv)
    if r.returncode != 0:
        raise ValueError(f"git {' '.join(argv)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()

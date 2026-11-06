"""The one seam to GitHub. Every call goes through `run`, so tests swap in a fake."""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

from . import proc
from .claims import ClaimError

ISSUE_ID = re.compile(r"^(?:gh-|#)?(\d+)$", re.IGNORECASE)


class GhError(ClaimError):
    pass


def run(cwd: Path, *argv: str) -> subprocess.CompletedProcess:
    return proc.run(["gh", *argv], cwd)


def _ok(r: subprocess.CompletedProcess, what: str) -> str:
    if r.returncode != 0:
        raise GhError(f"{what}: {(r.stderr or r.stdout).strip()}")
    return r.stdout


def issue_number(task_id: str) -> int | None:
    m = ISSUE_ID.match(task_id)
    return int(m.group(1)) if m else None


def issue_view(cwd: Path, number: int) -> dict:
    fields = "number,title,body,labels,milestone,state,comments,url"
    out = _ok(
        run(cwd, "issue", "view", str(number), "--json", fields),
        f"gh issue view {number}",
    )
    data = json.loads(out)
    data["labels"] = [lb["name"] for lb in data.get("labels", [])]
    return data


CONFIRM_QUERY = """query($owner: String!, $name: String!, $n: Int!) {
  repository(owner: $owner, name: $name) { issue(number: $n) {
    userContentEdits(first: 100) { nodes { editedAt diff } }
    timelineItems(itemTypes: [LABELED_EVENT], last: 100) {
      nodes { ... on LabeledEvent { createdAt label { name } } } } } } }"""


def body_when_labeled(cwd: Path, number: int, label: str, body_now: str) -> str | None:
    """The issue body as it was when `label` was last added, from the edit history. None
    when the body was never edited or the label never added: nothing changed since.

    Observed on real issues (2026-10-23), not promised by the docs: each revision's
    `diff` holds the whole body, the newest equals the current body, and the oldest is
    the original at creation. Both are checked here — if GitHub changes the shape, this
    says so instead of guessing (a wrong guess would void or wave through a card)."""
    out = _ok(run(cwd, "api", "graphql", "-F", "owner={owner}", "-F", "name={repo}",
                  "-F", f"n={number}", "-f", f"query={CONFIRM_QUERY}"),
              f"history of #{number}")  # fmt: skip
    issue = json.loads(out)["data"]["repository"]["issue"]
    times = [e["createdAt"] for e in issue["timelineItems"]["nodes"]
             if (e.get("label") or {}).get("name") == label]  # fmt: skip
    edits = sorted(issue["userContentEdits"]["nodes"], key=lambda e: e["editedAt"])
    if not times or not edits:
        return None
    if (edits[-1].get("diff") or "").replace("\r\n", "\n") != body_now.replace(
        "\r\n", "\n"
    ):
        raise GhError(f"#{number}: the edit history does not end with the current body — "
                      "cannot tell what the confirmation covered")  # fmt: skip
    at = max(times)  # ISO-8601 UTC strings order as times
    before = [e for e in edits if e["editedAt"] <= at]
    if not before:
        raise GhError(f"#{number}: the edit history does not reach back to the "
                      "confirmation (over 100 edits?) — the user confirms again")  # fmt: skip
    return before[-1]["diff"]


def issue_create(
    cwd: Path, title: str, body: str, labels: list[str], milestone: str | None
) -> tuple[int, str]:
    args = ["issue", "create", "--title", title, "--body", body]
    for lb in labels:
        args += ["--label", lb]
    if milestone:
        args += ["--milestone", milestone]
    url = _ok(run(cwd, *args), "gh issue create").strip().splitlines()[-1]
    return int(url.rstrip("/").rsplit("/", 1)[-1]), url


def issue_labels(
    cwd: Path, number: int, add: Sequence[str] = (), remove: Sequence[str] = ()
) -> None:
    if remove:
        # gh rejects the whole edit when one --remove-label names a label the repo does not
        # have (a new lifecycle label not yet created) — remove only what is on it.
        out = _ok(run(cwd, "issue", "view", str(number), "--json", "labels"),
                  f"gh issue view {number}")  # fmt: skip
        current = {lb["name"] for lb in json.loads(out).get("labels", [])}
        remove = [lb for lb in remove if lb in current]
    args = ["issue", "edit", str(number)]
    for lb in add:
        args += ["--add-label", lb]
    for lb in remove:
        args += ["--remove-label", lb]
    if len(args) > 3:
        _ok(run(cwd, *args), f"gh issue edit {number}")


def ensure_label(cwd: Path, name: str, color: str, description: str) -> bool:
    """Create or update a label; True if it was created."""
    r = run(
        cwd, "label", "create", name, "--color", color, "--description", description
    )
    if r.returncode == 0:
        return True
    run(cwd, "label", "edit", name, "--color", color, "--description", description)
    return False


def ensure_milestone(cwd: Path, title: str, description: str) -> bool:
    """Milestones have no gh subcommand; the REST API via `gh api`. True if created."""
    if title in milestones(cwd):
        return False
    _ok(
        run(cwd, "api", "repos/{owner}/{repo}/milestones", "-f", f"title={title}",
            "-f", f"description={description}"),
        "create milestone",
    )  # fmt: skip
    return True


def milestones(cwd: Path) -> list[str]:
    """Titles from every page: one page of 100 would let a 101st epic look new."""
    out = _ok(
        run(cwd, "api", "--paginate", "--jq", ".[].title",
            "repos/{owner}/{repo}/milestones?state=all&per_page=100"),
        "list milestones",
    )  # fmt: skip
    return out.splitlines()


def milestone_issues(cwd: Path, slug: str) -> list[dict]:
    out = _ok(
        run(cwd, "issue", "list", "--milestone", slug, "--state", "all", "--limit", "500",
            "--json", "number,title,state,labels,url"),
        f"issues of milestone {slug}",
    )  # fmt: skip
    return json.loads(out)


def pr_for_branch(cwd: Path, branch: str) -> str | None:
    r = run(cwd, "pr", "view", branch, "--json", "url,state")
    if r.returncode != 0:
        return None
    data = json.loads(r.stdout)
    return data["url"] if data.get("state") == "OPEN" else None


def pr_create(cwd: Path, branch: str, base: str, title: str, body: str) -> str:
    out = _ok(run(cwd, "pr", "create", "--head", branch, "--base", base, "--title", title,
                  "--body", body), "gh pr create")  # fmt: skip
    return out.strip().splitlines()[-1]


def commit_status(cwd: Path, sha: str, state: str, context: str, description: str,
                  url: str = "") -> None:  # fmt: skip
    args = ["api", "-X", "POST", f"repos/{{owner}}/{{repo}}/statuses/{sha}",
            "-f", f"state={state}", "-f", f"context={context}",
            "-f", f"description={description[:140]}"]  # fmt: skip
    if url:
        args += ["-f", f"target_url={url}"]
    _ok(run(cwd, *args), f"status {context} on {sha[:10]}")


def required_contexts(cwd: Path, branch: str) -> list[str]:
    """Status checks branch protection requires on `branch`; [] when it requires none."""
    r = run(
        cwd,
        "api",
        f"repos/{{owner}}/{{repo}}/branches/{branch}/protection/required_status_checks",
    )
    if r.returncode != 0:
        if "404" in r.stderr or "Not Found" in r.stderr:
            return []
        raise GhError(
            f"branch protection of {branch}: {(r.stderr or r.stdout).strip()}"
        )
    data = json.loads(r.stdout or "{}")
    return [
        *data.get("contexts", []),
        *(c.get("context", "") for c in data.get("checks", [])),
    ]


# GitHub's answer when the repository does not allow auto-merge.
# Only the text: the mutation's name (enablePullRequestAutoMerge) ends its other errors too.
AUTO_MERGE_OFF = re.compile(r"auto.?merge is not allowed", re.IGNORECASE)


def pr_auto_merge(cwd: Path, url: str) -> bool:
    """Turn on auto-merge; False when the repository has it switched off — the
    PR stays open for a person to merge. Any other failure is an error."""
    r = run(cwd, "pr", "merge", url, "--auto", "--squash", "--delete-branch")
    if r.returncode != 0 and AUTO_MERGE_OFF.search(r.stderr or r.stdout or ""):
        return False
    _ok(r, "gh pr merge --auto")
    return True


def merged_pr(cwd: Path, task_id: str, globs: list[str] | None = None) -> dict | None:
    """The task's PR (head branch task/<id> or task/<id>-*), newest first; None if none."""
    mine = task_prs(cwd, task_id, globs)
    if not mine:
        return None
    merged = [p for p in mine if p["state"] == "MERGED"]
    return max(merged or mine, key=lambda p: p["number"])


def workflow_run(cwd: Path, workflow: str, sha: str) -> dict | None:
    out = _ok(
        run(cwd, "run", "list", "--workflow", workflow, "--commit", sha, "--limit", "5",
            "--json", "databaseId,status,conclusion,url,event"),
        f"gh run list {workflow}",
    )  # fmt: skip
    runs = [r for r in json.loads(out) if r.get("event") != "pull_request"]
    return runs[0] if runs else None


def issue_close(
    cwd: Path, number: int, comment: str, reason: str = "completed"
) -> None:
    _ok(
        run(
            cwd, "issue", "close", str(number), "--reason", reason, "--comment", comment
        ),
        f"gh issue close {number}",
    )


def task_prs(cwd: Path, task_id: str, globs: list[str] | None = None) -> list[dict]:
    """All PRs whose head is task/<id> or task/<id>-*, asked by branch: a list of
    the whole repo stops at --limit, and old tasks fall out of it. A glob without wildcards
    is an exact `--head`; one with them a `head:<prefix>` search. Search lags a fresh merge,
    so when it finds nothing of the task the newest 100 PRs are checked too."""
    globs = globs or [f"task/{task_id}", f"task/{task_id}-*"]
    found: dict[int, dict] = {}
    for g in globs:
        prefix = re.split(r"[*?\[]", g, maxsplit=1)[0]
        query = ["--head", g] if prefix == g else ["--search", f"head:{prefix}"]
        found.update((p["number"], p) for p in _pr_list(cwd, *query))
    if not any(
        _is_task_branch(p["headRefName"], task_id, globs) for p in found.values()
    ):
        found.update((p["number"], p) for p in _pr_list(cwd))
    return [
        p for p in found.values() if _is_task_branch(p["headRefName"], task_id, globs)
    ]


def _pr_list(cwd: Path, *query: str) -> list[dict]:
    out = _ok(
        run(cwd, "pr", "list", "--state", "all", "--limit", "100", *query, "--json",
            "number,state,headRefName,headRefOid,mergeCommit,url"),
        "gh pr list",
    )  # fmt: skip
    return json.loads(out)


def _is_task_branch(head: str, task_id: str, globs: list[str] | None) -> bool:
    """`globs` come from task.branch_globs (the project's branch template); the default
    matches task/<id> and task/<id>-<slug>."""
    globs = globs or [f"task/{task_id}", f"task/{task_id}-*"]
    return any(fnmatch.fnmatchcase(head, g) for g in globs)


def pr_close(cwd: Path, url: str, comment: str) -> None:
    _ok(run(cwd, "pr", "close", url, "--comment", comment), f"gh pr close {url}")


def agent_ready(cwd: Path) -> list[dict]:
    """Open issues an agent may take on its own: ready + agent-ok (the Linear rule)."""
    out = _ok(
        run(cwd, "issue", "list", "--state", "open", "--label", "ready", "--label",
            "agent-ok", "--limit", "100", "--json", "number,title,url"),
        "gh issue list",
    )  # fmt: skip
    return sorted(json.loads(out), key=lambda i: i["number"])


def runs_for_commit(cwd: Path, sha: str) -> list[dict]:
    out = _ok(
        run(cwd, "run", "list", "--commit", sha, "--limit", "20", "--json",
            "databaseId,status,conclusion,name,workflowName,url,event,attempt,headBranch"),
        "gh run list",
    )  # fmt: skip
    return json.loads(out)


def pr_checks(cwd: Path, branch: str) -> list[dict]:
    """Every check of the branch's PR — Actions or not (`workflow` is "" for commit
    statuses and apps); [] when there is no PR or no checks. Exit 8 (pending) and 1
    (failing) still print the JSON."""
    r = run(cwd, "pr", "checks", branch, "--json", "name,state,bucket,link,workflow")
    if r.stdout.strip().startswith("["):
        return json.loads(r.stdout)
    err = (r.stderr or r.stdout).strip()
    if (
        r.returncode == 0
        or "no checks reported" in err
        or "no pull requests found" in err
    ):
        return []
    raise GhError(f"gh pr checks {branch}: {err}")


def pr_mergeable(cwd: Path, branch: str) -> str:
    """MERGEABLE | CONFLICTING | UNKNOWN (not computed yet, e.g. right after a push);
    "" when there is no PR for the branch."""
    r = run(cwd, "pr", "view", branch, "--json", "mergeable")
    if r.returncode != 0:
        return ""
    return str(json.loads(r.stdout).get("mergeable") or "")


def rerun_failed(cwd: Path, run_id: int) -> None:
    _ok(run(cwd, "run", "rerun", str(run_id), "--failed"), f"gh run rerun {run_id}")


def run_log_failed(cwd: Path, run_id: int) -> str:
    return _ok(
        run(cwd, "run", "view", str(run_id), "--log-failed"), f"gh run view {run_id}"
    )


# The step logs check.yml uploads when `h check` fails or the job is cancelled; one
# per attempt — artifact names are unique within a run.
LOGS_ARTIFACT = "h-check-logs-{attempt}"


def run_download(cwd: Path, run_id: int, name: str, dest: Path) -> None:
    """The run's artifact `name`, unpacked into `dest`."""
    _ok(run(cwd, "run", "download", str(run_id), "-n", name, "-D", str(dest)),
        f"gh run download {run_id} -n {name}")  # fmt: skip


def run_lost_runner(cwd: Path, run_id: int) -> bool:
    """A job failed but none of its steps did: the runner went away mid-job ("lost
    communication with the server") — the code was never judged."""
    out = _ok(
        run(cwd, "run", "view", str(run_id), "--json", "jobs"),
        f"gh run view {run_id}",
    )
    failed = [
        j for j in json.loads(out).get("jobs", []) if j.get("conclusion") == "failure"
    ]
    return bool(failed) and not any(
        s.get("conclusion") == "failure" for j in failed for s in j.get("steps", [])
    )


# The check job as the harness.yml template names it: its `check` job calls check.yml,
# whose job is `check`. Skipped = a docs-only diff.
CHECK_JOB = "check / check"


CHANGES_JOB = "check / changes"
# check.yml's step that runs only when main took a PR's green check of its tree.
REUSE_STEP = "Reuse the PR check"


def check_state(jobs: list[dict]) -> str:
    """checked | reused | skipped. Skipped means no code was tested (docs only);
    reused is a skipped check job whose tree a PR run checked green."""
    check = next((j for j in jobs if j.get("name") == CHECK_JOB), None)
    if check is None or check.get("conclusion") != "skipped":
        return "checked"
    changes = next((j for j in jobs if j.get("name") == CHANGES_JOB), {})
    reused = any(s.get("name") == REUSE_STEP and s.get("conclusion") == "success"
                 for s in changes.get("steps") or [])  # fmt: skip
    return "reused" if reused else "skipped"


def run_check_state(cwd: Path, run_id: int) -> str:
    out = _ok(
        run(cwd, "run", "view", str(run_id), "--json", "jobs"),
        f"gh run view {run_id}",
    )
    return check_state(json.loads(out).get("jobs", []))


def run_skipped_check(cwd: Path, run_id: int) -> bool:
    """The run tested no code: its check job was skipped, and not for a reused PR check."""
    return run_check_state(cwd, run_id) == "skipped"


def run_job_conclusions(cwd: Path, run_id: int) -> list[str]:
    """Conclusions of a run's jobs; a run cancelled while still queued has none."""
    out = _ok(
        run(cwd, "run", "view", str(run_id), "--json", "jobs"),
        f"gh run view {run_id}",
    )
    return [j.get("conclusion") or "" for j in json.loads(out).get("jobs", [])]


def workflow_runs_on(cwd: Path, workflow: str, branch: str) -> list[dict]:
    """Recent non-PR runs of `workflow` on `branch`, newest first."""
    out = _ok(
        run(cwd, "run", "list", "--workflow", workflow, "--branch", branch, "--limit", "20",
            "--json", "databaseId,headSha,status,conclusion,url,event"),
        f"gh run list {workflow} --branch {branch}",
    )  # fmt: skip
    return [r for r in json.loads(out) if r.get("event") != "pull_request"]


def pr_number(pr: str) -> int:
    """A PR url or number → number."""
    tail = str(pr).rstrip("/").rsplit("/", 1)[-1]
    if not tail.isdigit():
        raise GhError(f"not a PR url or number: {pr!r}")
    return int(tail)


def pr_head(cwd: Path, number: int) -> tuple[str, str]:
    """(head sha, author login) of a PR."""
    out = _ok(
        run(cwd, "pr", "view", str(number), "--json", "headRefOid,author"),
        f"gh pr view {number}",
    )
    data = json.loads(out)
    return data["headRefOid"], (data.get("author") or {}).get("login", "")


def pr_info(cwd: Path, number: int) -> dict:
    """Title, body, author login, head sha and label names of a PR."""
    out = _ok(
        run(
            cwd,
            "pr",
            "view",
            str(number),
            "--json",
            "title,body,author,headRefOid,labels",
        ),
        f"gh pr view {number}",
    )
    data = json.loads(out)
    return {
        "title": data.get("title") or "",
        "body": data.get("body") or "",
        "author": (data.get("author") or {}).get("login", ""),
        "sha": data["headRefOid"],
        "labels": [lb.get("name", "") for lb in data.get("labels") or []],
    }


def viewer(cwd: Path) -> str:
    """Login of the account `gh` acts as."""
    return _ok(run(cwd, "api", "user", "--jq", ".login"), "gh api user").strip()


def issue_comments(cwd: Path, number: int) -> list[dict]:
    """All comments of an issue or PR as {id, body, user}; one JSON object per line."""
    out = _ok(
        run(cwd, "api", "--paginate", "--jq", ".[] | {id, body, user: .user.login}",
            f"repos/{{owner}}/{{repo}}/issues/{number}/comments?per_page=100"),
        f"comments of #{number}",
    )  # fmt: skip
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def comment_create(cwd: Path, number: int, body: str) -> None:
    _ok(run(cwd, "api", f"repos/{{owner}}/{{repo}}/issues/{number}/comments", "-f",
            f"body={body}"), f"comment on #{number}")  # fmt: skip


def comment_update(cwd: Path, comment_id: int, body: str) -> None:
    _ok(run(cwd, "api", "-X", "PATCH", f"repos/{{owner}}/{{repo}}/issues/comments/{comment_id}",
            "-f", f"body={body}"), f"update comment {comment_id}")  # fmt: skip

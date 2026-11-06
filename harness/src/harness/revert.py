"""`h revert <sha>` — the fast way back when the post-merge run is red.

Heavy checks run after the merge, so main can turn red; the fix is a revert PR, not a hot
patch. The agent opens it, the user merges it (decided 2026-10-23): no review status is
set here. The revert is made in a throwaway worktree off origin/<base>, so the caller's
checkout — the main one, or a task worktree — is never touched.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field

from . import cards, gh, git
from .config import Config

LABEL = "regression"


class RevertError(Exception):
    pass


@dataclass
class Result:
    pr: str = ""
    branch: str = ""
    lines: list[str] = field(default_factory=list)


def _git(cwd, *argv: str) -> str:
    r = git.run(cwd, *argv)
    if r.returncode != 0:
        raise RevertError(f"git {' '.join(argv)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def failed_run(cfg: Config, sha: str) -> str:
    """The URL of a failed run on `sha`, "" when there is none (or gh cannot tell)."""
    try:
        runs = gh.runs_for_commit(cfg.root, sha)
    except gh.GhError:
        return ""
    bad = [r for r in runs if r.get("conclusion") in ("failure", "timed_out")]
    return bad[0]["url"] if bad else ""


def run(cfg: Config, ref: str, run_url: str = "") -> Result:
    base = f"origin/{cfg.base_branch}"
    _git(cfg.root, "fetch", "--quiet", "origin", cfg.base_branch)
    sha = _git(cfg.root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if git.run(cfg.root, "merge-base", "--is-ancestor", sha, base).returncode != 0:
        raise RevertError(f"{sha[:10]} is not on {base}: nothing merged to revert")
    subject = _git(cfg.root, "log", "-1", "--format=%s", sha)
    parents = _git(cfg.root, "rev-list", "--parents", "-n", "1", sha).split()[1:]

    res = Result(branch=cfg.auto_branch(f"revert-{sha[:10]}", f"revert/{sha[:10]}"))
    path = cfg.root / cfg.worktree_dir / f"revert-{sha[:10]}"
    if path.exists() or git.run(cfg.root, "rev-parse", "--verify", "--quiet",
                                f"refs/heads/{res.branch}").returncode == 0:  # fmt: skip
        raise RevertError(f"{res.branch} already exists — a revert is under way")
    _git(cfg.root, "worktree", "add", "--quiet", "-b", res.branch, str(path), base)
    try:
        r = git.run(
            path, "revert", "--no-edit", *(["-m", "1"] if len(parents) > 1 else []), sha
        )
        if r.returncode != 0:
            git.run(path, "revert", "--abort")
            raise RevertError(
                f"git revert {sha[:10]} conflicts with later commits on {base} — "
                "a human resolves it:\n" + (r.stderr or r.stdout).strip()
            )
        _git(path, "push", "--quiet", "-u", "origin", res.branch)
    finally:
        git.run(cfg.root, "worktree", "remove", "--force", str(path))
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            git.run(cfg.root, "worktree", "prune")
        git.run(cfg.root, "branch", "-D", res.branch)

    run_url = run_url or failed_run(cfg, sha)
    body = [
        f"Reverts {sha} ({subject}).",
        "",
        f"The post-merge run is red: {run_url}" if run_url else
        "The post-merge run of this commit is red.",
        "",
        ("Opened by `h revert`. **The user merges it** — no agent review: it only "
        "restores the state before the commit. Fix forward in a new task after the merge."),
    ]  # fmt: skip
    if cfg.ship_footer:
        body += ["", cfg.ship_footer]
    res.pr = gh.pr_create(cfg.root, res.branch, cfg.base_branch, f'Revert "{subject}"',
                          "\n".join(body))  # fmt: skip
    gh.ensure_label(cfg.root, LABEL, *cards.LABELS[LABEL])
    if gh.run(cfg.root, "pr", "edit", res.pr, "--add-label", LABEL).returncode != 0:
        res.lines.append(f"label {LABEL} not added")
    res.lines.append(f"revert {sha[:10]}: PR opened — {res.pr}")
    res.lines.append("  merge: user — the revert waits for the user's merge")
    return res

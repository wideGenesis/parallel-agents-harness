"""Path policy: who reviews and who merges, decided by the files a change touches.

    [[policy]]
    paths = ["migrations/**", ".github/workflows/**"]
    review = true
    merge = "user"

Each changed file is held to every rule whose glob matches it, or to the project defaults
(`[review] required`, `[ship] merge`) when none does. The change as a whole gets the strictest
outcome over all of them — review if anything needs it, the user merges if anything says so —
so neither rule order nor a lenient path touched alongside can waive review.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path

from .claims import ClaimError
from .config import Config


@dataclass
class Decision:
    review: bool
    merge: str  # user | agent
    reason: str  # which file and rule made the merge/review strict; "" = defaults only
    review_reason: str = ""  # only what required review — for a review refusal


def _outcomes(cfg: Config, path: str) -> list[tuple[bool, str, str]]:
    """(review, merge, label) for every rule matching `path` — all of them, not the first:
    fnmatch's `*` crosses `/`, so `*.md` also matches `migrations/notes.md`, and rule order
    must not decide whether a migration skips review (reviewer finding). A file no
    rule matches gets the project defaults."""
    out = []
    for rule in cfg.policies:
        if any(fnmatch.fnmatchcase(path, g) for g in rule["paths"]):
            out.append((rule.get("review", cfg.review_required),
                        rule.get("merge", cfg.ship_merge),
                        f"{rule['label']} {rule['paths'][0]}"))  # fmt: skip
    return out or [(cfg.review_required, cfg.ship_merge, "defaults")]


def decide(cfg: Config, files: list[str]) -> Decision:
    review, merge = False, "agent"
    review_why = merge_why = ""

    def better(current: str, label: str) -> bool:
        # A named rule explains more than "defaults", whichever file sorts first.
        return not current or (current.startswith("defaults") and label != "defaults")

    for f in sorted(files):
        for r, m, label in _outcomes(cfg, f):
            if r and (not review or better(review_why, label)):
                review, review_why = True, f"{label} ← {f}"
            if m == "user" and (merge != "user" or better(merge_why, label)):
                merge, merge_why = "user", f"{label} ← {f}"
    if not files:
        return Decision(cfg.review_required, cfg.ship_merge, "")
    reasons = [x for x in (merge_why, review_why) if x and not x.startswith("defaults")]
    review_reason = review_why if not review_why.startswith("defaults") else ""
    return Decision(review, merge, reasons[0] if reasons else "", review_reason)


def for_worktree(cfg: Config, wt: Path) -> Decision:
    """The decision for a task worktree's changes; without a base ref (shallow or detached
    checkout) the project defaults apply instead of a crash."""
    from . import affected, config, git

    wcfg = cfg if cfg.root.resolve() == wt.resolve() else config.load(wt)
    try:
        files = affected.changed_files(wcfg, affected.resolve_base(wcfg, None))
    except affected.GitError as e:
        if isinstance(e, git.GitTimeout):  # a hung git must not loosen the path policy
            raise
        return Decision(cfg.review_required, cfg.ship_merge, "")
    return decide(wcfg, files)


class UserOnly(ClaimError):
    pass


def user_only(what: str) -> None:
    """Refuse inside a Claude Code session. Deny rules match command text and are
    not a boundary — `uv run h …`, `h -C . …` slip past them; the session's environment
    does not. A guard against accidents, not intent: the variable can be unset."""
    if os.environ.get("CLAUDECODE"):
        raise UserOnly(
            f"{what} is the user's decision — the user runs it in their own terminal, "
            "not an agent in a Claude Code session"
        )

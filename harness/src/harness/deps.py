"""Dependabot PRs: they go past `h ship`, so nothing set the required check
`harness/review` and branch protection held them forever (2026-10-25).

`h deps review` sets it for the updates AGENTS.md lets the agent merge — patch, and minor
of a package past 1.0 — when the PR's CI is green. Everything else (major, minor of a 0.x
package, a version it cannot read) is the user's: `h deps approve`, in their terminal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import gh, policy, prreview
from .claims import ClaimError
from .config import Config

DEPENDABOT = "app/dependabot"
AGENT_KINDS = ("patch", "minor")
# Worst first: a group takes the kind of its riskiest update.
SEVERITY = ("unknown", "major", "minor-0.x", "minor", "patch")

BUMP = re.compile(r"\bbump (\S+) from (\S+) to (\S+)", re.IGNORECASE)
REQUIREMENT = re.compile(
    r"\bupdate (\S+) requirement from (\S+) to (\S+)", re.IGNORECASE
)
GROUP = re.compile(r"\bthe \S+ group\b", re.IGNORECASE)
GROUP_LINE = re.compile(r"^Updates `([^`]+)` from (\S+) to (\S+?)\.?$", re.MULTILINE)
LOWER = re.compile(r"(?:>=|==|~=|\^|~)\s*v?(\d+(?:\.\d+)*)")
VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)")


class DepsError(ClaimError):  # the CLI prints it and exits 2
    pass


@dataclass
class Update:
    package: str
    old: str
    new: str
    kind: str


@dataclass
class Decision:
    number: int
    sha: str
    kind: str
    updates: list[Update]
    security: bool

    @property
    def agent_may(self) -> bool:
        return self.kind in AGENT_KINDS


def _version(spec: str) -> tuple[int, ...] | None:
    """A version, or the lower bound of a requirement (`<0.11,>=0.9` → 0.9)."""
    spec = spec.strip("`'\",")
    if (m := LOWER.search(spec)) or (m := VERSION.match(spec)):
        text = m[1]
    else:
        return None
    return tuple(int(p) for p in text.split("."))


def kind_of(old: str, new: str) -> str:
    a, b = _version(old), _version(new)
    if a is None or b is None or b <= a:
        return "unknown"
    a, b = a + (0,) * (3 - len(a)), b + (0,) * (3 - len(b))
    if a[0] != b[0]:
        return "major"
    if a[1] != b[1]:
        return "minor-0.x" if a[0] == 0 else "minor"
    return "patch"


def updates(title: str, body: str) -> list[Update]:
    """The updates a Dependabot PR makes: a group lists them in its body, a single one
    names it in the title. Both count: the body quotes third-party release notes, and a
    line planted there must not replace the title's major with a patch (review)
    — a planted line can only add an update, and the riskiest one decides."""
    m = BUMP.search(title) or REQUIREMENT.search(title)
    if not m and not GROUP.search(title):
        return []  # nothing the title vouches for: body lines alone could be planted
    found = [(p, o, n) for p, o, n in GROUP_LINE.findall(body or "")]
    if m:
        found.insert(0, (m[1], m[2], m[3]))
    return [Update(p, o, n, kind_of(o, n)) for p, o, n in found]


def classify(title: str, body: str) -> tuple[str, list[Update]]:
    ups = updates(title, body)
    if not ups:
        return "unknown", []
    return min((u.kind for u in ups), key=SEVERITY.index), ups


def _ci_green(cfg: Config, number: int) -> str | None:
    """None when every check but ours passed; else why not."""
    checks = [
        c
        for c in gh.pr_checks(cfg.root, str(number))
        if c.get("name") != prreview.STATUS_CONTEXT
    ]
    if not checks:
        return "no CI checks on the PR yet"
    pending = [c["name"] for c in checks if c.get("bucket") == "pending"]
    if pending:
        return "CI still running: " + ", ".join(pending)
    failed = [c["name"] for c in checks if c.get("bucket") not in ("pass", "skipping")]
    if failed:
        return (
            "CI red: "
            + ", ".join(failed)
            + " — do not merge; the cause goes into a PR comment"
        )
    return None


def decide(cfg: Config, pr: str) -> Decision:
    """Refuses a PR that is not Dependabot's or whose CI is not green."""
    number = gh.pr_number(pr)
    info = gh.pr_info(cfg.root, number)
    if info["author"] != DEPENDABOT:
        raise DepsError(
            f"PR #{number} is by {info['author'] or 'unknown'}, not Dependabot — "
            "own PRs go through `h ship`"
        )
    if why := _ci_green(cfg, number):
        raise DepsError(f"PR #{number}: {why}")
    # CI is read by PR number: a Dependabot rebase in between would put the status on
    # a commit whose CI nobody looked at.
    if (now := gh.pr_info(cfg.root, number)["sha"]) != info["sha"]:
        raise DepsError(
            f"PR #{number} moved to {now[:10]} while its CI was read — run it again"
        )
    kind, ups = classify(info["title"], info["body"])
    security = any("security" in lb.lower() for lb in info["labels"])
    return Decision(number, info["sha"], kind, ups, security)


def _mark(cfg: Config, d: Decision, who: str) -> None:
    prreview.publish_status(
        cfg, d.sha, f"dependabot {d.kind} — {who}, no code review", ""
    )


def review(cfg: Config, pr: str) -> Decision:
    """The agent's path: sets the check only for the kinds AGENTS.md leaves to it."""
    d = decide(cfg, pr)
    if not d.agent_may:
        tag = " (security)" if d.security else ""
        raise DepsError(
            f"PR #{d.number}: {d.kind} update{tag} — the user's decision; "
            f"they run `h deps approve {d.number}` in their own terminal"
        )
    _mark(cfg, d, "agent")
    return d


def approve(cfg: Config, pr: str) -> Decision:
    """The user's path: any kind, the same author and CI checks."""
    policy.user_only("approving a Dependabot PR")
    d = decide(cfg, pr)
    _mark(cfg, d, "approved by the user")
    return d

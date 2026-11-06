"""The review verdict, published to the PR.

`h ship` writes one PR comment per PR and updates it on every later ship:

    <!-- h:review {"head": "<reviewed sha>", "pr_head": "<pushed sha>", "verdict": "approve", …} -->
    <the project's [review] pr_comment template, rendered>

The first line is for machines — `h review verify-pr`, and later the required GitHub check
and a project's own gates read it; the rest is for
people and follows the project's own format.

Trust: anyone can write a comment, so only the PR author's is read — the account `h ship`
acts as. That keeps out other commenters, not the author; a check that the author cannot
forge needs a token of its own.
"""

from __future__ import annotations

import json
import re

from . import gh
from .config import Config
from .review import Verdict

MARKER = "<!-- h:review "
MARKER_RE = re.compile(r"<!-- h:review (\{.*?\}) -->")

DEFAULT_TEMPLATE = """**Review: {verdict}** — `{reviewer}` ({model}), round {round}
Reviewed head: `{head}`

{findings}"""


def _findings(v: Verdict) -> str:
    if not v.findings:
        return "_No findings._"
    return "\n".join(
        f"- [{f.get('severity', '?')}] `{f.get('file')}:{f.get('line')}` {f.get('text', '')}"
        for f in v.findings
    )


def render(cfg: Config, v: Verdict, pr_head: str) -> str:
    meta = {"head": v.head, "pr_head": pr_head, "verdict": v.verdict, "round": v.round,
            "model": v.model, "reviewer": "reviewer"}  # fmt: skip
    template = cfg.review_pr_comment or DEFAULT_TEMPLATE
    fields = {**{k: str(x) for k, x in meta.items()}, "findings": _findings(v)}
    try:
        body = template.format(**fields)
    except (KeyError, IndexError, ValueError) as e:
        raise ValueError(
            f"[review] pr_comment: bad placeholder ({e}); use {sorted(fields)}"
        ) from e
    return f"{MARKER}{json.dumps(meta, separators=(',', ':'))} -->\n{body}\n"


def publish(cfg: Config, pr_url: str, v: Verdict, pr_head: str) -> str:
    """Create or update our review comment on the PR. Returns 'created' or 'updated'."""
    number = gh.pr_number(pr_url)
    body = render(cfg, v, pr_head)
    me = gh.viewer(cfg.root)
    for c in gh.issue_comments(cfg.root, number):
        if c.get("user") == me and (c.get("body") or "").startswith(MARKER):
            gh.comment_update(cfg.root, c["id"], body)
            return "updated"
    gh.comment_create(cfg.root, number, body)
    return "created"


def _meta(body: str) -> dict | None:
    m = MARKER_RE.match(body)
    if not m:
        return None
    try:
        meta = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return meta if isinstance(meta, dict) else None


def verify(cfg: Config, pr: str) -> tuple[bool, str]:
    """Is there an approving harness verdict for the PR's current head?

    Matched against `pr_head` — the sha ship pushed, which is the reviewed head plus the
    tool-made format commit — not against `head`, which a template may show instead."""
    number = gh.pr_number(pr)
    head, author = gh.pr_head(cfg.root, number)
    found = None
    for c in gh.issue_comments(cfg.root, number):
        if c.get("user") == author and (meta := _meta(c.get("body") or "")):
            found = meta
    if found is None:
        return (
            False,
            f"PR #{number}: no harness review comment by the PR author ({author})",
        )
    if found.get("pr_head") != head:
        return (
            False,
            f"PR #{number}: review is for {str(found.get('pr_head'))[:10]}, head is {head[:10]}",
        )
    if found.get("verdict") != "approve":
        return False, f"PR #{number}: verdict is {found.get('verdict')}"
    return True, f"PR #{number}: approved for {head[:10]} (round {found.get('round')})"


# the verdict as a commit status, so branch protection can require it. A new push
# is a new sha without the status — the check is pending again until the next ship.
STATUS_CONTEXT = "harness/review"


def publish_status(cfg: Config, sha: str, description: str, url: str = "") -> None:
    gh.commit_status(cfg.root, sha, "success", STATUS_CONTEXT, description, url)

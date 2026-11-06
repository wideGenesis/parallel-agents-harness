"""Task cards: the agent-ready issue format and the deterministic Definition of Ready.

The format is a slimmer version of a pilot project's card: business AC
first (AC-B, Given/When/Then), technical AC tracing to them (AC-T ←B1), a Verify command,
size, and the Trivial escape hatch. Issue forms and `h task new` both render sections as
`### <Heading>`, so a card parses the same whichever way it was created.

Readiness is checked by code, not by the agent's judgement. The one input code cannot
produce is the user's confirmation of the business criteria — the `ac-confirmed` label,
which agents are denied from setting (`h init` adds the deny rule).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# (heading, required-for-every-card)
SECTIONS = [
    ("Problem", True),
    ("Assumptions", True),
    ("AC-B (business, Given/When/Then)", False),
    ("AC-T (technical, ← AC-B)", False),
    ("Verify", True),
    ("Size", True),
    ("Trivial", True),
    ("Trivial reason", False),
]
EMPTY = {"", "_no response_", "tbd", "_tbd_", "…", "..."}

AC_CONFIRMED = "ac-confirmed"
# Lifecycle labels are a display of state; the claim on origin is the truth (ADR-0002).
LABELS = {
    "draft": ("d4c5f9", "Card is being written"),
    "needs-clarification": ("fbca04", "Open questions before work can start"),
    "ready": ("0e8a16", "Definition of Ready passed"),
    AC_CONFIRMED: (
        "1d76db",
        "The user confirmed the business criteria (agents never set this)",
    ),
    "in-progress": ("5319e7", "Claimed by a session"),
    "review": ("c5def5", "PR open"),
    "merged": ("0052cc", "PR merged, waiting for deploy"),
    "deployed": ("006b75", "Deployed; closing gate pending"),
    "dropped": ("e4e669", "Decided not to do — closed as not planned"),
    "ci-blocked": (
        "b60205",
        "CI failed twice for real — the user decides (h ci reset)",
    ),
    "cleanup-blocked": (
        "b60205",
        "Closing gate found leftovers — see the last comment",
    ),
    "size:S": ("ededed", "Small"),
    "size:M": ("ededed", "Medium"),
    "size:L": ("ededed", "Large — needs an exec-plan"),
    "agent-ok": ("c2e0c6", "An agent may pick this up on its own"),
    "regression": ("b60205", "main went red after a merge — revert first (h revert)"),
    "full-ci": ("fbca04", "Run the heavy checks on this PR too ([check] heavy)"),
}

# Whole words: `then` inside `authentication` or `then-current` is not a Then. Not `\b`:
# a hyphen is a word boundary to it, and `then-current` would still pass.
_W = r"(?<![\w-])({})(?![\w-])"
GIVEN_WHEN_THEN = re.compile(
    ".*".join(_W.format(k) for k in ("given", "when", "then")),
    re.IGNORECASE | re.DOTALL,
)
TRACE = re.compile(r"←\s*B\d|<-\s*B\d", re.IGNORECASE)
NEEDS_CLARIFICATION = re.compile(r"\[NEEDS CLARIFICATION", re.IGNORECASE)


def parse(body: str) -> dict[str, str]:
    """`### Heading` sections → {canonical heading: text}."""
    out: dict[str, str] = {}
    current = None
    lines: list[str] = []
    for line in (body or "").splitlines():
        if line.startswith("### "):
            if current:
                out[current] = "\n".join(lines).strip()
            heading = line[4:].strip()
            # Longest matching base wins: "Trivial reason" must not land in "Trivial".
            matches = [
                h
                for h, _ in SECTIONS
                if heading.lower().startswith(h.split(" (")[0].lower())
            ]
            current = max(matches, key=len) if matches else heading
            lines = []
        elif current:
            lines.append(line)
    if current:
        out[current] = "\n".join(lines).strip()
    return out


def render(sections: dict[str, str]) -> str:
    return (
        "\n\n".join(
            f"### {h}\n\n{sections.get(h, '').strip() or '_No response_'}"
            for h, _ in SECTIONS
        )
        + "\n"
    )


def _empty(text: str | None) -> bool:
    return text is None or text.strip().lower() in EMPTY


def _items(text: str) -> list[str]:
    """List items, each with its indented continuation lines joined: Markdown
    reads a wrapped line as part of the item. A blank line or a new item ends one."""
    items: list[str] = []
    open_item = False
    for ln in text.splitlines():
        if re.match(r"^\s*[-*]\s", ln):
            items.append(ln.strip())
            open_item = True
        elif open_item and ln.strip() and ln[:1].isspace():
            items[-1] += " " + ln.strip()
        else:
            open_item = False
    return items


@dataclass
class Verdict:
    ready: bool
    problems: list[str] = field(default_factory=list)
    trivial: bool = False


def _confirmed_part(body: str) -> str:
    """What the user's confirmation covers: AC-B and the trivial claim. Ticking a box is
    progress, not a change (decided 2026-10-23), so marks are ignored."""
    s = parse(body)
    keys = ("AC-B (business, Given/When/Then)", "Trivial", "Trivial reason")
    text = "\n".join((s.get(k) or "").strip() for k in keys)
    return re.sub(r"\[[xX ]\]", "[ ]", re.sub(r"[ \t]+", " ", text))


def check_ready(
    body: str, labels: list[str], confirmed_body: str | None = None
) -> Verdict:
    """`confirmed_body`: the body when the user added the confirmation label, if it was
    edited since (gh.body_when_labeled); None = unchanged."""
    s = parse(body)
    problems: list[str] = []
    for heading, required in SECTIONS:
        if required and _empty(s.get(heading)):
            problems.append(f"section '{heading}' is empty")

    trivial = (s.get("Trivial") or "").strip().lower().startswith("yes")
    # The agent writes the card, so "trivial" is a claim the user confirms too.
    if AC_CONFIRMED not in labels:
        what = "the trivial claim needs" if trivial else "business criteria need"
        problems.append(f"{CONFIRM_PROBLEM_PREFIX}: {what} the user's label "
                        f"'{AC_CONFIRMED}'")  # fmt: skip
    elif confirmed_body is not None and _confirmed_part(
        confirmed_body
    ) != _confirmed_part(body):
        problems.append(f"{CONFIRM_PROBLEM_PREFIX}: AC-B or Trivial changed after the "
                        f"user confirmed — the user confirms again (h task confirm)")  # fmt: skip
    if trivial:
        if _empty(s.get("Trivial reason")):
            problems.append(
                "Trivial: yes needs a reason (no product surface: refactor, test fix, docs…)"
            )
    else:
        ac_b = _items(s.get("AC-B (business, Given/When/Then)") or "")
        ac_t = _items(s.get("AC-T (technical, ← AC-B)") or "")
        if not ac_b:
            problems.append("AC-B has no items")
        problems += [
            f"AC-B not Given/When/Then: {i[:60]}"
            for i in ac_b
            if not GIVEN_WHEN_THEN.search(i)
        ]
        if not ac_t:
            problems.append("AC-T has no items")
        problems += [
            f"AC-T does not trace to an AC-B (←B1): {i[:60]}"
            for i in ac_t
            if not TRACE.search(i)
        ]

    size = (s.get("Size") or "").strip().upper()[:1]
    if size and size not in "SML":
        problems.append(f"Size must be S, M or L, got {s.get('Size')!r}")
    if NEEDS_CLARIFICATION.search(body or ""):
        problems.append(CLARIFY_PROBLEM)
    return Verdict(not problems, problems, trivial)


CLARIFY_PROBLEM = "card still has [NEEDS CLARIFICATION] markers"
CONFIRM_PROBLEM_PREFIX = "not confirmed by the user"
STATE_LABELS = ("draft", "needs-clarification", "ready")


def state_label(v: Verdict) -> str:
    """The lifecycle label a verdict implies. Labels are derived from the card, never read
    back as gates: a stale 'needs-clarification' once kept a confirmed card unready.

    needs-clarification = the card is written, only the user's part is missing
    (confirmation of AC-B, answers to [NEEDS CLARIFICATION]); draft = the card itself is
    incomplete."""
    if v.ready:
        return "ready"
    user_part = all(
        p == CLARIFY_PROBLEM or p.startswith(CONFIRM_PROBLEM_PREFIX) for p in v.problems
    )
    return "needs-clarification" if user_part else "draft"


def brief(issue: dict) -> str:
    """The issue as a file the agent reads instead of calling the network again."""
    parts = [
        f"# {issue['title']} (#{issue['number']})",
        "",
        f"<{issue.get('url', '')}>",
        "",
    ]
    if issue.get("milestone"):
        parts.append(f"Epic: {issue['milestone']['title']}")
    parts += [
        f"Labels: {', '.join(issue.get('labels', []))}",
        "",
        issue.get("body") or "",
    ]
    comments = issue.get("comments") or []
    if comments:
        parts += ["", "## Comments", ""]
        for c in comments:
            parts += [
                f"**{c.get('author', {}).get('login', '?')}**:",
                c.get("body", ""),
                "",
            ]
    return "\n".join(parts).rstrip() + "\n"

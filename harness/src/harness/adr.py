"""The project's ADRs in the task brief and the review package.

An agent should know the decisions already taken before it changes code, and a reviewer
should see when a change contradicts one. Both get the index — number, status, title, one
line per ADR, no text — and the full text of the ADRs the card names (`ADR-0013`). A
project without the ADR directory gets nothing: the brief stays the card.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config

# brief.md = card + this section; the review package swaps it for a fresh one, since the
# branch may add or change ADRs after the task started.
HEADING = "## ADR (harness)"
FILE = re.compile(r"^(\d{1,4})[-_].*\.md$")
MENTION = re.compile(r"\bADR-(\d{1,4})\b")
TITLE_PREFIX = re.compile(r"^ADR[-\s]?\d+\s*[:—–.-]\s*", re.IGNORECASE)
# `- status: accepted` (a project's own format) or `Status: accepted · 2026-10-20 · …` (harness).
STATUS = re.compile(r"^[\s>*-]*status\**\s*:\s*\**\s*([^\s·,;|*]+)",
                    re.IGNORECASE)  # fmt: skip
STATUS_LINES = 15  # the status belongs to the header, not to a quote in the body
KNOWN = {
    "accepted": "accepted",
    "proposed": "proposed",
    "draft": "proposed",
    "superseded": "superseded",
    "deprecated": "deprecated",
    "rejected": "rejected",
}  # fmt: skip


@dataclass
class Adr:
    number: int
    title: str
    status: str
    path: Path

    @property
    def ref(self) -> str:
        return f"ADR-{self.number:04d}"


def parse(path: Path, number: int) -> Adr:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    title = next((ln[2:].strip() for ln in lines if ln.startswith("# ")), path.stem)
    status = "unknown"
    for ln in lines[:STATUS_LINES]:
        if m := STATUS.match(ln):
            status = KNOWN.get(m.group(1).strip().lower(), "unknown")
            break
    return Adr(number, TITLE_PREFIX.sub("", title) or path.stem, status, path)


def index(cfg: Config, wt: Path) -> list[Adr]:
    """ADRs of the project in `wt`, by number; [] without `[adr] dir` or the directory."""
    if not cfg.adr_dir:
        return []
    root = wt / cfg.adr_dir
    if not root.is_dir():
        return []
    found: dict[int, Adr] = {}
    for path in sorted(root.glob("*.md")):
        m = FILE.match(path.name)
        if m and int(m.group(1)) not in found:
            found[int(m.group(1))] = parse(path, int(m.group(1)))
    return [found[n] for n in sorted(found)]


def mentioned(text: str) -> list[int]:
    seen: list[int] = []
    for m in MENTION.finditer(text):
        if int(m.group(1)) not in seen:
            seen.append(int(m.group(1)))
    return seen


def section(cfg: Config, wt: Path, card: str) -> str:
    """The ADR block for a brief or package: index + full text of ADRs the card names."""
    adrs = index(cfg, wt)
    if not adrs:
        return ""
    by_number = {a.number: a for a in adrs}

    def rel(p: Path) -> str:
        return p.relative_to(wt).as_posix()

    parts = [
        "", HEADING, "",
        (f"Decisions taken in this project (`{cfg.adr_dir}`). A change that contradicts an "
        "accepted one needs a new ADR that supersedes it, not a silent override. A decision "
        "with a trade-off: `h progress note --adr \"…\"`; write the ADR by the project's "
        "template."), "",
        *[f"- {a.ref} [{a.status}] {a.title} — `{rel(a.path)}`" for a in adrs],
    ]  # fmt: skip
    named = [n for n in mentioned(card) if n in by_number]
    missing = [n for n in mentioned(card) if n not in by_number]
    if missing:
        parts += ["", "Named in the card but not found: "
                  + ", ".join(f"ADR-{n:04d}" for n in missing)]  # fmt: skip
    for n in named:
        a = by_number[n]
        body = a.path.read_text(encoding="utf-8", errors="replace").strip()
        fence = "`" * max(4, _longest_run(body, "`") + 1)
        parts += ["", f"### {a.ref} (named in the card) — `{rel(a.path)}`", "",
                  fence + "markdown", body, fence]  # fmt: skip
    return "\n".join(parts) + "\n"


def _longest_run(text: str, ch: str) -> int:
    return max(
        (len(m.group(0)) for m in re.finditer(re.escape(ch) + "+", text)), default=0
    )


def with_section(cfg: Config, wt: Path, card: str) -> str:
    """`card` with its ADR block replaced by one built from `wt` now."""
    card = strip(card)
    block = section(cfg, wt, card)
    return card.rstrip("\n") + "\n" + block if block else card


def strip(text: str) -> str:
    cut = text.find("\n" + HEADING + "\n")
    return text if cut < 0 else text[: cut + 1]


def candidates(notes: list[dict]) -> list[str]:
    """Ledger notes marked `adr?` (`h progress note --adr`) — ADR candidates for the PR."""
    return [n["text"] for n in notes if n.get("kind") == "adr" and n.get("text")]

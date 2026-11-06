"""`h progress` — a task's ledger that survives compaction and a crashed session.

The state is `.artifacts/progress.json` in the task worktree; `.artifacts/progress.md` is
rendered from it on every change, so the agent's notes never collide with the mechanical
part. Code writes the mechanics (HEAD, changed files, last `h check`, review and CI
rounds); the agent adds notes. An AC-T item is marked done only by `h progress done`, and
only when the last `h check` on this very commit was green — not on the agent's word
(superpowers `task-done`). The marks are a hint for the next session, not a ship gate.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import adr, affected, check, lock, review
from .config import Config

STATE = Path(".artifacts") / "progress.json"
FILE = Path(".artifacts") / "progress.md"
BRIEF = Path(".artifacts") / "task" / "brief.md"
CI_LOGS = Path(".artifacts") / "ci"
NOTES_MAX = 20
FILES_MAX = 15
AC_T = re.compile(r"^\s*- \[[ xX]\] (T\d+)\b[^:]*:?\s*(.*)$")


class ProgressError(ValueError):
    pass


@dataclass
class Item:
    id: str
    text: str


def card_items(wt: Path) -> list[Item]:
    """AC-T items of the card in brief.md (`- [ ] T1 (←B1): …`)."""
    path = wt / BRIEF
    if not path.is_file():
        return []
    items: list[Item] = []
    for line in adr.strip(path.read_text(encoding="utf-8")).splitlines():
        m = AC_T.match(line)
        if m and m.group(1) not in {i.id for i in items}:
            items.append(Item(m.group(1), m.group(2).strip()))
    return items


def _load(wt: Path) -> dict:
    try:
        data = json.loads((wt / STATE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"done": {}, "notes": []}
    if not isinstance(data, dict):
        return {"done": {}, "notes": []}
    data.setdefault("done", {})
    data.setdefault("notes", [])
    return data


@contextmanager
def _locked(task_id: str) -> Iterator[None]:
    """One writer of a task's ledger at a time: two sessions or a hook adding notes at
    once would each save what they read and lose the other's note."""
    with lock.slot(1, name=f"progress-{task_id}", poll=0.05):
        yield


def _save(wt: Path, data: dict) -> None:
    # ADR candidates go into the PR body: the cap drops only the other notes.
    keep = len(data["notes"]) - NOTES_MAX
    data["notes"] = [n for i, n in enumerate(data["notes"])
                     if i >= keep or n.get("kind") == "adr"]  # fmt: skip
    path = wt / STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _check_line(wt: Path, head: str) -> str:
    rec = check.last(wt)
    if rec is None:
        return "never run in this worktree"
    if rec.get("failed"):
        state = "FAILED: " + ", ".join(rec["failed"])
    else:
        state = "green"
    extra = [w for w, on in (("dirty tree", rec.get("dirty")),
                             ("changed while it ran", rec.get("stale")),
                             ("subset: --only or --base", rec.get("partial")),
                             ("no step ran", not rec.get("ran")),
                             ("did not run: " + ", ".join(rec.get("missed", [])),
                              rec.get("missed"))) if on]  # fmt: skip
    if rec["head"] != head:
        extra.append(f"on {rec['head'][:10]}, not HEAD")
    return (
        state
        + (f" ({', '.join(extra)})" if extra else "")
        + f" at {rec.get('at', '?')}"
    )


def _review_line(wt: Path) -> str:
    try:
        v = review.read_verdict(wt)
        rnd = review._round(wt)
    except review.ReviewError as e:
        return f"unreadable ({e})"
    if v is None:
        return f"round {rnd}, no verdict yet" if rnd else "not started"
    return f"round {v.round}: {v.verdict} on {v.head[:10]}"


def render(cfg: Config, wt: Path, task_id: str) -> str:
    data = _load(wt)
    head = review._head(wt)
    subject = review._git(wt, "log", "-1", "--format=%s").strip()
    branch = review._git(wt, "branch", "--show-current").strip()
    try:
        files = affected.changed_files(cfg, affected.resolve_base(cfg, None))
    except affected.GitError:
        files = []
    shown = files[:FILES_MAX] + (
        [f"… {len(files) - FILES_MAX} more"] if len(files) > FILES_MAX else []
    )
    ci_rounds = len(list((wt / CI_LOGS).glob("round-*.log")))
    lines = [
        f"# Progress — {task_id}",
        "",
        "Written by `h progress`; add notes with `h progress note`, mark card items with",
        "`h progress done T<n>` (needs a green `h check` on HEAD). Do not edit by hand.",
        "",
        "## State (mechanical)",
        "",
        f"- branch: `{branch}` · HEAD `{head[:10]}` {subject}",
        f"- changed files ({len(files)}): " + (", ".join(f"`{f}`" for f in shown) or "none"),
        f"- last h check: {_check_line(wt, head)}",
        f"- review: {_review_line(wt)}",
        f"- CI failed rounds logged here: {ci_rounds}",
        f"- updated: {_now()}",
        "",
        "## Card (AC-T)",
        "",
    ]  # fmt: skip
    items = card_items(wt)
    for it in items:
        mark = data["done"].get(it.id)
        box = f"[x] {it.id} — done on `{mark['head'][:10]}`" if mark else f"[ ] {it.id}"
        lines.append(f"- {box}: {it.text}")
    if not items:
        lines.append("_no AC-T items in .artifacts/task/brief.md_")
    lines += ["", "## Notes (next steps, decisions)", ""]
    for n in data["notes"]:
        tag = {"decision": "decision", "adr": "decision, adr?"}.get(
            n.get("kind"), "next"
        )
        lines.append(f"- [{tag}] {n['text']} ({n.get('at', '')})")
    if not data["notes"]:
        lines.append("_none yet_")
    return "\n".join(lines) + "\n"


def update(cfg: Config, wt: Path, task_id: str) -> Path:
    """Re-render progress.md; the hook path — never touches the agent's notes."""
    with _locked(task_id):
        return _write(cfg, wt, task_id)


def _write(cfg: Config, wt: Path, task_id: str) -> Path:
    path = wt / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(cfg, wt, task_id), encoding="utf-8")
    return path


def done(cfg: Config, wt: Path, task_id: str, item: str) -> Path:
    item = item.upper()
    ids = [i.id for i in card_items(wt)]
    if item not in ids:
        raise ProgressError(
            f"no {item} in the card's AC-T ({', '.join(ids) or 'none in brief.md'})"
        )
    head = review._head(wt)
    rec = check.last(wt)
    why = (
        "no h check recorded in this worktree" if rec is None
        # Before the HEAD test: a commit during the run moves HEAD, and the honest
        # reason is that no step saw the new tree.
        else "the tree changed while the last h check ran" if rec.get("stale")
        else f"the last h check ran on {rec['head'][:10]}, not HEAD" if rec["head"] != head
        else "the last h check ran on a dirty tree — commit first" if rec.get("dirty")
        else "the last h check ran only a subset (--only or --base)" if rec.get("partial")
        else "no step ran in the last h check (nothing affected? try --full)"
        if not rec.get("ran")
        else "steps did not run: " + ", ".join(rec["missed"]) if rec.get("missed")
        else "the last h check failed: " + ", ".join(rec["failed"]) if rec.get("failed")
        else ""
    )  # fmt: skip
    if why:
        raise ProgressError(f"{item} not marked — {why}; run `h check` on HEAD first")
    with _locked(task_id):
        data = _load(wt)
        data["done"][item] = {"head": head, "at": _now()}
        _save(wt, data)
        return _write(cfg, wt, task_id)


def note(cfg: Config, wt: Path, task_id: str, text: str, kind: str = "next") -> Path:
    text = " ".join(text.split())
    if not text:
        raise ProgressError("empty note")
    with _locked(task_id):
        data = _load(wt)
        data["notes"].append({"kind": kind, "text": text, "at": _now()})
        _save(wt, data)
        return _write(cfg, wt, task_id)


def adr_candidates(wt: Path) -> list[str]:
    return adr.candidates(_load(wt)["notes"])


def read(wt: Path) -> str | None:
    path = wt / FILE
    return path.read_text(encoding="utf-8") if path.is_file() else None

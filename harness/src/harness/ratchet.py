"""`h ratchet` — suppression counts may only go down (SUMMARY #14).

Under pressure an agent silences a check instead of fixing it: a type-ignore comment, a
noqa, an allow attribute, an eslint-disable — or it skips the failing test ("never buy a
green CI", vllm tests/AGENTS.md). A prose rule does not stop that; a counter does. The
base lives in `.harness/ratchet.json`, committed with the project: a count above it is a
red `h check` naming the new lines; a count below it is committed by `h ship`.

Existing debt never blocks: the first base is the current state (`h init`).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from . import affected, deadcode, git
from .config import Config

BASE = Path(".harness") / "ratchet.json"
MAX_BYTES = 1_000_000  # bigger files are generated or vendored
VENDORED = {"node_modules", "vendor", "vendored", "third_party", ".venv"}
SHOW = 10  # new lines listed per grown counter

PY = (".py", ".pyi")
RS = (".rs",)
TS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


def _codes(group: str | None) -> list[str]:
    return [c.strip() for c in (group or "").split(",") if c.strip()]


def _eslint_codes(group: str | None) -> list[str]:
    # "rule-a, rule-b -- why": the justification is not a rule, editing it is not growth.
    rules = re.split(r"(?:^|\s)--(?:\s|$)", group or "", maxsplit=1)[0]
    return [c for c in re.split(r"[\s,]+", rules) if c]


@dataclass(frozen=True)
class Pattern:
    stack: str
    kind: str  # suppress | skip-test
    name: str
    regex: re.Pattern[str]
    exts: tuple[str, ...]
    per_code: bool = False  # one counter per rule code in group 1
    split: Callable[[str | None], list[str]] = _codes

    def rules(self, m: re.Match[str]) -> list[str]:
        codes = self.split(m.group(1)) if self.per_code and m.groups() else []
        return [f"{self.name}[{c}]" for c in codes] or [self.name]


PATTERNS = (
    Pattern("python", "suppress", "type: ignore",
            re.compile(r"#\s*type:\s*ignore\b(?:\[([^\]]*)\])?"), PY, True),
    Pattern("python", "suppress", "noqa",
            re.compile(r"#\s*noqa\b(?::\s*([A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*))?"), PY, True),
    Pattern("python", "suppress", "pyright: ignore",
            re.compile(r"#\s*pyright:\s*ignore\b(?:\[([^\]]*)\])?"), PY, True),
    Pattern("python", "skip-test", "pytest.mark",
            re.compile(r"@pytest\.mark\.(skip|skipif|xfail)\b"), PY, True),
    Pattern("python", "skip-test", "pytest",
            re.compile(r"\bpytest\.(skip|xfail)\("), PY, True),
    Pattern("python", "skip-test", "unittest",
            re.compile(r"@unittest\.(skip\w*|expectedFailure)\b"), PY, True),
    Pattern("rust", "suppress", "allow",
            re.compile(r"#!?\[(?:allow|expect)\(([^)]*)\)\]"), RS, True),
    Pattern("rust", "skip-test", "#[ignore]", re.compile(r"#\[ignore\b"), RS),
    Pattern("ts", "suppress", "eslint-disable",
            re.compile(r"\b(?:eslint|oxlint)-disable(?:-next-line|-line)?\b([^*\n]*)"),
            TS, True, _eslint_codes),
    Pattern("ts", "suppress", "@ts",
            re.compile(r"@ts-(ignore|expect-error|nocheck)\b"), TS, True),
    Pattern("ts", "skip-test", "test",
            re.compile(r"\b(?:it|test|describe)\.(skip|only|todo)\s*\("), TS, True),
    Pattern("ts", "skip-test", "x-test",
            re.compile(r"\b(xit|xtest|xdescribe)\s*\("), TS, True),
)  # fmt: skip

# The one accepted skip: a strict xfail that points at the open issue for a bug on main.
STRICT_XFAIL = re.compile(r"xfail\(.*strict\s*=\s*True.*(#\d+|issues/\d+)")


@dataclass(frozen=True)
class Hit:
    key: str  # "<stack>:<kind>:<rule>"
    path: str
    line: int
    text: str


def _hits(path: str, line_no: int, line: str) -> Iterator[Hit]:
    ext = Path(path).suffix
    for p in PATTERNS:
        if ext not in p.exts:
            continue
        for m in p.regex.finditer(line):
            if p.kind == "skip-test" and STRICT_XFAIL.search(line):
                continue
            for rule in p.rules(m):
                yield Hit(f"{p.stack}:{p.kind}:{rule}", path, line_no, line.strip())


def _git_text(cfg: Config, *argv: str) -> str:
    # The runner decodes with errors="replace": one undecodable byte must not hide the count.
    r = git.run(cfg.root, *argv)
    if r.returncode != 0:
        raise affected.GitError(f"git {' '.join(argv)}: {r.stderr.strip()}")
    return r.stdout


def _files(cfg: Config) -> list[str]:
    """Tracked and untracked-not-ignored: a new file the agent has not added still counts."""
    exts = {e for p in PATTERNS for e in p.exts}
    out = _git_text(cfg, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return sorted(
        {
            f
            for f in out.split("\0")
            if f and Path(f).suffix in exts and not VENDORED & set(Path(f).parts)
        }
    )


def _whitelists(cfg: Config) -> Iterator[Hit]:
    """Every deadcode whitelist entry is a suppression like a noqa: the list grows
    only by `h ratchet --update --allow-growth`, visible in the PR diff."""
    for stack in cfg.stacks:
        path = deadcode.whitelist_path(cfg, stack)
        try:
            rel = path.resolve().relative_to(cfg.root.resolve()).as_posix()
        except ValueError:  # outside the project: not ours to count
            continue
        for n, entry, _ in deadcode.entries(path):
            yield Hit(f"{stack}:suppress:deadcode-whitelist", rel, n, entry)


def scan(cfg: Config) -> list[Hit]:
    hits: list[Hit] = list(_whitelists(cfg))
    for rel in _files(cfg):
        path = cfg.root / rel
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # deleted but not staged, or a dangling link
            continue
        # LF only, as git numbers lines: splitlines() also breaks on CR, FF, U+2028.
        for i, line in enumerate(body.split("\n"), 1):
            hits.extend(_hits(rel, i, line))
    return hits


def count(hits: list[Hit]) -> dict[str, int]:
    return dict(sorted(Counter(h.key for h in hits).items()))


def parse(text: str) -> dict[str, int]:
    try:
        data = json.loads(text)
        return {str(k): int(v) for k, v in data["counts"].items()}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError) as e:
        raise ValueError(
            f"{BASE.as_posix()}: unreadable ({e}); restore it from git"
        ) from e


def load_base(cfg: Config) -> dict[str, int] | None:
    path = cfg.root / BASE
    if not path.is_file():
        return None
    return parse(path.read_text(encoding="utf-8"))


def write_base(cfg: Config, counts: dict[str, int]) -> None:
    path = cfg.root / BASE
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"version": 1, "counts": counts}
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8", newline="\n")


@dataclass
class Result:
    counts: dict[str, int]
    base: dict[str, int]
    grown: dict[str, tuple[int, int]]  # key → (base, now)
    shrunk: dict[str, tuple[int, int]]
    new: dict[
        str, list[Hit]
    ]  # grown key → the lines that are new since the base branch


def _added_lines(cfg: Config, base_ref: str | None) -> set[tuple[str, int]]:
    """(path, line) of every line added since the base branch, uncommitted work included."""
    try:
        sha = affected.resolve_base(cfg, base_ref)
        # quotepath off: a quoted non-ASCII path ("\321\202…") would match no hit.
        diff = _git_text(
            cfg, "-c", "core.quotepath=false", "diff", "-U0", "--no-renames",
            "--relative", sha,
        )  # fmt: skip
    except affected.GitError:
        return set()
    added: set[tuple[str, int]] = set()
    path, line, header = "", 0, False
    for ln in diff.split("\n"):
        if ln.startswith("diff --git "):
            header = True  # until the first hunk: "+++ b/…" is a header, not content
        elif header and ln.startswith("+++ "):
            # A path with a space gets a trailing tab from git: "+++ b/my mod.py\t".
            path = ln[6:].removesuffix("\t") if ln.startswith("+++ b/") else ""
        elif ln.startswith("@@"):
            header = False
            m = re.search(r"\+(\d+)", ln)
            line = int(m.group(1)) if m else 0
        elif not header and ln.startswith("+") and path:
            added.add((path, line))
            line += 1
    for rel in _git_text(cfg, "ls-files", "-z", "--others", "--exclude-standard").split(
        "\0"
    ):
        if rel:  # an untracked file is new as a whole
            added.add((rel, -1))
    return added


def check(cfg: Config, base_ref: str | None = None) -> Result | None:
    """None when the project has no base yet (ratchet not enabled)."""
    base = load_base(cfg)
    if base is None:
        return None
    hits = scan(cfg)
    counts = count(hits)
    keys = set(counts) | set(base)
    grown = {
        k: (base.get(k, 0), counts.get(k, 0))
        for k in sorted(keys)
        if counts.get(k, 0) > base.get(k, 0)
    }
    shrunk = {
        k: (base.get(k, 0), counts.get(k, 0))
        for k in sorted(keys)
        if counts.get(k, 0) < base.get(k, 0)
    }
    new: dict[str, list[Hit]] = {}
    if grown:
        added = _added_lines(cfg, base_ref)
        untracked = {p for p, n in added if n == -1}
        for k in grown:
            mine = [h for h in hits if h.key == k]
            fresh = [
                h for h in mine if (h.path, h.line) in added or h.path in untracked
            ]
            new[k] = fresh or mine  # base branch unknown: show every occurrence
    return Result(counts, base, grown, shrunk, new)


def update(cfg: Config, allow_growth: bool = False) -> tuple[dict[str, int], list[str]]:
    """Write the current counts as the base. Raising a count needs `allow_growth` — that
    change shows in the PR diff of `.harness/ratchet.json`, where the reviewer sees it."""
    counts = count(scan(cfg))
    base = load_base(cfg)
    grown = [k for k, n in counts.items() if base is not None and n > base.get(k, 0)]
    if grown and not allow_growth:
        return counts, grown
    write_base(cfg, counts)
    return counts, []


def limit(
    onto: dict[str, int], parent: dict[str, int], mine: dict[str, int]
) -> dict[str, int]:
    """What a base may hold after `mine` (a change of `parent`) is replayed onto `onto`:
    the other side's counts plus only the growth `mine` itself made — that growth was in
    its own diff, where a reviewer saw it."""
    keys = set(onto) | set(mine)
    return {k: onto.get(k, 0) + max(0, mine.get(k, 0) - parent.get(k, 0)) for k in keys}


def resolve(
    cfg: Config, onto: dict[str, int], parent: dict[str, int], mine: dict[str, int]
) -> dict[str, int]:
    """A conflict in the base is settled by a recount: the base follows from the code.
    A count above `limit` stays capped there, so `h check` names the extra suppression
    instead of the merge legitimising it."""
    cap = limit(onto, parent, mine)
    counts = {k: min(n, cap.get(k, 0)) for k, n in count(scan(cfg)).items()}
    counts = {k: n for k, n in counts.items() if n}
    write_base(cfg, counts)
    return counts


def lowers(old: dict[str, int], new: dict[str, int]) -> bool:
    """`new` raises no counter and adds none — the only base change a tool may commit."""
    return all(k in old and n <= old[k] for k, n in new.items())

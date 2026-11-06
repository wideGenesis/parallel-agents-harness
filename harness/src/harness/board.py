"""Project board (GitHub Projects v2): a picture of the task lifecycle, one board per repo.

The board only displays state — the claim on origin and the card labels are the truth
(ADR-0002). So every board call is best effort: a failure becomes one warning line and
never changes the outcome of the `h` command that triggered it.

The project's built-in Status field gets our stages as options, so GitHub's default
Board layout groups by lifecycle stage without any custom view (views have no API).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import gh, git
from .config import Config

STAGES = [
    ("Draft", "GRAY"),
    ("Needs clarification", "YELLOW"),
    ("Ready", "GREEN"),
    ("In progress", "PURPLE"),
    ("Review", "BLUE"),
    ("Merged", "BLUE"),
    ("Deployed", "ORANGE"),
    ("Done", "GREEN"),
    ("Dropped", "RED"),
]
LABEL_STAGE = {
    "draft": "Draft",
    "needs-clarification": "Needs clarification",
    "ready": "Ready",
    "in-progress": "In progress",
    "review": "Review",
    "merged": "Merged",
    "deployed": "Deployed",
    "dropped": "Dropped",
}
MANUAL_STEPS = [
    "open the board → View 1 → Layout: Board (grouped by Status)",
    "… → Workflows → Auto-archive items: `is:closed updated:<@today-14d` → On",
]


class BoardError(gh.GhError):
    pass


def _json(r: subprocess.CompletedProcess, what: str):
    if r.returncode != 0:
        raise BoardError(f"{what}: {(r.stderr or r.stdout).strip()}")
    return json.loads(r.stdout or "null")


def _cache_path(cfg: Config) -> Path:
    r = git.run(cfg.root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    base = Path(r.stdout.strip()) if r.returncode == 0 else cfg.root / ".git"
    return base / "harness" / "board.json"


def _ids(cfg: Config) -> dict:
    """project id, Status field id and option ids — cached in the shared .git, so a stage
    move costs two API calls instead of four."""
    path = _cache_path(cfg)
    key = f"{cfg.board_owner}/{cfg.board_number}"
    cache = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if key in cache and set(cache[key]["options"]) == {s for s, _ in STAGES}:
        return cache[key]
    n, owner = str(cfg.board_number), cfg.board_owner
    project = _json(
        gh.run(cfg.root, "project", "view", n, "--owner", owner, "--format", "json"),
        "project view",
    )
    fields = _json(
        gh.run(
            cfg.root, "project", "field-list", n, "--owner", owner, "--format", "json"
        ),
        "field-list",
    )
    status = next((f for f in fields["fields"] if f["name"] == "Status"), None)
    if status is None:
        raise BoardError("project has no Status field — run `h board init`")
    ids = {
        "project": project["id"],
        "field": status["id"],
        "options": {o["name"]: o["id"] for o in status.get("options", [])},
    }
    cache[key] = ids
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    return ids


def move(cfg: Config, issue_url: str, stage: str) -> str | None:
    """Put the issue in `stage`. Returns a warning line instead of raising."""
    if not cfg.board_number:
        return "board: not configured (h board init) — skipped"
    try:
        ids = _ids(cfg)
        option = ids["options"].get(stage)
        if option is None:
            return f"board: no column {stage!r} — run `h board init`"
        item = _json(
            gh.run(cfg.root, "project", "item-add", str(cfg.board_number), "--owner",
                   cfg.board_owner, "--url", issue_url, "--format", "json"),
            "item-add",
        )  # fmt: skip
        r = gh.run(cfg.root, "project", "item-edit", "--id", item["id"], "--project-id",
                   ids["project"], "--field-id", ids["field"],
                   "--single-select-option-id", option)  # fmt: skip
        if r.returncode != 0:
            raise BoardError(f"item-edit: {(r.stderr or r.stdout).strip()}")
    except (BoardError, gh.GhError, json.JSONDecodeError, KeyError, OSError) as e:
        return f"board: not updated ({str(e).splitlines()[0][:120]})"
    return None


def stage_of(labels: list[str], closed: bool = False) -> str:
    if "dropped" in labels:
        return "Dropped"
    if closed:
        return "Done"
    for label in ("deployed", "merged", "review", "in-progress", "ready",
                  "needs-clarification", "draft"):  # fmt: skip
        if label in labels:
            return LABEL_STAGE[label]
    return "Draft"


def _write_config(cfg: Config, owner: str, number: int) -> bool:
    path = cfg.root / "harness.toml"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if "[board]" in text:
        return False
    block = f'\n[board]\nowner = "{owner}"\nnumber = {number}\n'
    path.write_text(text.rstrip("\n") + "\n" + block, encoding="utf-8", newline="\n")
    return True


def init(cfg: Config) -> tuple[str, list[str]]:
    """Create or adopt the repo's board; idempotent. Returns (url, change lines)."""
    changes: list[str] = []
    repo = _json(gh.run(cfg.root, "repo", "view", "--json", "owner,name"), "repo view")
    owner, name = repo["owner"]["login"], repo["name"]
    number = cfg.board_number
    if not number:
        made = _json(gh.run(cfg.root, "project", "create", "--owner", owner, "--title",
                            name, "--format", "json"), "project create")  # fmt: skip
        number = int(made["number"])
        changes.append(f"project #{number} created")
    cfg.board_owner, cfg.board_number = owner, number
    link = gh.run(
        cfg.root,
        "project",
        "link",
        str(number),
        "--owner",
        owner,
        "--repo",
        f"{owner}/{name}",
    )
    if link.returncode == 0:
        changes.append(f"linked to {owner}/{name}")

    fields = _json(gh.run(cfg.root, "project", "field-list", str(number), "--owner", owner,
                          "--format", "json"), "field-list")  # fmt: skip
    status = next(f for f in fields["fields"] if f["name"] == "Status")
    if [o["name"] for o in status.get("options", [])] != [s for s, _ in STAGES]:
        options = ",".join(
            f'{{name:"{s}",color:{c},description:""}}' for s, c in STAGES
        )
        query = (
            "mutation($f:ID!){updateProjectV2Field(input:{fieldId:$f,"
            f"singleSelectOptions:[{options}]}}){{projectV2Field{{... on ProjectV2SingleSelectField{{id}}}}}}}}"
        )
        _json(gh.run(cfg.root, "api", "graphql", "-f", f"query={query}", "-f", f"f={status['id']}"),
              "set Status options")  # fmt: skip
        changes.append("Status columns: " + " → ".join(s for s, _ in STAGES))
        _cache_path(cfg).unlink(missing_ok=True)
    if _write_config(cfg, owner, number):
        changes.append("harness.toml: [board] added — commit it")

    # Every page: `issue list --limit` would leave the rest off the board. The
    # issues endpoint also returns PRs; they are not cards.
    r = gh.run(cfg.root, "api", "--paginate", "--jq",
               ".[] | select(.pull_request | not) | {url: .html_url, state, labels: [.labels[].name]}",
               "repos/{owner}/{repo}/issues?state=all&per_page=100")  # fmt: skip
    if r.returncode != 0:
        raise BoardError(f"issue list: {(r.stderr or r.stdout).strip()}")
    issues = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip()]
    for i in issues:
        closed = i["state"].upper() == "CLOSED"
        warn = move(cfg, i["url"], stage_of(i["labels"], closed))
        if warn:
            changes.append(warn)
    changes.append(f"{len(issues)} issues placed")
    # The link from the API: a hand-made /users/... is wrong for an organisation's repo.
    project = _json(gh.run(cfg.root, "project", "view", str(number), "--owner", owner,
                           "--format", "json"), "project view")  # fmt: skip
    return project["url"], changes

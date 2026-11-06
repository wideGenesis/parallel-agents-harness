import json
import shutil
import subprocess
from pathlib import Path

import pytest

from harness import deadcode, gh, init, installed, machine, pg

POLYGON = Path(__file__).resolve().parents[2] / "polygon"
IGNORE = shutil.ignore_patterns(
    "target", ".venv", "node_modules", "__pycache__", ".artifacts"
)


def git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture(autouse=True)
def _outside_agent_session(monkeypatch):
    """Tests run as the user would, like CI; an agent session sets CLAUDECODE."""
    monkeypatch.delenv("CLAUDECODE", raising=False)


@pytest.fixture(autouse=True)
def _quiet_machine(request, tmp_path, monkeypatch):
    """The machine budget with its defaults and an idle CPU: the suite itself
    loads the machine, and a heavy step under test would wait for it. test_machine.py
    tests the real readings."""
    monkeypatch.setenv("HARNESS_MACHINE", str(tmp_path / "no-machine.toml"))
    # The defaults of a 24-thread machine: a 2-vCPU CI runner would make them 1 core.
    monkeypatch.setattr(machine, "threads", lambda: 24)
    if request.module.__name__ != "test_machine":
        monkeypatch.setattr(machine, "load", lambda sample=0.5: 0.0)


@pytest.fixture(autouse=True)
def _isolated_lock_dir(tmp_path, monkeypatch):
    """Never take the machine-wide slots: the `h check` running these tests may hold the
    last one, and a test waiting for it waits forever."""
    monkeypatch.setenv("HARNESS_LOCK_DIR", str(tmp_path / "locks"))


@pytest.fixture(autouse=True)
def _own_postgres_prefix(monkeypatch):
    """A test never sees a real run's throwaway Postgres: under the isolated lock dir every
    real run's lock looks free, and a sweep removed live databases on the machine.
    `h-pytest-pg-` is not matched by `name=h-check-pg-`, so older h's sweeps skip tests' too."""
    monkeypatch.setattr(pg, "PREFIX", "h-pytest-pg-")


@pytest.fixture(autouse=True)
def _installed_h_is_current(monkeypatch):
    """doctor and ship compare the running h with origin; under test it runs from
    a worktree with unmerged changes. test_installed.py imports the real check."""
    monkeypatch.setattr(installed, "check", lambda *a, **k: installed.Status("ok"))


@pytest.fixture(autouse=True)
def _deadcode_tools_off(request, monkeypatch):
    """The deadcode step runs real tools (uv sync, cargo-machete, knip) on every polygon
    copy a test checks; tests of other steps see it find nothing. test_deadcode.py runs it
    for real."""
    if request.module.__name__ != "test_deadcode":
        monkeypatch.setitem(deadcode.ADAPTERS, "rust", lambda *a: ([], ""))
        monkeypatch.setitem(deadcode.ADAPTERS, "python", lambda *a: ([], ""))
        monkeypatch.setitem(deadcode.ADAPTERS, "ts", lambda *a: ([], ""))


HARNESS_SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _harness_sha(monkeypatch):
    """`h init` pins the project's CI to a harness commit: no git or gh lookup."""
    monkeypatch.setattr(init, "harness_sha", lambda: HARNESS_SHA)


def make_repo(tmp_path, subdir=""):
    """A throwaway git repo with polygon committed on main and a `work` branch checked out."""
    repo = tmp_path / "repo"
    proj = repo / subdir if subdir else repo
    shutil.copytree(POLYGON, proj, ignore=IGNORE)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "switch", "-q", "-c", "work")
    return proj


# git is the slow part on Windows: one process costs ~40-60 ms whatever the folder, and the
# fixtures below used to start ~10 of them per test. They are built once per session
# (per xdist worker) and copied; a copy is as independent as a fresh build.
URL = "@ORIGIN@"  # origin url in template configs, replaced by the copy's own path


def _copy_repos(template: Path, dest: Path, names: tuple[str, ...]) -> None:
    for name in names:
        shutil.copytree(template / name, dest / name)
        cfg = dest / name / ".git" / "config"
        if cfg.is_file():
            # bytes, not text: write_text on Windows would turn git's LF into CRLF
            data = cfg.read_bytes()
            if URL.encode() in data:
                origin = (dest / "origin.git").as_posix().encode("utf-8")
                cfg.write_bytes(data.replace(URL.encode(), origin))


@pytest.fixture(scope="session")
def _repo_template(tmp_path_factory):
    base = tmp_path_factory.mktemp("repo-template")
    make_repo(base)
    return base


@pytest.fixture
def repo(tmp_path, _repo_template):
    _copy_repos(_repo_template, tmp_path, ("repo",))
    return tmp_path / "repo"


TOML = """
[project]
name = "poly"
base = "main"

[worktrees]
dir = ".worktrees"
include = [".env"]
ports = ["api", "web"]

[tasks]
claim_ttl_hours = 4
"""


def out(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


WORLD = ("origin.git", "seed", "a", "b")


@pytest.fixture
def world(tmp_path, _world_template):
    """origin (bare) + two independent clones = two sessions on possibly different hosts."""
    _copy_repos(_world_template, tmp_path, WORLD)
    return tmp_path / "origin.git", tmp_path / "a", tmp_path / "b"


@pytest.fixture(scope="session")
def _world_template(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("world-template")
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    shutil.copytree(POLYGON, seed, ignore=IGNORE)
    (seed / "harness.toml").write_text(TOML, encoding="utf-8")
    (seed / ".gitignore").write_text(".worktrees/\n.env\n", encoding="utf-8")
    git(seed, "init", "-q", "-b", "main")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "init")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "origin", "main")
    for name in ("a", "b"):
        c = tmp_path / name
        git(tmp_path, "clone", "-q", str(origin), str(c))
        # CI runners have no git identity; anything that commits in a clone needs one.
        git(c, "config", "user.name", name)
        git(c, "config", "user.email", f"{name}@example.com")
        (c / ".env").write_text("SECRET=1\n", encoding="utf-8")
    for name in ("seed", "a", "b"):
        git(tmp_path / name, "remote", "set-url", "origin", URL)
    return tmp_path


# --- fake GitHub ---


class FakeGitHub:
    def __init__(self):
        self.issues: dict[int, dict] = {}
        self.milestones: list[str] = []
        self.labels: set[str] = set()
        self.prs: list[
            dict
        ] = []  # {number, state, headRefName, mergeCommit, url, auto}
        self.unindexed: set[int] = set()  # PRs search does not see yet (fresh merges)
        self.runs: dict[str, dict] = {}  # merge sha -> workflow run
        self.closed_with: dict[int, str] = {}
        self.comments: dict[int, list[dict]] = {}  # issue/PR number -> comments
        self.pr_heads: dict[int, str] = {}  # PR number -> head sha
        self.viewer = "me"  # the account gh acts as; authors our PRs and comments
        self.statuses: dict[str, dict] = {}  # sha -> commit status fields
        self.required: list[str] | str = []  # required contexts, or "404" / "error"
        self.auto_merge_error = ""  # stderr of `gh pr merge --auto`, "" = it works

    def comment(self, n, body, user=None):
        cs = self.comments.setdefault(n, [])
        cs.append({"id": 1000 + sum(map(len, self.comments.values())), "body": body,
                   "user": user or self.viewer})  # fmt: skip

    def add_issue(
        self, body, labels, title="Login with Google", state="OPEN", number=None
    ):
        n = number or len(self.issues) + 1
        self.issues[n] = {"number": n, "title": title, "body": body, "labels": list(labels),
                          "milestone": None, "state": state, "comments": [], "url": f"https://x/issues/{n}"}  # fmt: skip
        return n

    def __call__(self, cwd, *argv):
        a = list(argv)
        ok = lambda out="": subprocess.CompletedProcess(a, 0, out, "")
        if a[:2] == ["issue", "view"]:
            i = dict(self.issues[int(a[2])])
            i["labels"] = [{"name": n} for n in i["labels"]]
            return ok(json.dumps(i))
        if a[:2] == ["issue", "create"]:
            labels = [a[k + 1] for k, x in enumerate(a) if x == "--label"]
            n = self.add_issue(
                a[a.index("--body") + 1], labels, a[a.index("--title") + 1]
            )
            if "--milestone" in a:
                self.issues[n]["milestone"] = {"title": a[a.index("--milestone") + 1]}
            return ok(self.issues[n]["url"] + "\n")
        if a[:2] == ["issue", "edit"]:
            i = self.issues[int(a[2])]
            i["labels"] += [a[k + 1] for k, x in enumerate(a) if x == "--add-label"]
            gone = {a[k + 1] for k, x in enumerate(a) if x == "--remove-label"}
            i["labels"] = [x for x in i["labels"] if x not in gone]
            return ok()
        if a[:2] == ["label", "create"]:
            new = a[2] not in self.labels
            self.labels.add(a[2])
            return subprocess.CompletedProcess(a, 0 if new else 1, "", "")
        if a[:2] == ["label", "edit"]:
            return ok()
        if a[:2] == ["api", "graphql"] and any("LabeledEvent" in x for x in a):
            # Edit history: an issue with "confirmed_body" was edited after confirmation.
            i = self.issues[int(next(x for x in a if x.startswith("n="))[2:])]
            # Shape as observed on GitHub: newest first, newest == the body, oldest ==
            # the original at creation. "history" overrides it for odd shapes.
            then = i.get("confirmed_body")
            edits = [] if then is None else [
                {"editedAt": "2026-01-03T00:00:00Z", "diff": i["body"]},
                {"editedAt": "2026-01-01T00:00:00Z", "diff": then},
            ]  # fmt: skip
            edits = i.get("history", edits)
            events = [
                {"createdAt": "2026-01-02T00:00:00Z", "label": {"name": "ac-confirmed"}}
            ]
            issue = {
                "userContentEdits": {"nodes": edits},
                "timelineItems": {"nodes": events},
            }
            return ok(json.dumps({"data": {"repository": {"issue": issue}}}))
        if a[0] == "api" and any("/statuses/" in x for x in a):
            sha = next(x for x in a if "/statuses/" in x).rsplit("/", 1)[-1]
            field = {x.split("=", 1)[0]: x.split("=", 1)[1] for x in a if "=" in x}
            self.statuses[sha] = field
            return ok("{}")
        if a[0] == "api" and any("required_status_checks" in x for x in a):
            # self.required: list of contexts | "404" (no protection) | "error"
            if self.required in ("404", "error"):
                err = (
                    "HTTP 404: Not Found"
                    if self.required == "404"
                    else "HTTP 500: boom"
                )
                return subprocess.CompletedProcess(a, 1, "", err)
            checks = [{"context": c} for c in self.required if c.startswith("app:")]
            contexts = [c for c in self.required if not c.startswith("app:")]
            return ok(json.dumps({"contexts": contexts, "checks": checks}))
        if a[:2] == ["api", "user"]:
            return ok(self.viewer + "\n")
        if a[0] == "api" and "/comments" in " ".join(a):
            path = next(x for x in a[1:] if x.startswith("repos/"))
            body = next((x[5:] for x in a if x.startswith("body=")), None)
            if "-X" in a:  # PATCH issues/comments/<id>
                cid = int(path.rsplit("/", 1)[-1])
                c = next(
                    c for cs in self.comments.values() for c in cs if c["id"] == cid
                )
                c["body"] = body
                return ok("{}")
            n = int(path.split("/issues/")[1].split("/")[0])
            if body is not None:
                self.comment(n, body)
                return ok("{}")
            # --paginate --jq '.[] | {...}': one compact object per line
            return ok("".join(json.dumps(c) + "\n" for c in self.comments.get(n, [])))
        if a[0] == "api" and any("milestones" in x for x in a):
            if "-f" in a:
                self.milestones.append(a[a.index("-f") + 1].split("=", 1)[1])
                return ok("{}")
            # One page of 100 unless --paginate, as on GitHub.
            titles = self.milestones if "--paginate" in a else self.milestones[:100]
            if "--jq" in a:  # only `.[].title` is asked
                return ok("".join(t + "\n" for t in titles))
            return ok(json.dumps([{"title": t} for t in titles]))
        if a[:2] == ["issue", "list"] and "--label" in a:
            want = {a[k + 1] for k, x in enumerate(a) if x == "--label"}
            return ok(json.dumps([
                {"number": i["number"], "title": i["title"], "url": i["url"]}
                for i in self.issues.values()
                if i["state"] == "OPEN" and want <= set(i["labels"])
            ]))  # fmt: skip
        if a[:2] == ["issue", "list"] and "--milestone" in a:
            ms = a[a.index("--milestone") + 1]
            return ok(json.dumps([
                {**i, "labels": [{"name": n} for n in i["labels"]]}
                for i in self.issues.values() if (i["milestone"] or {}).get("title") == ms
            ]))  # fmt: skip
        if a[:2] == ["issue", "close"]:
            n = int(a[2])
            self.issues[n]["state"] = "CLOSED"
            self.closed_with[n] = a[a.index("--comment") + 1]
            if "--reason" in a:
                self.issues[n]["state_reason"] = a[a.index("--reason") + 1]
            return ok()
        if a[:2] == ["pr", "list"]:
            # As on GitHub: newest first, cut at --limit (default 30); --head is exact,
            # `--search head:p` a prefix match that misses PRs not indexed yet.
            prs = sorted(self.prs, key=lambda p: p["number"], reverse=True)
            if "--head" in a:
                prs = [p for p in prs if p["headRefName"] == a[a.index("--head") + 1]]
            if "--search" in a:
                q = a[a.index("--search") + 1].removeprefix("head:")
                prs = [p for p in prs if p["headRefName"].startswith(q)
                       and p["number"] not in self.unindexed]  # fmt: skip
            limit = int(a[a.index("--limit") + 1]) if "--limit" in a else 30
            return ok(json.dumps(prs[:limit]))
        if a[:2] == ["pr", "view"] and "headRefOid,author" in a:
            head = self.pr_heads.get(int(a[2]), "")
            return ok(
                json.dumps({"headRefOid": head, "author": {"login": self.viewer}})
            )
        if a[:2] == ["pr", "view"]:
            pr = next(
                (p for p in self.prs if p["headRefName"] == a[2] or p["url"] == a[2]),
                None,
            )
            if pr is None:
                return subprocess.CompletedProcess(a, 1, "", "no pull requests found")
            return ok(json.dumps(pr))
        if a[:2] == ["pr", "create"]:
            n = 100 + len(self.prs)
            url = f"https://x/pull/{n}"
            self.prs.append({"number": n, "state": "OPEN", "headRefName": a[a.index("--head") + 1],
                             "mergeCommit": None, "url": url, "auto": False})  # fmt: skip
            return ok(url + "\n")
        if a[:2] == ["pr", "edit"] and "--add-label" in a:
            pr = next(p for p in self.prs if p["url"] == a[2])
            pr.setdefault("labels", []).append(a[a.index("--add-label") + 1])
            return ok()
        if a[:2] == ["pr", "close"]:
            pr = next(p for p in self.prs if p["url"] == a[2])
            pr["state"] = "CLOSED"
            return ok()
        if a[:2] == ["pr", "merge"]:
            pr = next(
                p for p in self.prs if p["url"] == a[2] or p["headRefName"] == a[2]
            )
            if "--auto" in a and self.auto_merge_error:
                return subprocess.CompletedProcess(a, 1, "", self.auto_merge_error)
            pr["auto"] = "--auto" in a
            return ok()
        if a[:2] == ["run", "list"] and "--branch" in a:
            # newest first: the order runs were recorded in, reversed
            return ok(
                json.dumps(
                    [{**r, "headSha": sha} for sha, r in reversed(self.runs.items())]
                )
            )
        if a[:2] == ["run", "view"]:
            r = next(r for r in self.runs.values() if r.get("databaseId") == int(a[2]))
            return ok(json.dumps({"jobs": r.get("jobs", [])}))
        if a[:2] == ["run", "list"]:
            run = self.runs.get(a[a.index("--commit") + 1])
            return ok(json.dumps([run] if run else []))
        raise AssertionError(f"unexpected gh call: {a}")


@pytest.fixture
def github(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(gh, "run", fake)
    return fake

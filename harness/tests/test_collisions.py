import json
import subprocess
import time

from conftest import git

from harness import check, cli, collisions, config


def put(repo, rel, text="same\n"):
    """Stage `rel` under exactly this spelling. The same text for every spelling: on a
    case-insensitive disk they are one file, and git must see none of them modified."""
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    sha = subprocess.run(
        ["git", "hash-object", "-w", rel],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip
    git(repo, "update-index", "--add", "--cacheinfo", f"100644,{sha},{rel}")


def commit(repo, msg):
    git(repo, "commit", "-q", "-m", msg)


def on_base(repo, *pairs):
    """Commit the paths and move main to it: a collision the task did not make."""
    for rel in pairs:
        put(repo, rel)
    commit(repo, "old")
    git(repo, "branch", "-f", "main", "HEAD")


def step_result(repo):
    cfg = config.load(repo)
    [step] = [
        s for s in check.plan(cfg, [], {}, ("lint",)) if s.stack == "case-collisions"
    ]
    [res] = check.run(cfg, [step])
    return res


def test_find_groups_by_casefold_and_reports_topmost():
    found = collisions.find(
        ["docs/ADR/a.md", "docs/adr/a.md", "docs/adr/b.md", "src/Foo.ts", "src/foo.ts",
         "lib/x", "Lib/y", "ok/one.py"],
        changed=["src/foo.ts"],
    )  # fmt: skip
    assert found.errors == [["src/Foo.ts", "src/foo.ts"]]
    assert found.warnings == [["docs/ADR/", "docs/adr/"], ["Lib/", "lib/"]]


def test_file_and_directory_collide():
    found = collisions.find(["foo", "Foo/bar"], changed=["Foo/bar"])
    assert found.errors == [["Foo/", "foo"]]


def test_no_collisions_in_20000_paths_under_a_second():
    paths = [f"pkg{i % 50}/mod{i % 400}/file_{i}.py" for i in range(20_000)]
    start = time.monotonic()
    found = collisions.find(paths, changed=paths[:100])
    assert time.monotonic() - start < 1.0
    assert (found.errors, found.warnings, found.paths) == ([], [], 20_000)


def test_new_collision_fails_and_names_both(repo, capsys):
    git(repo, "config", "core.ignorecase", "false")
    on_base(repo, "Notes.md")
    put(repo, "notes.md")
    commit(repo, "new")
    res = step_result(repo)
    assert res.status == "fail"
    assert "Notes.md | notes.md" in res.tail


def test_new_file_in_differently_cased_directory_fails(repo):
    git(repo, "config", "core.ignorecase", "false")
    on_base(repo, "docs/adr/a.md")
    put(repo, "docs/ADR/b.md")
    commit(repo, "new")
    res = step_result(repo)
    assert res.status == "fail" and "docs/ADR/ | docs/adr/" in res.tail


def test_old_collision_warns_and_passes(repo, capsys):
    git(repo, "config", "core.ignorecase", "false")
    on_base(repo, "Old.md", "old.md")
    res = step_result(repo)
    assert res.status == "ok"
    assert res.warnings == ["old case collision: Old.md | old.md"]
    assert cli.main(["-C", str(repo), "check", "--only", "lint"]) == 0
    assert "Old.md | old.md" in capsys.readouterr().out
    assert cli.main(["-C", str(repo), "check", "--only", "lint", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    [row] = [r for r in rows if r["stack"] == "case-collisions"]
    assert row["status"] == "ok" and row["warnings"] == res.warnings


def test_clean_repo_shows_the_step(repo, capsys):
    assert cli.main(["-C", str(repo), "check"]) == 0
    out = capsys.readouterr().out
    assert "1 ok, 0 failed" in out

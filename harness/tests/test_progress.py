"""Progress ledger that survives compaction."""

import io
import json

import pytest
from conftest import git
from test_review import CHECKS, TRIVIAL

from harness import check, cli, config, init, progress, task

AC_T = """
### AC-T (technical, ← AC-B)

- [ ] T1 (←B1): first item
- [ ] T2 (←B2): second item
"""


@pytest.fixture
def wt(world, github):
    _, a, _ = world
    with (a / "harness.toml").open("a", encoding="utf-8") as f:
        f.write(CHECKS)
    git(a, "commit", "-qam", "checks")
    git(a, "push", "-q", "origin", "main")
    github.add_issue(TRIVIAL, ["ac-confirmed"], number=5)
    r = task.start(config.load(a), "gh-5")
    (r.path / progress.BRIEF).write_text("# card\n" + AC_T, encoding="utf-8")
    f = r.path / "py" / "poly-lib" / "src" / "poly_lib" / "__init__.py"
    f.write_text(f.read_text(encoding="utf-8") + "# change\n", encoding="utf-8")
    git(r.path, "commit", "-qam", "feat: change")
    return r.path


def ledger(wt) -> str:
    text = progress.read(wt)
    assert text is not None
    return text


def stdin(monkeypatch, payload):
    raw = json.dumps(payload).encode("utf-8")
    monkeypatch.setattr(
        "sys.stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
    )


def test_done_needs_a_green_check_on_head(wt):
    cfg = config.load(wt)
    with pytest.raises(progress.ProgressError, match="no h check recorded"):
        progress.done(cfg, wt, "gh-5", "T1")

    assert cli.main(["-C", str(wt), "check"]) == 0
    progress.done(cfg, wt, "gh-5", "t1")  # case-insensitive
    assert "[x] T1" in ledger(wt) and "[ ] T2" in ledger(wt)

    rec = check.last(wt)
    assert rec is not None
    (wt / check.LAST).write_text(
        json.dumps({**rec, "failed": ["python test"]}), encoding="utf-8"
    )
    with pytest.raises(progress.ProgressError, match="failed: python test"):
        progress.done(cfg, wt, "gh-5", "T2")

    (wt / "new.txt").write_text("x", encoding="utf-8")  # uncommitted work
    assert cli.main(["-C", str(wt), "check"]) == 0
    with pytest.raises(progress.ProgressError, match="dirty tree"):
        progress.done(cfg, wt, "gh-5", "T2")
    git(wt, "add", "new.txt")
    git(wt, "commit", "-qm", "more")
    with pytest.raises(progress.ProgressError, match="not HEAD"):
        progress.done(cfg, wt, "gh-5", "T2")

    assert cli.main(["-C", str(wt), "check", "--only", "fmt"]) == 0
    with pytest.raises(progress.ProgressError, match="subset"):
        progress.done(cfg, wt, "gh-5", "T2")
    # `--base HEAD` plans no steps at all: green, but it proved nothing.
    assert cli.main(["-C", str(wt), "check", "--base", "HEAD"]) == 0
    with pytest.raises(progress.ProgressError, match="subset"):
        progress.done(cfg, wt, "gh-5", "T2")
    with pytest.raises(progress.ProgressError, match="no T9"):
        progress.done(cfg, wt, "gh-5", "T9")


def test_notes_survive_updates_and_are_capped(wt):
    cfg = config.load(wt)
    progress.note(cfg, wt, "gh-5", "chose A over B", kind="adr")
    for i in range(progress.NOTES_MAX + 4):
        progress.note(cfg, wt, "gh-5", f"step {i}")
    progress.update(cfg, wt, "gh-5")  # what the hooks do
    text = ledger(wt)
    assert f"step {progress.NOTES_MAX + 3}" in text
    assert "step 3\n" not in text  # oldest dropped
    assert "chose A over B" in text  # an ADR candidate outlives the cap
    assert text.count("- [next]") == progress.NOTES_MAX
    assert "HEAD" in text and "feat: change" in text  # mechanics written by code


def test_cli_progress(wt, capsys):
    assert (
        cli.main(["-C", str(wt), "progress", "note", "--decision", "use", "testmon"])
        == 0
    )
    assert cli.main(["-C", str(wt), "progress", "done", "T1"]) == 1  # no check yet
    assert "T1 not marked" in capsys.readouterr().out
    assert cli.main(["-C", str(wt), "progress", "show"]) == 0
    assert "[decision] use testmon" in capsys.readouterr().out
    assert cli.main(["-C", str(wt), "progress", "note", "write", "the", "docs"]) == 0
    assert cli.main(["-C", str(wt), "progress", "show", "--json"]) == 0
    out = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert out["task"] == "gh-5" and out["done"] == 0
    assert "[next] write the docs" in ledger(wt)


def test_hooks_write_and_restore_the_ledger(wt, monkeypatch, capsys):
    stdin(monkeypatch, {"cwd": str(wt), "trigger": "auto"})
    assert cli.main(["hook", "pre-compact"]) == 0
    assert (wt / progress.FILE).is_file()  # mechanics written with no agent note
    capsys.readouterr()

    stdin(monkeypatch, {"cwd": str(wt), "source": "compact"})
    assert cli.main(["hook", "session-start"]) == 0
    out = capsys.readouterr().out
    assert "Progress — gh-5" in out and "[ ] T1" in out


def test_hooks_are_silent_outside_a_task_and_never_block(tmp_path, monkeypatch, capsys):
    stdin(monkeypatch, {"cwd": str(tmp_path)})
    assert cli.main(["hook", "session-start"]) == 0
    stdin(monkeypatch, {"cwd": str(tmp_path)})
    assert cli.main(["hook", "some-future-event"]) == 0  # newer settings, older h
    assert capsys.readouterr().out == ""


def test_resume_prints_the_ledger(world, wt, capsys):
    _, a, _ = world
    progress.note(config.load(wt), wt, "gh-5", "next: write the docs")
    capsys.readouterr()
    assert cli.main(["-C", str(a), "task", "start", "gh-5", "--resume"]) == 0
    out = capsys.readouterr().out
    assert "resumed" in out and "next: write the docs" in out


def test_init_installs_ledger_hooks(tmp_path):
    (tmp_path / "harness.toml").write_text(
        '[project]\nname = "demo"\n', encoding="utf-8"
    )
    settings: dict = {}
    changes = init._merge_settings(settings, config.load(tmp_path))
    assert "hook PreCompact → h hook pre-compact" in changes
    [pre] = settings["hooks"]["PreCompact"]
    assert "matcher" not in pre and pre["hooks"][0]["args"] == ["hook", "pre-compact"]
    [start] = settings["hooks"]["SessionStart"]
    assert start["matcher"] == "startup|resume|compact"
    assert init._merge_settings(settings, config.load(tmp_path)) == []  # idempotent


def test_a_commit_during_the_check_makes_it_stale(wt, monkeypatch):
    # the green result belongs to the tree the steps saw, not to a later commit.
    cfg = config.load(wt)
    real = check.run

    def commit_meanwhile(*a, **kw):
        results = real(*a, **kw)
        (wt / "late.txt").write_text("x", encoding="utf-8")
        git(wt, "add", "late.txt")
        git(wt, "commit", "-qm", "late")
        return results

    monkeypatch.setattr(check, "run", commit_meanwhile)
    assert cli.main(["-C", str(wt), "check"]) == 0
    rec = check.last(wt)
    assert rec is not None and rec["stale"]
    with pytest.raises(progress.ProgressError, match="changed while"):
        progress.done(cfg, wt, "gh-5", "T1")
    progress.update(cfg, wt, "gh-5")
    assert "changed while it ran" in ledger(wt)


def test_a_check_where_steps_did_not_run_proves_nothing(wt, monkeypatch):
    cfg = config.load(wt)

    def none_ran(cfg, steps, on_wait=None):
        return [check.StepResult(s, "skip") for s in steps]

    monkeypatch.setattr(check, "run", none_ran)
    assert cli.main(["-C", str(wt), "check"]) == 0
    with pytest.raises(progress.ProgressError, match="no step ran"):
        progress.done(cfg, wt, "gh-5", "T1")

    def one_missed(cfg, steps, on_wait=None):
        return [
            check.StepResult(s, "skip" if s.kind == "lint" else "ok") for s in steps
        ]

    monkeypatch.setattr(check, "run", one_missed)
    assert cli.main(["-C", str(wt), "check"]) == 0
    with pytest.raises(progress.ProgressError, match="did not run: python lint"):
        progress.done(cfg, wt, "gh-5", "T1")


def test_note_arguments_are_not_lost(wt, capsys):
    for argv, says in (
        (["note", "foo", "--decision", "bar"], "after --decision"),
        (["done", "T1", "--adr", "bar"], "only for note"),
        (["note", "--decision", "a", "--adr", "b"], "one of"),
    ):
        assert cli.main(["-C", str(wt), "progress", *argv]) == 2
        assert says in capsys.readouterr().err
    assert progress.read(wt) is None  # nothing written, nothing half-kept


def test_concurrent_notes_are_all_kept(wt):
    from concurrent.futures import ThreadPoolExecutor

    cfg = config.load(wt)

    def write(who: str) -> None:
        for i in range(5):
            progress.note(cfg, wt, "gh-5", f"{who} {i}")

    with ThreadPoolExecutor(2) as pool:
        runs = [pool.submit(write, w) for w in ("a", "b")]
    for r in runs:
        r.result()  # re-raises what the writer raised
    notes = [n["text"] for n in progress._load(wt)["notes"]]
    assert sorted(notes) == sorted(f"{w} {i}" for w in "ab" for i in range(5))


def test_changed_files_are_capped(wt):
    for i in range(progress.FILES_MAX + 3):
        (wt / f"f{i:02}.txt").write_text("x", encoding="utf-8")
    git(wt, "add", "f*.txt")
    git(wt, "commit", "-qm", "many")
    text = progress.render(config.load(wt), wt, "gh-5")
    line = next(s for s in text.splitlines() if s.startswith("- changed files"))
    total = int(line.split("(")[1].split(")")[0])
    assert total > progress.FILES_MAX and f"… {total - progress.FILES_MAX} more" in line
    assert line.count("`") == 2 * (progress.FILES_MAX + 1)  # + "… N more"


def test_a_broken_task_file_does_not_break_the_hook(wt, monkeypatch, capsys):
    (wt / task.TASK_FILE).write_text("{broken", encoding="utf-8")
    for event in ("pre-compact", "session-start"):
        stdin(monkeypatch, {"cwd": str(wt)})
        assert cli.main(["hook", event]) == 0
        err = capsys.readouterr().err
        assert err.count("\n") == 1 and "progress ledger" in err

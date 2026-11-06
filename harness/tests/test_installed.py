"""the installed h is compared with the package on origin/<base> of its source."""

import shutil

import pytest
from conftest import git

from harness import cli, installed
from harness.installed import check as real_check  # conftest stubs `installed.check`


def _commit(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    git(repo, "add", rel)
    git(repo, "commit", "-qm", f"touch {rel}")


@pytest.fixture
def source(tmp_path):
    """origin.git and its clone `main`, the package at harness/src/harness/."""
    git(tmp_path, "init", "-q", "--bare", "-b", "main", "origin.git")
    main = tmp_path / "main"
    git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), "main")
    _commit(main, "harness/src/harness/cli.py", "v1\n")
    _commit(main, "harness/src/harness/templates/skill.md", "s1\n")
    git(main, "push", "-q", "origin", "HEAD:main")
    return tmp_path, main


def _copy(tmp_path, main):
    """What `uv tool install <dir>` leaves: a copy of the package, with bytecode."""
    pkg = tmp_path / "site-packages" / "harness"
    shutil.rmtree(pkg, ignore_errors=True)
    shutil.copytree(main / "harness/src/harness", pkg)
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "cli.cpython-312.pyc").write_bytes(b"\0")
    return pkg


def test_a_copy_of_origin_is_current(source):
    tmp, main = source
    pkg = _copy(tmp, main)
    (pkg / "cli.py").write_bytes(b"v1\r\n")  # CRLF checkout: same file
    assert real_check(pkg, (main / "harness", False)).state == "ok"


def test_a_copy_older_than_origin_is_behind_with_a_reinstall(source):
    tmp, main = source
    pkg = _copy(tmp, main)
    _commit(main, "harness/src/harness/cli.py", "v2\n")
    git(main, "push", "-q", "origin", "HEAD:main")
    st = real_check(pkg, (main / "harness", False))
    assert st.state == "behind" and st.stale
    assert "cli.py" in st.detail
    assert st.fix == (f"git -C {main.as_posix()} pull && "
                      f"uv tool install --reinstall {(main / 'harness').as_posix()}")  # fmt: skip
    assert (
        "installed h is behind origin/main" in st.message() and st.fix in st.message()
    )


def test_an_editable_clone_behind_origin_needs_only_a_pull(source):
    tmp, main = source
    other = tmp / "other"
    git(tmp, "clone", "-q", str(tmp / "origin.git"), "other")
    _commit(other, "harness/src/harness/new.py", "n\n")
    git(other, "push", "-q", "origin", "HEAD:main")
    git(main, "fetch", "-q", "origin")
    st = real_check(main / "harness/src/harness", (main / "harness", True))
    assert st.state == "behind"
    assert st.fix == f"git -C {main.as_posix()} pull"


def test_an_install_from_a_branch_differs(source):
    _tmp, main = source
    git(main, "switch", "-q", "-c", "feat")
    _commit(main, "harness/src/harness/cli.py", "unmerged\n")
    st = real_check(main / "harness/src/harness", (main / "harness", True))
    assert st.state == "differs" and st.stale
    assert "differs from origin/main" in st.message()
    assert "uv tool install --reinstall --editable" in st.fix


def test_an_install_from_a_worktree_differs_and_points_at_the_main_checkout(source):
    tmp, main = source
    wt = tmp / "wt"
    git(main, "worktree", "add", "-q", "-b", "task", str(wt))
    _commit(wt, "harness/src/harness/cli.py", "unmerged\n")
    st = real_check(_copy(tmp, wt), (wt / "harness", False))
    assert st.state == "differs"
    assert st.fix.endswith(
        f"uv tool install --reinstall {(main / 'harness').as_posix()}"
    )


def test_unknown_when_there_is_nothing_to_compare(source, tmp_path):
    _tmp, main = source
    assert real_check(main / "harness/src/harness", None).state == "unknown"
    plain = tmp_path / "plain" / "harness"
    plain.mkdir(parents=True)
    assert "not a git checkout" in real_check(plain, (plain, False)).detail
    git(main, "remote", "remove", "origin")
    st = real_check(main / "harness/src/harness", (main / "harness", True))
    assert st.state == "unknown" and "no origin/main" in st.detail


BEHIND = installed.Status("behind", "origin/main (cli.py)", "git -C /m pull")


def test_doctor_fails_on_a_stale_h_and_prints_the_fix(repo, monkeypatch, capsys):
    monkeypatch.setattr(installed, "check", lambda: BEHIND)
    assert cli.main(["-C", str(repo), "doctor"]) == 1
    out = capsys.readouterr().out
    assert (
        "FAIL installed h is behind origin/main (cli.py) — update: `git -C /m pull`"
        in out
    )


def test_doctor_notes_an_unchecked_h_without_failing(repo, monkeypatch, capsys):
    monkeypatch.setattr(installed, "check", lambda: installed.Status("unknown", "why"))
    cli.main(["-C", str(repo), "doctor"])
    cap = capsys.readouterr()
    assert "doctor: installed h not checked (why)" in cap.err
    assert "installed h" not in cap.out


def test_uncommitted_edits_in_an_editable_main_checkout_differ(source):
    _, main = source
    (main / "harness/src/harness/cli.py").write_text("local edit\n", encoding="utf-8")
    st = real_check(main / "harness/src/harness", (main / "harness", True))
    assert st.state == "differs"
    assert "status -- harness/src/harness" in st.fix and "pull" not in st.fix


def test_a_broken_install_record_is_unknown(monkeypatch):
    class Dist:
        def read_text(self, name):
            return "{not json"

    monkeypatch.setattr(installed.metadata, "distribution", lambda name: Dist())
    assert real_check().state == "unknown"


def test_ignored_files_in_an_editable_checkout_do_not_count(source):
    """an ignored .mypy_cache in the editable package made ship refuse."""
    _, main = source
    pkg = main / "harness/src/harness"
    (pkg / ".mypy_cache" / "3.14").mkdir(parents=True)
    (pkg / ".mypy_cache" / "3.14" / "cli.data.json").write_text("{}", encoding="utf-8")
    (pkg / "notes.local").write_text("x", encoding="utf-8")
    (main / ".gitignore").write_text("*.local\n", encoding="utf-8")
    assert real_check(pkg, (main / "harness", True)).state == "ok"


def test_an_extra_file_git_does_not_ignore_still_differs(source):
    _, main = source
    pkg = main / "harness/src/harness"
    (pkg / "new_module.py").write_text("x = 1\n", encoding="utf-8")
    assert real_check(pkg, (main / "harness", True)).state == "differs"

"""The edit hook reads its payload as UTF-8 whatever the locale."""

import io
import json
import sys

from harness import cli, fmt

PY = sys.executable
# A real, deterministic formatter: drops trailing blank lines.
STRIP = [PY, "-c", ("import sys, pathlib; p = pathlib.Path(sys.argv[1]); "
         "p.write_bytes(p.read_bytes().rstrip(b'\\r\\n') + b'\\n')")]  # fmt: skip


def locale_stdin(payload: dict) -> io.TextIOWrapper:
    """UTF-8 bytes behind a cp1251 text stream — stdin on a Russian Windows."""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return io.TextIOWrapper(io.BytesIO(raw), encoding="cp1251")


def test_hook_formats_a_file_with_a_cyrillic_path(tmp_path, monkeypatch):
    monkeypatch.setattr(fmt, "_commands", lambda cfg, stack, f: [[*STRIP, str(f)]])
    (tmp_path / "harness.toml").write_text("", encoding="utf-8")
    f = tmp_path / "パッケージ" / "テスト.py"
    f.parent.mkdir()
    f.write_bytes(b"X = 1\n\n\n")
    monkeypatch.setattr(
        "sys.stdin", locale_stdin({"tool_input": {"file_path": str(f)}})
    )
    assert cli.main(["hook", "post-edit"]) == 0
    assert f.read_bytes() == b"X = 1\n"


def test_hook_says_when_the_file_is_missing(tmp_path, monkeypatch, capsys):
    gone = tmp_path / "ない.py"
    monkeypatch.setattr(
        "sys.stdin", locale_stdin({"tool_input": {"file_path": str(gone)}})
    )
    assert cli.main(["hook", "post-edit"]) == 2
    err = capsys.readouterr().err
    assert "ない.py" in err and "not found" in err


def test_pre_write_guard_sees_a_cyrillic_verdict_path(monkeypatch):
    path = "C:/ユーザー/x/.artifacts/review/verdict.json"
    monkeypatch.setattr("sys.stdin", locale_stdin({"tool_input": {"file_path": path}}))
    assert cli.main(["hook", "pre-write"]) == 2

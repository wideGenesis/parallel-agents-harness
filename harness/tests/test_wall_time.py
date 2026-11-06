"""the h check summary gives how long the check took, not the sum of its steps — on
a pilot project the sum was 2337 s for a check that ended at +1450 s."""

import json

from harness import check, cli


def res(stack, kind, started, seconds, status="ok"):
    r = check.StepResult(check.Step(stack, kind, ["x"]), status, seconds, "")
    r.started = started
    return r


PILOT = [  # a real run's timeline
    res("rust", "lint", 8, 138), res("rust", "sql", 146, 406), res("rust", "test", 146, 1189),
    res("ts", "typecheck", 146, 425), res("ts", "test", 1336, 114),
    res("python", "test", 0, 0, status="skip"),
]  # fmt: skip


def test_wall_is_the_end_of_the_last_step_not_the_sum():
    assert sum(r.seconds for r in PILOT) == 2272
    assert check.wall(PILOT) == 1450


def test_nothing_ran_is_zero():
    assert check.wall([]) == 0.0
    assert check.wall([res("rust", "test", 0, 0, status="skip")]) == 0.0


def test_the_summary_and_json_give_the_wall_time(repo, monkeypatch, capsys):
    monkeypatch.setattr(check, "run", lambda cfg, steps, on_wait=None: list(PILOT))
    cli.main(["-C", str(repo), "check", "--full"])
    out = capsys.readouterr().out
    assert "(1450.0s" in out and "2272" not in out
    cli.main(["-C", str(repo), "check", "--full", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert {r["wall_s"] for r in rows} == {1450.0}
    assert [
        r["seconds"] for r in rows if r["kind"] == "test" and r["stack"] == "rust"
    ] == [1189]


def test_last_json_keeps_the_wall_time(repo, monkeypatch):
    monkeypatch.setattr(check, "run", lambda cfg, steps, on_wait=None: list(PILOT))
    cli.main(["-C", str(repo), "check", "--full"])
    rec = check.last(repo)
    assert rec is not None and rec["wall_s"] == 1450.0

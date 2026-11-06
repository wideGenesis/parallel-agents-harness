"""A failed test step's evidence: what the runner left in test-results/ is copied
into the run directory, so the next run deleting it loses nothing."""

import pathlib
import sys

import pytest

from harness import check, config, stacks

# A runner that leaves a trace like Playwright's retain-on-failure, then fails or passes.
RUNNER = """import pathlib, sys
d = pathlib.Path("ts/app/test-results/generate-spec-56")
d.mkdir(parents=True, exist_ok=True)
(d / "trace.zip").write_bytes(b"PK trace")
sys.exit(int(sys.argv[1]))
"""


def _ts_test(repo, code):
    (repo / "runner.py").write_text(RUNNER, encoding="utf-8")
    py = sys.executable.replace("\\", "/")
    with (repo / "harness.toml").open("a", encoding="utf-8") as f:
        f.write(f"\n[stacks.ts.commands]\ntest = '\"{py}\" runner.py {code}'\n")
    cfg = config.load(repo)
    steps = check.plan(cfg, stacks.detect(cfg), None, ("test",))
    [step] = [s for s in steps if s.stack == "ts"]
    return cfg, step


def test_failed_test_keeps_the_trace(repo):
    cfg, step = _ts_test(repo, 1)
    r = check._run(cfg, step)
    assert r.status == "fail" and "evidence kept: .artifacts/check/" in r.tail
    run_dir = (cfg.root / r.log).parent
    kept = run_dir / "ts-test" / "ts" / "app" / "test-results" / "generate-spec-56"
    assert (kept / "trace.zip").read_bytes() == b"PK trace"


def test_passed_test_keeps_nothing(repo):
    cfg, step = _ts_test(repo, 0)
    r = check._run(cfg, step)
    assert r.status == "ok"
    assert not ((cfg.root / r.log).parent / "ts-test").exists()


def test_named_evidence_dirs_replace_the_default(repo):
    cfg, _ = _ts_test(repo, 1)
    # set on the config: polygon's harness.toml already has a [stacks.ts] table
    cfg.stacks["ts"].evidence = ["reports"]
    [step] = [s for s in check.plan(cfg, stacks.detect(cfg), None, ("test",))
              if s.stack == "ts"]  # fmt: skip
    assert step.evidence == [cfg.root / "reports"]
    (cfg.root / "reports").mkdir()
    (cfg.root / "reports" / "shot.png").write_bytes(b"png")
    r = check._run(cfg, step)
    assert (
        (cfg.root / r.log).parent.joinpath("ts-test", "reports", "shot.png").is_file()
    )


def test_size_limit(repo, monkeypatch):
    monkeypatch.setattr(check, "EVIDENCE_LIMIT", 3)
    cfg, step = _ts_test(repo, 1)
    r = check._run(cfg, step)
    assert "1 file(s) over the 0 MB limit left out" in r.tail


@pytest.mark.parametrize("bad", [["/abs"], ["../x"], ["C:/x"], "reports", [""]])
def test_evidence_config_is_validated(tmp_path, bad):
    import json

    (tmp_path / "harness.toml").write_text(
        f"[stacks.ts]\nevidence = {json.dumps(bad)}\n", encoding="utf-8"
    )
    with pytest.raises(config.ConfigError, match="stacks.ts.evidence"):
        config.load(tmp_path)


def test_symlinks_are_not_followed(repo, monkeypatch):
    """A link could pull a file from outside the project into .artifacts. Faked with
    is_symlink: creating a real link needs a privilege Windows rarely grants."""
    cfg, step = _ts_test(repo, 1)
    results = cfg.root / "ts" / "app" / "test-results"
    results.mkdir(parents=True, exist_ok=True)
    (results / "link.txt").write_text("s", encoding="utf-8")
    linked = cfg.root / "linked"
    linked.mkdir()
    (linked / "secret.txt").write_text("s", encoding="utf-8")
    real = pathlib.Path.is_symlink
    monkeypatch.setattr(pathlib.Path, "is_symlink",
                        lambda p: p.name in ("link.txt", "linked") or real(p))  # fmt: skip
    step.evidence = [*step.evidence, linked]
    r = check._run(cfg, step)
    kept = (cfg.root / r.log).parent / "ts-test"
    trace = kept / "ts" / "app" / "test-results" / "generate-spec-56" / "trace.zip"
    assert trace.is_file()
    assert not list(kept.rglob("link.txt")) and not list(kept.rglob("secret.txt"))

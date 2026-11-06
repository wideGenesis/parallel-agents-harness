"""Heavy checks after the merge or by label, and the revert path back."""

import json

import pytest
from conftest import git, out

from harness import check, cli, config, revert, stacks
from harness.config import ConfigError


def with_heavy(proj, heavy='["rust.test", "typecheck"]'):
    toml = proj / "harness.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8") + f"\n[check]\nheavy = {heavy}\n",
        encoding="utf-8",
    )
    git(proj, "commit", "-qam", "heavy")
    git(proj, "branch", "-f", "main", "HEAD")  # gate rules come from the base
    return config.load(proj)


def planned(cfg, tier):
    found = stacks.detect(cfg)
    return {
        (s.stack, s.kind): s
        for s in check.plan(cfg, found, None, check.FAST + check.SLOW, tier=tier)
    }


def test_pr_tier_defers_heavy_steps(repo):
    steps = planned(with_heavy(repo), "pr")
    assert steps[("rust", "test")].heavy and steps[("rust", "test")].cmd is None
    assert steps[("python", "typecheck")].heavy and steps[("ts", "typecheck")].heavy
    assert not steps[("rust", "lint")].heavy and steps[("rust", "lint")].cmd
    assert not steps[("python", "test")].heavy


def test_full_tier_runs_them(repo):
    steps = planned(with_heavy(repo), "full")
    assert not any(s.heavy for s in steps.values())
    assert steps[("rust", "test")].cmd


def test_a_deferred_step_does_not_block_ship(repo):
    [step] = [s for s in planned(with_heavy(repo), "pr").values()
              if (s.stack, s.kind) == ("rust", "test")]  # fmt: skip
    assert not check.StepResult(step, "skip").blocks_ship


def test_heavy_is_a_gate_rule_read_from_the_base(repo):
    toml = repo / "harness.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8") + '\n[check]\nheavy = ["test"]\n',
        encoding="utf-8",
    )
    git(repo, "commit", "-qam", "branch marks tests heavy")
    # A branch cannot defer its own tests: the base has no heavy steps yet.
    assert config.load(repo).heavy == []


@pytest.mark.parametrize("bad", ['["rust.bench"]', '["go.test"]', '"test"', "[1]"])
def test_heavy_entries_are_validated(tmp_path, bad):
    (tmp_path / "harness.toml").write_text(
        f"[check]\nheavy = {bad}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="check.heavy"):
        config.load(tmp_path)


def test_check_cli_reports_deferred_steps(repo, capsys, monkeypatch):
    cfg = with_heavy(repo, '["rust.test"]')
    (repo / "crates" / "poly-core" / "src" / "lib.rs").write_text(
        "pub fn x() {}\n", encoding="utf-8"
    )
    monkeypatch.setattr(check, "run", lambda cfg, steps, on_wait=None: [
        check.StepResult(s, "skip" if s.cmd is None else "ok") for s in steps])  # fmt: skip
    assert cli.main(["-C", str(cfg.root), "check", "--json", "--only", "test"]) == 0
    rows = json.loads(capsys.readouterr().out)
    [rust] = [r for r in rows if r["stack"] == "rust"]
    assert rust["heavy"] and rust["status"] == "skip" and not rust["blocks_ship"]
    assert cli.main(["-C", str(cfg.root), "check", "--only", "test"]) == 0
    text = capsys.readouterr().out
    assert "1 heavy after merge" in text and "--tier full" in text


# --- h revert ---


def merged(a, name="feature.txt", text="new\n"):
    (a / name).write_text(text, encoding="utf-8")
    git(a, "add", name)
    git(a, "commit", "-qm", f"feat: {name}")
    git(a, "push", "-q", "origin", "main")
    return out(a, "rev-parse", "HEAD")


def test_revert_branch_follows_the_project_rule(world, github):
    # [branches] auto names the revert branch too.
    _, a, _ = world
    with (a / "harness.toml").open("a", encoding="utf-8") as f:
        f.write('\n[branches]\nauto = "nocard/{name}"\n')
    git(a, "commit", "-qam", "branch rule")
    git(a, "push", "-q", "origin", "main")
    sha = merged(a)
    res = revert.run(config.load(a), sha[:10])
    assert res.branch == f"nocard/revert-{sha[:10]}"
    assert github.prs[-1]["headRefName"] == res.branch


def test_revert_opens_a_pr_for_the_user(world, github):
    origin, a, _ = world
    sha = merged(a)
    github.runs[sha] = {"status": "completed", "conclusion": "failure", "url": "run/9"}
    res = revert.run(config.load(a), sha[:10])
    [pr] = github.prs
    assert pr["headRefName"] == res.branch == f"revert/{sha[:10]}"
    assert pr["labels"] == ["regression"]
    assert any("merge: user" in ln for ln in res.lines)
    # origin has the revert; the caller's checkout and branches are untouched
    tip = out(a, "--git-dir", str(origin), "log", "-1", "--format=%s", res.branch)
    assert tip == 'Revert "feat: feature.txt"'
    files = out(a, "--git-dir", str(origin), "ls-tree", "--name-only", res.branch)
    assert "feature.txt" not in files.split()
    assert (a / "feature.txt").is_file() and out(
        a, "branch", "--show-current"
    ) == "main"
    assert res.branch not in out(a, "branch", "--list")
    assert not (a / ".worktrees" / f"revert-{sha[:10]}").exists()


def test_revert_body_links_the_failed_run(world, github, monkeypatch):
    _, a, _ = world
    sha = merged(a)
    bodies = []
    real = github.__call__

    def spy(cwd, *argv):
        if argv[:2] == ("pr", "create"):
            bodies.append(argv[argv.index("--body") + 1])
        return real(cwd, *argv)

    from harness import gh

    monkeypatch.setattr(gh, "run", spy)
    revert.run(config.load(a), sha, "https://x/runs/42")
    assert "https://x/runs/42" in bodies[0] and sha in bodies[0]


def test_revert_refuses_a_commit_not_on_main(world, github):
    _, a, _ = world
    git(a, "switch", "-qc", "side")
    (a / "side.txt").write_text("x\n", encoding="utf-8")
    git(a, "add", "side.txt")
    git(a, "commit", "-qm", "side")
    with pytest.raises(revert.RevertError, match="not on origin/main"):
        revert.run(config.load(a), "HEAD")
    assert github.prs == []


def test_revert_conflict_leaves_nothing_behind(world, github):
    origin, a, _ = world
    sha = merged(a, "f.txt", "one\n")
    merged(a, "f.txt", "two\n")  # a later commit changes the same line
    with pytest.raises(revert.RevertError, match="conflicts"):
        revert.run(config.load(a), sha)
    assert github.prs == []
    assert f"revert/{sha[:10]}" not in out(a, "branch", "--list")
    assert not (a / ".worktrees" / f"revert-{sha[:10]}").exists()
    assert out(a, "--git-dir", str(origin), "branch", "--list", "revert/*") == ""

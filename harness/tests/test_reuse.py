"""a push to main reuses the merged PR's green check of the same tree instead of
checking it again — and never on doubt."""

import json
import re
import subprocess
from pathlib import Path

import pytest
from conftest import git, out

from harness import check, cli, config, gh, init, reuse, stacks

REPO = Path(__file__).resolve().parents[2]
R = "o/p"
FLOW = ".github/workflows/harness.yml"


@pytest.fixture
def main(tmp_path):
    """A main branch: docs commit D, code commit P (before), merge commit M (the push)."""
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    for name in ("d", "p", "m"):
        (root / f"{name}.txt").write_text(name, encoding="utf-8")
        git(root, "add", ".")
        git(root, "commit", "-qm", name)
    shas = out(root, "log", "--format=%H", "-3").split()
    m, p, d = shas
    return config.load(root), m, p, d, out(root, "rev-parse", "HEAD^{tree}")


def jobs(check_="success", reused=False):
    steps = [{"name": gh.REUSE_STEP, "conclusion": "success" if reused else "skipped"}]
    return {"jobs": [{"name": gh.CHANGES_JOB, "conclusion": "success", "steps": steps},
                     {"name": gh.CHECK_JOB, "conclusion": check_}]}  # fmt: skip


def world(m, p, tree, **over):
    """The API answers for a merge that may reuse; `over` replaces any of them."""
    api = {
        f"repos/{R}/commits/{m}/pulls": [
            {"number": 7, "merge_commit_sha": m, "merged_at": "2026-11-04T10:00:00Z",
             "head": {"sha": "h" * 40}}],
        f"repos/{R}/actions/artifacts?name=h-checked-{tree}&per_page=30": {"artifacts": [
            {"expired": False, "created_at": "2026-11-04T09:00:00Z",
             "workflow_run": {"id": 100, "head_sha": "h" * 40, "repository_id": 1,
                              "head_repository_id": 1}}]},
        f"repos/{R}/actions/runs/100": {"event": "pull_request", "path": FLOW,
                                        "html_url": "https://x/runs/100"},
        f"repos/{R}/actions/runs/100/jobs?per_page=50": jobs(),
        f"repos/{R}/actions/runs?head_sha={p}&event=push&per_page=20": {"workflow_runs": [
            {"id": 90, "path": FLOW, "status": "completed", "conclusion": "success",
             "created_at": "2026-11-04T08:00:00Z"}]},
        f"repos/{R}/actions/runs/90/jobs?per_page=50": jobs(),
    }  # fmt: skip
    api.update(over)
    return api


def serve(monkeypatch, api):
    asked = []

    def run(cwd, *argv):
        assert argv[0] == "api", argv
        asked.append(argv[1])
        if argv[1] not in api or api[argv[1]] is None:
            return subprocess.CompletedProcess(argv, 1, "", f"HTTP 404: {argv[1]}")
        return subprocess.CompletedProcess(argv, 0, json.dumps(api[argv[1]]), "")

    monkeypatch.setattr(gh, "run", run)
    return asked


def decide(cfg, m, p):
    return reuse.decide(cfg, m, p, repo=R, workflow=FLOW)


def test_a_merge_of_a_checked_tree_reuses_the_pr_check(main, monkeypatch):
    cfg, m, p, _, tree = main
    serve(monkeypatch, world(m, p, tree))
    d = decide(cfg, m, p)
    assert d.reuse and d.pr == 7 and d.run == "https://x/runs/100"
    assert f"tree {tree[:10]} was checked green by PR #7" in d.reason
    assert d.heavy is False


def test_main_moved_before_the_merge_checks_everything(main, monkeypatch):
    cfg, m, p, _, tree = main
    key = f"repos/{R}/actions/artifacts?name=h-checked-{tree}&per_page=30"
    serve(monkeypatch, world(m, p, tree, **{key: {"artifacts": []}}))
    d = decide(cfg, m, p)
    assert not d.reuse and "no green check of PR #7" in d.reason


@pytest.mark.parametrize("bad", [
    {"head_sha": "x" * 40},  # another PR's run: a branch cannot plant this tree
    {"head_repository_id": 2},  # a fork's run chose its own workflow file
])  # fmt: skip
def test_an_artifact_not_bound_to_the_merged_pr_is_ignored(main, monkeypatch, bad):
    cfg, m, p, _, tree = main
    api = world(m, p, tree)
    key = f"repos/{R}/actions/artifacts?name=h-checked-{tree}&per_page=30"
    api[key]["artifacts"][0]["workflow_run"].update(bad)
    serve(monkeypatch, api)
    assert not decide(cfg, m, p).reuse


@pytest.mark.parametrize("run", [
    {"event": "pull_request", "path": ".github/workflows/other.yml"},
    {"event": "workflow_dispatch", "path": FLOW},
])  # fmt: skip
def test_only_a_pr_run_of_the_same_workflow_vouches(main, monkeypatch, run):
    cfg, m, p, _, tree = main
    serve(monkeypatch, world(m, p, tree, **{f"repos/{R}/actions/runs/100": run}))
    assert not decide(cfg, m, p).reuse


def test_a_pr_run_whose_check_failed_does_not_vouch(main, monkeypatch):
    cfg, m, p, _, tree = main
    api = world(
        m, p, tree, **{f"repos/{R}/actions/runs/100/jobs?per_page=50": jobs("failure")}
    )
    serve(monkeypatch, api)
    assert not decide(cfg, m, p).reuse


def test_a_push_that_is_no_pr_merge_checks_everything(main, monkeypatch):
    cfg, m, p, _, tree = main
    serve(monkeypatch, world(m, p, tree, **{f"repos/{R}/commits/{m}/pulls": []}))
    d = decide(cfg, m, p)
    assert not d.reuse and "not the merge commit of a PR" in d.reason


@pytest.mark.parametrize(("status", "conclusion", "said"), [
    ("completed", "failure", "is failure, not green"),
    ("completed", "cancelled", "is cancelled, not green"),
    ("in_progress", None, "has not finished"),
])  # fmt: skip
def test_main_not_green_before_the_merge_checks_everything(main, monkeypatch, status,
                                                            conclusion, said):  # fmt: skip
    cfg, m, p, _, tree = main
    key = f"repos/{R}/actions/runs?head_sha={p}&event=push&per_page=20"
    run = {"id": 90, "path": FLOW, "status": status, "conclusion": conclusion,
           "created_at": "2026-11-04T08:00:00Z"}  # fmt: skip
    serve(monkeypatch, world(m, p, tree, **{key: {"workflow_runs": [run]}}))
    d = decide(cfg, m, p)
    assert not d.reuse and said in d.reason


def test_a_docs_only_skip_before_the_merge_defers_to_its_parent(main, monkeypatch):
    """a skipped run tested nothing; the verdict is its parent's."""
    cfg, m, p, d, tree = main
    api = world(
        m, p, tree, **{f"repos/{R}/actions/runs/90/jobs?per_page=50": jobs("skipped")}
    )
    api[f"repos/{R}/actions/runs?head_sha={d}&event=push&per_page=20"] = {"workflow_runs": [
        {"id": 80, "path": FLOW, "status": "completed", "conclusion": "failure",
         "created_at": "2026-11-04T07:00:00Z"}]}  # fmt: skip
    serve(monkeypatch, api)
    r = decide(cfg, m, p)
    assert not r.reuse and f"({d[:10]}) is failure" in r.reason


def test_a_reused_verdict_before_the_merge_is_green(main, monkeypatch):
    cfg, m, p, _, tree = main
    api = world(m, p, tree,
                **{f"repos/{R}/actions/runs/90/jobs?per_page=50": jobs("skipped", reused=True)})  # fmt: skip
    serve(monkeypatch, api)
    assert decide(cfg, m, p).reuse


def test_any_lookup_failure_checks_everything(main, monkeypatch):
    cfg, m, p, _, tree = main
    serve(monkeypatch, world(m, p, tree, **{f"repos/{R}/commits/{m}/pulls": None}))
    d = decide(cfg, m, p)
    assert not d.reuse and "reuse lookup failed" in d.reason and "HTTP 404" in d.reason


def test_outside_actions_never_reuses(main, monkeypatch):
    cfg, m, p, _, _ = main
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_WORKFLOW_REF", raising=False)
    assert not reuse.decide(cfg, m, p).reuse


def test_the_workflow_path_comes_from_the_callers_ref(main, monkeypatch):
    cfg, m, p, _, tree = main
    monkeypatch.setenv("GITHUB_REPOSITORY", R)
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", f"{R}/{FLOW}@refs/heads/main")
    serve(monkeypatch, world(m, p, tree))
    assert reuse.decide(cfg, m, p).reuse


def test_heavy_steps_still_run_on_main(main, monkeypatch):
    cfg, m, p, _, tree = main
    serve(monkeypatch, world(m, p, tree))
    monkeypatch.setattr(reuse, "_heavy", lambda cfg: True)
    d = decide(cfg, m, p)
    assert d.reuse and d.heavy


def test_the_heavy_tier_plans_only_the_deferred_steps(monkeypatch):
    from conftest import POLYGON

    cfg = config.load(POLYGON)
    cfg.heavy = ["rust.test"]
    steps = check.plan(
        cfg, stacks.detect(cfg), None, check.FAST + check.SLOW, tier="heavy"
    )
    assert [(s.stack, s.kind) for s in steps] == [("rust", "test")]
    assert steps[0].cmd is not None
    cfg.heavy = []
    assert (
        check.plan(cfg, stacks.detect(cfg), None, check.FAST + check.SLOW, tier="heavy")
        == []
    )


def test_cli_prints_the_decision_and_never_fails(main, monkeypatch, capsys):
    cfg, m, p, _, tree = main
    monkeypatch.setenv("GITHUB_REPOSITORY", R)
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", f"{R}/{FLOW}@refs/heads/main")
    serve(monkeypatch, world(m, p, tree, **{f"repos/{R}/commits/{m}/pulls": None}))
    assert cli.main(["-C", str(cfg.root), "ci", "reused", "--json", "--sha", m,
                     "--before", p]) == 0  # fmt: skip
    assert json.loads(capsys.readouterr().out)["reuse"] is False


@pytest.mark.parametrize(("jobs_", "state"), [
    (jobs(), "checked"),
    (jobs("skipped"), "skipped"),
    (jobs("skipped", reused=True), "reused"),
])  # fmt: skip
def test_check_state(jobs_, state):
    assert gh.check_state(jobs_["jobs"]) == state


def test_check_yml_reuses_on_push_and_records_the_tree_on_a_green_pr():
    text = (REPO / ".github/workflows/check.yml").read_text(encoding="utf-8")
    changes = text[text.index("  changes:\n") : text.index("\n  check:\n")]
    assert (
        "      actions: read\n" in changes and "      pull-requests: read\n" in changes
    )
    assert 'h ci reused --json --sha "$SHA" --before "$BEFORE"' in changes
    assert (
        f"      - name: {gh.REUSE_STEP}\n        if: steps.reuse.outputs.reuse == 'true'"
        in changes
    )
    assert ("run: ${{ steps.affected.outputs.run == 'true' && !(steps.reuse.outputs.reuse "
            "== 'true' && steps.reuse.outputs.heavy != 'true') }}") in changes  # fmt: skip
    assert "tier: ${{ steps.reuse.outputs.reuse == 'true' && 'heavy' ||" in changes
    job = text[text.index("\n  check:\n") :]
    rec = job[job.index("      - name: Record the checked tree") :]
    rec = rec[: rec.index("      - name: Upload step logs")]
    assert "if: success() && github.event_name == 'pull_request'" in rec
    assert "name: h-checked-${{ steps.checked.outputs.tree }}" in rec
    assert reuse.ARTIFACT.format(tree="${{ steps.checked.outputs.tree }}") in rec
    assert re.search(r"uses: actions/upload-artifact@[0-9a-f]{40} # v", rec)
    assert job.index("      - name: h check\n") < job.index(
        "      - name: Record the checked tree"
    )


def test_harness_yml_grants_the_check_job_the_reads(tmp_path):
    init.run(config.load(tmp_path))
    body = (tmp_path / FLOW).read_text(encoding="utf-8")
    job = body[body.index("  check:\n") : body.index("    uses: ")]
    assert "      actions: read\n" in job and "      pull-requests: read\n" in job
    assert "      contents: read\n" in job
    tc = (REPO / ".github/workflows/template-check.yml").read_text(encoding="utf-8")
    assert "      actions: read\n" in tc and "      pull-requests: read\n" in tc


def test_ci_status_has_json(tmp_path, monkeypatch, capsys):
    from harness import ci

    monkeypatch.setattr(ci, "rounds", lambda cfg, task_id: 2)
    cfg = config.load(tmp_path)
    assert cli.main(["-C", str(cfg.root), "ci", "status", "gh-9", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"task": "gh-9", "rounds": 2, "max": 2,
                                                   "blocked": True}  # fmt: skip


@pytest.mark.parametrize(("heavy", "skip", "want"), [
    (["ts.test"], [], True),
    (["rust.test"], [], True),
    (["ts.test"], ["test"], False),  # disabled in harness.toml: nothing to run on main
    ([], [], False),
])  # fmt: skip
def test_heavy_is_decided_without_the_tools(monkeypatch, heavy, skip, want):
    """The `changes` job has h only: no pnpm, uv tools or cargo extras."""
    from conftest import POLYGON

    from harness import tools

    monkeypatch.setattr(tools, "usable", lambda t: False)
    cfg = config.load(POLYGON)
    cfg.heavy = heavy
    cfg.stacks["ts"].skip = skip
    assert reuse._heavy(cfg) is want

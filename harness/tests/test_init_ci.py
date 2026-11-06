"""`h init` installs the project's CI: the harness reusable workflow by full SHA."""

import json
import re
from pathlib import Path

import pytest
from conftest import HARNESS_SHA

from harness import __version__, cli, config, gh, init

FLOW = ".github/workflows/harness.yml"
REPO = Path(__file__).resolve().parents[2]
_real_harness_sha = init.harness_sha  # conftest stubs it for every other test


def test_init_creates_ci_pinned_by_sha(tmp_path):
    changes = init.run(config.load(tmp_path))
    assert any(c.startswith(f"create {FLOW}") for c in changes)
    body = (tmp_path / FLOW).read_text(encoding="utf-8")
    uses = f"uses: {init.HARNESS_REPO}/.github/workflows/check.yml@{HARNESS_SHA}"
    assert f"{uses} # harness {__version__}" in body
    assert 'branches: ["main"]' in body
    assert "\n  required:\n    if: always()\n    needs: [check]" in body
    assert "{" + "sha}" not in body and "{" + "repo}" not in body

    again = init.run(config.load(tmp_path))  # idempotent
    assert not [c for c in again if FLOW in c]
    assert (tmp_path / FLOW).read_text(encoding="utf-8") == body


def test_init_uses_the_project_base_branch(tmp_path):
    (tmp_path / "harness.toml").write_text(
        '[project]\nbase = "trunk"\n', encoding="utf-8"
    )
    init.run(config.load(tmp_path))
    assert 'branches: ["trunk"]' in (tmp_path / FLOW).read_text(encoding="utf-8")


def test_init_defaults_to_github_hosted_without_inputs(tmp_path):
    init.run(config.load(tmp_path))
    body = (tmp_path / FLOW).read_text(encoding="utf-8")
    assert body.count("runs-on: ubuntu-latest") == 2
    assert "with:" not in body and "{" + "runs_on}" not in body


def test_init_writes_the_project_runner_and_database(tmp_path):
    # a project that runs only on its own runners, and its tests need Postgres.
    (tmp_path / "harness.toml").write_text(
        '[ci]\nruns-on = ["self-hosted", "linux", "x64", "app", "rust"]\n'
        'postgres-image = "postgres:18.6"\n',
        encoding="utf-8",
    )
    init.run(config.load(tmp_path))
    body = (tmp_path / FLOW).read_text(encoding="utf-8")
    assert body.count("runs-on: [self-hosted, linux, x64, app, rust]") == 2
    assert "ubuntu-latest" not in body
    assert (
        "    with:\n"
        """      runs-on: '["self-hosted", "linux", "x64", "app", "rust"]'\n"""
        "      postgres-image: postgres:18.6\n"
    ) in body


def test_config_shows_the_ci_settings(tmp_path, capsys):
    (tmp_path / "harness.toml").write_text(
        '[ci]\nruns-on = ["self-hosted", "linux"]\npostgres-image = "postgres:18.6"\n',
        encoding="utf-8",
    )
    assert cli.main(["-C", str(tmp_path), "config"]) == 0
    assert (
        "ci: runs-on self-hosted, linux, postgres postgres:18.6"
        in capsys.readouterr().out
    )


def test_timeout_reaches_the_check_job_as_an_input(tmp_path, capsys):
    """slow self-hosted runners raise the limit themselves."""
    (tmp_path / "harness.toml").write_text(
        "[ci]\ntimeout-minutes = 45\n", encoding="utf-8"
    )
    init.run(config.load(tmp_path))
    body = (tmp_path / FLOW).read_text(encoding="utf-8")
    assert "    with:\n      timeout-minutes: 45\n" in body
    assert cli.main(["-C", str(tmp_path), "config"]) == 0
    assert "timeout 45 min" in capsys.readouterr().out
    assert cli.main(["-C", str(tmp_path), "config", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ci"]["timeout-minutes"] == 45


def test_default_timeout_adds_no_line(tmp_path, capsys):
    init.run(config.load(tmp_path))
    assert "    with:" not in (tmp_path / FLOW).read_text(encoding="utf-8")
    assert cli.main(["-C", str(tmp_path), "config"]) == 0
    assert "timeout " not in capsys.readouterr().out


def test_workflow_default_timeout_is_the_config_default():
    flow = Path(__file__).resolve().parents[2] / ".github/workflows/check.yml"
    text = flow.read_text(encoding="utf-8")
    declared = re.search(r"\n      timeout-minutes:\n(?:        .*\n)*", text)
    assert declared and f"default: {config.CI_TIMEOUT_MINUTES}\n" in declared.group(0)
    assert "    timeout-minutes: ${{ inputs.timeout-minutes }}\n" in text


def test_a_failed_check_uploads_its_step_logs():
    """GitHub lost a failed job's log; the step logs outlive it as an artifact."""
    text = (REPO / ".github/workflows/check.yml").read_text(encoding="utf-8")
    step = text[text.index("      - name: Upload step logs\n") :]
    step = step[: step.index("\n      - name:", 1)]
    assert "if: (failure() || cancelled()) && inputs.upload-logs\n" in step
    assert re.search(r"uses: actions/upload-artifact@[0-9a-f]{40} # v", step)
    assert "name: h-check-logs-${{ github.run_attempt }}\n" in step
    assert "path: ${{ inputs.dir }}/.artifacts/check/\n" in step
    assert "include-hidden-files: true\n" in step and "retention-days: 7" in step
    assert text.index("      - name: h check\n") < text.index(
        "      - name: Upload step logs"
    )
    declared = re.search(r"\n      upload-logs:\n(?:        .*\n)*", text)
    assert declared and "type: boolean\n" in declared.group(0)
    assert "default: true\n" in declared.group(0)
    assert gh.LOGS_ARTIFACT.format(attempt="${{ github.run_attempt }}") in step


def test_upload_logs_off_reaches_the_check_job(tmp_path, capsys):
    (tmp_path / "harness.toml").write_text(
        "[ci]\nupload-logs = false\n", encoding="utf-8"
    )
    init.run(config.load(tmp_path))
    body = (tmp_path / FLOW).read_text(encoding="utf-8")
    assert "    with:\n      upload-logs: false\n" in body
    assert cli.main(["-C", str(tmp_path), "config"]) == 0
    assert "step logs not uploaded" in capsys.readouterr().out
    assert cli.main(["-C", str(tmp_path), "config", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ci"]["upload-logs"] is False


def test_upload_logs_on_by_default_adds_no_line(tmp_path):
    assert config.load(tmp_path).ci_upload_logs is True
    init.run(config.load(tmp_path))
    assert "upload-logs" not in (tmp_path / FLOW).read_text(encoding="utf-8")


def test_a_skipped_check_opens_no_issue(tmp_path):
    """a docs-only push skips `check`; only failure or cancelled report."""
    init.run(config.load(tmp_path))
    body = (tmp_path / FLOW).read_text(encoding="utf-8")
    when = next(ln for ln in body.splitlines() if ln.startswith("    if: always()"))
    assert "'skipped'" not in when
    assert "needs.check.result == 'failure'" in when


def test_a_docs_only_push_skips_the_check_but_never_narrows_it():
    """the previous tip decides docs-only; a push with code still checks
    everything — `base` (affected-only) stays the PR's."""
    text = (REPO / ".github/workflows/check.yml").read_text(encoding="utf-8")
    assert ("BEFORE: ${{ !inputs.full && github.event_name == 'push' && "
            "github.event.before || '' }}") in text  # fmt: skip
    assert 'git cat-file -e "$BEFORE^{commit}"' in text
    assert ("BASE: ${{ !inputs.full && (inputs.base || "
            "github.event.pull_request.base.sha) || '' }}") in text  # fmt: skip
    assert 'echo "base=$BASE"' in text
    ci = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    changes = ci[ci.index("\n  changes:") : ci.index("\n  harness:")]
    assert 'git cat-file -e "$BEFORE^{commit}"' in changes
    # The push branch needs the clone with history: no step of the job is PR-only.
    assert "fetch-depth: 0" in changes
    assert "if: github.event_name == 'pull_request'" not in changes


def test_a_cancelled_main_run_says_rerun_not_revert(tmp_path):
    """a timed-out check ends `cancelled`; no check judged the commit."""
    init.run(config.load(tmp_path))
    body = (tmp_path / FLOW).read_text(encoding="utf-8")
    start = body.index('if [ "$RESULT" = cancelled ]')
    cancelled = body[start : body.index("          else", start)]
    assert "gh run rerun $RUN_ID --failed" in cancelled
    assert "timeout-minutes" in cancelled and "h revert" not in cancelled
    assert 'title="Main run stopped at $short"' in body
    # The rerun it advises may fail for real: that issue must not be taken for this one.
    assert '--search "\\"$title\\" in:title"' in body


def test_init_single_runner_label_is_a_plain_string(tmp_path):
    (tmp_path / "harness.toml").write_text(
        '[ci]\nruns-on = "my-runner"\n', encoding="utf-8"
    )
    init.run(config.load(tmp_path))
    body = (tmp_path / FLOW).read_text(encoding="utf-8")
    assert body.count("runs-on: my-runner") == 3  # both jobs and the reusable input


@pytest.mark.parametrize(
    "ci",
    [
        "runs-on = []",
        "runs-on = 5",
        'runs-on = ["self-hosted", "a b"]',
        'runs-on = "x\\"; rm -rf /"',
        'postgres-image = "postgres; rm -rf /"',
        "postgres-image = 1",
        "timeout-minutes = 0",
        "timeout-minutes = 1441",
        'timeout-minutes = "45"',
        "timeout-minutes = true",
        "timeout-minutes = 4.5",
        'upload-logs = "no"',
        "upload-logs = 0",
    ],
)
def test_ci_settings_are_validated(tmp_path, ci):
    (tmp_path / "harness.toml").write_text(f"[ci]\n{ci}\n", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="ci\\."):
        config.load(tmp_path)


def test_init_keeps_an_existing_workflow_and_says_what_to_add(tmp_path):
    flow = tmp_path / FLOW
    flow.parent.mkdir(parents=True)
    flow.write_text("name: mine\n", encoding="utf-8")
    changes = init.run(config.load(tmp_path))
    assert flow.read_text(encoding="utf-8") == "name: mine\n"
    [tip] = [c for c in changes if FLOW in c]
    assert tip.startswith("tip:") and "check.yml@<sha>" in tip


def test_init_adds_ci_beside_other_workflows(tmp_path):
    other = tmp_path / ".github" / "workflows" / "ci.yaml"
    other.parent.mkdir(parents=True)
    other.write_text("name: ci\n", encoding="utf-8")
    changes = init.run(config.load(tmp_path))
    assert (tmp_path / FLOW).is_file()
    assert other.read_text(encoding="utf-8") == "name: ci\n"
    assert any(c.startswith("tip: kept your workflows (ci.yaml)") for c in changes)


def test_init_leaves_a_hand_wired_harness_workflow_alone(tmp_path):
    other = tmp_path / ".github" / "workflows" / "ci.yml"
    other.parent.mkdir(parents=True)
    other.write_text(
        f"jobs:\n  h:\n    uses: {init.HARNESS_REPO}/.github/workflows/check.yml@abc\n",
        encoding="utf-8",
    )
    assert not [c for c in init.run(config.load(tmp_path)) if "workflow" in c]
    assert not (tmp_path / FLOW).exists()


def test_init_dry_run_shows_the_workflow(tmp_path, capsys):
    assert cli.main(["-C", str(tmp_path), "init", "--dry-run"]) == 0
    assert f"create {FLOW}" in capsys.readouterr().out
    assert not (tmp_path / FLOW).exists()


def test_init_without_a_harness_sha_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(init, "harness_sha", lambda: None)
    changes = init.run(config.load(tmp_path))
    assert not (tmp_path / FLOW).exists()
    assert any(c.startswith(f"tip: {FLOW} not created") for c in changes)


def test_harness_sha_is_origin_base_of_the_h_source(world, monkeypatch):
    from harness import installed

    _origin, a, _b = world
    monkeypatch.setattr(installed, "_source", lambda: (a, True))
    want = installed._run(a, "rev-parse", "origin/main").stdout.strip()
    assert _real_harness_sha() == want


def test_harness_sha_falls_back_to_github(monkeypatch):
    import subprocess

    from harness import gh, installed

    monkeypatch.setattr(installed, "_source", lambda: None)
    asked = []

    def fake(cwd, *argv):
        asked.append(argv)
        return subprocess.CompletedProcess(argv, 0, HARNESS_SHA + "\n", "")

    monkeypatch.setattr(gh, "run", fake)
    assert _real_harness_sha() == HARNESS_SHA
    assert asked == [("api", f"repos/{init.HARNESS_REPO}/commits/main", "--jq", ".sha")]


def test_ci_template_runs_heavy_on_label_and_reports_red_main(tmp_path):
    """`full-ci` on a PR re-runs CI; a red push opens a `regression` issue."""
    init.run(config.load(tmp_path))
    body = (tmp_path / FLOW).read_text(encoding="utf-8")
    assert "types: [opened, synchronize, reopened, labeled]" in body
    when = ("    if: always() && github.event_name == 'push' && "
            "(needs.check.result == 'failure' || needs.check.result == 'cancelled')")  # fmt: skip
    job = ["  regression:", "    needs: [check]", when]
    assert chr(10).join(job) in body
    assert "issues: write" in body and "h revert $short" in body


NEW_SHA = "b" * 40


def _released(monkeypatch):
    """A newer harness: h init now pins NEW_SHA."""
    monkeypatch.setattr(init, "harness_sha", lambda: NEW_SHA)


def test_init_repins_an_untouched_workflow(tmp_path, monkeypatch):
    # CI follows the h release without a hand edit.
    init.run(config.load(tmp_path))
    _released(monkeypatch)
    dry = init.run(config.load(tmp_path), dry_run=True)
    assert any(c.startswith(f"update {FLOW} (harness {HARNESS_SHA[:12]}") for c in dry)
    assert HARNESS_SHA in (tmp_path / FLOW).read_text(
        encoding="utf-8"
    )  # dry: unwritten
    changes = init.run(config.load(tmp_path))
    assert any(c.startswith(f"update {FLOW}") for c in changes)
    body = (tmp_path / FLOW).read_text(encoding="utf-8")
    assert f"check.yml@{NEW_SHA} # harness {__version__}" in body
    assert not [c for c in init.run(config.load(tmp_path)) if FLOW in c]  # B4


def test_init_redraws_an_untouched_workflow_with_new_ci_settings(tmp_path, monkeypatch):
    init.run(config.load(tmp_path))
    (tmp_path / "harness.toml").write_text(
        '[ci]\nruns-on = ["self-hosted", "linux"]\npostgres-image = "postgres:18.6"\n',
        encoding="utf-8",
    )
    changes = init.run(config.load(tmp_path))  # same pin, new settings
    assert any(c.startswith(f"update {FLOW}") for c in changes)
    body = (tmp_path / FLOW).read_text(encoding="utf-8")
    assert (
        "runs-on: [self-hosted, linux]" in body
        and "postgres-image: postgres:18.6" in body
    )


def test_init_leaves_an_edited_workflow_and_prints_the_new_pin(tmp_path, monkeypatch):
    init.run(config.load(tmp_path))
    flow = tmp_path / FLOW
    edited = flow.read_text(encoding="utf-8").replace(
        "name: harness", "name: 'harness'"
    )
    flow.write_text(edited, encoding="utf-8")  # e.g. prettier's quotes
    assert not [c for c in init.run(config.load(tmp_path)) if FLOW in c]  # pin current
    _released(monkeypatch)
    [msg] = [c for c in init.run(config.load(tmp_path)) if FLOW in c]
    assert msg.startswith(f"MODIFIED {FLOW}: edited by the project")
    assert f"check.yml@{NEW_SHA} # harness {__version__}" in msg
    assert flow.read_text(encoding="utf-8") == edited


def test_init_repins_a_crlf_checkout(tmp_path, monkeypatch):
    init.run(config.load(tmp_path))
    flow = tmp_path / FLOW
    flow.write_bytes(flow.read_bytes().replace(b"\n", b"\r\n"))  # git autocrlf
    _released(monkeypatch)
    assert any(c.startswith(f"update {FLOW}") for c in init.run(config.load(tmp_path)))


def test_a_hand_edited_runner_is_an_edit_not_overwritten(tmp_path, monkeypatch):
    # runs-on edited in one job only — the project's, keep it.
    init.run(config.load(tmp_path))
    flow = tmp_path / FLOW
    text = flow.read_text(encoding="utf-8")
    edited = text.replace("runs-on: ubuntu-latest", "runs-on: my-box", 1)
    flow.write_text(edited, encoding="utf-8")
    _released(monkeypatch)
    [msg] = [c for c in init.run(config.load(tmp_path)) if FLOW in c]
    assert msg.startswith(f"MODIFIED {FLOW}")
    assert flow.read_text(encoding="utf-8") == edited


def test_a_file_from_an_older_template_is_untouched_by_that_template(
    tmp_path, monkeypatch
):
    # The template changes between releases: compare with the one that wrote the file.
    current = init._template("github/harness.yml")
    old = current.replace(
        "# Branch protection:", "# An older line.\n# Branch protection:"
    )
    flow = tmp_path / FLOW
    flow.parent.mkdir(parents=True)
    cfg = config.load(tmp_path)
    flow.write_text(
        init._render(cfg, HARNESS_SHA, "0.1.0", template=old), encoding="utf-8"
    )
    monkeypatch.setattr(
        init, "_template_at", lambda sha: old if sha == HARNESS_SHA else None
    )
    _released(monkeypatch)
    assert any(c.startswith(f"update {FLOW}") for c in init.run(cfg))
    assert "An older line" not in flow.read_text(encoding="utf-8")


def test_init_writes_the_project_setup_script(tmp_path, capsys):
    # a project's tests need a system tool that no stack toolchain brings.
    (tmp_path / "harness.toml").write_text(
        '[ci]\nsetup = "scripts/ci-setup.sh"\n', encoding="utf-8"
    )
    init.run(config.load(tmp_path))
    body = (tmp_path / FLOW).read_text(encoding="utf-8")
    assert "    with:\n      setup: scripts/ci-setup.sh\n" in body
    assert cli.main(["-C", str(tmp_path), "config"]) == 0
    assert "setup scripts/ci-setup.sh" in capsys.readouterr().out


@pytest.mark.parametrize(
    "setup",
    ["/etc/x.sh", "../x.sh", "scripts/../../x.sh", "a b.sh", "x.sh; rm -rf /",
     "$(id).sh", "-x.sh"],
)  # fmt: skip
def test_setup_must_be_a_plain_path_inside_the_project(tmp_path, setup):
    (tmp_path / "harness.toml").write_text(
        f"[ci]\nsetup = {setup!r}\n".replace("'", '"'), encoding="utf-8"
    )
    with pytest.raises(config.ConfigError, match="ci.setup"):
        config.load(tmp_path)


def test_no_setup_no_setup_input(tmp_path):
    # a project without [ci] setup gets no extra step input.
    init.run(config.load(tmp_path))
    assert "setup:" not in (tmp_path / FLOW).read_text(encoding="utf-8")


def test_repin_keeps_and_adds_the_setup_input(tmp_path, monkeypatch):
    # an untouched file is redrawn with [ci] setup.
    init.run(config.load(tmp_path))
    (tmp_path / "harness.toml").write_text(
        '[ci]\nsetup = "scripts/ci-setup.sh"\n', encoding="utf-8"
    )
    _released(monkeypatch)
    assert any(c.startswith(f"update {FLOW}") for c in init.run(config.load(tmp_path)))
    body = (tmp_path / FLOW).read_text(encoding="utf-8")
    assert "      setup: scripts/ci-setup.sh\n" in body
    assert not [c for c in init.run(config.load(tmp_path)) if FLOW in c]

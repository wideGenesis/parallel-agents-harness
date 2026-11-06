# Agent readiness: this harness and four large repositories

Assessed 2026-11-06. Each of four read-only subagents studied one clone and answered the
same ten questions with a `file:line` for every claim, or "not found" with what it searched.
A fifth subagent assessed this tool with the same rubric and was asked to be critical.
Nothing in the clones was run. Paths below are relative to each repository.

| Repository | Commit | Note |
| --- | --- | --- |
| openai/codex | `5deceaa92732` (main) | full clone |
| getsentry/sentry | `22f65cfab0fe` (master) | sparse clone, about 244 files: agent files, `.github`, configs — "not in the clone" does not mean absent upstream |
| supabase/supabase | `b77fb5f4e5d5` (master) | full clone |
| vllm-project/vllm | `224b2f54f19d` (main) | full clone |

The question: how does the repository support AI coding agents working on it, especially
several at once? These are products with mature human processes, not agent tools; branch
protection and ruleset settings live outside the repository and were not visible.

## openai/codex

- **Instructions:** no root `AGENTS.md` or `CLAUDE.md`. Two nested files:
  `codex-rs/tui/src/bottom_pane/AGENTS.md:1-12`, `codex-rs/exec-server/src/sandbox_integrity/AGENTS.md:1-48`.
  Nothing checks them.
- **Agent tooling:** label-triggered Codex prompts in `.github/codex/labels/*.md`; no
  `.agents`, `.claude` or MCP config for this repository.
- **Claiming, isolation:** none; `docs/contributing.md:5` — external PRs are not accepted.
  `.worktreeinclude:1` lists one file to copy into a new worktree.
- **Scoped checks:** a `changed` job gates the Rust jobs (`.github/workflows/rust-ci.yml:8-53`);
  Windows Bazel targets are selected by a script (`bazel.yml:206`); BuildBuddy remote cache.
- **Tool-enforced rules:** workspace clippy lints at `deny` (`codex-rs/Cargo.toml:582-601`),
  banned methods with reasons (`codex-rs/clippy.toml:8-27`), cargo-deny, boundary scripts
  (`repo-checks.yml:20-27`), a clean-worktree check for regenerated files (`justfile:172-182`).
- **CI:** one entry point and a `CI required` aggregate (`blocking-ci.yml:5-8,48-72`),
  post-merge CI (`postmerge-ci.yml:5-40`), required checks on the merge commit, SHA pins.
- **Red main:** no revert or red-main procedure found.
- **Strong:** one aggregate check; rules with reasons in the error; merge-commit validation.
- **Weak for parallel agents:** no entry point for agents, no claim, no isolation;
  CODEOWNERS covers core paths only (`.github/CODEOWNERS:1-17`).

## getsentry/sentry

- **Instructions:** root `AGENTS.md` (129 lines) declared the source of truth (`AGENTS.md:3`);
  `CLAUDE.md` files are `@AGENTS.md` shims; nested `src/`, `tests/`, `static/` guides.
  Nothing keeps them current.
- **Agent tooling:** `agents.toml:3-28` pulls skills from external repos unpinned
  (`pin = false`) and configures two MCP servers; `.claude/commands/*`; a read-leaning
  allowlist in `.claude/settings.json:3-101` with an empty deny list.
- **Claiming:** none. A nightly job finds convention violations for an AI fixer
  (`.github/workflows/refactor-tasks.yml:2-10`).
- **Isolation:** `.claude/worktrees` is gitignored (`.gitignore:74`); dev ports come from
  env vars (`rsbuild.config.ts:70-79`); one shared `devservices` stack (`AGENTS.md:27-34`).
- **Scoped checks:** paths filter (`backend.yml:45-50`); tests selected from coverage data on
  PRs, full suite on master (`backend.yml:39-41,79-87`); sharding (`backend.yml:170,204`).
- **Tool-enforced rules:** pre-commit with ruff, flake8 custom rules, mypy and its "weaklist"
  ratchet (`.pre-commit-config.yaml:22-173,57-77`); an oxlint suppression budget.
- **Review:** Warden AI review on PRs, failing at severity `high` (`warden.toml:13-17,84-94`);
  team CODEOWNERS; the CODEOWNERS coverage check is disabled (`codeowners-coverage.yml:3-10`).
- **CI:** one required aggregate per area (`backend.yml:673-700`), SHA pins, CodeQL weekly.
- **Red main:** a label-triggered revert bot (`fast-revert.yml:2-21`).
- **Strong:** coverage-based test selection; rules in hooks; server-side AI review; revert bot.
- **Weak for parallel agents:** no claim; no designed isolation; unpinned external skills.

## supabase/supabase

- **Instructions:** root `AGENTS.md` (75 lines), `CLAUDE.md` = `@AGENTS.md`; nested guides in
  `apps/studio`, `apps/docs`, `apps/kb`; a Copilot review policy
  (`.github/copilot-instructions.md:5-22`). No CI check on them.
- **Agent tooling:** 22 skills in `.agents/skills/` (`AGENTS.md:61-63`), hosted MCP (`.mcp.json:3-6`).
- **Claiming, isolation:** none; fixed dev ports per app (`AGENTS.md:7-17,29`).
- **Scoped checks:** paths filter and 4 shards for Studio unit tests
  (`studio-unit-tests.yml:32-57`); turbo cache; the typecheck runs the whole repo (`typecheck.yml:51-58`).
- **Tool-enforced rules:** a per-rule lint ratchet with weekly PRs that lower the baselines
  (`studio-lint-ratchet-decrease.yml:4-6`), knip, zizmor pinned with attestation
  (`zizmor.yml:20,50`), a 72-hour minimum release age for packages (`pnpm-workspace.yaml:7`).
- **Review:** team CODEOWNERS; Copilot review tuned to comment only above 85% confidence.
- **CI:** SHA pins; no aggregate required check found; `merge_group` only on typecheck.
- **Red main:** a Slack alert (`studio-master-alert.yml:3-18,62`); no revert policy found.
- **Strong:** one source of truth with task-to-skill routing; supply-chain hardening; a
  ratchet that is pushed down automatically.
- **Weak for parallel agents:** no claim, no isolation, colliding ports; agent files unchecked.

## vllm-project/vllm

- **Instructions:** root `AGENTS.md` (164 lines) with a duplicate-work check (`AGENTS.md:8-19`);
  nested guides; budgets of 200/300/500 lines enforced by a pre-commit hook that also
  checks links, frontmatter and `.claude/skills` symlinks (`tools/pre_commit/check_agent_files.py:26-73`).
- **Agent tooling:** skills in `.agents/skills/` with `agents/openai.yaml` for Codex too;
  a mandatory `pr-checklist` skill before any PR (`AGENTS.md:37`).
- **Claiming:** none; agents must search issues and PRs and stop on a duplicate
  (`AGENTS.md:8-19,39-41`) — best effort, two agents can still start at once.
- **Isolation:** none; one `.venv`, a shared incremental C++ build.
- **Scoped checks:** Buildkite steps declare `source_file_dependencies` and shard
  (`.buildkite/test_areas/attention.yaml:2-37`); pre-commit hooks filter by files.
- **Tool-enforced rules:** custom hooks (forbidden imports, new `torch.cuda` calls,
  `trust_remote_code=True`), test tethering with a shrink-only allowlist
  (`tools/pre_commit/check_test_tethering.py:3-23`), DCO sign-off.
- **Review:** a 275-line CODEOWNERS; mergify labels, assigns and rebases; a `ready` label
  that agents are told not to ask for (`.github/workflows/pre-commit.yml:31-45`).
- **CI:** GitHub Actions for lint, Buildkite for GPU tests triggered by trusted `/ci`
  comments (`run-ci-command.yml:18-42`); SHA pins; no aggregate required check found.
- **Red main:** a CI failure report and bisect instructions (`docs/contributing/ci/failures.md:14-100`).
- **Strong:** agent files checked by tools; tests tethered to CI; path-scoped, sharded CI.
- **Weak for parallel agents:** no claim or isolation; most CI needs GPUs and a maintainer.

## This harness (`h`)

- **Instructions:** `AGENTS.md` of at most 100 lines, `CLAUDE.md` = `@AGENTS.md`; tests
  check sizes, links, frontmatter and that every `harness.toml` section an agent file names
  is one `h` reads (`harness/tests/test_rules.py`); CI checks the skill mirrors.
- **Agent tooling:** 9 skills, 4 subagents (`locator`, `analyzer`, `reviewer`, `ci-triage`),
  Claude Code hooks for formatting, the verdict file and the progress ledger; a deny list
  for force-push, push to main and the user-only commands (`.claude/settings.json`).
- **Claiming:** a card is a GitHub issue with a Definition of Ready; a claim is
  `refs/claims/<ID>` on origin, created by a push without force — one winner — with
  heartbeat, TTL and a user-only steal (`docs/adr/0002-task-claims-on-origin.md`, `harness/src/harness/claims.py`).
- **Isolation:** a worktree per task, ports from a machine-wide registry, a throwaway
  Postgres per check, a machine CPU and load budget across sessions (`harness/src/harness/task.py`, `pg.py`, `machine.py`).
- **Scoped checks:** affected packages with reverse dependencies; pytest-testmon in the
  agent's own loop only, full runs in CI and ship; docs-only changes skip CI; a push to main
  reuses the merged PR's green check of the same tree.
- **Tool-enforced rules:** suppression and skip ratchet, test tethering, dead code,
  cargo deny, stdlib-only imports, SHA-pinned actions and workflow rules as tests.
- **Review:** a reviewer subagent on a model other than the author's, 3 rounds at most,
  `h ship` refuses without its approval. The verdict comment is posted by the author's
  account, so it stops accidents, not deliberate bypass (`harness/src/harness/prreview.py`).
- **CI:** a reusable workflow pinned by SHA, one `required` check, heavy steps after merge.
- **Red main:** a regression issue, `h revert` opens a revert PR the user merges.
- **Strong:** atomic claims across machines; isolation for many sessions on one machine;
  process rules enforced in code; cheap scoped CI; one command installs it in a repository.
- **Weak:** one user's Windows machine; GitHub and Claude Code only; local gates; tried on
  one pilot project over 18 days; test selection inside a package for Python only; no sharding.

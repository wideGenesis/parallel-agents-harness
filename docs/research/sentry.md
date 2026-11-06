# getsentry/sentry — summary

Commit: `22f65cf` · Clone date: 2026-10-20 · Length: ≤ 1 page (≈80 lines)

Every claim comes with a `path:line` in the repository. Mark guesses *(speculation)*.

## 1. Context for agents
- The canon is AGENTS.md; an explicit rule "do not add to CLAUDE.md and Cursor rules" (`AGENTS.md:3`). `CLAUDE.md` = one line `@AGENTS.md` (`CLAUDE.md:1`).
- Nested AGENTS.md files: the root has 129 lines, `src/` 162, `static/` 130, `tests/` 113, plus micro files in subfolders (3 and 17 lines: `src/sentry/issues/derived/AGENTS.md:1-17`).
- The root has a map "which AGENTS.md for which zone" by globs (`AGENTS.md:96-105`); child files start with a link to the root for commands (`src/AGENTS.md:3`, `tests/AGENTS.md:3`).
- Workflow instructions are moved into skills `.agents/skills/*` (~25: generate-migration, lint-fix, setup-dev, feature-flags…) with `references/` for progressive loading; `.claude/skills` is a symlink to `../.agents/skills` (`.claude/skills:1`). Rules for skills: diff-first, do not impose an output format (`AGENTS.md:107-113`).
- `.claude/commands/` (gh-pr, gh-review, setup-dev); `gh-pr.md:14-16` — "not a test plan, but a short description", a check for mixed front/back changes. `.cursor/` contains only `mcp.json` (`.cursor/mcp.json:1-9`) — rules are not duplicated.
- AGENTS.md has "decision trees" for typical tasks (`src/AGENTS.md:19-30`) and the rule "add tests to the existing file at the mirrored path" (`tests/AGENTS.md:9-16`).
- The agent gets the dev server log as a file `.artifacts/dev.log` (ANSI stripped) — the agent does not see the terminal (`AGENTS.md:40`).

## 2. Deterministic rails
- `prek` (a fast pre-commit) is the "single entry point" for lint/format/types; "before you finish a task, run `.venv/bin/prek run -q`"; it detects changed files itself (`AGENTS.md:44-51`).
- Hooks: ruff check/format, custom flake8 only for S* rules (ruff cannot run AST plugins) (`.pre-commit-config.yaml:23-43`), mypy only on pre-push and only with `SENTRY_MYPY_PRE_PUSH` (`:44-50`), oxlint/oxfmt/stylelint/knip for TS (`:102-131`), a ban on pushing to master (`:78-84`).
- Type ratchet: pygrep bans `# type: ignore`/`ignore-errors` (`:57-61`); the mypy weaklist is sorted and checked, additions to it are blocked (`:62-77`).
- `.claude/settings.json` — only an allow list of read-only commands (gh pr view, git diff, pnpm lint/test-ci…) (`.claude/settings.json:2-50`); no hooks *(in the part viewed)*.
- Hard bans in text: do not run `tsc` directly, only `pnpm run typecheck` (`AGENTS.md:79`); "do not run bare pytest" (`AGENTS.md:58`).

## 3. Parallelism and isolation
- No data: I found nothing on worktrees/ports/task claims in AGENTS.md and skills. There is only `required_permissions: ['all']` for Python commands in the sandbox (`AGENTS.md:18`) and the required `.venv` (`AGENTS.md:11-16`).
- Parallelism only in tests: `pytest -n3 --reuse-db` (`AGENTS.md:59`), per-worker Snuba in CI (`backend.yml:226`).

## 4. Checks and speed
- Tiers in AGENTS.md: locally — specific pytest files/nodeids; `make test-selective` — "not for routine use" (`AGENTS.md:55`); `pnpm lint:js <file>`, `pnpm test-ci <file>` (`AGENTS.md:84-93`).
- Affected tests are selected by coverage: CI downloads a combined coverage DB from GCS (`backend.yml:121-131`), `compute-sentry-selected-tests.py` maps changed files → tests via SQLite coverage (`compute-sentry-selected-tests.py:1-12`); the Makefile repeats this locally (`Makefile:178-204`). A hint for the agent: when CI fails, look at the output of the `select-tests` job and run the same nodeids (`AGENTS.md:55`).
- Sharding: a static test count (300 per shard, max 22) (`calculate-backend-test-shards.py:19-22`); `pytest-selected.py` — each shard imports only its own files, balanced by the file `backend-test-durations.json`, a deterministic plan from the checkout (`pytest-selected.py:3-18`).
- CI speedups: Postgres over a Unix socket "saves ~36s per shard" (`backend.yml:246-249`), Snuba bootstrap in the background in parallel with devservices (`backend.yml:232-237`), `SENTRY_DEVENV_FRONTEND_ONLY=1` skips migrations (`AGENTS.md:25-27`).
- Output compression: `prek run -q`, pytest `--quiet`.

## 5. CI/CD
- 33 workflow files; the hub `files-changed` (dorny/paths-filter + `.github/file-filters.yml`) decides what to run (`backend.yml:27-40`, `pre-commit.yml:50-66`).
- A single required check `Backend` with `if: always()` aggregates the results (failure/cancelled → fail, skipped → ok) (`backend.yml:674-696`) — the classic pattern for a path-filtered monorepo.
- `concurrency` with cancel-in-progress by head_ref (`backend.yml:13-15`); actions pinned by SHA (`backend.yml:99`); minimal `permissions` per job.
- Selective tests run only on PRs from the same repo and are disabled by the label `Trigger: Override Selective Testing` (`backend.yml:41, 82-86`); on master — a full run.
- Front and back are not deployed atomically: a CI check rejects mixed PRs (`AGENTS.md:123-129`; `label-pullrequest.yml:16,30`).
- Other: migrations-drift, openapi-diff, codeowners-coverage (a baseline file), type-coverage-diff, jest-balance (`.github/workflows/`).

## 6. Tasks, plans, review
- AI review: Warden (`warden.toml:1-25`) — skills on path triggers (sentry-backend-bugs, sentry-security, sentry-javascript-bugs), `failOn=high`, `reportOn=medium`; the skills are the same `.agents/skills`. Reviewer skills have `references/` per bug class.
- `.claude/plans/.gitkeep` — a place for plans (git ls-files), with no content. The PR template is legal boilerplate (`.github/PULL_REQUEST_TEMPLATE.md:1-10`), with no checklist. The rule "do not include customer data" (`AGENTS.md:119-121`).
- No subagents/roles as such; delegation goes through skills.

## 7. Adopt / do not adopt
| Practice | Decision | Benefit | Side effect |
|---|---|---|---|
| AGENTS.md as the canon + `@AGENTS.md` in CLAUDE.md + an explicit ban on duplication | Adopt | One source, no drift | Needs a linter for "do not write to CLAUDE.md" *(speculation)* |
| Nested AGENTS.md files per zone + a map in the root | Adopt | Saves context | Child files must stay short |
| Skills in `.agents/skills`, a symlink `.claude/skills` | Adopt | One set for Claude/Cursor/Warden | Symlinks on Windows are fragile |
| prek as the single entry point + mypy on pre-push | Adopt (an equivalent for our stack) | A fast local gate | mypy is deferred until push |
| Ratchet (weaklist, ban on type: ignore) | Adopt | Debt only shrinks | Needs helper scripts |
| Selective tests by coverage DB | Later | Huge savings on a monorepo | Needs infrastructure to store coverage (GCS) |
| Aggregating required check `if: always()` | Adopt | Works with path filters | The needs list must be maintained |
| Duplicated rules for Cursor | Do not adopt | — | Drift |
| Worktree isolation | They have no data — we build it ourselves | — | — |

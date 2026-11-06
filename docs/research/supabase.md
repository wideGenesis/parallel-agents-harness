# supabase/supabase — summary

Commit: `807fb3d` · Clone date: 2026-10-20 · Length: ≤ 1 page (≈80 lines)

Source: an analysis in a separate chat with a model, not rechecked against the clone. The chat has no line numbers (`path:line`) — file paths are given as the chat names them. Guesses are marked *(speculation)*.

## 1. Context for agents
- The root `AGENTS.md` has 75 lines, "a map, not a manual": a table of directories, commands, a short description of CI, a list of generated files that must not be edited, conventions, links to skills.
- `CLAUDE.md` is one line `@AGENTS.md`; the same in `apps/studio`, `apps/docs`, `apps/kb`.
- `apps/studio/AGENTS.md`: a table "task type → which skills to load" (queries → `studio-queries`, forms → `react-hook-form`, SQL → `safe-sql-execution`, etc.).
- `.agents/skills/` — 25+ skills; the path is not tied to a vendor; they are declared the source of truth for conventions ("load the ones you need, do not guess").
- `.github/copilot-instructions.md` does not duplicate the rules; it points to the same `AGENTS.md` and skills.

## 2. Deterministic rails
`.claude/settings.json`:
- `permissions.deny` forbids `Edit` of generated files: `**/routeTree.gen.ts`, `packages/api-types/types/**` and others.
- `SessionStart` hook: `pnpm install` only in a cloud session (`CLAUDE_CODE_REMOTE=true`); locally it does nothing.
- `PostToolUse` hook after every `Write`/`Edit`: `prettier --write` + `eslint --fix` only on the one changed file, in the right workspace.

## 3. Parallelism and isolation
No data. *(speculation, from the chat)* Tasks probably come in through Linear and Claude Code cloud sessions (the hook with `CLAUDE_CODE_REMOTE` points to this); no workflow where agents take tasks themselves was found in the code.

## 4. Checks and speed
Three levels of checks (the chat's conclusion): 1) a file — the hook after an edit; 2) affected paths — CI with filters; 3) the whole repository — merge queue, master, or a run by label.
- CI speed: `sparse-checkout` in jobs, `concurrency` with `cancel-in-progress`, `blacksmith-4vcpu` runners for heavy work.
- Fail-fast: a check for case hazards in file names runs before dependencies are installed.
- Caches and output compression: no data.

## 5. CI/CD
- Required on every PR: typecheck + lint (one workflow, parallel steps), Prettier, a typo check.
- The rest runs by path through `dorny/paths-filter`: Studio unit tests, e2e, docs tests.
- `typecheck.yml`: the `merge_group` trigger (merge queue).
- Ratchet: the number of ESLint warnings per rule may not grow (a new `any` or default export fails CI); once a week a bot lowers the baselines and opens a PR. `knip` blocks dead files and dependencies.
- The `autofix` label: formatting runs in a job without write permissions, the patch is saved as an artifact, and a separate job with a GitHub App token pushes it (untrusted code does not run next to secrets).
- Bots open PRs themselves: library updates, docs sync, lower ratchet thresholds.
- `zizmor` checks workflow security; all actions are pinned by SHA.
- Evals of AI features: a cheap preflight always runs; the full Braintrust run happens only on master or with the `run-evals` label.

## 6. Tasks, plans, review
- The backlog is in Linear; per `AGENTS.md`, internal context (metrics, vendors, strategy) goes into a Linear issue with a link, not into a public PR; tasks are referenced by ID (e.g. FE-3106).
- Stacked PRs via `gh stack`, a strict branch naming scheme; a stack is an exception and needs approval from the person who set the task.
- PR template: Problem → Solution → Review instructions (numbered steps for the reviewer).
- Migrations as a plan in the repository: `TANSTACK_MIGRATION.md` with a checklist of routes (an analogue of OpenAI's exec-plan).
- ADR: there is no classic `docs/adr` folder in the root; inside `apps/` only the AGENTS files were looked at — not verified.
- AI review: Copilot comments only at confidence above 85% and does not touch what CI checks. `.coderabbit.yaml`: per-path instructions (when `pages/**` is edited, remind about `routes/**`), generated files are excluded.
- Subagent roles: no data.

## 7. Adopt / do not adopt
Decisions follow the table "What to take for you (Rust / Python / TS)" from the chat.

| Practice | Decision | Benefit | Side effect |
|---|---|---|---|
| Formatting one file in a hook (Rust `rustfmt`; Python `ruff format/check --fix`; TS prettier/biome) | Adopt | A light level instead of a heavy run | No data. `clippy`/`pyright`/`tsc` do not go into the hook: they do not work at the file level; they run at the end of the task (`cargo clippy -p <crate>`, `turbo --affected`, `pytest` per package) |
| Ban on editing generated files via `deny` (Rust `*.pb.rs`, bindgen; Python — generated clients; TS `*.gen.ts`) | Adopt | No data | No data |
| Warning ratchet (Rust clippy warnings → baseline JSON; Python ruff, a counter per rule; TS eslint per rule) | Adopt | Agents do not add debt, legacy gets squeezed out | No data |
| Dead code (Rust `cargo-machete`/`cargo-udeps`; Python `vulture`/`deptry`; TS `knip`) | Adopt | No data | No data |
| CI filter by path (Rust — by crate; Python — by uv workspace package; TS turbo `--affected`) | Adopt | No data | No data |

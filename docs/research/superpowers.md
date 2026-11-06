# obra/superpowers — summary

Commit: `bb92a77` · Clone date: 2026-10-20 · Length: ≤ 1 page (≈80 lines)

Every claim comes with a `path:line` path in the repository. Mark guesses with *(speculation)*.

## 1. Context for agents
- Skill format: `skills/<name>/SKILL.md` with frontmatter `name` + `description` of the form "Use when …" (a trigger, not a summary): `skills/test-driven-development/SKILL.md:1-4`, `skills/writing-plans/SKILL.md:3`.
- The only skill always in context is `using-superpowers` (3.2 KB); the SessionStart hook injects it in full: `hooks/session-start:20-41`. The others load on demand through the Skill tool (`hooks/session-start:41`). In total, 15 SKILL.md files ≈ 163 KB / 3828 lines (`wc`).
- Token budgets for skills: onboarding <150 words, frequently loaded <200, others <500: `skills/writing-skills/SKILL.md:213-220`; description <500 chars: same file, `:103`. The skills themselves break this budget (subagent-driven-development 568 lines, writing-skills 681): bloat.
- The trigger is aggressive: "1% chance: you MUST invoke the skill", a "red flags" table: `skills/using-superpowers/SKILL.md:10-16,35-50`. For subagents, `<SUBAGENT-STOP>` turns the skill off: `:6-8`.
- The repo's `AGENTS.md` holds only contribution rules for AI (PR template, model disclosure): `AGENTS.md:1-30`; it says nothing about the project architecture.

## 2. Deterministic rails
- One hook: SessionStart on `startup|clear|compact`, which calls a bash script: `hooks/hooks.json:3-13`. No pre-commit, no deny rules, no PreToolUse *(per hooks.json)*.
- Discipline (TDD, verification) rests on prompt text, not code: "Iron Law: NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST" `skills/test-driven-development/SKILL.md:31-35`; "NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE" `skills/verification-before-completion/SKILL.md:14-17`. Enforcement is through rationalization tables (`skills/using-git-worktrees/SKILL.md:159-167`), i.e. persuasion, not gates.
- On the other hand, there are 3 deterministic scripts for SDD: `task-brief` (awk cuts "Task N" out of the plan into a file) `skills/subagent-driven-development/scripts/task-brief:15-27`; `review-package` (commit list + stat + diff -U10 in one file, with guards on BASE/HEAD) `scripts/review-package:1-30`; `sdd-workspace` (a git-ignored plan directory).

## 3. Parallelism and isolation
- Worktree skill: Step 0 detection (`git-dir != git-common-dir`, a guard for submodules) `skills/using-git-worktrees/SKILL.md:16-37`; priority: native tool (EnterWorktree) > `git worktree add` `:49-57`; directory `.worktrees/`, a mandatory `git check-ignore` `:63-88`; then install + baseline tests `:102-132`. Ports/env/task claims are not covered.
- Parallelism is limited: "Never dispatch multiple implementation subagents in parallel (conflicts)" `skills/subagent-driven-development/SKILL.md:143`. Only independent failure investigations run in parallel: `skills/dispatching-parallel-agents/SKILL.md:36-45`.
- State survives compaction through a ledger file `progress.md` in `.superpowers/sdd/<plan>/` (the first line is the plan path; `Task N: complete`): `skills/subagent-driven-development/SKILL.md:131-146`. Reason: "re-dispatched entire completed task sequences — the most expensive mistake" `:132-133`.

## 4. Checks and speed
- Tiers along the lines of "focused while iterating, a full run once before the commit": `skills/subagent-driven-development/implementer-prompt.md:47-48`. The reviewer does NOT rerun tests; it trusts the TDD evidence (RED/GREEN in the report): `task-reviewer-prompt.md:73-88`, `implementer-prompt.md:54-56`.
- Model choice per role; `model` must be set explicitly (otherwise the expensive one is inherited): `SKILL.md:184-204` (Model Selection section), "turn count beats token price" in the same place. No compression of command output.
- CI: `.github/` contains only templates/FUNDING, no workflows (`ls`). Tests live in `tests/<platform>` (claude-code, codex, hooks…); no data on how they run.

## 5. CI/CD
No data (no `.github/workflows`). The PR template is mandatory, and humans check it: `AGENTS.md:22-30`.

## 6. Tasks, plans, review
- Pipeline: brainstorming (HARD-GATE before code, `skills/brainstorming/SKILL.md:179-193`; spec in `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md` `:157`) -> writing-plans (`docs/superpowers/plans/…` `skills/writing-plans/SKILL.md:16`) -> SDD/executing-plans -> finishing-a-development-branch (tests -> a merge/PR/keep/discard menu).
- Plan: a header with Goal/Architecture/Tech Stack/Spec/Global Constraints/Review Focus `writing-plans/SKILL.md:56-80`; steps = one action with a verifiable result (test fail -> impl -> pass -> commit) `:43-50`; placeholders ("TBD") are banned `:159`; a task = the smallest unit with its own test cycle and review `:34-41`.
- Roles in SDD: controller (the main session) -> implementer (one per task, fresh context) -> task reviewer (spec + quality, one pass) -> scoped re-review -> a final whole-branch reviewer. Up to 5 rounds of fixes: R≤3 resumes the same agent, R≥4 uses a fresh and stronger one; at R=5 a breaker: `SKILL.md:61-121` (graph).
- Handoff to a subagent: the path to the task-brief file + 1 line of context + interfaces of previous tasks + the report path; "no accumulated summaries" (case: a 42k-char dispatch, 99% of it history) `SKILL.md:267-271`. Return: the full report to a file, <15 lines to the chat: Status DONE/DONE_WITH_CONCERNS/BLOCKED/NEEDS_CONTEXT, commits, 1 line on tests `implementer-prompt.md:61-67`.
- The reviewer gets 3 paths (brief, report, diff file) + Global Constraints "as a lens"; it reads the diff in one Read and does not walk the codebase; "Do not trust the report" `task-reviewer-prompt.md:21-50,64`. Subagents may not dispatch subagents `implementer-prompt.md:50-60`.
- "Rulings, not stalls": the controller resolves conflicts itself and writes `Ruling:` to the ledger; it stops only in 4 cases (irreversible action, security, a side effect outside the worktree, a broken plan) `SKILL.md:19-31`.

## 7. Adopt / do not adopt
| Practice | Decision | Benefit | Side effect |
|---|---|---|---|
| Artifacts as files, not in context (brief, report, diff-package) | Adopt | The controller does not bloat: `SKILL.md:231-233` | Needs scripts/a `.superpowers/` directory in gitignore |
| Scripts `task-brief` / `review-package` | Adopt (rewrite in Python/Rust) | Deterministic routine instead of an agent | bash+awk, fragile on Windows |
| Ledger `progress.md` + `git log` as the source of truth after compaction | Adopt | Protects against repeated work `SKILL.md:131-146` | Keeping it up to date is left to the agent's discipline *(speculation: a hook/script is better)* |
| Short subagent status + report to a file | Adopt | <15 lines in context `implementer-prompt.md:61` | — |
| Explicit model per role, escalation on fixes | Adopt | Savings `SKILL.md:184-204` | Needs a role->model table |
| Worktree: detect -> native tool -> git fallback, check-ignore | Adopt | Idempotent `using-git-worktrees/SKILL.md:16-88` | No ports/DB/task claims; we add them ourselves |
| Review gate after each task (spec+quality) | Partly | Catches drift | 2+ fresh contexts per task; for mechanical work, a batch/one review at the end |
| Always-on `using-superpowers` + the "1%" rule | Do not adopt | — | A constant ~3 KB + forced skills even on trivial tasks; the skill is invoked "before clarifying questions" `using-superpowers/SKILL.md:20` |
| Discipline through text (rationalization tables, CAPS) | Do not adopt as the basis | — | Bloats skills; our approach is hooks/CI gates |
| Huge SKILL.md files (568/681 lines) + dot graphs | Do not adopt | — | Context; we keep a skill ≤ ~100 lines |
| Sequential implementers (no parallelism) | Do not adopt | — | No gain from worktree parallelism; we use one worktree per agent |
| TDD evidence RED/GREEN in the report | Adopt | Verifiable without a rerun | Better captured by a script than taken from the agent's word |

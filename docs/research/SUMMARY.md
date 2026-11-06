# Stage 0 — consolidated matrix (2026-10-20)

Sources: the summaries in this folder (`<repo>.md`; every line has a `path:line`). Only
decisions are here. "×N" — how many independent repositories do the same.

## Adopt

| # | Practice | Source | Stage | Benefit | Side effect |
|---|---|---|---|---|---|
| 1 | `AGENTS.md` is the only canon, `CLAUDE.md` = `@AGENTS.md`, nested files per zone, the root is a map | sentry, ruff, supabase ×3 | 2 | one source for all agents and AI review | nested files drift without link checks |
| 2 | Skills in `.agents/skills/`, `.claude/skills` is a link; the AI reviewer uses the same skills with path triggers | sentry, ruff, supabase ×3 | 2 | vendor-neutral; review and author follow the same rules | symlinks on Windows need Developer Mode → copy/generate instead |
| 3 | Rules live in linters and CI, not in prose (`[workspace.lints]`, structural import checks, deny on generated files) | codex, supabase | 2, 6 | agents do not ignore red CI | rules as code need maintenance |
| 4 | Per-file fmt hook in PostToolUse; slow type checks run at pre-push / the final gate | supabase, sentry | 2, 3 | cheap checks on the spot | a delay on every edit |
| 5 | One local entry gate (`prek run -q` / `h check`) that finds changes itself; waves: read-only in parallel → fixers | sentry, ruff | 3 | the agent does not have to decide what to run | prek is one more dependency |
| 6 | Compressed output: print only on failure (`run_silent`) | humanlayer | 3 | the context stays clean | progress of long commands is lost → log to a file |
| 7 | Dev server logs in `.artifacts/*.log` without ANSI | sentry | 4 | the agent sees what it cannot see in the terminal | files grow → rotation |
| 8 | Flags for affected areas via `git diff --quiet BASE...HEAD -- paths`, without third-party actions | ruff, codex ×2 | 3, 6 | the same logic locally and in CI | hand-written dependencies between areas drift → generate them from the graph (cargo metadata / uv / pnpm) |
| 9 | One aggregating required check with `if: always()`, skipped=ok | sentry, codex, ruff ×3 | 6 | path filters work with branch protection | — |
| 10 | Heavy checks (full clippy/nextest/e2e) run post-merge or by label; light checks run on the PR | codex, supabase | 6 | fast PR cycle | breakage is caught after merge → fast revert path |
| 11 | `check-clean-worktree` after jobs (forgotten regeneration) | codex | 6 | catches drift in generated files | — |
| 12 | Only main writes the cache, PRs are read-only; build profiles `line-tables-only`, mold/lld | ruff, codex | 3, 6 | no cache poisoning, faster linking | cache miss on a branch's first PR build |
| 13 | CI security: actions pinned by SHA, `contents: read`, `persist-credentials: false`, zizmor, dependency age | ruff, supabase ×2 | 6 | supply chain | manual pin updates → renovate |
| 14 | Ratchet for warnings / `type: ignore` / weaklist | supabase, sentry ×2 | 6 | debt does not grow | needs a baseline when connected |
| 15 | Scout subagents (locator → paths only; analyzer → `file:line`) on a cheap model, with trimmed tools | humanlayer | 5, 7 | the main session does not read extra files | each one starts cold |
| 16 | Subagent: input is a path to a brief, output is a report in a file + ≤15 status lines (DONE/BLOCKED/…) | superpowers | 7 | history does not bloat the context | — |
| 17 | Review package: a script collects commits + stat + diff into one file | superpowers | 7 | the reviewer reads one file | — |
| 18 | The model is set explicitly per role; ≤3 rounds of fixes by the same agent, then a fresh/stronger one | superpowers | 7 | cost control | — |
| 19 | Handoff / progress ledger as state after compaction | humanlayer, superpowers ×2 | 5 | finished work is not repeated | a hook keeps it, not the agent |
| 20 | A script fills in metadata (date, sha, branch) | humanlayer | 5 | the model does not invent it | — |
| 21 | Worktree: idempotent `ensure`, path canonicalization, delete only a directory with a `.git` file, orphans Running→Failed | vibe-kanban | 4 | self-healing after a crash, safe GC | — |
| 22 | Branch `<short id>-<slug>`; setup/cleanup scripts per environment; `.worktreeinclude` / copying `.env` by glob | vibe-kanban, codex | 4 | no collisions, fast start | copy secrets only by an explicit list |
| 23 | Task claim — a lease on origin `refs/claims/<ID>` (push without force, heartbeat, TTL, `--steal`) | the pilot project (our own; competitors do not have this) | 4, 5 | stops "agents take someone else's task" | ls-remote at start; a crash holds the task until TTL |
| 24 | Task stages as labels (needs-research → ready → in-dev → review) + the rule "agents take only size ≤ S" | humanlayer/Linear | 5 | the Linear practice on GitHub | labels are not atomic → the truth is in the lease, the label is only a display |
| 25 | Markers `[NEEDS CLARIFICATION]` and `[P]` (parallelizable) in the task | spec-kit | 5 | a cheap signal to the agent | — |
| 26 | Port isolation: a random free port per environment | container-use | 4 | no collisions between 7 sessions | the port must be passed to the agent (env/file) |

## Do not adopt

| Practice | Source | Why |
|---|---|---|
| Container-use / Dagger as the core | container-use | Windows → Docker Desktop/WSL, early stage, workdir export after every command. Later — optional on Ubuntu |
| `--dangerously-skip-permissions` for autonomous sessions | humanlayer | bypasses the rails; use an allow list + deny instead |
| Discipline as text ("Iron Law", bans in the prompt) | superpowers, humanlayer | agents ignore prose → hooks and CI |
| Injecting a large skill into every session, "1% → invoke the skill" | superpowers | bloats the context |
| `branch -D` on a branch with the same name when creating a worktree | claude-squad | loses someone else's work → fail-fast |
| spec+plan+tasks for every feature, a "constitution" | spec-kit | ceremony; the pilot project's card is stronger |
| Bazel next to Cargo, a coverage DB for test selection, sharding into 22 | codex, sentry | not our scale; start with testmon / affected by package |
| tmux dependency | claude-squad | not available on Windows |

## Lessons along the way

- **MSYS path conversion.** Git Bash turned the sparse patterns `/.github/` into `C:/Program Files/Git/.github/`
  — three clones came out incomplete. Requirement for the harness: the Python CLI passes path arguments to git, not sh.
- **The pilot project:** hundreds of number reservations against a few live branches; most worktrees belong to archived cards.
  Worktree cleanup must run deterministically (at `h` start, not only from `--land`).
- **Process weight:** in the pilot project the card template is ~100 lines, and there are thousands of lines of shell scripts.
  A portable harness takes the mechanisms, not the specifics (review levels, invariants, migrations stay in the project).

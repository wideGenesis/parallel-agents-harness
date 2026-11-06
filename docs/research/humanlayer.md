# humanlayer/humanlayer — summary

Commit: `99abe67` · Clone date: 2026-10-20 · Length: ≤ 1 page (≈80 lines)

Every claim comes with a `path:line` path in the repository. Mark guesses with *(speculation)*.
Paths below are relative to the clone root. Focus: `.claude/` (33 commands, 6 agents).

## 1. Context for agents
- The root `CLAUDE.md` is short (~60 lines): a monorepo map and the `make setup/check/test` commands (`CLAUDE.md:41-45`); test details are delegated ("check `package.json`", `CLAUDE.md:52-60`).
- The main context technique is not a "big AGENTS.md" but artifacts in `thoughts/` (research/plans/handoffs/tickets): `thoughts/shared/{research,plans,handoffs/ENG-XXXX,tickets}` (`.claude/commands/research_codebase.md:87`, `create_handoff.md:13`, `ralph_research.md:7`). `humanlayer thoughts sync` syncs the directory, and all worktrees share it (`create_worktree.md:15-17`); `thoughts/` itself is not committed to the repo (`commit.md:20`).
- "Frequent intentional compaction" is implemented as `/create_handoff` + `/resume_handoff`: a compressed document with the sections Task/Critical References/Recent changes (file:line)/Learnings/Artifacts/Next Steps (`create_handoff.md:45-63`); "file:line references instead of large snippets" (`create_handoff.md:95`). Resume: read the critical research/plan files yourself, NOT through a subagent (`resume_handoff.md:28`).
- Rule: "read files in full, without limit/offset, in the main context before launching subagents" (`research_codebase.md:30-34`, `implement_plan.md:14`).

## 2. Deterministic rails
- `.claude/settings.json`: the only allow entry is `hack/spec_metadata.sh`; thinking 32000 (`settings.json:3-10`). No deny rules and no hooks.
- `hack/spec_metadata.sh` collects the date/commit/branch for frontmatter deterministically, so the LLM does not invent them (`hack/spec_metadata.sh:5-30`; called from `research_codebase.md:86`). Adopt.
- `hack/run_silent.sh`: the command stays silent and shows output only on failure; `VERBOSE=1` turns on full output (`hack/run_silent.sh:14-30`); the Makefile uses it (`Makefile:37-39`). This compresses output for the agent.
- Commit rules live in the prompt, not in a hook: `git add` file by file, never `-A`, do not commit `thoughts/` (`commit.md:18-21`). *(speculation)* better to turn this into a pre-commit/PreToolUse hook.

## 3. Parallelism and isolation
- `hack/create_worktree.sh [name] [base]`: a worktree in `~/wt/<repo>/<name>` (or `$HUMANLAYER_WORKTREE_OVERRIDE_BASE`), a new branch, copies `.claude/`, then `make setup`; if setup fails, it rolls back the worktree and the branch ("a worktree from a branch that fails setup is not allowed") (`create_worktree.sh:46-54,75-101`). Then `thoughts init/sync` (`:119-134`). A `make check test` check at creation is commented out (`:103-117`).
- `hack/cleanup_worktree.sh` does cleanup (`cleanup_worktree.sh:4-8`); `hack/port-utils.sh` finds a free port with lsof/nc and avoids the Vite range 10000-19999 (`port-utils.sh:3-25`). A script isolates ports.
- Starting a session in a worktree: `humanlayer launch --model opus -w <path> "<prompt>"`; the prompt includes the chain "implement_plan → commit → describe_pr → comment on the ticket" (`create_worktree.md:30-37`). A human confirms before the launch (`create_worktree.md:27`).
- Autonomous runs: `--dangerously-skip-permissions --dangerously-skip-permissions-timeout 15m` (`ralph_impl.md:31`), `oneshot` = research → a new plan session (`oneshot.md:6-7`). Task claim: "take the 1 highest-priority SMALL/XS from the top 10 of status X; if none, exit" (`ralph_impl.md:14-15`, `ralph_research.md:9-10`); claim = moving the status to "in progress" (`ralph_research.md:15`). The claim is not atomic *(speculation: a race between two sessions is possible)*.
- hld/CodeLayer (Go daemon + Tauri UI) orchestrates sessions and approvals (`CLAUDE.md:27-37`); I found no worktree logic in hld/hlyr (hlyr has only thoughts: `hlyr/src/thoughtsConfig.ts`): no data.

## 4. Checks and speed
- A single target `make check test` in the plan criteria (`implement_plan.md:46`); `make check` aggregates across subprojects (`Makefile:24-44`). No file/affected tiers: no data.
- A plan phase splits criteria into Automated (the agent runs them itself) and Manual (a human), with a pause after each phase; `[x]` checkboxes in the plan itself are the resume mechanism (`create_plan.md:225-240,345-355`; `implement_plan.md:50-65,79-82`). Several phases in a row only on explicit instruction (`implement_plan.md:63`).

## 5. CI/CD
- Out of focus (only a mention of `gh workflow run` in `CLAUDE.md:48`). There are `/ci_commit`/`/ci_describe_pr`: non-interactive variants ("never stop and ask", `commit.md:41`, `ci_commit.md`). Everything else: no data.

## 6. Tasks, plans, review
- Linear status pipeline: Triage → Spec Needed → Research Needed → Research in Progress → (Research in Review) → Ready for Plan → Plan in Progress → Plan in Review → Ready for Dev → In Dev → Code Review → Done (`linear.md:38-50`). New tickets always go to Triage (`linear.md:64`). Size (XS/SMALL) is the criterion for auto-pickup (`ralph_impl.md:15`). Each stage = its own command `ralph_research/plan/impl` (`ralph_*.md`).
- The ticket is exported to `thoughts/shared/tickets/ENG-xxxx.md` with its comments before work starts (`ralph_research.md:7-8`); missing information → a comment with a question and the status moved back (`ralph_research.md:17`); no plan → move the ticket back to "ready for spec" and exit (`ralph_impl.md:25`).
- Artifact name: `YYYY-MM-DD-ENG-XXXX-description.md` (`research_codebase.md:87-94`); YAML frontmatter: date, git_commit, branch, topic, tags, status (`research_codebase.md:100-111`).
- Research is "a documentarian, not a critic": no suggestions/evaluations (`research_codebase.md:10-17`). Research is kept separate from planning so it does not bias the result (`ralph_research.md:2f`, the "Be unbiased" line).
- Plan: Desired End State, What We're NOT Doing, phases with Automated/Manual criteria (`create_plan.md:193-240`); "No open questions in the final plan: stop and clarify" (`create_plan.md:338-343`); `iterate_plan` and `validate_plan` are separate commands.
- Subagents (all `model: sonnet`, restricted tools): `codebase-locator` (Grep/Glob/LS; "do not read contents", returns paths grouped as impl/test/config/types/entry points, `codebase-locator.md:4,60-100`), `codebase-analyzer` (Read/Grep/Glob/LS; "how it works", `file:line` required, `codebase-analyzer.md:4,61-85,117`), `codebase-pattern-finder` (pattern examples with file:line, `:4,37,206`), `thoughts-locator` (finds documents, one-line description each, `thoughts-locator.md:4,28`), `thoughts-analyzer` (extracts only decisions/constraints/whether still current, not a retelling, `thoughts-analyzer.md:56-75,145`), `web-search-researcher` (links are required in the report, `research_codebase.md:59-60`).
- Orchestration: locator first → then analyzer on the best hits; different queries in parallel; wait for ALL; live code takes priority over thoughts (`research_codebase.md:66-78`). "Do not tell agents HOW to search: they know" (`:71`).
- PR: `/describe_pr` from a template + a check; `/local_review` and `/debug` are separate commands.

## 7. Adopt / do not adopt
| Practice | Decision | Benefit | Side effect |
|---|---|---|---|
| Roles locator/analyzer/pattern-finder: narrow tools, sonnet, output format with `path:line` | Adopt | cheap, the answer is verifiable, the main session's context stays clean | output formats must be maintained in the agents |
| Handoff document (template Task/Learnings/Artifacts/Next) + resume | Adopt, store in the repo/Issue comments | context compaction between sessions | risk of going stale; needs frontmatter with the commit |
| `spec_metadata.sh` (deterministic metadata) | Adopt | no hallucinated dates/hashes | — |
| `run_silent.sh` (output only on failure) | Adopt | saves tokens | progress is lost with `VERBOSE=0` |
| Stage statuses as GitHub labels (`needs-research`, `needs-plan`, `ready-for-dev`, `in-dev`) + a size label `size:XS/S` | Adopt | a script auto-picks the "next" ticket | the claim is not atomic; needs a label swap via `gh` + a check |
| Plan with Automated/Manual criteria and checkboxes as state | Adopt | resumable, a clear gate | duplicates CI; reference the commands |
| "Research without evaluations", separate from planning | Adopt | less bias | an extra pass |
| Worktree: setup must pass, otherwise roll back | Adopt | we do not start from a broken base | slow start; needs a dependency cache |
| `--dangerously-skip-permissions` + a timeout as the norm | Do not adopt | — | risk; use deny rules + a sandbox instead |
| `thoughts/` as a separate synced repo (`humanlayer thoughts sync`) | Do not adopt | — | extra infrastructure; `docs/` in the repo or GitHub Issues/PR is enough |
| Rules (git add without `-A`, do not commit X) only in the prompt | Do not adopt as is | — | replace with a hook/pre-commit |
| Linear specifics (state UUIDs in the prompt, `linear.md:363-378`) | Do not adopt | — | hard coupling; on GitHub use labels/Projects |

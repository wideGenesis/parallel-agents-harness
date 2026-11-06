# Harness — plan (agreed 2026-10-20)

Goal: a portable harness for agents working in parallel (Rust / Python / TS) — speed and
quality through deterministic tools, worktree isolation and tiered CI.

## Principles

1. **Deterministic work goes to tools.** The CLI `h` (Python + uv, `uv tool install`) handles
   worktrees, dependencies, checks, PRs and backlog operations. The agent decides only what needs
   judgment.
2. **The main session writes code and delegates the rest.** The session takes a task and
   implements it itself; review, backlog grooming, CI log analysis and research go to subagents or
   scripts. Tools return compressed output (errors only).
3. **Core + stack adapters.** The core knows no languages; the adapters are `rust/python/ts`;
   a project describes itself in `harness.toml`; the escape hatch is its own commands.
4. **Red CI instead of prose.** A rule that can be checked becomes a check.

## Environment

- Development — Windows (Git Bash + PowerShell); production — Ubuntu servers; CI — GitHub Actions
  (Ubuntu). The CLI must work on both; shell scripts are only thin wrappers.
- Platform — GitHub (Issues/PR/Actions). We do not use Linear, but we take its practices
  (triage, rules for delegating to an agent by label/size, session statuses).
- Scale: up to ~7 parallel sessions, 24 cores, 128 GB RAM → heavy-build semaphore of ~3–4.

## Stages

| # | Stage | Result |
| --- | --- | --- |
| 0 ✅ | Competitor research | `docs/research/<repo>.md` + `SUMMARY.md` |
| 1 ✅ | Skeleton and decisions | ADR-0001, `harness.toml`, `h config/doctor`, polygon, harness CI (one required check); the pilot project moved to `h` |
| 2 ✅ | Context layer | `h init` (AGENTS.md, CLAUDE.md, settings.json: deny + post-edit / pre-write hooks), `h fmt [--changed]`, skills (`bug-fix`, `research`, `task-card`, `review-gate`, `task`, `ci-triage`, `rust`, `security-rules`, `grilling`), agents (`locator`, `analyzer`, `reviewer`, `ci-triage`), a task journal that survives compaction |
| 3 ✅ | Check tiers and caches | `h affected`, `h check` (adapters, waves), machine semaphore for heavy steps, sccache, test selection by file, affected in CI |
| 4 ✅ | Parallelism | `h task start/status/heartbeat/release/clean`, lease on origin (ADR-0002), ports, cleanup of squash-merged tasks and orphans, auto-heartbeat |
| — ✅ | Thin end-to-end slice | HARNESS-1: `h task start` → `h ship` → CI → merge → clean |
| 5 ✅ | Backlog and task contract | card = GitHub issue + Definition of Ready (only the user sets `ac-confirmed`), epics = milestone + `docs/epics/*.md` (auto-PR), Projects v2 board, `h task done` (gate: merge → deploy → cleanup → check → close), `h task drop` |
| 7 ✅ | Orchestrator | review gate (`reviewer` is a different model, only the subagent writes the verdict), `h ci watch` + `ci-triage` + a limit of 2 rounds, `task` skill, `h task next`, `[ship] merge` |
| H | Reliability (audit 2026-10-23) | parallel safety, honest checking, gate integrity, resilience to GitHub failures — see the "Audit" section; not done: proof that changed tests fail on base |
| 6 | CI/CD | reusable workflow by SHA, affected and docs-only in CI, ratchet, dead-code, heavy steps after merge, cache only from main, reuse of the PR check on push to main; not done: zizmor, a clean-worktree check after CI |
| 8 | Metrics | not started: task event log and report — lead time, green-first-pass, CI/review rounds |

## Stage 0 — sources

supabase/supabase, openai/codex, getsentry/sentry, obra/superpowers, humanlayer/humanlayer,
BloopAI/vibe-kanban, smtg-ai/claude-squad, dagger/container-use (only if containers are
fast and short-lived), astral-sh/ruff. github/spec-kit is excluded (overcomplicated).

## Known problem of the pilot project

Agents take someone else's task and stop. Cause: the reservation on origin holds the **number**,
not the **execution**; any session can join someone else's branch, and the "in progress" flag
lives in the branch and is not visible from main. Fix — Stage 4/5: an execution lease (ADR-0002).

## End-to-end slice — 2026-10-21

Task HARNESS-1 (`h ship` itself) went through the cycle `h task start` → edits → `h ship` → CI →
merge → `h task clean`. Time: start 5 s, ship ~2 min (tests took 103 s of it), CI ~2.5 min.

| # | Friction | Fix |
| --- | --- | --- |
| 1 | `task start` did not say what comes next | prints the next step |
| 2 | logs in `.artifacts/` counted as user changes | excluded from the check |
| 3 | edits through shell bypass the formatting hook | `h fmt --changed`; `ship` formats and commits `style:` itself |
| 4 | local ruff ≠ ruff in CI → lint passed locally, failed in CI | `h` runs the ruff pinned in the project |
| 5 | single-package project: "affected" = the whole package, tests 100+ s | test selection by file (pytest-testmon) |
| 6 | merge and `h task clean` are two manual steps | `[ship] merge` + `h task done` (Stage 7) |

## Stages 5 and 7 — 2026-10-22

All tasks of the `backlog` and `orchestrator` epics went through the full cycle: card → user
confirmation → `h task start` → review by a subagent → `h ship` → `h ci watch` → merge →
`h task done`. Review sent the work back once; 6 times it approved with remarks, and the
significant remarks were fixed.

| # | Friction | Status |
| --- | --- | --- |
| 7 | the hook removed an import added before its use | `--unfixable F401` in the hook |
| 8 | display labels blocked readiness | labels are derived from the card |
| 9 | Windows holds a worktree directory while a shell sits in it | the `done` gate stops with a command; a repeat finishes the job; the rule "done — from the main clone" in the `task` skill |
| 10 | `h epic new` left an uncommitted file | the epic is published by an auto-PR |
| 12 | two PRs for one epic conflicted | one PR per epic |
| 13 | `ship` wrote `Closes #N` when done = deploy | `Refs #N` |
| 14 | deny `Bash(*verdict.json*)` also blocks harmless grep | left on purpose; read the file with Read |
| 15 | a new label not created in the repo → `gh issue edit` rejected the whole edit | only labels that are present are removed |
| 16 | `pytest … \| tail && git commit` hides a test failure | the gates (`review prepare`, `ship`) catch it; rule — checks only through `h check` |

## Audit — 2026-10-23

Three subagents read `h` module by module (lease and cleanup; `check`/`affected`/hooks;
`ship`/review/CI/GitHub); some findings were reproduced in temporary repositories, and the most
serious ones were rechecked against the code. Each finding became a card.

Decisions:

- gate threat model — accidents are closed in `h`, the deciding gates move to the GitHub server
  (required check); a separate bot account for the agent — no;
- a second session in the same clone does not continue a task silently — only with an explicit `--resume`;
- a skipped check step (missing tool) — `h ship` refuses until the step is explicitly disabled in
  `harness.toml`;
- metrics — first without tokens.

| Area | Main points |
| --- | --- |
| parallel safety | two sessions in one clone — one owner (key = host + path) and they silently share the worktree; a global `clean` removes fresh claims and untouched worktrees; `drop` loses unpushed commits; a network failure of heartbeat = "stop"; `--steal` does not pull from origin and overwrites someone else's work; `5`/`gh-5`/`GH-5` are different claims |
| honest checking | non-ASCII paths are lost (`core.quotePath`) → "nothing affected"; shared stack configs (`.ruff.toml`, root `conftest.py`, eslint/biome) affect nothing; a skipped step = green; the `pnpm-workspace.yaml` parser; the hook reads stdin in cp1251; one fmt command for two roles; logs of parallel checks overwrite each other |
| gate integrity | a `style:` commit passes the gate with any code; the review package can be edited; the verdict path is compared case-sensitively (NTFS is case-insensitive); `Trivial: yes` bypasses confirmation, an edit to AC-B after confirmation does not reset it; deny rules compare the command text and are bypassed by writing the command another way |
| GitHub failures | `ship` updates a closed PR; PR search with `--limit 100` across the whole repo; `ci watch` waits for an unpushed SHA, spends rounds on `cancelled`, does not see checks that are not from Actions; strict UTF-8 on logs |

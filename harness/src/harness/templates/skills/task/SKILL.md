---
name: task
description: Use when asked to take, do, continue, finish or cancel a task or issue (gh-N), or "pick the next task". The whole task cycle from claim to closed issue, with the stops where the user decides.
---

# Task cycle

Every step below is an `h` command; run it, read its few lines, follow its `next:` hint.
Stops marked **USER** are decisions that are not yours.

## 1. Pick

- The user named a task → that one.
- "Take the next one" → `h task next`; take only what it lists (ready + `agent-ok`, not
  claimed). Nothing listed → say so and stop. Never pick an issue it does not list.

## 2. Start

`h task start gh-N` claims the task on origin and creates the worktree.
- `NOT READY` → **USER**: the card needs clarification or the business criteria need the
  user's confirmation. Use the `task-card` skill to ask; never set `ac-confirmed`.
- `taken by …` → someone works on it. Pick another. Taking over a stale claim is the
  **USER**'s: they run `h task start gh-N --steal` (denied to you).
- `already in progress in this clone` → another session here works on it. Pick another.
  `--resume` only to continue your own work — a crashed or restarted session, the same
  worktree and branch.

Then `cd` into the printed worktree and read `.artifacts/task/brief.md` — the card is the
spec. Work only inside that worktree. Touching another task's worktree is forbidden.
If the brief has an `ADR (harness)` section, those decisions bind the work: a change that
contradicts an accepted ADR needs a new ADR that supersedes it — ask the **USER** first.

## 3. Work

- Unfamiliar code → `research` skill. A bug → `bug-fix` skill (failing test first).
- Commit in small steps. `h check` runs fmt / lint / typecheck / tests on what you
  touched; it is also your heartbeat, so a long task keeps its claim.
- Progress ledger `.artifacts/progress.md` survives compaction: after a green `h check`
  on a commit, `h progress done T<n>` marks that card item; `h progress note "…"`
  records the next step, `h progress note --decision "…"` (or `--adr`) a choice made. Hooks bring it back.
- Scope drift — the card turns out wrong or incomplete → **USER**, then update the card.

## 4. Review

`review-gate` skill: `h review prepare` → `reviewer` subagent → fix blockers → repeat.
Three rejections → **USER**.

## 5. Ship

`h ship` — formats, checks, pushes, opens the PR with evidence and the review.
Then CI (`ci-triage` skill if it fails; two real failures → **USER**).

Merge per `[ship] merge` in harness.toml — `ship` prints `merge: agent — …` or `merge: user — …`:
- `agent` → merge the green PR yourself (squash).
- `user` → **USER**: report the PR link as ready and wait.

## 6. Done

From the **main checkout**, never from inside the task worktree (Windows will not delete
a directory a shell stands in): `cd <main checkout> && h task done gh-N`.
It waits for the deploy, cleans up, re-checks and closes the issue.
- `NOT CLOSED (deploy-…)` → wait or report the failed deploy run.
- `cleanup-blocked` → run the printed manual commands if you can, else **USER**; then
  `h task done gh-N` again.

## Cancel

Only on the user's explicit word: `h task drop gh-N --reason "<their reason>"`.
Unsaved work makes it refuse — losing it is the **USER**'s call: they add `--discard`
(denied to you).

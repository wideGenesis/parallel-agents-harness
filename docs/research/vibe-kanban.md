# BloopAI/vibe-kanban — summary

Commit: `d5cbb53` · Clone date: 2026-10-20 · Length: ≤ 1 page (≈80 lines)

Paths are relative to `competitor_repos/vibe-kanban/`. Rust (axum/sqlx/git2 + git CLI) + React. The README declares the project sunsetting (`README.md:19`).

## 1. Context for agents
`AGENTS.md` (root) describes the crates/packages structure, generation of TS types from Rust (ts-rs; do not edit generated files by hand), and build/check commands; nested `crates/remote/AGENTS.md`, `docs/AGENTS.md`, `packages/local-web/AGENTS.md` (`AGENTS.md:1-12`). There is also a `CLAUDE.md`.

## 2. Deterministic rails
- Repository-level scripts: workspace checks `pnpm run check` / `backend:check`, i18n checks, `check-legacy-frontend-paths.sh` with an allowlist (`scripts/`, `AGENTS.md`).
- User-defined per-repo project scripts: `setup_script`, `cleanup_script`, `archive_script`, `copy_files`, the `parallel_setup_script` flag (`crates/services/src/services/container.rs:564-617`, `:409-468`, `:1074-1076`). Setup runs as a separate process before the agent; in all-parallel mode it starts in parallel with the agent (`container.rs:1076-1110`); the cleanup action is chained to the end of the coding action (`:1089-1096`).
- `copy_files`: a comma-separated list of globs, copied from the source repo to the worktree; files with the same size are skipped; `\` -> `/` normalization (`crates/local-deployment/src/copy.rs:11-53`). This is the equivalent of copying `.env`.
- Shell: Windows `cmd /C`; interactive: PowerShell, otherwise cmd.exe (`crates/utils/src/shell.rs:15-39`).

## 3. Parallelism and isolation
- Branch name: `[prefix/]<4 hex of uuid>-<title slug ≤16 chars>` (`container.rs:786-796`, `crates/utils/src/text.rs:4-24`). The prefix is validated (`crates/git/src/validation.rs`). The short uuid protects against collisions between identical titles.
- Layout: `<tmp>/vibe-kanban/worktrees/<workspace>/<repo>`; a custom directory always gets `.vibe-kanban-workspaces` appended, so GC does not touch folders it does not own (`crates/worktree-manager/src/worktree_manager.rs:510-523`). One workspace can hold several repos.
- Creation: a branch from `target_branch` (`create_branch`, `:60-84`), then `ensure_worktree_exists` under a **process-local mutex per path** (`:15-17`, `:95-105`). Validity check = the path exists + is registered in `.git/worktrees/*/gitdir` + is valid (`:156-222`). Otherwise a full rebuild: `worktree remove -f`, metadata cleanup, `remove_dir_all`, `prune` (`:230-265`), then add with one retry after the metadata cleanup (`:303-330`). Paths are compared with `dunce::canonicalize` (Windows UNC prefix) (`:189-191`).
- Ports: `scripts/setup-dev-environment.js` finds free ports and writes `.dev-ports.json` (`:7-31`); this is for developing the project itself, not per-worktree ports for an agent. *(speculation)*: dev-server processes in a workspace get a port from the user's script; the core does not allocate one.
- Task claim/lease: **no file lock**; the DB is the protection. Before archiving, a check `has_running_non_dev_server_processes_for_workspace` runs (`container.rs:487`); a process has a Running/Failed status in SQLite. The mutex works only inside one process; there is no inter-process protection.
- Crash: at startup, `cleanup_orphan_executions` marks everything "Running" in the DB as Failed with no exit code and records the after-head commit (`container.rs:272-300`, called from `crates/server/src/startup.rs:160`). `cleanup_orphan_workspaces` deletes workspace directories that are not in the DB (`crates/workspace-manager/src/workspace_manager.rs:538-653`, called from `local-deployment/src/container.rs:304`); `DISABLE_WORKTREE_CLEANUP` turns it off. A suspicious directory is cleaned only if `.git` is a file (a sign of a worktree) (`worktree_manager.rs:525-529`).
- Cleanup: `spawn_workspace_deletion_cleanup` with optional branch deletion (`workspace_manager.rs:217-275`); subprocesses use `kill_on_drop(true)` (`crates/executors/src/actions/script.rs:58`).
- Monitoring: process statuses in the DB + live logs through msg_store; `pr_monitor` polls open PRs every 60 s (`crates/services/src/services/pr_monitor.rs:51-90`); on merge it archives the workspace if there are no other open PRs and the workspace is not pinned (`:171-195`).

## 4. Checks and speed
No data on tiered checks for agents. CI gates by path: `.github/workflows/test.yml` with path filters (`pnpm-lock.yaml`, `.cargo/**`, `:54-157`).

## 5. CI/CD
`test.yml` on `ubuntu-24.04` with a YAML anchor for the runner (`:24-28`), path filters by group; the `pre-release.yml` release builds a matrix that includes `x86_64/aarch64-pc-windows-msvc` (`:226-237`). Relay/remote deploys are separate workflows.

## 6. Tasks, plans, review
Kanban task -> workspace (+ sessions, processes) in the DB. Merging: `merge_changes` is a **squash**; it is refused if the base moved ahead (`BranchesDiverged`); conflicts are detected in memory through `merge_commits(fail_on_conflict)`; the base ref is updated without checkout; the task branch is reset to the squash (`crates/git/src/lib.rs:575-670`, `:1081-1125`). If the base is checked out, the merge goes through the CLI (`:601-627`). Rebase goes through the git CLI; it refuses when a rebase is already in progress and reports the conflicting files (`:1129-1201`). There is a PR flow, a `review` crate, and a task MCP server (`crates/mcp`).

## 7. Adopt / do not adopt
| Practice | Decision | Benefit | Side effect |
|---|---|---|---|
| Branch `prefix/<short id>-<slug>` | Adopt | No collisions, readable | Needs a task id from our backlog |
| `ensure_worktree_exists` is idempotent: validate -> recreate | Adopt | Self-healing after a crash | Needs fail-fast if the worktree is dirty |
| Owner-only GC: a base directory with a marker, `.git` as a file | Adopt | GC will not delete what it does not own | Requires a single root |
| `copy_files` by glob + setup/cleanup scripts in the project config | Adopt | Declarative, deterministic | Must keep secrets out of the log |
| Squash + block on `base ahead` + in-memory conflict check | Adopt | Safe integration | We lose the agent's history (squash) |
| In-memory lock only + DB status | Do not adopt | — | No protection between processes; needs a lease file with PID/TTL |
| Orphan Running -> Failed at startup | Adopt | Simple recovery | Loses the ability to resume *(speculation)* |
| Full server/DB/UI | Do not adopt | — | Overkill for a portable harness |

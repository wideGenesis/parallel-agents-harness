# smtg-ai/claude-squad — summary

Commit: `ce1ffb4` · Clone date: 2026-10-20 · Length: ≤ 1 page (≈80 lines)

Paths are relative to `competitor_repos/claude-squad/`. Go, a TUI manager: a tmux session + a git worktree per agent.

## 1. Context for agents
No data: no `AGENTS.md` and no `CLAUDE.md` in the root (checked with `ls`). This is an orchestrator tool, not a repository for agents; it imposes no rules on the agents themselves.

## 2. Deterministic rails
- All git goes through the CLI `git -C <path>` with `CombinedOutput` (`session/git/worktree_git.go:58-68`); no go-git for mutations.
- Commits are automatic and always `--no-verify` (`worktree_git.go:90`, `:144`): repository hooks are bypassed. The message is a template: `[claudesquad] update from '<title>' on <time>` (`app/app.go:728`).
- Auto-confirmation of agent prompts: a hash of the tmux pane (sha256) + a search for the hint string "No, and tell Claude what to do differently" / aider / gemini (`session/tmux/tmux.go:244-266`); the daemon presses Enter (`daemon/daemon.go:63-74`). Fragile: depends on the agent's UI text.
- Setup scripts, `.env` copying, dependency installation, ports: no data (grep over `*.go` found nothing).

## 3. Parallelism and isolation
- Branch name: `BranchPrefix + sessionName`, then `sanitizeBranchName` (lowercase, `[a-z0-9-_/.]`, collapsing hyphens; a comment about `DOMAIN\user` on Windows): `worktree.go:73-80`, `util.go:13-33`.
- Worktree path: `<configDir>/worktrees/<branch>_<hex(UnixNano)>`: unique by time, not by task (`worktree.go:50-68`). The worktree is outside the repo.
- Creation: from the current `HEAD` (a clean slate), `git worktree add -b`; `baseCommitSHA` is recorded for the diff (`worktree_ops.go:100-109`, `:62-69`). Before creation: `worktree remove -f`, `RemoveAll` of an orphaned folder and `branch -D` of a branch with the same name (`:96-102`). This **silently destroys** an existing branch.
- Task uniqueness: only the session name is the key (`DeleteInstance(title)`, `session/storage.go:97`); the tmux name has no spaces and has a prefix (`tmux.go:68-72`). No lease/lock between processes; state is one JSON file written with `os.WriteFile`, without locking or atomic replace (`config/state.go:106`). *(speculation)*: two `cs` on one config directory will overwrite each other's state.
- Statuses: Running/Ready/Loading/Paused (`session/instance.go:18-28`).
- Pause: auto-commit, detach tmux, `worktree remove` + prune; the branch is kept, and its name goes to the clipboard (`instance.go:423-503`). Resume refuses if the branch is checked out in the main repo (`:517-523`).
- Crash: on load, a dead tmux session does not bring everything down; it moves the instance to Paused (`instance.go:46-60` of the Start block, lines ≈248-258); an orphaned worktree is detected by `.git` (`worktree_git.go:162-179`). Rollback on a failed start: `Kill()` (`instance.go:208-216`).
- Cleanup: `Cleanup` = `worktree remove -f` + `branch -D` (except `isExistingBranch`) + `prune` (`worktree_ops.go:117-150`); the global `CleanupWorktrees` (`:174-236`) deletes all folders in `worktrees/`. A mistake: there `branch -D` runs without `-C`, i.e. in the process cwd *(speculation: a bug when started from another directory)*.
- Monitoring: a daemon process polls sessions every `DaemonPollInterval` (`daemon.go:51`, `config/config.go:41`), the pid is in `daemon.pid`, `StopDaemon` = `Kill` (`daemon.go:129-181`). Progress = "the pane changed" + diff stats against `baseCommitSHA` (`daemon.go:66-72`).
- Windows: natively only the daemon detach (`CREATE_NEW_PROCESS_GROUP|DETACHED_PROCESS`, `daemon/daemon_windows.go:12-14`) and polling the window size every 250 ms instead of SIGWINCH (`session/tmux/tmux_windows.go:30-55`). But it depends on tmux; the README requires tmux (`README.md:49`), i.e. in practice WSL *(speculation)*.

## 4. Checks and speed
No data on checks for agents. Its own: `go test -v ./...` in `.github/workflows/build.yml:41`, builds for linux/darwin/windows (`build.yml:25`), `lint.yml`.

## 5. CI/CD
`build.yml` (goos matrix), `lint.yml`, `release.yml`, `cla.yml` (CLA), `deploy-pages.yml`. All on `ubuntu-latest` (`build.yml:22`); no merge queue or path filters visible.

## 6. Tasks, plans, review
No backlog/plan: a task = a session name + a prompt (`SendPrompt`, `instance.go:632`). Merging: `Submit` → commit, `git push -u origin`, `gh repo sync`, open the branch in the browser (`worktree_git.go:71-126`, `app/app.go:722-737`); no merge/rebase/PR creation: a human makes the PR. Checkout = pause + the branch to the clipboard.

## 7. Adopt / do not adopt
| Practice | Decision | Benefit | Side effect |
|---|---|---|---|
| Worktree outside the repo in the config directory + name sanitizing | Adopt | No repo clutter, safe names | Needs GC and a link to the task |
| Pause = WIP commit + remove the worktree, keep the branch | Adopt | Frees disk/branch without losing work | `--no-verify` commits clutter the history |
| Dead session -> Paused, not an error | Adopt | One failure does not hide the others | Needs a visible indicator |
| `branch -D` of a same-name branch at Setup | Do not adopt | — | Loses someone else's work; needs fail-fast |
| JSON state without lock/atomic rename | Do not adopt | — | Races, corruption on a crash; needs a lease file |
| Auto-confirmation through a regex over the pane | Do not adopt | — | Fragile, bypasses the permission model; we use an allow-list |
| Only the name as the identifier, uniqueness by time | Partly | Simple | No protection against "two agents on one task" |

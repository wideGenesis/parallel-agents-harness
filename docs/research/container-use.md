# dagger/container-use — summary

Commit: `2e43e62` · Clone date: 2026-10-20 · Length: ≤ 1 page (≈80 lines)

Every claim comes with a `path:line` path in the repository. Mark guesses with *(speculation)*.

## 1. Context for agents
- The project is an MCP server + the `cu` CLI: a git branch per environment + a Dagger container, code in `/workdir`, the log in git notes, worktrees in `~/.config/container-use/worktrees/` (`AGENTS.md:5-9`).
- Rules for the agent: use ONLY the environment tools, do not install git in the container, always tell the user `cu log/checkout <env-id>` (`AGENTS.md:13-18`). Ready-made rules for Claude/Cursor/Windsurf: `rules/agent.md`, `rules/cursor.mdc`, `rules/windsurf.mdc`; `CLAUDE.md` is empty (0 lines), `AGENT.md` has 53 lines.
- The repo describes itself as "early development" (`README.md:35`).

## 2. Deterministic rails
- Git hooks are disabled for internal operations: `-c noHooks push/merge` (`repository/git.go:~165`, `repository/repository.go:622,633`).
- Validation of ids/refs and export paths against escaping the worktree (`repository/git.go:150-156,405`; issue #337).
- Lint/build run through a Dagger module (`AGENTS.md:57`, `.dagger/main.go:32-33`: CacheVolume for go mod/build). No restrictions such as deny rules for the agent: *no data*.

## 3. Parallelism and isolation
- Creation (`repository/repository.go:245-318`): petname id → `git push` HEAD to a "fork" `~/.config/container-use/repos/...` as branch `id` → `git worktree add` in the fork (`repository/git.go:166-181`) → initial commit → a container from `ubuntu:24.04` (`environment/config.go:13`) via Dagger.
- The container is built like this: `From(base)` → `setup_commands` WITHOUT sources (for the layer cache) → services → `WithDirectory(".", src)` → `install_commands` (`environment/environment.go:179-243`). Sources come from the fork with `NoCache: true` on every Create (`repository/repository.go:273`).
- Container state is stored as a Dagger container ID in `State.Container` (`environment/environment.go:128-143`); every `environment_run_cmd` command is a NEW `WithExec` on top of the saved state (a new container, `environment.go:270`); background processes do not see later FS changes (`mcpserver/tools.go:644`).
- Ports: services are separate containers, bound by name with `WithServiceBinding` (`environment.go:231-233`), exposed through `Host().Tunnel` with `Frontend: 0` (a random host port, `environment/service.go:75-83`), so there are no port collisions between agents; service start has a 30 s timeout (`service.go:13`).
- Concurrency: inter-process locks `user-repo`/`fork-repo`/`notes` (`repository/flock.go:19-26`). One MCP process can run many environments through `environment_id`; single-tenant mode = one session per process (`mcpserver/singletenant.go:1-6`).
- Cleanup: `cu delete`, `cu prune` (`cmd/container-use/delete.go`, `prune.go`); I found no automatic TTL *(speculation: containers live as long as the Dagger engine lives)*.

## 4. Checks and speed
- Cache: Dagger layers (setup before copying sources, `environment.go:222`); `CacheVolume` only in the repo's own build (`.dagger/main.go:32-33`); agent environments get no named cache volumes, so package managers rebuild their cache when a layer changes *(speculation)*.
- No startup time figures in the code/docs: *no data*. Visible costs of Create: git push+worktree add+fetch, export of the whole working directory to the host after EVERY command `Export(..., Wipe: true)` (`repository/git.go:376-398`; called from `tools.go:652`) → overhead grows with repo size *(speculation)*.
- Command output is returned in full (stdout+stderr, `environment.go:299-307`), with no compression.

## 5. CI/CD
- All workflows run on `ubuntu-latest` (`.github/workflows/test.yml:13`, `build.yml`, `lint.yml`, `release.yml`); CI does not test Windows, although the binary is built for windows (`.goreleaser.yaml:18-21`). Path filters/merge queue are not relevant for us: not studied.

## 6. Tasks, plans, review
- Returning the result: each edit/command → export to the worktree → commit → `git fetch container-use <id>` into your repo (`repository/git.go:286-318`); the branch shows up as `container-use/<id>`. A human runs `cu merge` (`merge --no-ff --autostash`) or `cu apply` (`--squash`) (`repository/repository.go:614-636`); `cu checkout`, `cu diff`, `cu log` (`cmd/container-use/`).
- Command history is in git notes `container-use`, state in `container-use-state` (`repository/repository.go:25-26`).
- The agent connects through MCP over stdio (`cmd/container-use/stdio.go:35`); tools: `environment_create/open/list/config/run_cmd/file_read/list/edit/write/delete/checkpoint/add_service/log/diff` (`mcpserver/tools.go:301-1034`). The agent may not edit files outside these tools.
- Secrets are resolved inside the container (`docs/secrets.mdx:7`).

## 7. Adopt / do not adopt
Requirements: Docker + Git (`docs/quickstart.mdx:11`); the Dagger engine starts through `dagger.Connect` (`cmd/container-use/stdio.go:23`). Windows: the code exists (`%APPDATA%` paths in `repository/repository.go:31-45`, `signal_windows.go`, detection of a Docker Desktop error in `docker_errors.go:22`), but there is no Windows CI or documentation; Linux containers on Windows work only through Docker Desktop/WSL2.

| Practice | Decision | Benefit | Side effect |
|---|---|---|---|
| Environment = git branch + worktree "fork" + container | Adopt the idea (branch per agent, a separate bare fork, `git push`/`fetch` as the result transport) | Does not touch your working directory; the result is an ordinary branch | The double repository and git notes make debugging harder |
| Container per agent instead of a worktree | Do not adopt as a replacement; optionally as a "sandbox" layer for risky tasks | Real isolation of processes, ports (random tunnel), DB services | Needs Docker/WSL2 on Windows; FS export after every command; the native Windows/MSVC toolchain is not available in a Linux container |
| Setup commands before copying sources (layer cache) | Adopt as a principle | Fast restart when the image is unchanged | Only with a stable base_image/setup |
| One MCP process with `environment_id` | Study | One server for many agents | A shared process is a single point of failure; shared in-memory state |
| Merge/apply (`--no-ff`/`--squash`) by a human | Adopt | An explicit control point | No auto-merge and no conflict resolution |
| Dependency on the Dagger engine | Do not adopt into the core | Cache and graphs out of the box | A heavy daemon, an early-stage project (`README.md:35`); 24 cores/128 GB is enough, but the software is poorly tested on Windows |

Verdict: adopt the idea "a branch + an isolated environment per agent, the result through a git ref"; do not adopt container-use itself into the core as a dependency, but evaluate it later as an optional Linux sandbox for production Ubuntu (Docker requirements are natural there). For development on Windows, worktrees remain the main isolation.

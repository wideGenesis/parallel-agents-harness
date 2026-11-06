# openai/codex — summary

Commit: `e1b9c94` · Clone date: 2026-10-20 · Length: ≤ 1 page (≈80 lines)

Every claim comes with a `path:line` in the repository. Mark guesses *(speculation)*.

## 1. Context for agents
- There is no root AGENTS.md/CLAUDE.md in this commit (ls-tree HEAD; `docs/agents_md.md:1-3` holds only a link to external docs). The rules "for ~120 crates" are not in the repo *(speculation: moved out of the repo or into a private layer)*.
- Only narrow nested AGENTS.md files per subsystem remain: `codex-rs/tui/src/bottom_pane/AGENTS.md:1-12` (keep module docs in sync with the state machine), `codex-rs/exec-server/src/sandbox_integrity/AGENTS.md:1-40` ("scope and review contract": what is deliberately not covered, so that review does not ask for too much).
- Workspace: 119 folders in `codex-rs/` (`ls codex-rs`); members in `codex-rs/Cargo.toml:2` take ≈197 lines.
- Product skills (samples) live in `codex-rs/skills/src/assets/samples/*/SKILL.md`; this is a product feature, not navigation for development.

## 2. Deterministic rails
- Quality rules live in the manifest, not in text: `[workspace.lints.clippy]` with deny on `unwrap_used`/`expect_used`/`redundant_clone`/`uninlined_format_args` and others (`codex-rs/Cargo.toml:577-613`).
- Architectural invariants are Python scripts in CI: tui does not import codex-core (`.github/scripts/verify_tui_core_boundary.py:3,16-21`), manifests inherit the workspace (`.github/workflows/repo-checks.yml:20-21`), Bazel clippy flags == Cargo lints (`repo-checks.yml:26-27`).
- A custom "argument-comment" lint on Dylint (`justfile:184-196`, `.github/workflows/rust-ci.yml:109-158`).
- justfile: `fmt`/`fmt-check`/`clippy`/`fix`/`test` (`justfile:49-61,86-88`); `cd codex-rs` by default (`justfile:1`); a cross-OS shell via `scripts/just-shell.py` with an `{args}` placeholder (`justfile:4-6`, `scripts/just-shell.py:1-7`).
- Generated artifacts have separate recipes (`write-config-schema`, `write-app-server-schema`, `write-hooks-schema`: `justfile:173-182`), and CI checks for a "clean worktree" after the job (`.github/actions/check-clean-worktree/action.yml:1-19`) — this catches a forgotten regeneration.
- Bloat blocking: a blob-size-policy with an allowlist (`.github/workflows/blob-size-policy.yml:1-25`, `.github/blob-size-allowlist.txt`).
- No Claude-style hooks for development were found in the repo (`.claude`/`.codex` are absent from the tree). The crate `codex-rs/hooks` is a product feature.

## 3. Parallelism and isolation
- `.worktreeinclude` contains `user.bazelrc` (`.worktreeinclude:1`) — a list of files that are not checked in and are copied into a new worktree.
- The crate `codex-rs/worktree` is a product feature; `nextest.toml:61` has an e2e test `worktree_start_and_fork…`. The repo does not describe a development process for parallel agents: "no data".
- CI: a concurrency group per PR; old runs are cancelled except on main (`.github/workflows/bazel.yml:10-14`).

## 4. Checks and speed
- Tiers: locally `just fmt` + `just clippy -p <crate>` / `just test -p <crate>` (the recipes accept `*args` and pass them to cargo: `justfile:57-61,86-88`) → PR CI → full CI after merge.
- nextest: the `local` profile inherits default (`codex-rs/.config/nextest.toml:28-29`); retries=1, slow-timeout 30s×2 (`nextest.toml:1-6`); test-groups with `max-threads` for heavy integration tests (`nextest.toml:31-52,72-76`); locally up to 4 app-server processes (`nextest.toml:78-82`).
- `justfile:81-85`: "Workspace crate features are banned, so no `--all-features`" — the feature ban simplifies the build matrix.
- Build profiles: `dev` with `debug="line-tables-only"` (`Cargo.toml:624-627`), `dev-small` without debug + strip (`:629-633`), `ci-test` with opt-level 0 to reduce disk pressure (`:652-655`), release with thin-LTO + codegen-units=4 (`:635-644`).
- An 8 MiB stack for tests (`justfile:8,88`), a shared `RUST_MIN_STACK`.
- Bazel as the main PR build: remote cache (BuildBuddy), disk cache, `--remote_cache_compression` (`.bazelrc:6-9`; `.github/workflows/README.md:10-12,20-23`).
- Full CI: builds a nextest archive once and runs shards (`rust-ci-full-nextest-platform.yml:52-53,168-178`), sccache + split cargo-home/target caches (`rust-ci-full.yml:256-311`), `CARGO_INCREMENTAL=0` (`:135`), cargo-chef warm-up (`:358`), Windows Dev Drive (`.github/actions/setup-ci/action.yml:14-20`).

## 5. CI/CD
- A single merge-gate point `blocking-ci.yml` + an aggregating job `CI required` with `always()` and `check_ci_results.py` (`blocking-ci.yml:3-5,48-72`) — one required check in the ruleset instead of a list.
- Path filter without external actions: `git diff --name-only base head` → flags `codex`/`workflows`/`argument_comment_lint` (`rust-ci.yml:7-53`); jobs run under `if: needs.changed.outputs…` (`:64,89`).
- Strategy: on PR — fast checks (fmt, shear, lint); heavy cargo-clippy/nextest/release — post-merge (`.github/workflows/README.md:3-31,33-37`).
- Security: all actions are pinned to SHA (`rust-ci.yml:17`), `persist-credentials: false`, fence audit mode (`blob-size-policy.yml:13-15`), `cargo-deny` (`blocking-ci.yml:23-26`, `codex-rs/deny.toml`), AI jobs only for `github.repository == 'openai/codex'` and in an `environment` (`issue-deduplicator.yml:12-15`).

## 6. Tasks, plans, review
- AI review/triage via Codex in Actions with prompts in `.github/codex/labels/*.md` (codex-review, codex-rust-review, codex-attempt, codex-triage), triggered by label (`issue-deduplicator.yml:3-13`).
- The Rust review prompt: be brief, the PR body must explain the motivation, one PR — one concept, do not bloat `core`, `assert_eq!` on the whole object instead of element by element (`.github/codex/labels/codex-rust-review.md:1-27`).
- CODEOWNERS (`.github/CODEOWNERS`), a CLA bot (`workflows/cla.yml`), issue templates. No ADR/plan/backlog format is visible in the repo: "no data".

## 7. Adopt / do not adopt
| Practice | Decision | Benefit | Side effect |
|---|---|---|---|
| Invariants as Python checks in CI (crate boundaries, lints==Bazel) | Adopt | Rules are executed, not read | Each script needs maintenance |
| Lints in `[workspace.lints]` with deny | Adopt | A single source, the agent does not argue | More clippy failures for the agent |
| `CI required` aggregator with `always()` | Adopt | One required check, no "green skip" | Needs our own script to parse needs |
| Path filter on `git diff` without external actions | Adopt | Less supply-chain risk, a fast PR | A hand-written list of paths |
| `check-clean-worktree` after the job | Adopt | Catches artifacts that were not regenerated | Failures from leftover files in tests |
| `.worktreeinclude` for local files that are not checked in | Adopt | Simple transfer of env into a worktree | Needs our own copy script |
| Heavy checks (full clippy/nextest) post-merge, light ones on the PR | Adopt (with a caveat) | Low PR latency | Breakage shows up after merge |
| nextest archive + shards, sccache | Later | Pays off on a large CI | Complex for small repos |
| Bazel next to Cargo | Do not adopt | Remote cache for ~120 crates | Double configuration, lint drift |
| Only narrow AGENTS.md files per subsystem with a "review contract" | Adopt the idea | Short scoped rules | No common entry point for the agent |

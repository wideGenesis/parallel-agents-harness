# astral-sh/ruff — summary

Commit: `3f0cdd9` · Clone date: 2026-10-20 · Length: ≤ 1 page (≈80 lines)

Every claim comes with a `path:line` in the repository. Mark guesses *(speculation)*.

## 1. Context for agents
- `CLAUDE.md` is one line `@AGENTS.md` (CLAUDE.md:1); the single source of truth is `AGENTS.md` (230 lines), flat; no nested AGENTS.md files are visible in the sparse slice.
- AGENTS.md: avoid duplicate work before a PR (AGENTS.md:5-9), PR labels/changelog (11-25), review is "deliberately nitpicky", numbered items, blocker vs non-blocking (33-38), exact test commands (59-110), clippy (135-139), Development Guidelines (212-230).
- Skills in `.agents/skills/*` (ty diagnostics, triage, ecosystem reports), connected as a plugin marketplace from the directory (`.claude/settings.json`: enabledPlugins/extraKnownMarketplaces); AGENTS.md points to them by task type (AGENTS.md:161-167) — progressive disclosure.
- Threat models for review are moved to `agents/references/*-threat-model.md` (AGENTS.md:29-31, 45-47).
- The rule "write for the reader, not for the session": no session history/drafts in PRs and comments (AGENTS.md:49-57).

## 2. Deterministic rails
- The agent's final gate: `uv run --only-dev --locked prek` (AGENTS.md:220); prek is a Rust runner compatible with pre-commit.
- `.pre-commit-config.yaml`: `fail_fast: false` (:1); revisions are pinned by SHA with `# frozen: vX` (:25, :66); `priority` 0/1/2 sets waves of parallel runs, fixers run after read-only hooks (:23, :135, :158). Hooks: rustfmt, typos, zizmor (:65), uv-lock --check (:123), ruff-format/check, mdformat, markdownlint; actionlint only in the `manual` stage because it is slow (:88-92).
- Deterministic rules: do not edit snapshots by hand, only regenerate them via env `INSTA_FORCE_PASS/INSTA_UPDATE/MDTEST_UPDATE_SNAPSHOTS` (AGENTS.md:63, 118); `cargo dev generate-all` after editing options/CLI (226); PEP 723 scripts only via `uv run` (229-230).
- SessionStart hook: `.claude/settings.json` -> `session-start.sh` branches on `CLAUDE_CODE_REMOTE=true` (session-start.sh:5-7); the web variant installs gh, writes `GH_REPO` into `CLAUDE_ENV_FILE`, adds clippy/rustfmt, and warns that nextest is missing -> `cargo test` (session-start-web.sh:4-20). There are no deny rules in settings.
- The fallback without nextest is spelled out explicitly (AGENTS.md:87-110).

## 3. Parallelism and isolation
- On worktrees/multi-agent sessions — no data. There is `.devcontainer/` and a remote session (web hook) — isolation by container/cloud.
- Put ad-hoc repro files outside the checkout, otherwise the root pyproject picks them up (AGENTS.md:183).
- Reviewers by path: `.github/pr-reviewer-pools.toml:1-30` (path-glob pools -> people).

## 4. Checks and speed
- Tiers: locally a scoped `cargo nextest run -p <crate> -E 'not binary(=file_watching)'` and `--test mdtest` (AGENTS.md:66-79); in CI the full `cargo insta test --test-runner nextest` (ci.yaml:444).
- `.config/nextest.toml`: test-group `serial` (max-threads=1) for file_watching (:2-8); profile `ci`: fail-fast=false, failure-output=immediate-final, slow-timeout 1s / terminate-after 60 (:10-24). CI enables it via `NEXTEST_PROFILE: ci` (ci.yaml:32).
- Caches: `Swatinem/rust-cache` (ci.yaml:354, 419); `shared-key: ruff-linux-debug` + `save-if: main` (:421-422) — PRs only read; ecosystem reads the same key with `save-if: false` (:791-792); `cache-bin: "false"` on Win/macOS because of a bug (:546-547). sccache was not found in ci.yaml. uv: `enable-cache`, `cache-python` (:433-434). The prek cache `~/.cache/prek` is keyed by the config hash (:1088-1092).
- Build speedup: `CARGO_INCREMENTAL=0`, `PROFILE_DEV_LTO=false`, `OPT_LEVEL=0` (:22-25), `DEBUG=line-tables-only` (:414), mold (:425-426), static CRT on MSVC (.cargo/config.toml:9-10). Debug builds instead of release for development (AGENTS.md:143).
- Output compression: nextest `status-level=skip`, `final-status-level=slow` (nextest.toml:17-24); a separate mdtest step with GitHub annotations and `|| true` (ci.yaml:435-442).

## 5. CI/CD
- `determine_changes` (ci.yaml:59-302): full history `--filter=blob:none --unshallow` (:98); merge-base = `GITHUB_SHA^1` for PRs, stacked PRs are taken into account (:105-107); then a flag per area via `git diff --quiet BASE...HEAD -- pathspec` (parser/linter/formatter/code/fuzz/ty/py-fuzzer/playground/benchmarks/release, :66-86, 128-302). Each flag includes Cargo.lock/toolchain and `ci.yaml` itself (:134-143). Dependencies between crates are written in by hand (ty: :270-278) *(speculation: drift risk)*.
- Job tiers: always (cargo fmt, shellcheck, prek, docs, python-package); `code==true || main` (clippy :346, test :410, deny :954, shear :977, wasm, msrv); narrow ones by flag (fuzz-parser :667, formatter :1139, playground :1163); ecosystem — only PRs and linter/formatter (:759-764); heavy ones (Win/macOS test :524-535, PGO :1053, CodSpeed benchmarks :1202+).
- Escape labels `no-test`/`no-build` (:394, :410, :1053).
- Ecosystem: the baseline is built on the merge-base, then the PR version; `ruff-ecosystem check/format` stable+preview on real repos, markdown into `GITHUB_STEP_SUMMARY`, an artifact for the commenter bot; it fails only on "project error" (ci.yaml:770-876).
- A single gate `required-checks-passed`: `if: always()`, a `needs` list, jq over `toJSON(needs)`, success|skipped = ok (:1496-1523) — the pattern for path-filtered jobs.
- Concurrency: group = workflow+ref+PR number|sha, `cancel-in-progress: true` (:12-14) — on main the sha is unique, so runs are not cancelled. `permissions: contents: read` at the top level (:3-4); `persist-credentials: false` (:89-90).
- Security: all actions by SHA + a version comment (:88, :354); `setup-uv` with `version` + sha256 `checksum` (:430-432); renovate `minimumReleaseAge: "85 days"` (renovate.json5:81); zizmor in prek (.pre-commit-config.yaml:65) with `.github/zizmor.yml` (forbidden-uses for taiki-e/install-action and cargo-deny-action — they prefer their own builds, :12-17; ignore secrets-outside-env for a list of workflows, :3-10); actionlint with a registry of custom runners (.github/actionlint.yaml:4-10); AI security review of PRs as a reusable workflow, only for same-repo PRs (ci.yaml:37-57).
- Runners: a ternary `github.repository == 'astral-sh/ruff' && '<depot|namespace|github-4core>' || 'ubuntu-latest'` — forks run on free runners (ci.yaml:63, 408, 529-530); `timeout-minutes` 5-20 on every job; `defaults.run.shell: bash` (:16-18); `UV_LOCKED=1` globally (:21).

## 6. Tasks, plans, review
- There is a PR template, CODEOWNERS, issue templates (.github/). Backlog/ADR/plans — no data.
- Review by an agent: numbered items, severity, blocker vs non-blocking (AGENTS.md:33-38); a separate threat-model mode for security (29-31, 45-47).
- Subagents: `.agents/skills/summarise-ecosystem-results/references/subagent-handoff.md` — a handoff protocol *(did not read the content)*.

## 7. Adopt / do not adopt
| Practice | Decision | Benefit | Side effect |
|---|---|---|---|
| `CLAUDE.md` = `@AGENTS.md` | Adopt | one source for all agents | keep Claude-specific parts separate |
| determine_changes via `git diff --quiet BASE...HEAD -- pathspec` | Adopt; generate the pathspec from the crate graph | deterministic, no third-party actions | a hand-written list drifts |
| The `required-checks-passed` gate (success or skipped) | Adopt | branch protection with path filters | needs `always()` |
| rust-cache `save-if: main` + shared-key | Adopt | PRs do not bloat the cache | the first PR after a lock change is cold |
| nextest profile `ci` + serial group | Adopt | less flakiness, readable output | — |
| Actions by SHA + uv checksum + renovate age | Adopt | supply chain | renovate noise |
| zizmor + actionlint in prek | Adopt | catches injections in workflows | actionlint is slow (manual) |
| prek `priority` waves | Adopt | parallel hooks without fixer races | — |
| Ecosystem baseline-vs-PR | Adapt | catches regressions on a real corpus | expensive, two builds |
| Runner ternary for forks | Adopt if we use paid runners | forks do not break | complicates `runs-on` |
| Labels `no-test`/`no-build` | No | a fast bypass | a hole in the gate |
| SessionStart hook with local/remote branching | Adopt | a reproducible environment | — |
| Snapshots only by regeneration | Adopt | determinism | — |

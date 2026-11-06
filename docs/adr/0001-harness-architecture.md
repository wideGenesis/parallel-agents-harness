# ADR-0001 — Harness architecture: CLI `h`, core + adapters, `harness.toml`

Status: accepted · 2026-10-20 · Basis: `docs/plan.md`, `docs/research/SUMMARY.md`

## Context

We need a portable harness for agents working in parallel in Rust / Python / TS projects.
Development is on Windows (Git Bash + PowerShell); CI and production are on Ubuntu. Up to ~7 parallel sessions.
Research showed that discipline rests on tools and CI, not on text
(SUMMARY #3), and that shell scripts are fragile on Windows (MSYS path conversion broke three clones).

## Decision

1. **One CLI `h`** — Python ≥ 3.11, installed with `uv tool install`, no runtime dependencies
   (stdlib: `argparse`, `tomllib`, `subprocess`, `json`). Everything deterministic is one of its
   subcommands: `doctor`, `config`, `check`, `task start/done/clean`, `ship`.
   Shell files are allowed only as one-line wrappers over `h` (git/Claude Code hooks).
2. **Core + stack adapters.** The core knows no languages. An adapter (`rust`, `python`, `ts`)
   can find packages, give the fmt / lint / test / typecheck commands for a set of packages,
   and map a file to a package. A project can override any command in the config.
3. **`harness.toml` in the project root** — the only place for configuration. A missing stack
   section = autodetect by manifest (`Cargo.toml`, `pyproject.toml`, `pnpm-workspace.yaml`).
4. **Output is for the agent.** By default only the summary and errors (≤ ~20 lines); the full log
   goes to `.artifacts/`. Every command has `--json` for scripts and subagents.
5. **Shared state lives on origin, local state in `.git/harness/`.** Task claims are
   refs on origin (ADR-0002); local caches and semaphores are in the clone's shared `.git`,
   so they are visible from all worktrees.
6. **Layout of the harness repository:** `harness/` (package), `templates/` (what
   is installed into a project), `polygon/` (reference Rust+Py+TS project for tests).

## Rejected

- **just / prek as the core** — one more dependency on every machine, and the recipes still
  run in a shell; we keep the option to call them from an adapter.
- **bash scripts (as in the pilot project)** — paths and encodings break on Windows; thousands of lines
  of bash are hard to test.
- **Rust for the CLI** — one binary, but slow iteration; come back to it if Python's startup
  time (`h` on every hook) becomes noticeable.
- **Containers as the isolation core** — see `docs/research/container-use.md`.

## Consequences

- `+` One language for the logic, tested with pytest on Windows and Ubuntu.
- `+` Connecting a project: `uv tool install` + `harness.toml` + `h init` (Stage 2).
- `−` `h` starts on Windows in ≈ 270 ms (interpreter 125 ms + imports ~105 ms); the whole
  post-edit hook takes 230–390 ms versus 86 ms for bare `ruff format` (measured 2026-10-20).
  Acceptable: an agent's edit takes seconds. The hook is kept synchronous: `async: true`
  races with the agent's next Edit ("file modified since read"). Fallback if it becomes
  noticeable: a fast path without argparse for `hook`, lazy imports.
- `−` The first edit of an unformatted file reformats the whole file, and the agent's next
  Edit may not find its `old_string` (observed 2026-10-21 on `cli.py`).
  A one-time cost at adoption; fixed by one `h fmt` over the old files before the start.
- `−` `which` is not enough: rustup installs a `rustfmt` proxy even without the component in the toolchain.
  Tool presence is checked by running `<tool> --version` (`harness/src/harness/tools.py`).
- `−` The machine needs uv; CI needs `astral-sh/setup-uv` by SHA.

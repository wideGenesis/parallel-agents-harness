---
name: rust
description: Use when writing, changing or reviewing Rust code (.rs files, Cargo.toml, a new crate or dependency), or when clippy, rustfmt or a cargo test step fails in h check. How Rust passes h check, what is banned and what to use instead, judgment rules clippy cannot check.
---

# Rust

Not a textbook. What `h check` runs, what the ratchet counts, what is banned and what
replaces it. Every rule names what checks it; a rule checked by nothing is only advice.

## How Rust passes `h check`

`h check` runs only the crates the change touched (`-p <crate>` each); a change to the
root `Cargo.toml`, `Cargo.lock`, `rust-toolchain*`, `.cargo/config.toml`, `clippy.toml`,
`rustfmt.toml`, `.config/nextest.toml` or `deny.toml` runs the whole workspace. Do not run `cargo test --workspace` or `cargo clippy --workspace` yourself —
`h check` is the gate, and it is the heartbeat that keeps the task claimed.

| Step | Command | Notes |
| --- | --- | --- |
| fmt | `cargo fmt --check` | `h ship` formats before it commits; per file: `rustfmt --edition <crate's>` |
| lint + typecheck | `cargo clippy -p … --all-targets -- -D warnings` | every warning fails; tests and examples are linted too |
| test | `cargo nextest run -p …` (`cargo test` without nextest) | nextest does not run doctests |
| tether | `h tether` | every `tests/*.rs` must be run by some step |
| ratchet | `h ratchet` | counts `#[allow]`, `#[expect]`, `#[ignore]` — see below |

A step in `[check] heavy` runs after merge or with `h check --tier full`.

## Rules

### 1. Errors — `thiserror` in libraries, `anyhow` in binaries

- A library crate returns its own `thiserror` enum; callers match variants
  (`PaymentError::InsufficientFunds`), never parse a message.
- `main` and binary-only code use `anyhow::Result` with `.context("what was being done")`.
- No `Box<dyn Error>` in a public API; no `String` as an error type.
- Checked by: reviewer (`references/review.md`). Clippy cannot tell a library from a binary.

### 2. No `unwrap` / `expect` outside tests

Use `?`, `ok_or_else`, `unwrap_or_default`, or a typed error. A real invariant the type
system cannot express → return an error anyway; a panic in a server is an outage.
- Checked by: clippy `unwrap_used`, `expect_used` = `deny`, tests exempt via `clippy.toml`
  (`allow-unwrap-in-tests`, `allow-expect-in-tests`). `h doctor` prints the exact block
  when the project lacks it. Without it — reviewer only.

### 3. Fix, do not silence

`#[allow(..)]`, `#[expect(..)]` and `#[ignore]` are counted by `h ratchet`; the count may
only go down. A new one fails `h check`; raising the base needs the user.
- A lint fires → change the code. Clippy is right far more often than it is wrong.
- A test fails → fix the code or the test's wrong assumption; never `#[ignore]` it.
- Truly unavoidable (generated code, a false positive) → `#[expect(lint, reason = "…")]`
  on the smallest item, and say why in the commit; the reviewer blocks an unexplained one.
- Checked by: ratchet step of `h check`; reviewer rule on raised counts.

### 4. `unsafe` only with `// SAFETY:`

Every `unsafe` block has a `// SAFETY:` comment directly above it: which invariant holds
and why. No `unsafe` to dodge the borrow checker or for speed without a benchmark.
- Checked by: clippy `undocumented_unsafe_blocks` = `deny` (same `h doctor` block).

### 5. Tests

- Unit tests next to the code (`#[cfg(test)] mod tests`); integration tests in the crate's
  `tests/` — they see only the public API, which is the point.
- Filesystem: a real temporary directory (`tempfile::TempDir`), not a mocked `fs`. Network
  and paid services: a local fake (`wiremock`), behind a trait (rule 9).
- A new behaviour gets a test that fails without it; an invariant gets a positive and a
  negative case.
- Checked by: test step of `h check`; `h tether` for `tests/`; reviewer for missing tests.

### 6. Builds

Each worktree has its own `target/` (a shared one would queue every session on cargo's
build lock); `h check` sets `RUSTC_WRAPPER=sccache` when sccache is installed, so registry
crates compile once per machine. Do not set `CARGO_TARGET_DIR` and do not delete
`target/` to "fix" a build. `h doctor` suggests `debug = "line-tables-only"`, a fast
linker and `CARGO_INCREMENTAL=0` in CI — apply them to the project, they are its files.
- Checked by: `h doctor` tips.

## Judgment — what clippy cannot check

7. **Illegal states unrepresentable.** A status is an `enum` with data in its variants,
   not `is_active: bool` + `revoked_at: Option<_>`.
8. **Parse, don't validate — at the boundary.** HTTP input, CLI args, a provider's reply
   become typed domain values once, on entry; raw maps and `serde_json::Value` do not reach
   the domain. Provider fields do not leak into domain types.
9. **Newtypes for values that can be swapped.** `UserId`, `Amount`, `OrderId` — the
   compiler catches swapped arguments. Money is never a bare `i64`.
10. **Dependencies behind traits.** Clock, database, HTTP client, providers: the service
    takes `impl Trait` / `Arc<dyn Trait>`. Accept generics, return concrete types.
11. **Ownership.** Borrow by default; `&str` over `String` in parameters; shared state is
    an `Arc` of handles. A `.clone()` that only silences the borrow checker is a smell.
12. **Async hygiene.** No `std::thread::sleep`, blocking `std::fs` or blocking clients in
    `async fn`; CPU-bound work → `spawn_blocking`. No lock guard held across `.await`
    (clippy `await_holding_lock` is on by default). Every outbound call has a timeout.
13. **Modules by domain, not by kind.** `payments/`, `orders/`, not `models/`, `utils/`.
    `pub(crate)` by default; `pub` is a contract.
14. **Exhaustive matches on business enums.** No `_ =>` that hides a new variant; no
    `let _ = fallible()` that drops a `Result`.

Checked by: reviewer, against `references/review.md`. Decided a crate, a pattern or
dropped one with a trade-off → `h progress note --adr "…"` and an ADR.

## References

- `references/review.md` — the review checklist and red flags; the reviewer applies it.
- `references/domain-example.md` — what a project adds on top (web API, money in
  transactions, upstream resilience): an example, not rules.

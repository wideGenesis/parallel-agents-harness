# Rust review checklist

For the reviewer of a change that touches `.rs` or `Cargo.toml`, and for the author before
`h review prepare`. Skip what `h check` already enforces (fmt, clippy, ratchet counts) —
these are the questions a linter cannot answer. Rule numbers point to `../SKILL.md`.

State the architectural impact of the change explicitly: **high / medium / low**.

## Checklist

- [ ] Errors: library → `thiserror` enum, binary → `anyhow` with `.context`; no
      `Box<dyn Error>` in a public API; internal error text never reaches a user (1).
- [ ] No `unwrap` / `expect` outside tests, unless the project's clippy lints already
      deny them (2).
- [ ] A new `#[allow]`, `#[expect]` or `#[ignore]` has a reason in the code and the
      commit (3).
- [ ] Every `unsafe` block has a `// SAFETY:` comment that states a real invariant (4).
- [ ] New behaviour has a test that fails without it; the filesystem is a temp dir, not a
      mock; invariants have a positive and a negative case (5).
- [ ] States are enums with data, not flags plus options (7).
- [ ] Input is parsed into typed values at the boundary, not passed on raw (8).
- [ ] Values that can be swapped are newtypes; money is not a bare integer (9).
- [ ] External dependencies sit behind traits and are faked in tests (10).
- [ ] No blocking call in `async fn`; no guard across `.await`; outbound calls have a
      timeout (12).
- [ ] New modules follow the domain; `pub` only where it is a contract (13).
- [ ] Matches on business enums are exhaustive; no `Result` dropped with `let _ =` (14).
- [ ] Logs carry no secrets or personal data.
- [ ] Dead code removed, not left behind `#[allow(dead_code)]`.

## Red flags — usually a blocker

- `.clone()` added only to make the borrow checker quiet, with no reason given.
- `String` parameter where `&str` would do; a bare `i64` for money or an id.
- `_ =>` in a match on a business enum.
- `let _ = validate(x);` — a `Result` silently dropped.
- `serde_json::Value` or `HashMap<String, String>` in domain code.
- An outbound call with no timeout.
- A test made weaker (assertion removed, expected value changed to the actual one) to get
  green.

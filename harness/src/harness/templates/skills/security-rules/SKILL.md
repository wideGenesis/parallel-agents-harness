---
name: security-rules
description: Use when changing a GitHub Actions workflow or action, handling secrets, tokens or .env, building a shell command, SQL or a file path from input, deserialising data from outside, adding a dependency, or reviewing such a change. What h check catches, what only the reviewer catches, the safe replacement for each banned pattern.
---

# Security review

Not a textbook. Where secrets may live, how workflows stay safe, which patterns are banned
and what replaces them. Every rule names what checks it; a rule checked by nothing is only
advice — the reviewer then holds the line with the checklist at the end.

## What `h check` catches, what it does not

| Check | Catches |
| --- | --- |
| `h ratchet` | a silenced security lint: `# noqa: S602`, `#[allow(..)]`, `eslint-disable` — the count may only go down |
| deadcode step | a dependency the code does not use — red |
| lint step | ruff `S` (bandit), clippy, eslint security rules — only those the project enables |
| `h init` | its own `harness.yml` already follows rules 3–8; a hand edit shows as `MODIFIED` |
| reviewer | everything else, against [For the reviewer](#for-the-reviewer) |

`h` has no secret scanner and no workflow linter: GitHub push protection, gitleaks or
zizmor in the project are a backstop, not a replacement for these rules.

## Secrets

### 1. Never in code, logs, artifacts or PRs

A token, password, key or connection string with a password lives in the environment or
the CI secret store — not in source, tests, fixtures, `harness.toml`, commit messages,
`.artifacts/`, `h progress note`, PR bodies or comments. Tests use values that are
obviously fake (`test-token`). An error or log line that dumps a request, a config or the
environment redacts credentials first. A secret that was pushed is leaked: rotate it,
deleting the commit does not help.
- Checked by: reviewer. A regex cannot tell a key from a hash; no scanner in `h`.

### 2. `.env` reaches a worktree only by explicit list

`h task start` copies exactly the files in `[worktrees] include` (`include = [".env"]`) —
paths, not globs. Do not copy `.env*` by hand or by a setup command, do not commit `.env`
(it stays in `.gitignore`), and keep `.env.harness` to what `h` writes there (task id,
ports). A new secret the project needs → `.env.example` gets the name with a fake value.
- Checked by: `h task start` (copies listed paths only); reviewer for `.gitignore` and
  `.env.example`.

## GitHub Actions

The same audits zizmor runs. Examples: `references/patterns.md#github-actions`.
Rules 3–8 are checked by: the harness's own test suite for `harness.yml` that `h init`
installs; in the project's own workflows — reviewer (no workflow linter in `h`).

### 3. No `pull_request_target` or `workflow_run` running PR code

Both run with the base repository's secrets and a write token. A job that checks out the
PR's head under them hands the token to anyone who opens a PR. Use `pull_request`; if a
write is truly needed, the job never checks out or runs the PR's code.

### 4. Minimal `permissions`

Every workflow sets `permissions:` at the top (`contents: read`); a job that needs more
widens it for that job only. No `write-all`, no workflow without the key (the default
token may be read-write).

### 5. Actions pinned by full SHA

`uses: owner/action@<40-hex sha> # vX.Y.Z` — a tag can be moved to malicious code. Local
`./` and this repository's `$/` are fine. Dependabot keeps the SHAs current.

### 6. `persist-credentials: false`

`actions/checkout` leaves the token in `.git/config` for every later step unless told not
to. Only a job that pushes keeps it, and says why.

### 7. No `${{ }}` with outside data inside `run:`

`${{ github.event.* }}`, `github.head_ref`, `inputs.*` and step outputs are pasted into the
script before the shell sees it: a PR title `"; curl … | sh #` runs. Pass them through
`env:` and use `"$VAR"`. The same holds for `script:` of `actions/github-script`.

### 8. Caches are saved by the default branch only

A PR restores caches and never saves them, or a branch can poison what `main` builds with.

## Injection and untrusted input

Untrusted = anything from a request, a CLI argument, a file or archive from outside, an
issue, PR or commit text, an LLM's reply. Examples: `references/patterns.md`.

### 9. Shell: an argument list, never a string

Python `subprocess.run([...])` without `shell=True`, no `os.system`/`os.popen`; Rust
`Command::new(p).arg(x)`; Node `execFile`/`spawn` without `shell: true`. No `sh -c` with
input pasted in. An argument that may start with `-` goes after `--`.
- Checked by: ruff `S602`/`S605`/`S607` if the project enables `S` (a `noqa` is counted
  by ratchet); otherwise reviewer.

### 10. SQL: bind parameters

Values go as parameters (`?`, `$1`, `%s` with a tuple, `sqlx::query!`), never through an
f-string, `format!`, `+` or a template literal. An identifier (column, sort order) comes
from an allowlist in code, not from input.
- Checked by: ruff `S608` if enabled; otherwise reviewer.

### 11. Paths stay inside their root

A path built from input is resolved and checked to be under its root
(`resolved.relative_to(root.resolve())`), which also rejects `..`, absolute paths and
symlinks that point out. An archive: every member checked before writing (zip slip);
Python `tarfile.extractall(filter="data")`.
- Checked by: ruff `S202` for `tarfile` if enabled; otherwise reviewer.

### 12. No unsafe deserialisation or eval

Not on untrusted data: `pickle`, `marshal`, `shelve`, `yaml.load` (use `safe_load`),
`torch.load` without `weights_only=True`, `eval`/`exec`, Node `eval`/`new Function`/`vm`.
Parse into a typed structure (serde, pydantic, a schema) and reject the rest.
- Checked by: ruff `S301`/`S307`/`S506` if enabled; otherwise reviewer.

### 13. A new dependency is a decision

Before adding: needed (not three lines of code), maintained, the exact name (typosquats),
installed from the registry (not a git URL), with a lockfile entry. Dependabot's cooldown
lets fresh releases wait. A dependency that turned out unneeded is removed.
- Checked by: deadcode step (an unused one is red); reviewer for the rest.

Decided to keep a risk with a trade-off → `h progress note --adr "…"`.

## For the reviewer

Every item is a blocker when it fails. Skip what `h check` already failed on.

- [ ] No secret, token or real credential in the diff, tests, logs, PR text (1).
- [ ] `.env` stays out of git; a new secret is named in `.env.example` with a fake value (2).
- [ ] No `pull_request_target` / `workflow_run` running PR code (3).
- [ ] Every workflow has top-level `permissions:`; widening is per job and explained (4).
- [ ] Every `uses:` is a full SHA or local (5); checkout has `persist-credentials: false`
      unless the job pushes (6).
- [ ] No `${{ }}` with event, input or output data inside `run:` or `script:` (7).
- [ ] A PR cannot save a cache (8).
- [ ] Shell calls take an argument list; no `shell=True`, `os.system`, `sh -c` with input (9).
- [ ] SQL values are bound parameters; identifiers come from an allowlist (10).
- [ ] Paths from input are resolved and checked against their root; archives are filtered (11).
- [ ] No `pickle`/`yaml.load`/`eval` or equivalents on data from outside (12).
- [ ] A new dependency is needed, correctly named, from the registry, locked (13).
- [ ] A security lint silenced with `noqa` / `allow` / `eslint-disable` has a reason (ratchet).
- [ ] New authz-relevant code (endpoint, file write, command) checks who is calling.

## References

- `references/patterns.md` — banned pattern → replacement, by stack and for workflows.

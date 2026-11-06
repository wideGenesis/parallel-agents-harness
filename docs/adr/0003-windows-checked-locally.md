# ADR-0003 — Windows is checked locally, cloud CI is Ubuntu only

Status: accepted · 2026-10-24 · Basis: the gate decision of 2026-10-23 (`docs/plan.md`, "Audit — 2026-10-23")

## Context

`h` runs only on the user's Windows machine (24 cores, 128 GB; in the future, at most
one more Windows machine). "Portable" means that the harness is installed into every new repository
of a project, not that it moves to other machines. The projects themselves run on Ubuntu servers.

The harness CI had an Ubuntu + `windows-latest` matrix. The Ubuntu part took 56 s, while Windows took 8–10+ minutes with a limit of 10: `main` went red on a timeout, and the rerun took
8 min 51 s. Every PR waited for Windows, although it repeated work already done: `affected` works by
package, `harness` is one package, so any edit in `harness/` runs the whole
test suite in `h check` on this same Windows machine, and `h ship` does not open a PR without a green `h check`.

## Decision

1. `windows-latest` is removed from the harness CI. CI is one job on Ubuntu, and it is `required`.
2. Windows is checked by the local `h check` that `h ship` runs. The test
   `test_harness_change_runs_the_whole_suite_locally` (`harness/tests/test_rules.py`) holds the
   invariant: an edit in `harness/` selects the whole package, not a part of it.
3. The server-side required check stays (decision of 2026-10-23), but it covers only Ubuntu.
   For Windows the local gate applies: it protects against accidents, not against bypassing, like
   the other gates in `h`.

## Consequences

- CI on a PR takes about a minute instead of 8–10.
- The server will not catch a Windows breakage pushed past `h ship`: it will surface on the user's machine.
- A local run under the load of parallel sessions takes minutes; speeding up the fixtures is a separate task.
- This is a rule of the harness itself. For projects, CI runs on Ubuntu, and heavy jobs (Rust in the pilot project)
  can run on self-hosted runners.

## When to revisit

- `h` starts running on another OS or in cloud agent sessions.
- `h` runs inside the CI of projects (the `harness.yml` template) on more than Ubuntu.
- `affected` starts selecting part of the tests inside a package (testmon, etc.); then the test from item 2
  fails, and the decision must be revisited.

# Harness — rules for agents

A portable harness for agents working in parallel (Rust / Python / TS). Plan:
`docs/plan.md`, decisions: `docs/adr/`, research: `docs/research/SUMMARY.md`.

## Map

| Path | What |
| --- | --- |
| `harness/` | Python package, CLI `h` (stdlib only, ADR-0001) |
| `polygon/` | reference Rust+Py+TS project; every stage is tested on it |
| `harness/src/harness/templates/` | what `h init` installs into a project (inside the package, so it ships with `uv tool install`) |
| `.github/workflows/check.yml` | reusable CI for projects: `h init` installs `harness.yml`, which calls it by SHA; `h` comes from `.github/actions/setup-h` of the same commit; checked by `template-check.yml` on `polygon/` |
| `.agents/skills/` | skill sources; `.claude/skills/` is a copy made by `h skills sync`, do not edit it by hand |
| `.claude/agents/` | subagents: `locator` (paths only), `analyzer` (facts with file:line), `reviewer` (review verdict), `ci-triage` (cause of a CI failure) |
| `docs/` | plan, ADR, research |
| `competitor_repos/` | clones for research — untrusted, do not run, not in git |

## Commands

```text
h check [--tier full]                   # gate before PR: fmt/lint/deadcode/deps/typecheck/test/sql of what is affected (deps — cargo deny if deny.toml exists; sql — for an sqlx project on a throwaway database: base migrations, then the branch's, comparison with .sqlx/); [check] heavy — after merge, --tier full or the full-ci label
# deadcode: an unused dependency is red, dead code is a report ([check.deadcode]); a false positive goes to .harness/deadcode/<stack>.txt with the reason after #, then h ratchet --update --allow-growth
# [check] step-timeout-minutes = 30 — a longer step is killed with its child processes and fails under its own name; the step log is written as it runs (a cancelled job keeps it); every 5 minutes h check writes to stderr which steps are running and what they wait for
# ts: no node_modules or no pinned knip — one pnpm install --frozen-lockfile before the steps (log ts-install.log); if it fails — red ts install, the other steps run
# rust test: cargo nextest (all test binaries in one pool), then cargo test --doc for packages with a lib — in the same step and time limit; without nextest — cargo test one by one and a note in the report, a tip in h doctor; CI installs nextest itself; the summary splits rust test into build, tests and the rest (binary listing, doctests): a timeline row in CI, its own row locally, build_s/tests_s/rest_s in --json; no cargo/nextest lines — no split
# CI tool versions are exact, in check.yml (install-action: cargo-machete, cargo-deny, cargo-nextest), bumped by h releases; sqlx-cli — the version of the sqlx crate from the project's Cargo.lock (h config --json: stacks.rust.sqlx), only for an sqlx project
# h init in a project with prettier: h's files (harness.yml, task.yml, skills, .claude/agents/) go into .prettierignore, only the missing lines; .claude/settings.json is formatted, not ignored
h ratchet [--update]                    # suppressions and test skips only decrease; baseline — .harness/ratchet.json
h tether                                # every test file is run by some h check step; exceptions — [check] untethered on base
h affected [--json] [--base REF]        # what is affected; workflows/actions/harness.toml — everything; docs_only ([check] docs on base, default docs/** and *.md in the root) — CI skips the checks; on push to main — by the diff from the previous tip, otherwise a full run; h task done does not count a skipped run for an earlier merge
h ci reused --before SHA [--json]       # CI on push to main: a green run of the merged PR already checked the merge tree (artifact h-checked-<tree>, its own branch, the same workflow) and the previous main verdict is green → the check is not repeated, only [check] heavy runs (--tier heavy); any doubt — a full run; harness.yml gives job check the permissions actions/pull-requests: read
h task start <ID> [--slug s] [--resume] # claim on origin + worktree + .env/ports; --resume — continue your own; status | heartbeat | release | clean
h review prepare                        # package for the reviewer subagent (review-gate skill); ship refuses without its approve; model — [review] models in rotation, but not the author's model from Co-Authored-By; [review] docs (ADR, AGENTS.md, CLAUDE.md, .claude/rules, skills) — documents checklist: provenance of claims (by the project's scheme from [review] provenance, otherwise the built-in one: no mark = [T4], [T1]/[T2] need a locator), h downloads sources [T2: url] and [T2: host.tld/path] into the package, contradictions, ADR completeness
h review reset gh-N                     # user only: lift the limit of 3 review rejections
h ship                                  # from the task worktree: fmt commit → h check (a green full review prepare run on the same HEAD with no changes is reused) → PR title before push: one taken from the first commit follows it, a chosen one stays, --title changes it → push → PR with evidence
h deps review <PR>                      # Dependabot PR: harness/review for patch/minor ≥1.0; otherwise h deps approve — user only
h ci watch                              # waits for the PR's CI; red → log for the ci-triage subagent (GitHub lost the job log — from artifact h-check-logs-<attempt>; none either — red "log unavailable"); 2 rounds → stop (h ci reset — user only); gh failure — code 7
h revert <sha> [--run URL]              # main red after merge: a PR with the revert, the user merges it
h epic new <slug> --title T [--frame f]  # epic: milestone + docs/epics/<slug>.md (via an auto-PR)
h epic sync | status                    # the epic's task list from issues; summary across epics
h task new --title T --epic e --body f   # card = GitHub issue (task-card skill)
h task next                             # what the agent may take on its own: Ready + agent-ok, without a claim (task skill — the whole cycle)
h task ready gh-N                       # Definition of Ready; start refuses without READY
h task confirm gh-N                     # user only, in their own terminal: confirm the criteria
# [tasks] branch = "task/{id}-{slug}", card_path = "docs/backlog/*/cards/{id}.md" — branch template and card files (external IDs)
# [adr] dir = "docs/adr" — the ADR index and the full text of ADRs the card mentions, in brief.md and the review package; "" — turn off
# [branches] auto = "nocard/{name}" — branches h creates itself (epics, reverts), in the project's format; h init --github --labels-only — only labels on GitHub
# [ci] runs-on = ["self-hosted", "linux"], postgres-image = "postgres:18", setup = "scripts/ci-setup.sh", timeout-minutes = 45, node-version = "26", upload-logs = false — runner of all jobs, database for tests, setup script before h check, job check limit (default 30), Node (default 24, not lts/*) and upload of step logs as an artifact on failure or cancel (default yes; output is not masked) in harness.yml from h init; in CI, workflow variables take precedence over [check.env]
# [check] exclusive = ["ts.test"] — the step runs last and alone on the machine (e2e with timeouts); the other heavy steps of all sessions wait
# [[check.generated]] path = "web/src/api.ts", command = "pnpm gen:api", inputs = ["crates/api/**"] — a committed file from another stack: when inputs or the file change, h check regenerates and compares (step generated), does not change the tree; inputs affect the file's package
# [stacks.ts] evidence = ["apps/web/test-results"] — what a failed test step left (traces, screenshots), h copies to .artifacts/check/<run>/<stack>-test/ up to 200 MB; default for ts — the packages' test-results/
# [ci] postgres-image set → h check starts its own throwaway Postgres on a free port for a run with test/sql (DATABASE_URL points there), removes it with its volume at the end; the next run removes the orphan of a killed run (only of its own namespace — lock folder and OS are in the name h-check-pg-<ns>-<pid>-… — when the lock is free and the process is dead; h doctor shows others' orphans older than a day); no Docker — [check.env]; [check] postgres = false — turn off; harness tests use the prefix h-pytest-pg- and do not see others' containers
# test database (DATABASE_URL of test/sql steps — throwaway, CI or [check.env]) is checked every 10 s with a Postgres handshake from the host; silent 3 times in a row (throwaway — from the first second, external — after the first answer) — test/sql steps stop with one line "… stopped answering at +Ns", the other steps run; logs and state of the throwaway container go to .artifacts/check/<run>/postgres.log before removal (and on any failed test/sql); CI prints its database logs on a failed job
# ~/.harness/machine.toml: max_cores (default min(20, threads−2)), max_load (0.8) — machine budget for heavy steps of all sessions: (max_cores−1)//share slots, share = cores per step (CARGO_BUILD_JOBS, RUST_TEST_THREADS, NEXTEST_TEST_THREADS, PYTEST_XDIST_AUTO_NUM_WORKERS, H_CORES for vitest/Playwright); above max_load a step waits (up to 10 min)
# h check --plan [--json] — which steps would run, without running them (CI uses it to decide whether to start the database)
h task done gh-N                        # after merge: waits for deploy → cleanup → check → close the issue, [x] in the epic
h task drop gh-N --reason R [--discard]  # cancel at the user's word: issue not planned, PR closed, cleanup; the branch on origin stays; --discard, start --steal, release --force — user only (deny)
h progress [done T<n> | note TEXT | show]  # task journal: survives compaction; done — only with a green h check on HEAD
h board init                            # the repo's Projects v2 board; h commands move the cards themselves
cd harness && uv run pytest -q          # CLI tests
cd harness && uv run h -C ../polygon config
cd harness && uv run h -C ../polygon doctor
```

## Rules

- Documentation, code, commits and identifiers are in English.
- Logic goes in `h` (Python), not in shell: Git Bash rewrites path arguments (MSYS path
  conversion), and this has already broken sparse-checkout.
- Output of `h` commands is for the agent: summary + errors, full log in `.artifacts/`, `--json` always.
- New behaviour of `h` comes with a test on `polygon/` or in a temporary directory.
- If you change the behaviour of `h` or the format of `harness.toml`, bump the version in the same PR
  (`pyproject.toml` + `__init__.py`) and, if a project needs the new logic, `[harness] version`
  in `harness.toml`.
- A new rule starts as a test or a lint (`harness/tests/test_rules.py`); prose gets only what
  code cannot check.

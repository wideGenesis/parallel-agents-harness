---
name: ci-triage
description: Use after `h ship`, or when a PR's CI is red. Waits for CI, has the ci-triage subagent explain a failure without the log entering the main session, and stops after two real failures.
---

# CI triage

From the task worktree: `h ci watch`. It waits for the PR's checks of HEAD.

- **green** → it prints `merge: agent …` or `merge: user …`; follow the `task` skill.
- **red** → it already re-ran the failed jobs once (flakes do not count) and wrote a
  compressed log. Spawn the `ci-triage` subagent with the worktree path and the log path
  it printed. Fix what it reports, commit, review again (`review-gate`), `h ship`, then
  `h ci watch` again. Do not open the raw CI log yourself. When GitHub lost the job log,
  the round log is built from the step logs the run uploaded (`h-check-logs-<attempt>`);
  `log unavailable` for every failed run means there is nothing to triage: follow its
  `next:` (rerun the failed jobs), and if it repeats, show the user the run.
- **pending** (exit 3) → CI still running after the wait; run `h ci watch` again.
- **conflict** (exit 4) → the PR conflicts with the base and GitHub starts no CI for it: rebase onto the base, resolve, then `h review prepare` → `h ship`.
- **no-pr** (exit 5) → the branch has no open PR (the last one was merged or closed), so
  no CI runs: `h ship` opens a new PR, then `h ci watch`.
- **unpushed** (exit 6) → HEAD is not the branch tip on origin, so no CI runs for it:
  follow its `next:` (`h ship` for local commits; rebase first if origin moved ahead).
- **gh error** (exit 7) → gh failed, the CI state is unknown — not green: run `h ci watch`
  again; if it repeats, show the user the error.
- **blocked** (exit 2) → the first line says why; no round was counted unless it says so.
  - two real failures → STOP: show the user the two round logs' causes and ask how to
    proceed. Only the user runs `h ci reset`; `h ship` refuses until then.
  - cancelled with no newer run → run the printed `gh run rerun`, then `h ci watch`.
  - did not test the code (awaiting approval, startup failure, stale) → follow its `next:`;
    an approval is the user's.
  - checks from outside Actions red → STOP: show the user the checks it lists.

A flake that the subagent identifies as such still counted as a round once its rerun
failed too — say so to the user instead of pushing an empty commit to retry.

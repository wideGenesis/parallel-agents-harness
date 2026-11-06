---
name: review-gate
description: Use when a task's work is committed and ready to ship, or when `h ship` refuses with "no review" or "review requested changes". Runs the review gate — package, reviewer subagent, fixes — without reading the diff into the main session.
---

# Code review gate

`h ship` refuses without an approving verdict from the `reviewer` subagent for the current
HEAD. You cannot write the verdict yourself — the tool refuses.

1. Commit everything, then from the task worktree: `h review prepare`.
   It runs `h check`, writes `.artifacts/review/package.md` and prints the round and
   its model (`[review] models`, default sonnet, sonnet, opus).
2. Spawn the `reviewer` subagent with the printed model as `model` and one line: the
   worktree path. Do not paste the diff or your opinion of it — a fresh context is the
   point. The gate refuses a verdict written by another model.
3. Read its ≤ 15-line reply, then run `h review status`: it checks the verdict and keeps
   an approve on origin, so a lost worktree does not turn it into a rejection.
   - **approve** → `h ship`. Notes go into the PR; address them only if cheap.
   - **changes-requested** → fix each blocker, commit, back to step 1. Any new commit
     needs a new review (the verdict is bound to HEAD). The next round shows the
     reviewer only your fix and the previous findings — unless you rewrote the branch
     (rebase, amend), then it gets the whole diff again.
4. After 3 rejected rounds `h review prepare` refuses: stop, show the user the findings
   and ask how to proceed. Do not argue with the reviewer in a loop. Only the user lifts
   the limit (`h review reset gh-N` in their terminal). Rounds after an approve (the
   branch changed since) spend no attempt.

Rebasing onto a newer base:
- `h review prepare` refuses while the branch conflicts with the base — rebase first, so
  no round is spent on a version that cannot merge.
- Behind the base with no conflicts: `h ship` rebases by itself and the approval carries
  over — no need to rebase first.
- A clean rebase of an approved commit keeps the approval: `h ship` directly (prepare says
  there is nothing to review).
- A rebase with conflicts you resolved by hand gets a *delta review*: prepare packages only
  what changed since the approved version. It does not count toward the 3 rounds, but has
  its own limit (`[review] delta_rounds`, default 2); past it, ask the user.
- Files matching `[review] paths` add a skill's checklist to the package; the gate
  refuses a verdict that does not name it — run the reviewer again, not a new round.
  `prepare` refusing over a skill that is not installed → tell the user.

If you disagree with a blocker, say so to the user with the reason — never work around
the gate.

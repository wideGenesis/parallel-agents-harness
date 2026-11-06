---
name: bug-fix
description: Use when fixing a bug, a failing test, a crash, or unexpected behaviour. Reproduce with a failing test first, then one hypothesis at a time, then the minimal fix.
---

# Bug fix

Bug = a failing test first. A fix without a reproducing test is a guess that can come back.

## 1. Reproduce

1. Find the owning package: `h config` lists packages; the fix and the test live in the same one.
2. Write the smallest test that fails for the reported reason — assert the *correct* behaviour.
3. Run only that package's tests and see it fail with the expected message.
   A test failing for a different reason does not reproduce the bug — fix the test first.
4. Cannot be automated (infra, external service, UI only)? Write exact manual repro steps
   into the task and say so in the report. Do not skip to the fix.

## 2. Find the cause — one hypothesis at a time

- State the hypothesis in one line, then the cheapest observation that would refute it
  (a log line, a debugger value, a narrower test).
- Read the code path end to end before changing it; for unfamiliar code use the `research`
  skill instead of reading many files yourself.
- After 3 refuted hypotheses: stop, write what was ruled out, and ask the user.

## 3. Fix

- Smallest change that makes the test pass. No refactoring in the same change.
- Run the package's tests, then the packages that depend on it.
- Look for the same mistake elsewhere: `grep` the pattern; fix or list the other sites.

## Forbidden

- Editing or deleting the test to make it pass; `skip`/`ignore` on the failing test.
- Catching and swallowing the error, adding retries or sleeps to hide a race.
- "Fixed" without the test having failed before the fix.

## Report (≤ 6 lines)

```text
Cause:    <one line, file:line>
Test:     <test name> — failed before, passes after
Fix:      <what changed>
Also hit: <other sites with the same pattern, or "none">
Why missed: <why existing tests did not catch it>
```

# ADR-0002 — Task claim: lease on origin, `refs/claims/<ID>`

Status: accepted · 2026-10-21 · Basis: experience of the pilot project, `docs/research/SUMMARY.md` #21–23

## Context

In the pilot project, agents took other agents' tasks and stopped: the reservation held the number, not the execution,
`--attach` let any session in, and the "in progress" status lived in the branch. Checking "is there a worktree"
does not work: a worktree is visible only to its own clone, it survives a crash and a merge, and a cloud session
does not see it (9 of 14 worktrees of the pilot project belonged to archived cards). None of
the competitors we studied has a cross-process claim.

## Decision

1. A claim is a ref `refs/claims/<ID>` on origin that points to a JSON blob:
   `task, owner (host|worktree path), branch, by (git email), started, heartbeat`.
2. Atomicity comes from git (verified on a bare repo and on GitHub 2026-10-21):
   creation is a push without force (the second push gets a reject); heartbeat/steal/release use
   `--force-with-lease=<ref>:<sha>` (compare-and-swap against the blob that was read).
3. The claim is taken **before** anything is created locally: the loser of a race leaves nothing behind.
4. Owner = host + worktree path. This key cannot tell apart two sessions of one clone,
   so a repeated `h task start` in the same place refuses; continuing your own task
   (after a session crash) is possible only with an explicit `--resume`.
5. Expiry: when the heartbeat is older than `tasks.claim_ttl_hours` (default 4), the claim can
   be taken over only with an explicit `--steal`; the task branch is kept, and the previous owner's work
   is pulled from origin.
6. Release: `h task release` (giving up) or `h task clean` after merge (including squash —
   detected with `git cherry` against a trial squash commit).
7. Refs outside `refs/heads|tags` do not trigger GitHub Actions (11 runs before and after e2e).

## Consequences

- `+` Visible to all clones, machines and cloud sessions; a race has exactly one winner.
- `+` A session crash does not block a task forever: TTL + `--steal`.
- `−` A network call at start and on heartbeat (~1 s).
- `−` A crash holds the task until the TTL expires; machine clocks must be roughly in sync (a TTL in hours
  makes a drift of minutes irrelevant).
- `−` Heartbeat was manual at first (`h task heartbeat`); since Stage 7 the `h` commands extend it themselves.

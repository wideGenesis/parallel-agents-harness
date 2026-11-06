---
name: grilling
description: Use when the user asks to grill, interrogate or stress-test a plan, idea or task, or when work is too vague to write a card or plan for. Interviews the user in rounds over a tree of decisions until nothing is silently assumed; the user decides, you look up the facts.
---

# Grilling

Interview the user until you both understand the task the same way. Model it as a
**design tree**: each decision has decisions hanging off it ("sync or async?" opens
"what if the queue is down?"). Nothing gets built during grilling.

## Rounds

The **frontier** is every open decision whose prerequisites are already settled — what
you can ask now without guessing answers you have not heard. Ask the whole frontier as
one round, then wait. Two questions that depend on each other never share a round; the
dependent one waits for a later round.

Each answer reshapes the tree: settled decisions unlock new ones, some branches die.
Recompute the frontier from scratch before every round. Rounds stay few; questions can
be many.

Round format, in the user's language:

```text
❓ **Q1** — **<short title>**: <question; options if there are several, and what each
means for the product's user>

➡️ <your recommended answer and why, one or two lines>

---

❓ **Q2** — …
```

- Word every question so that "yes" accepts your recommendation.
- The user answers by number ("1 yes, 2 the second, 3 no — because…").
- If the user says an answer changes an earlier one, reopen that branch next round.
- If the user asked for one question at a time, ask one per message.

## Facts are yours, decisions are theirs

Never ask the user what the code, the docs, the issues or `git log` can tell you. A
frontier question that needs a fact goes to a `locator` / `analyzer` subagent (skill
`research`). Do not block the round on it: only the questions downstream of that fact
wait; ask the rest now.

Never answer a decision yourself, even when you are sure — put it to the user with your
recommendation. An interview nobody answered is your opinion, not their requirements.

## What to ask about

Skip what is already settled or trivially follows. Priority goes to questions whose
answer changes the plan. Branches that usually hide assumptions:

- who the result is for and what changes for them; what "done" looks like;
- boundaries — what is explicitly out of scope;
- edge cases and failures — empty, huge, concurrent, repeated, half-done, offline;
- data and state — what is stored, migrated, what happens to the existing data;
- compatibility — who else calls this, what must not break;
- how we prove it works — the test or manual check that settles it.

## Keep the log

For anything longer than one round, keep `.artifacts/grill/<slug>.md` up to date after
each round:

```text
# <topic>
## Settled
- D1: <decision> — <why, in a few words>
## Open (frontier)
- <question>
## Deferred / out of scope
- <what and why>
## Assumptions the user accepted without discussing
- <assumption>
```

It survives context compaction and is the input for the next step.

## Ending

The session ends when the frontier is empty — or earlier if the user says "enough": then
move every open question to *Assumptions* or `[NEEDS CLARIFICATION: …]` and say so. If
the tree keeps growing past a handful of rounds, the scope is too big: propose splitting
it into an epic (`h epic new`) and grill the pieces.

Then show the settled decisions as a short list and ask the user to confirm this is the
shared understanding. Do not start implementing until they confirm. After that:

- a task → skill `task-card`: settled decisions become AC-B / AC-T, open ones become
  `[NEEDS CLARIFICATION: …]`;
- an architectural choice → an ADR in `docs/adr/`;
- otherwise → the log file is the result.

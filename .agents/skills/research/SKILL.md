---
name: research
description: Use before changing unfamiliar code or planning a task that touches more than a couple of files. Delegates searching and reading to the locator and analyzer subagents so the main session gets file:line facts, not file dumps.
---

# Research

The main session should not read twenty files to find three lines. Searching and reading go
to subagents; you get back paths and `file:line` facts.

Skip this skill when you already know the file — just open it.

## Steps

1. **Split the question** into 1–4 independent topics ("where are sessions created",
   "how is the rate limit applied").
2. **Locate** — one `locator` subagent per topic, all in parallel. It returns paths grouped
   by role and reads nothing.
3. **Analyze** — pick the ≤ 3 most relevant locations and send each to an `analyzer`
   subagent with the concrete question. It returns facts with `file:line`, no opinions.
4. **Wait for all** subagents before concluding. Live code beats docs and comments: if
   they disagree, the code is the truth and the doc is a finding.
5. **Write** the result to `.artifacts/research/<topic>.md` when it feeds a plan or another
   session; otherwise keep it as a short summary in your reply.

## Delegation prompt shape

```text
Question: <one concrete question>
Scope: <dirs or packages to stay inside>
Return: <paths only | facts with file:line>, ≤ 30 lines
```

## Result shape

```text
Topic: <name>
Facts:
- <fact> — path/to/file.rs:120
Open questions:
- <what research could not settle>
```

No recommendations inside research results — deciding is the next step, made by you.

---
name: task-card
description: Use when the user describes work to be done, asks to create a task, card, issue or epic, or when a task you are about to start is not ready. Turns a discussion into an agent-ready GitHub issue; business criteria always go to the user for confirmation.
---

# Task card

A card is a GitHub issue in a fixed format. You write it; the user confirms the business
criteria. Never invent criteria the user did not agree to.

## Create

If the work is still too vague to fill Problem and AC-B, run skill `grilling` first and
build the card from its log.

1. **Epic first if it is new**: `h epic new <slug> --title "…"`, then fill *Why*,
   *Scope*, *Done when* in `docs/epics/<slug>.md` from the discussion. Leave the
   generated task block alone.
2. **Write the card** to `.artifacts/cards/<slug>.md` with these `###` sections:
   Problem · Assumptions · AC-B (business, Given/When/Then) · AC-T (technical, ← AC-B) ·
   Verify · Size (S/M/L) · Trivial (no/yes) · Trivial reason.
   - AC-B in the user's domain language: `- [ ] B1: Given …, When …, Then …`.
   - AC-T each traces back: `- [ ] T1 (←B1): …`.
   - Verify = exact commands that prove it (`h check`, a test name, manual steps).
   - Anything not settled: write `[NEEDS CLARIFICATION: question]` instead of guessing.
3. **Create**: `h task new --title "…" --epic <slug> --body .artifacts/cards/<slug>.md`.
   It prints the readiness verdict.

## Get the business criteria confirmed

Ask the user 3–5 questions about the contested edge cases, each **with your proposed
answer** and what it means for the product's user — not a list to rubber-stamp. Update the
issue with the answers. Only the user adds the `ac-confirmed` label; you cannot (the
tool refuses), and you never ask them to let you.

`Trivial: yes` (refactor without behaviour change, test fix, cleanup, docs) skips AC —
the reason must say why there is no product surface. It does not skip the user: they
confirm the trivial claim with the same label.

The confirmation covers the text it was given for. Editing AC-B or the Trivial fields
afterwards makes the card not ready until the user confirms again (`h task confirm gh-N` in their terminal); ticking
checkboxes does not.

## Before starting

`h task ready gh-<N>` must say READY; `h task start gh-<N>` refuses otherwise and lists
what is missing. A partly filled card is fine to create — clarify it when you pick it up.

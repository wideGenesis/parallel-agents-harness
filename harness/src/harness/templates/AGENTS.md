# {name} — rules for agents

The single source of rules for all agents; `CLAUDE.md` imports this file.
It holds only what an agent cannot infer from the code. Keep the file short (≤ 150 lines):
it is a map; details go in the nested `AGENTS.md` files of packages and in `.agents/skills/`.

## Map

| Path | What |
| --- | --- |
| `...` | ... |

## Commands

```text
h config            # project stacks and packages
h doctor            # is everything installed
```

A hook formats a changed file automatically — do not run the formatter by hand.

Work on a task (gh-N) uses the `task` skill: the whole cycle from `h task next` to `h task done`,
with stops where the user decides.

## Do not

- Generated files (`[generated]` in `harness.toml`) are not edited — regenerate them.

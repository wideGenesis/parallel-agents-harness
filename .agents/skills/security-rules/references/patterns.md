# Banned pattern → replacement

Examples for the rules of `../SKILL.md`; rule numbers in brackets. Not exhaustive: the
rule is what counts, these are the shapes it takes most often.

## GitHub Actions

```yaml
# Banned
on: pull_request_target                      # [3] with a checkout of the PR head
permissions: write-all                       # [4]
steps:
  - uses: actions/checkout@v4                # [5] a tag; [6] token left in .git/config
  - run: echo "${{ github.event.pull_request.title }}"   # [7] the title runs as shell

# Replacement
on: pull_request
permissions:
  contents: read                             # widen per job, with a reason
jobs:
  build:
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - env:
          TITLE: ${{ github.event.pull_request.title }}
        run: echo "$TITLE"
```

Outside data in `${{ }}` is safe in `env:`, `with:` and `if:`, never inside `run:` or a
`script:` body. Caches [8]: `actions/cache/restore` in PRs, `actions/cache/save` only on
the default branch; `setup-uv` with `save-cache:` and `rust-cache` with `save-if:` set to
a push to the default branch.

## Python

| Banned | Replacement |
| --- | --- |
| `subprocess.run(f"git log {ref}", shell=True)` [9] | `subprocess.run(["git", "log", "--", ref])` |
| `os.system(cmd)`, `os.popen(cmd)` [9] | `subprocess.run([...], check=True)` |
| `cur.execute(f"... WHERE id = {uid}")` [10] | `cur.execute("... WHERE id = %s", (uid,))` |
| `f"ORDER BY {col}"` from input [10] | `col = ALLOWED[col_key]` — a dict in code |
| `open(root / name)` with `name` from input [11] | `p = (root / name).resolve(); p.relative_to(root.resolve())` |
| `tar.extractall(dest)` [11] | `tar.extractall(dest, filter="data")` |
| `pickle.loads(body)`, `yaml.load(text)` [12] | `json.loads`, `yaml.safe_load`, then a typed model |
| `eval(expr)` [12] | `ast.literal_eval` for literals; otherwise a parser |
| `logger.info("config %s", cfg)` with a token inside [1] | log the fields that are not secret |

ruff: `S` enables these checks (`S602`, `S605`, `S608`, `S301`, `S506`, `S307`, `S202`).
A project that turns `S` on gets them in the lint step; a `# noqa: S…` is counted by the
ratchet.

## Rust

| Banned | Replacement |
| --- | --- |
| `Command::new("sh").arg("-c").arg(format!("git log {r}"))` [9] | `Command::new("git").args(["log", "--", r])` |
| `format!("SELECT … WHERE id = {id}")` [10] | `sqlx::query!("SELECT … WHERE id = $1", id)` |
| `root.join(name)` with `name` from input [11] | canonicalize, then `starts_with(&root)`; reject `..` components |
| `serde_json::Value` passed into the domain [12] | deserialize into a typed struct, `deny_unknown_fields` where it matters |

## TypeScript / Node

| Banned | Replacement |
| --- | --- |
| ``exec(`git log ${ref}`)`` [9] | `execFile("git", ["log", "--", ref])` |
| ``db.query(`… WHERE id = ${id}`)`` [10] | `db.query("… WHERE id = $1", [id])` |
| `path.join(root, name)` with `name` from input [11] | `const p = path.resolve(root, name); if (!p.startsWith(root + path.sep)) throw …` |
| `eval(s)`, `new Function(s)`, `vm.runInContext` [12] | `JSON.parse` + a schema (zod, valibot) |

## Dependencies [13]

- Check the exact name on the registry page: `requests` vs `request`, `@scope/pkg` vs `pkg`.
- From the registry, with a lockfile entry: not `git+https://…`, not a local tarball.
- Prefer a maintained package with recent releases; a fresh release waits out Dependabot's
  cooldown.

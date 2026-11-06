"""`h` — entry point. Output is written for an agent: result line + failures only."""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
from pathlib import Path

from . import (
    __version__,
    affected,
    board,
    cards,
    check,
    ci,
    claims,
    config,
    deadcode,
    deps,
    done,
    drop,
    epics,
    fmt,
    gh,
    git,
    init,
    installed,
    pg,
    policy,
    progress,
    prreview,
    ratchet,
    reuse,
    revert,
    review,
    rustbuild,
    ship,
    skills,
    sqlx,
    stacks,
    task,
    tether,
    tools,
    version,
)


def _rel(cfg: config.Config, p: Path) -> str:
    try:
        return p.resolve().relative_to(cfg.root.resolve()).as_posix() or "."
    except ValueError:
        return p.as_posix()


def cmd_config(args: argparse.Namespace) -> int:
    cfg = config.load(args.dir)
    found = stacks.detect(cfg)
    if args.json:
        print(
            json.dumps(
                {
                    "root": cfg.root.as_posix(),
                    "source": cfg.source.as_posix() if cfg.source else None,
                    "name": cfg.name,
                    "base": cfg.base_branch,
                    "heavy_jobs": cfg.heavy_job_limit(),
                    "generated": cfg.generated,
                    "generators": [
                        {"path": g.path, "command": g.command, "inputs": list(g.inputs)}
                        for g in cfg.generators
                    ],
                    "review_paths": cfg.review_paths,
                    "ci": {
                        "runs-on": cfg.ci_runs_on,
                        "postgres-image": cfg.ci_postgres_image,
                        "setup": cfg.ci_setup,
                        "timeout-minutes": cfg.ci_timeout_minutes,
                        "node-version": cfg.ci_node_version,
                        "upload-logs": cfg.ci_upload_logs,
                    },
                    "stacks": {
                        st.name: {
                            "manifest": _rel(cfg, st.manifest),
                            "packages": {
                                p.name: _rel(cfg, p.path) for p in st.packages
                            },
                            "overrides": cfg.stacks[st.name].commands,
                            # the sqlx-cli CI installs: the project's sqlx
                            **(
                                {"sqlx": sqlx.cli_version(st)}
                                if st.name == "rust"
                                else {}
                            ),
                        }
                        for st in found
                    },
                },
                indent=2,
            )
        )
        return 0
    print(
        f"{cfg.name}  root={cfg.root.as_posix()}  config={'harness.toml' if cfg.source else 'defaults'}"
    )
    for st in found:
        names = [p.name for p in st.packages]
        print(f"  {st.name}: {len(names)} packages  ({', '.join(names)})")
    if not found:
        print("  no stacks detected")
    for glob, skill in cfg.review_paths.items():
        print(f"  review: {glob} → checklist {skill}")
    limit = cfg.ci_timeout_minutes
    custom = limit != config.CI_TIMEOUT_MINUTES
    node = cfg.ci_node_version if cfg.ci_node_version != config.CI_NODE_VERSION else ""
    if (
        cfg.ci_runs_on != ["ubuntu-latest"]
        or cfg.ci_postgres_image
        or cfg.ci_setup
        or custom
        or node
        or not cfg.ci_upload_logs
    ):
        db = f", postgres {cfg.ci_postgres_image}" if cfg.ci_postgres_image else ""
        setup = f", setup {cfg.ci_setup}" if cfg.ci_setup else ""
        time = f", timeout {limit} min" if custom else ""
        node_ = f", node {node}" if node else ""
        logs = "" if cfg.ci_upload_logs else ", step logs not uploaded"
        print(
            f"  ci: runs-on {', '.join(cfg.ci_runs_on)}{db}{setup}{time}{node_}{logs}"
        )
    return 0


def _git(*argv: str) -> str | None:
    try:
        r = git.run(None, *argv)
    except FileNotFoundError:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = config.load(args.dir)
    problems: list[str] = []
    checks = 0

    def need(ok: bool, msg: str) -> None:
        nonlocal checks
        checks += 1
        if not ok:
            problems.append(msg)

    # the one thing every other command refuses on, shown here as a line.
    pinned = version.mismatch(cfg.harness_version)
    need(pinned is None, pinned or "")
    need(_git("--version") is not None, "git not found on PATH")
    if sys.platform == "win32":
        # Worktree paths under .worktrees/<id>/node_modules/... exceed MAX_PATH quickly.
        need(
            _git("config", "core.longpaths") == "true",
            "git core.longpaths is off: run `git config --global core.longpaths true`",
        )
    found = stacks.detect(cfg)
    for w in stacks.warnings(found):
        need(False, w)
    for st in found:
        for tool in st.tools:
            if shutil.which(tool) is None:
                need(False, f"{st.name}: `{tool}` not found on PATH")
            else:
                need(
                    tools.usable(tool),
                    f"{st.name}: `{tool}` is on PATH but `{tool} --version` fails"
                    + (
                        " (rustup: `rustup component add rustfmt`)"
                        if tool == "rustfmt"
                        else ""
                    ),
                )
        sc = cfg.stacks[st.name]
        if "deadcode" not in sc.skip and "deadcode" not in sc.commands:
            gap = deadcode.missing(cfg, st)
            need(gap is None, gap or "")
    stale = skills.drift(cfg)
    need(not stale, "skills out of sync (run `h skills sync`): " + "; ".join(stale))
    # without the required check, a PR opened past `h ship` merges unreviewed.
    # Only for a GitHub origin with gh at hand; the user sets protection (decided).
    origin = _git("-C", str(cfg.root), "remote", "get-url", "origin") or ""
    if cfg.review_required and "github.com" in origin and tools.usable("gh"):
        try:
            required = gh.required_contexts(cfg.root, cfg.base_branch)
            need(prreview.STATUS_CONTEXT in required,
                 f"branch protection of {cfg.base_branch} does not require the check "
                 f"`{prreview.STATUS_CONTEXT}` — add it (Settings → Branches → required "
                 "status checks), or a PR opened past h ship merges without review")  # fmt: skip
        except gh.GhError as e:
            print(f"doctor: branch protection not checked ({e})", file=sys.stderr)
    # gates from an old or unmerged h check the wrong rules without saying so.
    own = installed.check()
    need(not own.stale, own.message())
    if own.state == "unknown":
        print(f"doctor: installed h not checked ({own.detail})", file=sys.stderr)

    # Tips do not fail doctor: the harness works without them, just slower.
    tips: list[str] = []
    # worktrees of killed epic publishes only take disk space.
    try:
        left = epics.leftovers(cfg, clean=args.clean)
    except task.ClaimError:  # not a git repository
        left = []
    dead = [str(p) for p, st in left if st in ("stale", "failed")]
    if dead:
        tips.append(
            f"{len(dead)} worktree(s) left by killed epic publishes"
            + ("" if args.clean else " — `h doctor --clean` removes them")
            + ": "
            + "; ".join(dead)
        )
    removed = [str(p) for p, st in left if st == "removed"]
    if removed:
        print(f"doctor: removed {len(removed)} publish worktree(s)", file=sys.stderr)
    # a stack added after `h init` has a language skill waiting for it.
    have = {st.name for st in found} | ({"sql"} if stacks.has_sql(cfg) else set())
    lang = skills.missing(cfg, have)
    if lang:
        tips.append(f"language skill(s) {', '.join(lang)} for the project's stacks are "
                    "not installed — run `h init`")  # fmt: skip
    for st in found:
        cmds = cfg.stacks[st.name].commands
        if "fmt" in cmds and "fmt_write" not in cmds:
            tips.append(
                f"stacks.{st.name}.commands: `fmt` is the check h check runs; the edit hook "
                "and ship format with the built-in formatter — set `fmt_write` to use yours"
            )
    for st in found:
        if st.name == "rust":
            tips += rustbuild.tips(cfg, st)
    if any(st.name == "rust" for st in found) and not tools.usable("cargo-nextest"):
        # on a pilot project 65 test binaries took 307 s one after another
        tips.append(
            "install cargo-nextest — rust test then runs every test binary in one pool, "
            "not one at a time (`cargo install cargo-nextest --locked` / "
            "`cargo binstall cargo-nextest`); CI installs it"
        )
    if any(st.name == "rust" for st in found) and not tools.usable("sccache"):
        tips.append(
            "install sccache — registry crates then compile once for all worktrees "
            "(`scoop install sccache` / `cargo binstall sccache`)"
        )
    # a sweep removes only its own side's containers; another side's that is gone
    # leaves its own for good — named here, removed by hand.
    if (
        cfg.ci_postgres_image
        and (docker := pg._docker())
        and (orphaned := pg.orphans(docker))
    ):
        tips.append(
            f"{len(orphaned)} throwaway Postgres container(s) over {pg.ORPHAN_H} h old that no "
            "sweep here may remove (another lock dir or OS) — if their runs are gone: "
            f"`docker rm -f -v {' '.join(orphaned)}`"
        )

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not problems,
                    "checks": checks,
                    "problems": problems,
                    "tips": tips,
                    "installed": own.__dict__,
                    "stale_publish": [{"path": str(p), "state": st} for p, st in left],
                }
            )
        )
    else:
        print(f"doctor: {checks - len(problems)}/{checks} ok")
        for p in problems:
            print(f"  FAIL {p}")
        for t in tips:
            print(f"  tip  {t}")
    return 1 if problems else 0


def cmd_fmt(args: argparse.Namespace) -> int:
    files = list(args.files)
    if args.changed:
        # Edits made through a shell never pass the PostToolUse hook; this catches them.
        cfg = config.load(args.dir)
        base = affected.resolve_base(cfg, None)
        files += [cfg.root / f for f in affected.changed_files(cfg, base)]
    if not files:
        raise ValueError("h fmt: give files or --changed")
    results = []
    for f in files:
        if not f.is_file():  # deleted in the branch
            continue
        cfg = config.load(args.dir or f.parent)
        results.append(fmt.format_file(cfg, f))
    failed = [r for r in results if r.status == "failed"]
    if args.json:
        print(json.dumps([r.__dict__ for r in results]))
    else:
        done = sum(r.status == "formatted" for r in results)
        print(
            f"fmt: {done} formatted, {len(results) - done - len(failed)} skipped, {len(failed)} failed"
        )
        for r in failed:
            print(f"  FAIL {r.file}\n{r.detail}")
    return 1 if failed else 0


def _pre_write(payload: dict) -> int:
    path = (payload.get("tool_input") or {}).get("file_path") or ""
    if review.is_verdict_path(path) and payload.get("agent_type") != review.REVIEWER:
        print(
            "h: only the `reviewer` subagent writes the review verdict — "
            "run `h review prepare`, then spawn the reviewer",
            file=sys.stderr,
        )
        return 2
    return 0


WATCH_GH_ERROR = 7  # `h ci watch`: gh failed, CI state unknown


def cmd_ci(args: argparse.Namespace) -> int:
    cfg = config.load(args.dir)
    start = args.dir or Path.cwd()
    if args.action == "reused":
        # check.yml's push path asks it; never fails the job — a "no" checks everything
        if not args.before:
            raise ValueError("ci reused: --before <previous main tip> is required")
        d = reuse.decide(cfg, args.sha or "HEAD", args.before)
        if args.json:
            print(json.dumps(d.as_dict()))
        else:
            print(f"ci reused: {'yes' if d.reuse else 'no'} — {d.reason}")
        return 0
    if args.action == "watch":
        try:
            r = ci.watch(cfg, start)
        except gh.GhError as e:
            # Its own code: through main() a gh failure exited 2 — `blocked`'s.
            if args.json:
                print(json.dumps({"status": "gh-error", "lines": [str(e)], "log": ""}))
                return WATCH_GH_ERROR
            print(f"ci: gh failed, CI state unknown — {e}", file=sys.stderr)
            print("  next: run `h ci watch` again; if it repeats, show the user the error",
                  file=sys.stderr)  # fmt: skip
            return WATCH_GH_ERROR
        lines = list(r.lines)
        if r.status == "green":
            found = task.current_task(start)
            decision = policy.for_worktree(cfg, found[0]) if found else None
            lines.append(merge_line(cfg, decision))
        if args.json:
            print(json.dumps({"status": r.status, "lines": lines, "log": r.log or ""}))
        else:
            for line in lines:
                print(f"ci: {line}" if line is lines[0] else f"  {line}")
        return {"green": 0, "pending": 3, "red": 1, "blocked": 2, "conflict": 4,
                "no-pr": 5, "unpushed": 6}[r.status]  # fmt: skip
    task_id = (
        task.canonical_id(cfg, args.id)
        if args.id
        else (task.current_task(start) or (None, None))[1]
    )
    if not task_id:
        raise ValueError("give a task id or run inside a task worktree")
    if args.action == "reset":
        policy.user_only("h ci reset")
        cleared = ci.reset(cfg, task_id)
        if (n := gh.issue_number(task_id)) is not None:
            try:
                gh.issue_labels(cfg.root, n, remove=["ci-blocked"])
            except gh.GhError:
                pass
        if args.json:
            print(json.dumps({"task": task_id, "cleared": cleared}))
            return 0
        print(
            f"ci {task_id}: " + ("round counter cleared" if cleared else "no counter")
        )
        return 0
    n = ci.rounds(cfg, task_id)
    if args.json:
        print(json.dumps({"task": task_id, "rounds": n, "max": ci.MAX_ROUNDS,
                          "blocked": n >= ci.MAX_ROUNDS}))  # fmt: skip
        return 0
    print(
        f"ci {task_id}: {n} of {ci.MAX_ROUNDS} rounds used"
        + (" — BLOCKED" if n >= ci.MAX_ROUNDS else "")
    )
    return 0


def cmd_deps(args: argparse.Namespace) -> int:
    """the required check for Dependabot PRs, which never pass `h ship`."""
    cfg = config.load(args.dir)
    d = (deps.review if args.action == "review" else deps.approve)(cfg, args.pr)
    if args.json:
        print(json.dumps({"pr": d.number, "sha": d.sha, "kind": d.kind,
                          "security": d.security,
                          "updates": [u.__dict__ for u in d.updates]}))  # fmt: skip
    else:
        names = ", ".join(f"{u.package} {u.old} → {u.new}" for u in d.updates) or "?"
        print(f"deps: PR #{d.number} ({d.kind}: {names}) — {prreview.STATUS_CONTEXT} set "
              f"on {d.sha[:10]}; next: gh pr merge {d.number} --squash")  # fmt: skip
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    cfg = config.load(args.dir)
    start = args.dir or Path.cwd()
    if args.action == "verify-pr":
        if not args.target:
            raise ValueError("h review verify-pr needs a PR number or url")
        ok, line = prreview.verify(cfg, args.target)
        print(json.dumps({"ok": ok, "line": line}) if args.json else f"review: {line}")
        return 0 if ok else 1
    if args.action == "reset":
        policy.user_only("h review reset")
        task_id = (
            task.canonical_id(cfg, args.target)
            if args.target
            else (task.current_task(start) or (None, None))[1]
        )
        if not task_id:
            raise ValueError("give a task id or run inside a task worktree")
        cleared = review.reset(cfg, task_id)
        if args.json:
            print(json.dumps({"task": task_id, "cleared": cleared is not None,
                              "counters": cleared}))  # fmt: skip
        elif cleared is None:
            print(f"review {task_id}: no counter")
        else:
            print(f"review {task_id}: counter cleared — {cleared.get('rejections', cleared.get('rounds', 0))} "
                  f"rejection(s), {cleared.get('rounds', 0)} package(s) before")  # fmt: skip
        return 0
    if args.action == "prepare":
        try:
            pkg, rnd, _results = review.prepare(cfg, start)
        except review.NothingToReview as e:
            print(json.dumps({"nothing_to_review": str(e)}) if args.json
                  else f"review: nothing to review — {e}")  # fmt: skip
            return 0
        found = task.current_task(start)
        wt = found[0] if found else start
        state = review._state(wt)
        delta, model = state.get("delta"), state.get("model", "")
        if args.json:
            print(json.dumps({"package": pkg.as_posix(), "round": rnd, "model": model,
                              "author": state.get("author") or [],
                              "delta": delta or 0, "fix": bool(state.get("fix")),
                              "worktree": wt.as_posix()}))  # fmt: skip
            return 0
        print(f"review: package ready, delta review {delta} of {cfg.review_delta_rounds}"
              if delta else f"review: package ready, round {rnd} of {review.MAX_ROUNDS}"
              f"{', fix diff' if state.get('fix') else ''}")  # fmt: skip
        author = state.get("author") or []
        print(
            f"  model: {model}" + (f" (author: {', '.join(author)})" if author else "")
        )
        print(f"  {pkg.as_posix()}")
        print(f"  next: spawn the `{review.REVIEWER}` subagent with model {model} and: "
              f"worktree {wt.as_posix()}")  # fmt: skip
        return 0
    found = task.current_task(start)
    if found is None:
        raise ValueError("not inside a task worktree")
    v = review.read_verdict(found[0])
    if v is None:
        print(json.dumps({"verdict": None}) if args.json else "review: none yet")
        return 1
    head = review._head(found[0])
    ok = v.verdict == "approve" and v.head == head
    recorded, why = review.record_checked(cfg, *found) if ok else (False, "")
    if args.json:
        print(json.dumps({"verdict": v.verdict, "round": v.round, "head": v.head,
                          "current": v.head == head, "findings": v.findings,
                          "recorded": recorded, "why": why}))  # fmt: skip
        return 0 if ok else 1
    fresh = "current" if v.head == head else f"stale (HEAD {head[:10]})"
    print(f"review: {v.verdict}, round {v.round}, {fresh}")
    if ok:
        print(f"  approve {why}" if recorded else f"  not recorded: {why}")
    for f in v.findings:
        print(
            f"  [{f.get('severity')}] {f.get('file')}:{f.get('line')} — {f.get('text')}"
        )
    return 0 if ok else 1


def _progress_hook(event: str, payload: dict) -> int:
    """PreCompact / SessionStart. Never blocks: exit 2 on PreCompact would stop the
    compaction, so any failure here is swallowed. SessionStart stdout becomes context the
    agent sees — the ledger comes back without the agent having to remember it."""
    try:
        found = task.current_task(Path(payload.get("cwd") or "."))
        if found is None:
            return 0
        wt, task_id = found
        progress.update(config.load(wt), wt, task_id)
        text = progress.read(wt)
    except Exception as e:  # noqa: BLE001 — whatever broke, the session goes on
        print(f"h: progress ledger not updated: {type(e).__name__}: {e}",
              file=sys.stderr)  # fmt: skip
        return 0
    if event == "session-start" and text:
        print(f"h: task {task_id} — progress ledger (.artifacts/progress.md):\n")
        print(text)
    return 0


HOOK_EVENTS = ("post-edit", "pre-write", "pre-compact", "session-start")


def cmd_hook(args: argparse.Namespace) -> int:
    # Claude Code PostToolUse: JSON on stdin. Silent on success; exit 2 sends stderr back
    # to the agent, used only when the formatter rejects the file (usually a syntax error).
    # Claude Code sends UTF-8; text-mode stdin uses the locale (cp1251 on a Russian
    # Windows) and garbles non-ASCII paths, so the file is "not found".
    buf = getattr(sys.stdin, "buffer", None)
    try:
        raw = buf.read().decode("utf-8") if buf is not None else sys.stdin.read()
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = None
    if args.event not in HOOK_EVENTS:
        # A settings.json newer than the installed h must not break the session: an
        # argparse error exits 2, which on PreCompact would block compaction.
        return 0
    if not isinstance(payload, dict):
        if args.event == "pre-write":  # a guard fails closed
            print("h: pre-write hook got an unreadable payload — write refused",
                  file=sys.stderr)  # fmt: skip
            return 2
        return 0
    if args.event == "pre-write":
        return _pre_write(payload)
    if args.event in ("pre-compact", "session-start"):
        return _progress_hook(args.event, payload)
    path = (payload.get("tool_input") or {}).get("file_path") or (
        payload.get("tool_response") or {}
    ).get("filePath")
    if not path:
        return 0
    file = Path(path)
    if not file.is_absolute():
        file = Path(payload.get("cwd") or ".") / file
    if not file.is_file():
        print(f"h fmt: {file} from the hook payload not found — not formatted",
              file=sys.stderr)  # fmt: skip
        return 2
    cfg = config.load(file.parent)
    if skills.is_skill_source(cfg, file):
        # Keep .claude/skills in step with the source the agent just edited.
        skills.sync(cfg)
        return 0
    r = fmt.format_file(cfg, file)
    if r.status == "failed":
        print(f"h fmt: {r.file} rejected by formatter:\n{r.detail}", file=sys.stderr)
        return 2
    try:  # throttled: a network call per edit would cost a second each
        if warn := task.auto_heartbeat(cfg, file.parent, task.HOOK_INTERVAL_S):
            print(f"h: warning — {warn}; work goes on", file=sys.stderr)
    except task.TakenOver as e:
        print(f"h: stop — {e}", file=sys.stderr)
        return 2
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    cfg = config.load(args.dir)
    if args.labels_only and not args.github:
        raise ValueError("--labels-only goes with --github")
    changes = [] if args.labels_only else init.run(cfg, dry_run=args.dry_run)
    if args.github:
        changes += init.github(cfg, dry_run=args.dry_run)
    if args.json:
        # MODIFIED entries are project-edited skills/agents a newer h left alone.
        modified = [c for c in changes if c.startswith("MODIFIED ")]
        print(json.dumps({"dry_run": args.dry_run, "changes": changes,
                          "modified": modified}))  # fmt: skip
        return 0
    print(
        f"init{' (dry run)' if args.dry_run else ''}: "
        f"{sum(not c.startswith('tip:') for c in changes)} changes in {cfg.root.as_posix()}"
    )
    for c in changes:
        print(f"  {c}")
    return 0


def cmd_revert(args: argparse.Namespace) -> int:
    cfg = config.load(args.dir)
    try:
        res = revert.run(cfg, args.sha, args.run or "")
    except revert.RevertError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(f"revert: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"ok": True, "pr": res.pr, "branch": res.branch}))
    else:
        print("\n".join(res.lines))
    return 0


def cmd_affected(args: argparse.Namespace) -> int:
    cfg = config.load(args.dir)
    found = stacks.detect(cfg)
    base = affected.resolve_base(cfg, args.base)
    files = affected.changed_files(cfg, base)
    impact = affected.compute(cfg, found, files)
    if args.json:
        print(
            json.dumps(
                {
                    "base": base,
                    "files": files,
                    "docs_only": affected.docs_only(cfg, files, impact),
                    "stacks": {
                        name: {
                            "all": imp.all,
                            "reason": imp.reason,
                            "changed": sorted(imp.changed),
                            "affected": sorted(imp.affected),
                        }
                        for name, imp in impact.items()
                    },
                },
                indent=2,
            )
        )
        return 0
    print(f"affected: {len(files)} changed files since {base[:10]}")
    if affected.docs_only(cfg, files, impact):
        print("  docs only: CI skips its checks")
    for name, imp in impact.items():
        if imp.all:
            print(f"  {name}: ALL ({imp.reason} changed)")
        elif imp.affected:
            extra = sorted(imp.affected - imp.changed)
            tail = f" + dependents {', '.join(extra)}" if extra else ""
            print(f"  {name}: {', '.join(sorted(imp.changed))}{tail}")
    return 0


def _timeline(results: list[check.StepResult]) -> str:
    """Steps in the order they ran: start from the check's start, duration, wait, result —
    the CI log has no .artifacts to look at."""
    ran = sorted((r for r in results if r.status != "skip"), key=lambda r: r.started)
    lines = ["  timeline:"]
    for r in ran:
        extra = [f"waited {r.waited:.0f}s"] if r.waited >= 1 else []
        if r.phases is not None:
            extra.append(str(r.phases))  # which part of rust test is slow
        more = f" ({'; '.join(extra)})" if extra else ""
        lines.append(f"    +{r.started:.0f}s {r.step.stack} {r.step.kind} "
                     f"{r.seconds:.0f}s {r.status}{more}")  # fmt: skip
    return "\n".join(lines)


def _phase_fields(r: check.StepResult) -> dict[str, float | None]:
    p = r.phases
    if p is None:
        return {"build_s": None, "tests_s": None, "rest_s": None}
    return {
        "build_s": None if p.build is None else round(p.build, 1),
        "tests_s": None if p.tests is None else round(p.tests, 1),
        "rest_s": None if p.rest is None else round(p.rest, 1),
    }


def cmd_check(args: argparse.Namespace) -> int:
    bad = set(args.only) - set(check.FAST + check.SLOW)
    if bad:
        raise ValueError(f"--only: unknown {sorted(bad)}")
    cfg = config.load(args.dir)
    if warn := task.auto_heartbeat(cfg, args.dir or Path.cwd()):
        print(warn, file=sys.stderr)
    found = stacks.detect(cfg)
    for w in stacks.warnings(found):
        print(f"h check: warning: {w}", file=sys.stderr)
    impact, base, files = None, None, None
    if not args.full:
        base = affected.resolve_base(cfg, args.base)
        files = affected.changed_files(cfg, base)
        impact = affected.compute(cfg, found, files)
    # Only this loop selects tests; review prepare, ship and --full run them all.
    steps = check.plan(
        cfg,
        found,
        impact,
        args.only,
        select_tests=not args.full,
        tier=args.tier,
        base=base,
        files=files,
    )
    if args.plan:
        # What would run, nothing run: CI asks it before starting a database.
        planned = [
            {"stack": s.stack, "kind": s.kind, "runs": bool(s.cmd or s.call)}
            for s in steps
        ]
        if args.json:
            print(json.dumps(planned))
        else:
            for p in planned:
                print(f"  {p['stack']} {p['kind']}: {'runs' if p['runs'] else 'skip'}")
        return 0
    limit = cfg.heavy_job_limit()

    def on_wait(step: check.Step, why: str = "") -> None:
        # stderr: the agent should know why nothing is happening, stdout stays parseable.
        what = why or (
            "waits for the whole machine ([check] exclusive: every heavy step here "
            "and in other sessions finishes first)"
            if check.exclusive(cfg, step)
            else f"waits for a heavy-job slot (all {limit} busy on this machine)"
        )
        print(f"h check: {step.stack} {step.kind} {what}", file=sys.stderr, flush=True)

    before = check.snapshot(cfg)
    results = check.run(cfg, steps, on_wait)
    # A custom --base narrows "affected" (`--base HEAD` plans nothing): like --only, it
    # does not prove the branch, so `h progress done` must not trust it.
    subset = set(args.only) != set(check.FAST + check.SLOW)
    check.record(
        cfg,
        before,
        results,
        partial=subset or (args.base is not None and not args.full),
    )
    failed = [r for r in results if r.status == "fail"]
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "stack": r.step.stack,
                        "kind": r.step.kind,
                        "status": r.status,
                        "seconds": round(r.seconds, 1),
                        "waited": round(r.waited, 1),
                        "started": round(r.started, 1),
                        "wall_s": round(check.wall(results), 1),
                        **_phase_fields(r),
                        "reason": r.step.reason,
                        "heavy": r.step.heavy,
                        "blocks_ship": r.blocks_ship,
                        "note": r.note,
                        "warnings": r.warnings,
                        "log": r.log,
                        "tail": r.tail,
                    }
                    for r in results
                ]
            )
        )
        return 1 if failed else 0
    if not results:
        print("check: nothing affected")
        return 0
    ok = sum(r.status == "ok" for r in results)
    disabled = sum(r.status == "skip" and r.step.disabled for r in results)
    heavy = sum(r.status == "skip" and r.step.heavy for r in results)
    skipped = sum(r.status == "skip" for r in results) - disabled - heavy
    in_ci = os.environ.get("CI") == "true"
    total = check.wall(results)  # not the sum: steps run in parallel
    waited = max((r.waited for r in results), default=0.0)
    print(
        f"check: {ok} ok, {len(failed)} failed, {skipped} skipped"
        + (f", {disabled} disabled" if disabled else "")
        + (f", {heavy} heavy after merge" if heavy else "")
        + f" ({total:.1f}s"
        + (f", queued {waited:.1f}s" if waited >= 1 else "")
        + ")"
        + ("" if args.full else " — affected only")
    )
    for r in results:
        if r.status == "skip" and r.step.disabled:
            print(f"  disabled {r.step.stack} {r.step.kind} (harness.toml)")
        elif r.blocks_ship:
            print(f"  skip {r.step.stack} {r.step.kind}: {r.step.reason} — blocks h ship;"
                  f" make it run, or on purpose: {r.disable_line}")  # fmt: skip
        elif r.status == "skip":
            print(f"  skip {r.step.stack} {r.step.kind}: {r.step.reason}")
        elif r.note:
            print(f"  {r.step.stack} {r.step.kind}: {r.note}")
        if r.status == "ok":
            # in CI the timeline row carries it
            if r.phases is not None and not in_ci:
                print(f"  {r.step.stack} {r.step.kind}: {r.phases}")
            for w in r.warnings:
                print(f"  warning {r.step.stack}: {w}")
    if in_ci:  # the CI log is all a reader gets there
        print(_timeline(results))
    for r in failed:
        where = _rel(cfg, r.step.cwd) if r.step.cwd else "."
        paths_note = "" if where == "." else f", paths relative to {where}/"
        split = "" if r.phases is None else f"; {r.phases}"
        print(
            f"  FAIL {r.step.stack} {r.step.kind} ({r.seconds:.1f}s{paths_note}{split})"
            f" — full log: {r.log}"
        )
        print("    " + r.tail.replace("\n", "\n    "))
    return 1 if failed else 0


def cmd_ratchet(args: argparse.Namespace) -> int:
    cfg = config.load(args.dir)
    if args.update:
        counts, grown = ratchet.update(cfg, allow_growth=args.allow_growth)
        if grown:
            print(f"ratchet: NOT UPDATED — {len(grown)} counter(s) would grow:")
            for k in grown:
                print(f"  {k}")
            print(
                "  fix the new suppressions; a deliberate one needs --allow-growth "
                "(the reviewer sees the base change in the PR)"
            )
            return 1
        print(
            f"ratchet: base written — {sum(counts.values())} suppressions "
            f"in {ratchet.BASE.as_posix()}"
        )
        return 0
    res = ratchet.check(cfg, args.base)
    if res is None:
        print(
            f"ratchet: not enabled — no {ratchet.BASE.as_posix()}; `h ratchet --update`"
        )
        return 0
    if args.json:
        print(
            json.dumps(
                {
                    "counts": res.counts,
                    "grown": {k: list(v) for k, v in res.grown.items()},
                    "shrunk": {k: list(v) for k, v in res.shrunk.items()},
                    "new": {
                        k: [
                            {"path": h.path, "line": h.line, "text": h.text} for h in hs
                        ]
                        for k, hs in res.new.items()
                    },
                }
            )
        )
        return 1 if res.grown else 0
    if res.grown:
        print(f"ratchet: FAIL — {len(res.grown)} counter(s) above the base")
        for k, (was, now) in res.grown.items():
            print(f"  {k}: {was} → {now}")
            hits = res.new[k]
            for h in hits[: ratchet.SHOW]:
                print(f"    {h.path}:{h.line}  {h.text[:100]}")
            if len(hits) > ratchet.SHOW:
                print(f"    … {len(hits) - ratchet.SHOW} more")
        print("  fix the cause instead of silencing it; see `h ratchet --help`")
        return 1
    print(f"ratchet: ok — {sum(res.counts.values())} suppressions, none new")
    if res.shrunk:
        print(
            f"  {len(res.shrunk)} counter(s) went down — `h ship` commits the lower "
            "base (or `h ratchet --update` now)"
        )
    return 0


def cmd_tether(args: argparse.Namespace) -> int:
    cfg = config.load(args.dir)
    loose = tether.find(cfg, stacks.detect(cfg))
    if args.json:
        print(json.dumps([vars(x) for x in loose]))
        return 1 if loose else 0
    if not loose:
        print("tether: ok — every test file is run by a check step")
        return 0
    print(f"tether: FAIL — {len(loose)} test file(s) no check step runs")
    for x in loose[: ratchet.SHOW]:
        print(f"  {x.path} — {x.why}")
    if len(loose) > ratchet.SHOW:
        print(f"  … {len(loose) - ratchet.SHOW} more")
    print(
        "  make a step run it (a package, a `test` script); a deliberate exception goes "
        'to harness.toml on the base branch: [check] untethered = { "<glob>" = "<reason>" }'
    )
    return 1


def cmd_ship(args: argparse.Namespace) -> int:
    cfg = config.load(args.dir)
    try:
        r = ship.ship(cfg, args.dir or Path.cwd(), args.title, args.draft)
    except ship.ShipRefused as e:
        print(f"ship refused: {e}")
        for res in e.results:
            if res.status == "fail":
                print(f"  FAIL {res.step.stack} {res.step.kind} — full log: {res.log}")
                print("    " + res.tail.replace("\n", "\n    "))
        return 1
    ok = sum(x.status == "ok" for x in r.results)
    print(f"ship {r.task}: {'PR opened' if r.created else 'PR updated'} — {r.url}")
    print(f"  {ok}/{len(r.results)} check steps ok, evidence in the PR body")
    for n in r.notes:
        print(f"  {n}")
    finish = "done" if gh.issue_number(r.task) is not None else "clean"
    print(f"  {merge_line(cfg, r.policy)}")
    print(f"  then: `h task {finish} {r.task}` from the main checkout once merged")
    return 0


def merge_line(cfg: config.Config, decision: policy.Decision | None = None) -> str:
    """The exact wording the task skill relies on — the skill tells agents to act on it. With a
    path-policy decision it says which rule and file made it so."""
    merge = decision.merge if decision else cfg.ship_merge
    why = f" (policy: {decision.reason})" if decision and decision.reason else ""
    if merge == "agent":
        return f"merge: agent — after green CI{why}"
    return f"merge: user — PR ready, waiting{why}"


def cmd_board(args: argparse.Namespace) -> int:
    cfg = config.load(args.dir)
    url, changes = board.init(cfg)
    print(f"board: {url}")
    for c in changes:
        print(f"  {c}")
    print("  once, by hand (no API for views/workflows):")
    for step in board.MANUAL_STEPS:
        print(f"    - {step}")
    return 0


def cmd_epic(args: argparse.Namespace) -> int:
    cfg = config.load(args.dir)
    if args.action == "new":
        if not args.title:
            raise ValueError("h epic new: --title is required")
        frame = args.frame.read_text(encoding="utf-8") if args.frame else ""
        rel, made_ms, url = epics.new(cfg, args.slug, args.title, frame)
        print(f"epic {args.slug}: milestone {'created' if made_ms else 'exists'}")
        print(
            f"  {rel}: "
            + (f"published via {url} (auto-merge)" if url else "already exists")
        )
        return 0
    if args.action == "sync" and args.publish:
        for slug in [args.slug] if args.slug else sorted(gh.milestones(cfg.root)):
            url = epics.publish_sync(cfg, slug)
            print(f"  {slug}: " + (f"{url} (auto-merge)" if url else "up to date"))
        return 0
    if args.action == "sync":
        slugs = [args.slug] if args.slug else sorted(gh.milestones(cfg.root))
        changed = 0
        for slug in slugs:
            path = cfg.root / epics.EPICS_DIR / f"{slug}.md"
            if not path.is_file():
                print(f"  {slug}: no {path.name} in this checkout — skipped")
                continue
            text = path.read_text(encoding="utf-8")
            new = epics.sync_text(cfg, slug, text)
            if new != text:
                path.write_text(new, encoding="utf-8", newline="\n")
                changed += 1
                print(f"  {slug}: task list updated")
        print(f"epic sync: {changed} file(s) changed — commit them with your task")
        return 0
    for slug, counts in epics.status(cfg):
        total = sum(v for k, v in counts.items() if k != "Dropped")
        parts = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        print(f"{slug}: {counts.get('Done', 0)}/{total} done — {parts or 'no tasks'}")
    return 0


def _print_verdict(task_id: str, v: cards.Verdict) -> None:
    print(f"task {task_id}: {'READY' if v.ready else 'NOT READY'}")
    for p in v.problems:
        print(f"  - {p}")


def _print_progress(wt: Path, task_id: str) -> None:
    """A resumed task shows where it stopped."""
    try:
        progress.update(config.load(wt), wt, task_id)
    except (OSError, ValueError, affected.GitError, review.ReviewError):
        pass
    text = progress.read(wt)
    if text:
        print("  progress (.artifacts/progress.md):")
        print("    " + text.rstrip().replace("\n", "\n    "))


def cmd_progress(args: argparse.Namespace) -> int:
    kinds = [k for k in ("decision", "adr") if getattr(args, k)]
    if kinds and args.action != "note":
        raise ValueError(
            f"--{kinds[0]} is only for note: h progress note --{kinds[0]} TEXT"
        )
    if len(kinds) > 1:
        raise ValueError("give one of --decision, --adr")
    if kinds and args.text:
        # `note foo --decision bar` would keep "bar" and silently drop "foo".
        raise ValueError(f"text both before and after --{kinds[0]}: put it all after "
                         f"the flag — h progress note --{kinds[0]} TEXT")  # fmt: skip
    start = args.dir or Path.cwd()
    found = task.current_task(start)
    if found is None:
        raise ValueError("not inside a task worktree")
    wt, task_id = found
    cfg = config.load(wt)
    try:
        if args.action == "done":
            if not args.text:
                raise ValueError("h progress done T<n>")
            path = progress.done(cfg, wt, task_id, args.text[0])
            print(f"progress: {args.text[0].upper()} marked done on HEAD")
        elif args.action == "note":
            kind, words = (
                ("adr", args.adr) if args.adr
                else ("decision", args.decision) if args.decision
                else ("next", args.text)
            )  # fmt: skip
            path = progress.note(cfg, wt, task_id, " ".join(words), kind)
            print(f"progress: {kind} note added")
        else:
            path = progress.update(cfg, wt, task_id)
    except progress.ProgressError as e:
        print(f"progress: {e}")
        return 1
    items = progress.card_items(wt)
    marked = len(set(progress._load(wt)["done"]) & {i.id for i in items})
    if args.json:
        print(json.dumps({"task": task_id, "path": _rel(cfg, path),
                          "done": marked, "total": len(items)}))  # fmt: skip
        return 0
    if args.action == "show":
        print(progress.read(wt) or "", end="")
        return 0
    print(f"  {_rel(cfg, path)} — {marked}/{len(items)} AC-T done")
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    cfg = config.load(args.dir)
    if args.id and args.action != "new":
        # before any claim, path or gh call; the card is needed only to start
        args.id = task.canonical_id(cfg, args.id, need_card=args.action == "start")
    if args.action == "new":
        if not args.title:
            raise ValueError("h task new: --title is required")
        if args.epic and not epics.exists(cfg, args.epic):
            raise ValueError(f"no epic {args.epic!r}: h epic new {args.epic} --title …")
        sections = (
            cards.parse(args.body.read_text(encoding="utf-8")) if args.body else {}
        )
        body = cards.render(sections)
        verdict = cards.check_ready(body, [])
        labels = [cards.state_label(verdict)]
        size = (sections.get("Size") or "").strip().upper()[:1]
        if size in ("S", "M", "L"):
            labels.append(f"size:{size}")
        number, url = gh.issue_create(cfg.root, args.title, body, labels, args.epic)
        print(f"task gh-{number}: created — {url}")
        _print_verdict(f"gh-{number}", verdict)
        if warn := board.move(cfg, url, board.LABEL_STAGE[labels[0]]):
            print(f"  {warn}")
        return 0

    if args.action == "ready":
        issue_no = gh.issue_number(args.id or "")
        if issue_no is None:
            raise ValueError("h task ready needs an issue id: gh-42")
        issue, verdict = task.card_verdict(cfg.root, issue_no)
        _print_verdict(args.id, verdict)
        # Bring the display labels in line with the card — once in progress, start owns them;
        # a closed issue belongs to done/drop, so a re-check must not pull it back to Ready.
        if issue.get("state", "OPEN").upper() != "OPEN":
            print("  closed: labels and board left as is")
        elif "in-progress" not in issue["labels"]:
            want = cards.state_label(verdict)
            stale = [
                lb for lb in cards.STATE_LABELS if lb != want and lb in issue["labels"]
            ]
            add = [] if want in issue["labels"] else [want]
            if add or stale:
                try:
                    gh.issue_labels(cfg.root, issue_no, add=add, remove=stale)
                    print(
                        f"  labels: {want}"
                        + (f" (removed {', '.join(stale)})" if stale else "")
                    )
                except gh.GhError as e:
                    print(f"  warning: labels not updated ({e})")
            if warn := board.move(cfg, issue["url"], board.LABEL_STAGE[want]):
                print(f"  {warn}")
        return 0 if verdict.ready else 1

    if args.action == "start":
        r = task.start(cfg, args.id, args.slug or "", args.steal, args.resume)
        if args.json:
            print(json.dumps({"task": r.task, "branch": r.branch,
                              "worktree": r.path.as_posix(), "resumed": r.resumed,
                              "stolen_from": r.stolen_from, "ports": r.ports,
                              "setup_failed": r.setup_failed, "notes": r.notes}))  # fmt: skip
            return 1 if r.setup_failed else 0
        verb = "resumed" if r.resumed else "took over" if r.stolen_from else "started"
        print(f"task {r.task}: {verb} on {r.branch}")
        if r.stolen_from:
            print(f"  previous holder: {r.stolen_from}")
        print(f"  worktree: {r.path.as_posix()}")
        if r.ports:
            print("  ports: " + ", ".join(f"{k}={v}" for k, v in r.ports.items()))
        for n in r.notes:
            print(f"  {n}")
        if r.resumed or r.stolen_from:
            _print_progress(r.path, r.task)
        # Slice friction #1: the agent had to work out the next move itself.
        print(f'  next: cd "{r.path.as_posix()}" → work, commit → h ship')
        return 1 if r.setup_failed else 0

    if args.action == "status":
        ids = claims.list_all(cfg)
        print(f"claims on origin: {len(ids)}")
        for tid in ids:
            got = claims.read(cfg, tid)
            if got is None:
                continue
            c = got[1]
            mine = c.owner == claims.owner_key(task.worktree_path(cfg, tid))
            stale = " STALE" if c.stale(cfg.claim_ttl_hours) else ""
            print(
                f"  {tid}: {c.by} on {c.owner.split('|')[0]}, "
                f"heartbeat {c.age_hours():.1f}h ago{stale}{' (this clone)' if mine else ''}"
            )
        return 0

    if args.action == "heartbeat":
        found = task.current_task(Path.cwd())
        if args.id:
            tid, wt = args.id, task.worktree_path(cfg, args.id)
        elif found:
            wt, tid = found
        else:
            raise claims.ClaimError("not inside a task worktree; pass the task id")
        task.heartbeat(cfg, tid, wt)
        print(f"task {tid}: heartbeat sent")
        return 0

    if args.action == "release":
        ref = task.release(cfg, args.id, force=args.force)
        spelled = f" (as {ref})" if ref and ref != args.id else ""
        print(f"task {args.id}: " + (f"claim released{spelled}" if ref
                                     else "nothing to release"))  # fmt: skip
        return 0

    if args.action == "next":
        # Claims an older h took as GH-5 or 5 hold the same issue.
        taken = {gh.issue_number(t) for t in claims.list_all(cfg)}
        free = [i for i in gh.agent_ready(cfg.root) if i["number"] not in taken]
        # The 'ready' label may predate an edit of the criteria: check each card;
        # a card that cannot be checked is skipped, not the whole listing.
        checked = []
        for i in free:
            try:
                if task.card_verdict(cfg.root, i["number"])[1].ready:
                    checked.append(i)
            except gh.GhError as e:
                print(f"next: skipped gh-{i['number']} ({e})", file=sys.stderr)
        free = checked
        if args.json:
            print(json.dumps([{"id": f"gh-{i['number']}", "title": i["title"],
                               "url": i["url"]} for i in free]))  # fmt: skip
            return 0 if free else 1  # same exit code as the text mode
        if not free:
            print(
                "next: nothing for an agent — no open issue with ready + agent-ok and no claim"
            )
            return 1
        print(f"next: {len(free)} task(s) an agent may take, lowest number first")
        for i in free:
            print(f"  gh-{i['number']}  {i['title']}")
        print(f"  take one: h task start gh-{free[0]['number']}")
        return 0

    if args.action == "confirm":
        policy.user_only("confirming a card")
        issue_no = gh.issue_number(args.id or "")
        if issue_no is None:
            raise ValueError("h task confirm needs an issue id: gh-42")
        issue = gh.issue_view(cfg.root, issue_no)
        if cards.AC_CONFIRMED in issue["labels"]:
            # A fresh label event: a confirmation covers the card as it is now.
            gh.issue_labels(cfg.root, issue_no, remove=[cards.AC_CONFIRMED])
        gh.issue_labels(cfg.root, issue_no, add=[cards.AC_CONFIRMED])
        print(
            f"task {args.id}: confirmed ({cards.AC_CONFIRMED}); next: h task ready {args.id}"
        )
        return 0

    if args.action == "drop":
        if not args.id:
            raise ValueError("h task drop needs a task id")
        rep = drop.drop(cfg, args.id, args.reason or "", args.discard)
        print(f"task {rep.task}: DROPPED" + (" (leftovers)" if rep.manual else ""))
        for line in rep.lines:
            print(f"  {line}")
        for m in rep.manual:
            print(f"  manual: {m}")
        return 1 if rep.manual else 0

    if args.action == "done":
        if not args.id:
            raise ValueError("h task done needs a task id")
        rep = done.done(cfg, args.id)
        print(
            f"task {rep.task}: {'CLOSED' if rep.closed else 'NOT CLOSED'} ({rep.stage})"
        )
        for line in rep.lines:
            print(f"  {line}")
        if rep.manual:
            print("  manual, then run `h task done` again:")
            for m in rep.manual:
                print(f"    - {m}")
        return 0 if rep.closed else 1

    items = task.clean(cfg, only=args.id, dry_run=args.dry_run)
    if args.json:
        rows = [
            {"task": i.task, "path": i.path.as_posix(), "branch": i.branch,
             "state": i.state, "action": i.action, "manual": i.manual}
            for i in items
        ]  # fmt: skip
        print(json.dumps(rows, indent=1))
        return 1 if any(i.manual for i in items) else 0
    removed = sum(i.action in ("removed", "would remove") for i in items)
    print(
        f"clean{' (dry run)' if args.dry_run else ''}: {removed} of {len(items)} task worktrees"
    )
    for i in items:
        print(f"  {i.task}: {i.state}, {i.action}")
        if i.manual:
            print(f"    manual: {i.manual}")
    # Leftovers needing a human make the exit code non-zero: a closing gate must not pass.
    return 1 if any(i.manual for i in items) else 0


def cmd_skills(args: argparse.Namespace) -> int:
    cfg = config.load(args.dir)
    if args.check:
        stale = skills.drift(cfg) + skills.template_drift(cfg)
        print(f"skills: {'in sync' if not stale else f'{len(stale)} out of sync'}")
        for s in stale:
            print(f"  {s}")
        return 1 if stale else 0
    changes, conflicts = skills.sync(cfg)
    print(f"skills: {len(changes)} changes, {len(conflicts)} conflicts")
    for c in changes:
        print(f"  {c}")
    for c in conflicts:
        print(f"  CONFLICT {c}")
    return 1 if conflicts else 0


def _version_gate(args: argparse.Namespace) -> int:
    """a project written for a newer h is not run by an older one."""
    required = config.load(args.dir).harness_version
    problem = version.mismatch(required)
    if problem is None:
        return 0
    if getattr(args, "json", False):
        print(json.dumps({"error": "version", "required": required.strip(),
                          "installed": __version__, "message": problem}))  # fmt: skip
    else:
        print(f"h: {problem}", file=sys.stderr)
        print("  `h doctor` and `h config` still run", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    # A Windows console defaults to cp1252/cp866: one non-ASCII char in a path or message
    # would crash the command instead of printing it.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):  # not when captured by pytest
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        if args.cmd not in version.UNCHECKED and (refused := _version_gate(args)):
            return refused
        return args.fn(args)
    except (
        config.ConfigError,
        FileNotFoundError,
        ValueError,
        affected.GitError,
        claims.ClaimError,
    ) as e:
        print(f"h: {e}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="h", description=__doc__)
    parser.add_argument("--version", action="version", version=f"h {__version__}")
    parser.add_argument(
        "-C", dest="dir", type=Path, default=None, help="project directory"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn, help_ in (
        ("config", cmd_config, "show resolved config and detected packages"),
        ("doctor", cmd_doctor, "check that required tools are installed"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--json", action="store_true")
        p.set_defaults(fn=fn)
        if name == "doctor":
            p.add_argument(
                "--clean",
                action="store_true",
                help="remove worktrees left by killed epic publishes",
            )

    p = sub.add_parser("fmt", help="format files with their stack's formatter")
    p.add_argument("files", nargs="*", type=Path)
    p.add_argument(
        "--changed",
        action="store_true",
        help="every file changed since the base branch",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_fmt)

    p = sub.add_parser("check", help="fmt/lint/typecheck/test for affected packages")
    p.add_argument(
        "--full", action="store_true", help="every package, not just affected"
    )
    p.add_argument("--base", help="base ref (default: origin/<base>, then <base>)")
    p.add_argument(
        "--only",
        type=lambda s: tuple(k for k in s.split(",") if k),
        default=check.FAST + check.SLOW,
        help="comma-separated subset of fmt,lint,deadcode,deps,typecheck,test,sql,generated",
    )
    p.add_argument(
        "--tier",
        choices=check.TIERS,
        default="pr",
        help="pr: [check] heavy steps wait for the merge (default); full: run them too; "
        "heavy: only them (main reused the PR's check)",
    )
    p.add_argument(
        "--plan", action="store_true", help="list the steps that would run; run nothing"
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser(
        "ratchet", help="suppression and test-skip counts may only go down"
    )
    p.add_argument(
        "--update", action="store_true", help="write the current counts as the base"
    )
    p.add_argument(
        "--allow-growth",
        action="store_true",
        help="update: also when a count grows (deliberate; shows in the PR diff)",
    )
    p.add_argument("--base", help="base ref for new lines (default: origin/<base>)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_ratchet)

    p = sub.add_parser("tether", help="every test file is run by some check step")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_tether)

    p = sub.add_parser(
        "affected", help="packages touched by changes since the base branch"
    )
    p.add_argument("--base", help="base ref (default: origin/<base>, then <base>)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_affected)

    p = sub.add_parser(
        "task", help="claim a task, manage its worktree, clean up after merge"
    )
    p.add_argument(
        "action",
        choices=[
            "new",
            "ready",
            "start",
            "status",
            "heartbeat",
            "release",
            "clean",
            "done",
            "drop",
            "next",
            "confirm",
        ],
    )
    p.add_argument(
        "--json", action="store_true", help="start, next, clean: machine-readable"
    )
    p.add_argument("id", nargs="?", help="task id, e.g. HARNESS-12 or gh-42")
    p.add_argument("--title", help="new: issue title")
    p.add_argument("--reason", help="drop: why the task is not done (required)")
    p.add_argument(
        "--discard", action="store_true", help="drop: throw away unsaved changes"
    )
    p.add_argument("--epic", help="new: epic slug (milestone)")
    p.add_argument(
        "--body", type=Path, help="new: markdown file with the card's ### sections"
    )
    p.add_argument("--slug", help="start: short branch suffix, task/<id>-<slug>")
    p.add_argument(
        "--steal", action="store_true", help="start: take over a stale claim"
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="start: continue a task this clone holds (e.g. after a crashed session)",
    )
    p.add_argument(
        "--force", action="store_true", help="release: drop someone else's claim"
    )
    p.add_argument("--dry-run", action="store_true", help="clean: report only")
    p.set_defaults(fn=cmd_task)

    p = sub.add_parser("ship", help="check, push and open/update the PR of this task")
    p.add_argument("--title", help="PR title (default: first commit subject)")
    p.add_argument("--draft", action="store_true")
    p.set_defaults(fn=cmd_ship)

    p = sub.add_parser("ci", help="watch the PR's CI; triage failures; 2-round limit")
    p.add_argument("action", choices=["watch", "status", "reset", "reused"])
    p.add_argument(
        "id", nargs="?", help="status/reset: task id (default: this worktree)"
    )
    p.add_argument("--before", help="reused: main's previous tip (the push's `before`)")
    p.add_argument("--sha", help="reused: the main commit (default HEAD)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_ci)

    p = sub.add_parser("deps", help="Dependabot PR: set the required review check")
    p.add_argument(
        "action",
        choices=["review", "approve"],
        help="review: the agent, patch/minor past 1.0 only; approve: the user, any",
    )
    p.add_argument("pr", metavar="PR", help="PR number or url")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_deps)

    p = sub.add_parser("review", help="review gate: package for the reviewer subagent")
    p.add_argument("action", choices=["prepare", "status", "verify-pr", "reset"])
    p.add_argument(
        "target",
        nargs="?",
        metavar="PR|ID",
        help="verify-pr: PR number or url; reset: task id (default: this worktree)",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_review)

    p = sub.add_parser("skills", help="mirror .agents/skills into .claude/skills")
    p.add_argument("action", choices=["sync"])
    p.add_argument(
        "--check", action="store_true", help="report drift, change nothing (CI)"
    )
    p.set_defaults(fn=cmd_skills)

    p = sub.add_parser(
        "progress",
        help="task ledger that survives compaction: update, done, note, show",
    )
    p.add_argument("action", nargs="?", default="update",
                   choices=["update", "done", "note", "show"])  # fmt: skip
    p.add_argument("text", nargs="*", help="done: T<n>; note: the next step")
    # The kinds take their own text: a store_true flag between `note` and the text makes
    # argparse bind the empty `text` before the flag and reject the words after it.
    p.add_argument(
        "--decision", nargs="+", metavar="TEXT", help="note: a decision taken"
    )
    p.add_argument(
        "--adr",
        nargs="+",
        metavar="TEXT",
        help="note: a decision with a trade-off (ADR?)",
    )
    p.add_argument("--json", action="store_true", help="machine-readable result")
    p.set_defaults(fn=cmd_progress)

    p = sub.add_parser(
        "hook", help="entry points for agent hooks (reads JSON on stdin)"
    )
    p.add_argument("event", help=", ".join(HOOK_EVENTS))
    p.set_defaults(fn=cmd_hook)

    p = sub.add_parser(
        "revert",
        help="PR reverting a merged commit whose post-merge run is red",
    )
    p.add_argument("sha", help="the merged commit on the base branch")
    p.add_argument("--run", help="URL of the failed run (default: found on the commit)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_revert)

    p = sub.add_parser(
        "init",
        help="install AGENTS.md, CLAUDE.md, .claude/settings.json and the CI workflow",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--github", action="store_true", help="also create the card labels on GitHub"
    )
    p.add_argument(
        "--labels-only",
        action="store_true",
        help="with --github: only the labels, nothing in the working tree",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("board", help="the repo's project board (GitHub Projects v2)")
    p.add_argument("action", choices=["init"])
    p.set_defaults(fn=cmd_board)

    p = sub.add_parser("epic", help="epics: a milestone + docs/epics/<slug>.md")
    p.add_argument("action", choices=["new", "sync", "status"])
    p.add_argument("slug", nargs="?")
    p.add_argument("--title", help="new: epic title")
    p.add_argument("--frame", type=Path, help="new: markdown with Why/Scope/Done when")
    p.add_argument(
        "--publish",
        action="store_true",
        help="sync: land the change via its auto-merged PR",
    )
    p.set_defaults(fn=cmd_epic)
    return parser


if __name__ == "__main__":
    sys.exit(main())

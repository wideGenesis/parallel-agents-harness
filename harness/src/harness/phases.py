"""Where the time of `rust test` went: building the test binaries, running them, the rest.
One number for the step cannot tell a slow build from slow tests, and the summary needs
that. Read from what the commands already print; nothing found → no split, no warning."""

from __future__ import annotations

import re
from dataclasses import dataclass

# cargo: "Finished `test` profile [unoptimized + debuginfo] target(s) in 5m 12s" (older
# cargo: "Finished test [...]"); its elapsed format is "0.55s" below a minute, else "75m 03s".
FINISHED = re.compile(
    r"^\s*Finished\b.*\btarget\(s\) in (?:(\d+)m )?(\d+(?:\.\d+)?)s\s*$"
)
# nextest: "Summary [ 312.456s] 2464 tests run: 2464 passed, 0 skipped"
SUMMARY = re.compile(r"^\s*Summary \[\s*(\d+(?:\.\d+)?)s\]")
# cargo test, one binary after another: "test result: ok. 3 passed; …; finished in 0.12s"
RESULT = re.compile(r"^\s*test result: .*\bfinished in (\d+(?:\.\d+)?)s\s*$")


@dataclass(frozen=True)
class Phases:
    build: float | None
    tests: float | None
    rest: float | None  # the step minus the two: nextest listing binaries, doctests

    def __str__(self) -> str:
        parts = (("build", self.build), ("tests", self.tests), ("rest", self.rest))
        return ", ".join(f"{name} {s:.0f}s" for name, s in parts if s is not None)


def rust_test(output: str, seconds: float) -> Phases | None:
    """The split of a `rust test` step from the output of its first command (nextest or
    cargo test; the doctests that follow land in the rest), ANSI stripped."""
    build = tests = None
    results: list[float] = []
    for line in output.splitlines():
        if build is None and (m := FINISHED.match(line)):
            build = int(m[1] or 0) * 60 + float(m[2])
        elif tests is None and (m := SUMMARY.match(line)):
            tests = float(m[1])
        elif m := RESULT.match(line):
            results.append(float(m[1]))
    if tests is None and results:
        tests = sum(results)
    if build is None and tests is None:
        return None
    rest = None
    if build is not None and tests is not None:
        rest = max(0.0, seconds - build - tests)
    return Phases(build, tests, rest)

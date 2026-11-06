"""a tool that crashes shows its error message and exit code in the h check summary,
not only the end of its stack (knip on a pilot project: the tail was `at async build …`)."""

import pytest

from harness import check, deadcode

NODE_CRASH = (
    ["$ /x/node_modules/.bin/knip --reporter json --no-progress",
     "file:///x/knip/dist/WorkspaceWorker.js:428",
     "        const config = plugin.config[0];",
     "                                    ^",
     "",
     "TypeError: Cannot read properties of undefined (reading '0')"]
    + [f"    at async step{i} (file:///x/knip/dist/graph/build.js:{100 + i}:35)"
       for i in range(20)]
    + ["", "Node.js v24.21.0"]
)  # fmt: skip


@pytest.mark.parametrize("line", [
    "TypeError: Cannot read properties of undefined (reading '0')",
    "Error: Cannot find module 'left-pad'",
    "Uncaught SyntaxError: Unexpected token '}'",
    "  RangeError: Maximum call stack size exceeded",
    "FatalProcessOutOfMemoryException: heap limit",
])  # fmt: skip
def test_a_js_error_line_is_a_failure_line(line):
    assert check.FAILURE.search(line)


@pytest.mark.parametrize("line", [
    "test http::error::tests::maps_not_found ... ok",
    "test queue::a_failed_rebuild_waits ... ok",
    "fn error_is_mapped() {",
    "    TypeErrorKind::Missing => 1,",
    "Errors: none",
])  # fmt: skip
def test_names_that_mention_errors_stay_unmatched(line):
    assert not (check.FAILURE.search(line) and not check.PASS_LINE.search(line))


def test_a_node_crash_summary_shows_the_message():
    tail = check.focus_tail([ln for ln in NODE_CRASH if ln.strip()])
    assert "TypeError: Cannot read properties of undefined (reading '0')" in tail
    assert len(tail) <= check.TAIL


@pytest.mark.parametrize(("code", "said"), [
    (2, "exit 2"), (137, "exit 137"), (-9, "killed by SIGKILL"), (-200, "killed by signal 200"),
])  # fmt: skip
def test_how_a_tool_ended(code, said):
    assert deadcode.exited(code) == said

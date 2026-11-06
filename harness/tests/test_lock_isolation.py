import os
import subprocess
import sys
from pathlib import Path

from harness import lock

HARNESS = Path(__file__).resolve().parents[1]
# A test that runs a heavy step through check.run, so it takes a machine slot.
HEAVY_TEST = "tests/test_check.py::test_dotenv_feeds_steps_and_process_env_wins"


def test_tests_do_not_wait_for_slots_held_by_outer_check(tmp_path, monkeypatch):
    # `h check` holds a machine slot while it runs these tests; with every slot
    # held by outer runs, a test that took a real slot waited forever.
    machine = tmp_path / "machine-locks"
    monkeypatch.setenv("HARNESS_LOCK_DIR", str(machine))
    limit = max(1, (os.cpu_count() or 4) // 6)
    held = []
    for _ in range(limit):
        got = lock.try_acquire(limit)
        assert got is not None
        held.append(got)
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             f"--basetemp={tmp_path / 'inner'}", HEAVY_TEST],
            cwd=HARNESS,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )  # fmt: skip
    finally:
        for got in held:
            lock.release(got[1])
    assert r.returncode == 0, r.stdout + r.stderr

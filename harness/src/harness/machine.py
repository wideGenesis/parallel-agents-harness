"""The machine's budget for heavy check steps, shared by every project and session.

`~/.harness/machine.toml`:

    max_cores = 20     # never more than this many cores for heavy steps at once
    max_load = 0.8     # a heavy step does not start while CPU load is above this

Every heavy step takes the same share of cores, so "the shares never exceed max_cores" is a
number of slots in the machine-wide queue (lock.py); one core stays for the throwaway
Postgres of a run. The share goes to the tools as env: cargo, Rust tests, pytest's
workers, and H_CORES for what h cannot pass a flag to (a project's vitest/Playwright config).
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

POSTGRES_CORES = 1  # held back for the run's throwaway Postgres
SHARES = 3  # heavy steps at once by default
SMALL = 4  # threads up to which the machine keeps every core and no Postgres reserve


@dataclass(frozen=True)
class Budget:
    max_cores: int
    max_load: float
    slots: int  # heavy steps at once
    share: int  # cores each

    def env(self, cores: int | None = None) -> dict[str, str]:
        """What a heavy step's tools read to stay within `cores` (its share by default)."""
        n = str(cores or self.share)
        return {
            "CARGO_BUILD_JOBS": n,
            "RUST_TEST_THREADS": n,
            "NEXTEST_TEST_THREADS": n,
            "PYTEST_XDIST_AUTO_NUM_WORKERS": n,  # `-n auto` takes this many workers
            "H_CORES": n,
        }


class BudgetError(ValueError):
    """A bad machine file: `h` reports it like a bad harness.toml, without a traceback."""


def path() -> Path:
    if override := os.environ.get("HARNESS_MACHINE"):
        return Path(override)
    return Path.home() / ".harness" / "machine.toml"


def threads() -> int:
    return os.cpu_count() or 4


def budget(heavy_jobs: int = 0) -> Budget:
    """The machine file's limits, or the defaults: min(20, threads − 2) cores, 0.8 load.
    `heavy_jobs` (harness.toml, the older key) still sets the number of steps at once."""
    data: dict = {}
    f = path()
    if f.is_file():
        try:
            data = tomllib.loads(f.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise BudgetError(f"{f}: {e}") from e
    n = threads()
    # A small machine (a 2-vCPU CI runner) keeps all of it: two spare threads would be most.
    cores = data.get("max_cores", min(20, n - 2) if n > SMALL else n)
    load = data.get("max_load", 0.8)
    if not isinstance(cores, int) or isinstance(cores, bool) or cores < 1:
        raise BudgetError(f"{f}: max_cores must be a whole number of cores, at least 1")
    if (
        not isinstance(load, (int, float))
        or isinstance(load, bool)
        or not 0 < load <= 1
    ):
        raise BudgetError(f"{f}: max_load must be a fraction in (0, 1], e.g. 0.8")
    usable = max(1, cores - (POSTGRES_CORES if cores > SMALL else 0))
    # At least 2 cores a share where there are that many: cargo -j1 crawls.
    slots = heavy_jobs or min(SHARES, max(1, usable // 2))
    share = max(1, usable // slots)
    return Budget(cores, float(load), max(1, min(slots, usable // share)), share)


def _proc_stat() -> tuple[int, int]:
    """(idle, total) jiffies from /proc/stat."""
    fields = Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]
    nums = [int(x) for x in fields]
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    return idle, sum(nums)


def _windows_times() -> tuple[int, int]:
    """(idle, total) 100 ns units from GetSystemTimes; kernel time includes idle."""

    class FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    idle, kernel, user = FileTime(), FileTime(), FileTime()
    if sys.platform != "win32":  # windll exists on Windows only; the checker knows that
        raise OSError("GetSystemTimes is Windows only")
    kernel32 = ctypes.windll.kernel32
    if not kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    ):
        raise OSError("GetSystemTimes failed")

    def v(t: FileTime) -> int:
        return (t.high << 32) | t.low

    return v(idle), v(kernel) + v(user)


def load(sample: float = 0.5) -> float | None:
    """Machine CPU load in [0, 1] over `sample` seconds; None when it cannot be read."""
    try:
        if sys.platform == "win32":
            read = _windows_times
        elif Path("/proc/stat").is_file():
            read = _proc_stat
        else:
            return min(1.0, os.getloadavg()[0] / threads())
        i0, t0 = read()
        time.sleep(sample)
        i1, t1 = read()
    except (OSError, AttributeError, ValueError, IndexError):
        return None
    total = t1 - t0
    return None if total <= 0 else max(0.0, min(1.0, 1 - (i1 - i0) / total))

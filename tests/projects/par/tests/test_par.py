"""A test that passes only when no other process is running it at the same time."""

import contextlib
import os
import tempfile
import time

from par import m

#: Named at collection, so every process forked after it shares the one directory.
RUNNING = os.path.join(tempfile.gettempdir(), f"par-running-{os.getpid()}")


def _others() -> list[str]:
    """Return the pids, other than this process's, marked as running the test."""
    return [name for name in os.listdir(RUNNING) if name != str(os.getpid())]


def test_alone():
    """Call every function, and fail when another process runs this test meanwhile.

    The test marks its process in `RUNNING`, looks for other marks before and after a
    sleep, and removes its mark. Every copy keeps its mark through the whole sleep,
    so each of two overlapping copies sees the other, and serial copies see nothing.
    """
    for function in (m.f1, m.f2, m.f3, m.f4):
        function()
    os.makedirs(RUNNING, exist_ok=True)
    mine = os.path.join(RUNNING, str(os.getpid()))
    with open(mine, "w"):
        pass
    try:
        seen = set(_others())
        time.sleep(1.0)
        seen.update(_others())
    finally:
        os.remove(mine)
        with contextlib.suppress(OSError):
            os.rmdir(RUNNING)
    assert not seen

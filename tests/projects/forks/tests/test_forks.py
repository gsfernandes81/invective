"""Tests that each run one of `forks.m`'s functions in another process."""

import multiprocessing
import os
import subprocess
import sys

from forks import m

#: The directory that holds the `forks` package, for a fresh interpreter's path.
SRC = os.path.dirname(os.path.dirname(m.__file__))


def test_in_process():
    """`in_process` answers True in the test's own process."""
    assert m.in_process() is True


def test_pool():
    """A fork-context pool's workers double 1 and 2."""
    with multiprocessing.get_context("fork").Pool(2) as pool:
        assert pool.map(m.in_pool, [1, 2]) == [2, 4]


def test_os_fork():
    """A forked child writes `in_fork()` to a pipe, and the test reads 5 from it."""
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:
        # The child must never return into pytest, whatever `in_fork` does.
        try:
            os.close(read)
            os.write(write, str(m.in_fork()).encode())
        finally:
            os._exit(0)
    os.close(write)
    with os.fdopen(read, "rb") as fh:
        data = fh.read()
    os.waitpid(pid, 0)
    assert data == b"5"


def test_subprocess():
    """A fresh interpreter imports `forks.m` and prints `in_subprocess()`, 9."""
    done = subprocess.run(
        [sys.executable, "-c", "from forks import m; print(m.in_subprocess())"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONPATH": SRC},
    )
    assert done.stdout == "9\n"

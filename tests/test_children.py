"""The scheduler reaps the children it started and no other, and none outlives a parent.

A parent sent SIGTERM or SIGHUP takes its running children with it.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import signal
import subprocess
import sys
import time

import pytest

from invective import children


# The xdist worker this suite runs on has execnet's threads; the child forked here
# only sleeps.
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
def test_a_child_the_scheduler_did_not_start_keeps_its_exit_status(
    tmp_path: pathlib.Path,
) -> None:
    """A process the parent started elsewhere stays waitable, with its exit status.

    Such a process is a conftest's server, say. Reaping with `waitpid(-1)` would
    take it and leave its owner a status of 0.
    """
    other = subprocess.Popen(
        [sys.executable, "-c", "print('up'); raise SystemExit(7)"], stdout=subprocess.PIPE
    )
    assert other.stdout is not None
    assert other.stdout.read() == b"up\n"
    done = []
    job = children.Job("job", lambda: time.sleep(0.5), str(tmp_path / "out"), None)
    children.run_all([job], 1, done.append)
    assert [d.key for d in done] == ["job"]
    assert other.wait(timeout=10) == 7


# A parent that runs one child that sleeps, and writes the child's pid to its own
# stdout, which the child's redirection has not touched.
SLEEPER = """
import os, sys, time
from invective import children
stdout = os.dup(1)
def sleep():
    os.write(stdout, b"%d\\n" % os.getpid())
    time.sleep(30)
children.run_all([children.Job("sleep", sleep, sys.argv[1], None)], 1, lambda done: None)
"""


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_parent_told_to_stop_takes_its_children_with_it(
    tmp_path: pathlib.Path, signum: int
) -> None:
    """The parent dies of the signal, and the child it was running is gone by then.

    Dying of the signal is what the parent would do with no scheduler running.
    """
    parent = subprocess.Popen(
        [sys.executable, "-c", SLEEPER, str(tmp_path / "out")], stdout=subprocess.PIPE
    )
    assert parent.stdout is not None
    child = int(parent.stdout.readline())
    try:
        parent.send_signal(signum)
        assert parent.wait(timeout=10) == -signum
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"the child {child} outlived its parent")
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(child, signal.SIGKILL)


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
def test_a_child_has_none_of_the_schedulers_handlers_or_its_pipe(
    tmp_path: pathlib.Path,
) -> None:
    """A child starts with the default handlers and none of the scheduler's descriptors.

    A Python handler in the child would interrupt a C extension's blocking call with
    EINTR, and its copy of the stopping handler would kill the child's siblings.
    """
    out = tmp_path / "out"
    before = set(os.listdir("/proc/self/fd"))

    def look() -> None:
        """Write, in the child, what it found of the scheduler's set-up."""
        found = (
            signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL,
            all(signal.getsignal(each) == signal.SIG_DFL for each in children.STOPPING),
            sorted(set(os.listdir("/proc/self/fd")) - before),
        )
        os.write(1, b"%r %r %r" % found)

    children.run_all([children.Job("look", look, str(out), None)], 1, lambda _done: None)
    assert out.read_text() == "True True []"

"""The scheduler: it reaps the children it started and no other, and none
outlives a parent told to stop."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from invective import children


# The xdist worker this suite runs on has execnet's threads; the child
# forked here only sleeps.
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
def test_a_child_the_scheduler_did_not_start_keeps_its_exit_status(tmp_path):
    """A process the parent started elsewhere (a conftest's server, say)
    stays waitable, with its status, after the scheduler has reaped its
    own; reaping with `waitpid(-1)` would take it and leave `0`."""
    other = subprocess.Popen([sys.executable, "-c", "print('up'); raise SystemExit(7)"],
                             stdout=subprocess.PIPE)
    assert other.stdout.read() == b"up\n"
    done = []
    job = children.Job("job", lambda: time.sleep(0.5), str(tmp_path / "out"), None)
    children.run_all([job], 1, done.append)
    assert [d.key for d in done] == ["job"]
    assert other.wait(timeout=10) == 7


# A parent that runs one child that sleeps, and writes the child's pid to
# its own stdout, which the child's redirection has not touched.
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
def test_a_parent_told_to_stop_takes_its_children_with_it(tmp_path, signum):
    """The parent dies of the signal, as it would with no scheduler running,
    and the child it was running is gone by then."""
    parent = subprocess.Popen([sys.executable, "-c", SLEEPER, str(tmp_path / "out")],
                              stdout=subprocess.PIPE)
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
            raise AssertionError("the child %d outlived its parent" % child)
    finally:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
def test_a_child_has_none_of_the_schedulers_handlers_or_its_pipe(tmp_path):
    """A Python handler in the child would interrupt a C extension's blocking
    call with EINTR, and its copy of the stopping handler would kill the
    child's siblings."""
    out = tmp_path / "out"
    before = set(os.listdir("/proc/self/fd"))

    def look():
        os.write(1, b"%r %r %r" % (
            signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL,
            all(signal.getsignal(each) == signal.SIG_DFL for each in children.STOPPING),
            sorted(set(os.listdir("/proc/self/fd")) - before)))

    children.run_all([children.Job("look", look, str(out), None)], 1, lambda done: None)
    assert out.read_text() == "True True []"

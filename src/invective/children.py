"""Forking a child from the collected parent, and running many children at once.

A child is a function run after `os.fork` in a process group of its own, with
`/dev/null` for stdin and one file for its stdout and stderr, and it ends in
`os._exit`. Nothing a child does reaches the parent but the files it writes. The
scheduler keeps up to N children alive, wakes on each exit through a pipe that
Python's signal wakeup writes to on `SIGCHLD`, and kills a child's whole process
group when its wall budget runs out. A parent sent SIGTERM or SIGHUP while the
scheduler runs kills every running child's group before it stops.

The code a child runs here, from the fork to `os._exit`, is marked `@switch.harness`,
so that a self-run gives it no guard: the canary raising in it would leave the child
running the parent's scheduler.
"""

from __future__ import annotations

import contextlib
import errno
import os
import select
import signal
import sys
import time
import traceback
from collections.abc import Callable
from types import FrameType
from typing import NamedTuple

from invective import switch

#: A signal handler set from Python, or `SIG_DFL` or `SIG_IGN`.
type _Handler = Callable[[int, FrameType | None], object] | int


class Job[K](NamedTuple):
    """A function to run in a child, under a key the caller gets back with its end."""

    key: K
    run: Callable[[], None]
    out: str  #: the file the child's stdout and stderr go to
    budget: float | None  #: seconds of wall time, or None for no limit


class Done[K](NamedTuple):
    """How the child of a job ended."""

    key: K  #: the job's key
    status: int  #: as `os.waitpid` gives it
    timed_out: bool  #: the scheduler killed the child at the end of its budget
    seconds: float  #: wall seconds from the fork to the reaping


#: A running child's pid to its job, its start, its deadline, and whether the
#: scheduler has killed it for passing the deadline.
type _Running[K] = dict[int, tuple[Job[K], float, float | None, bool]]

#: The signals that stop the parent, whose children go with it.
STOPPING = (signal.SIGTERM, signal.SIGHUP)

#: The exit status of a child whose function raised (`EX_SOFTWARE`).
_RAISED = 70


@switch.harness
def _flush_stdio() -> None:
    """Flush `sys.stdout` and `sys.stderr`, ignoring a stream that cannot flush."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()


@switch.harness
def fork[K](job: Job[K], undo: Callable[[], None] = lambda: None) -> int:
    """Start *job* in a child and return its pid; the call does not return in the child.

    *undo*, called first in the child, takes back what the scheduler set up in the
    parent: its signal handlers and its pipe. The child exits 0 when the job's
    function returns, and `_RAISED`, with the traceback in its output file, when it
    raises.
    """
    # Output buffered in the parent would otherwise be written again by each child.
    _flush_stdio()
    pid = os.fork()
    if pid:
        # Both sides set the group, so a kill sent before the child's own call
        # still reaches it.
        with contextlib.suppress(OSError):
            os.setpgid(pid, pid)
        return pid

    code = _RAISED
    try:
        undo()
        with contextlib.suppress(OSError):
            os.setpgid(0, 0)
        fd = os.open(job.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        os.close(fd)
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.close(devnull)
        job.run()
        code = 0
    except BaseException:
        with contextlib.suppress(Exception):
            traceback.print_exc()
    finally:
        _flush_stdio()
        os._exit(code)


def kill_group(pid: int) -> None:
    """Send SIGKILL to the process group *pid* and to the process *pid*.

    Either may be gone already, which is not an error.
    """
    with contextlib.suppress(OSError):
        os.killpg(pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


def run_one[K](job: Job[K]) -> Done[K]:
    """Run *job* in a child, wait for it to end, and return how it ended."""
    got: list[Done[K]] = []
    run_all([job], 1, got.append)
    return got[0]


def run_all[K](jobs: list[Job[K]], workers: int, done: Callable[[Done[K]], None]) -> None:
    """Run every job, *workers* at a time, calling *done* as each one's child ends.

    A child over its budget is killed with its process group, and its `Done` has
    `timed_out` set. SIGTERM or SIGHUP to the parent meanwhile kills and reaps every
    running child, then stops the parent as the handler it had before would. A
    signal the parent ignored stays ignored.
    """
    pending = list(jobs)
    running: _Running[K] = {}
    read_end, write_end = os.pipe()
    os.set_blocking(read_end, False)
    os.set_blocking(write_end, False)
    before_fd = signal.set_wakeup_fd(write_end, warn_on_full_buffer=False)
    before_chld = signal.signal(signal.SIGCHLD, lambda *_args: None)
    found: dict[int, _Handler | None] = {
        signum: signal.getsignal(signum) for signum in STOPPING
    }
    # A handler of None was not set from Python and cannot be put back; the
    # default is the nearest.
    before: dict[int, _Handler] = {
        signum: signal.SIG_DFL if handler is None else handler
        for signum, handler in found.items()
        if handler != signal.SIG_IGN
    }

    def stop(signum: int, _frame: object) -> None:
        """Kill and reap every running child, then take the signal as before."""
        for pid in list(running):
            kill_group(pid)
        for pid in list(running):
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)
        # The signal is sent again under the handler the parent had, rather than
        # raised as SystemExit here: with the default handler the parent dies of the
        # signal, the status a supervisor expects, and pytest's session does not go
        # on to report an interrupted run as an internal error. A handler a plugin
        # set runs as it would have.
        signal.signal(signum, before[signum])
        os.kill(os.getpid(), signum)

    @switch.harness
    def undo() -> None:
        """In a child: restore the parent's handlers and close the scheduler's pipe."""
        signal.set_wakeup_fd(-1)
        # A Python handler left in a child interrupts its blocking calls with
        # EINTR, which a C extension may not retry.
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        for signum, handler in before.items():
            signal.signal(signum, handler)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, STOPPING)
        os.close(read_end)
        os.close(write_end)

    for signum in before:
        signal.signal(signum, stop)
    try:
        while pending or running:
            _start(pending, running, workers, undo)
            _expire(running)
            _reap(running, done)
            if not running:
                continue

            deadlines = [
                d for (_j, _s, d, k) in running.values() if d is not None and not k
            ]
            timeout = max(0.0, min(deadlines) - time.monotonic()) if deadlines else None
            select.select([read_end], [], [], timeout)
            with contextlib.suppress(BlockingIOError):
                os.read(read_end, 4096)
    finally:
        for pid in running:
            kill_group(pid)
        for pid in list(running):
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)
        for signum, handler in before.items():
            signal.signal(signum, handler)
        signal.signal(signal.SIGCHLD, before_chld)
        signal.set_wakeup_fd(before_fd)
        os.close(read_end)
        os.close(write_end)


def _start[K](
    pending: list[Job[K]], running: _Running[K], workers: int, undo: Callable[[], None]
) -> None:
    """Fork pending jobs, first in first out, until *workers* children are running.

    Each child runs *undo* first, and is in *running* before SIGTERM or SIGHUP can
    reach the scheduler's handler.
    """
    while pending and len(running) < workers:
        job = pending.pop(0)
        started = time.monotonic()
        deadline = started + job.budget if job.budget is not None else None
        # Blocked until the child is in `running`, so `stop` finds it.
        signal.pthread_sigmask(signal.SIG_BLOCK, STOPPING)
        try:
            running[fork(job, undo)] = (job, started, deadline, False)
        finally:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, STOPPING)


def _expire[K](running: _Running[K]) -> None:
    """Kill the group of every running child past its deadline, and mark it killed."""
    now = time.monotonic()
    for pid, (job, started, deadline, killed) in list(running.items()):
        if deadline is not None and now >= deadline and not killed:
            kill_group(pid)
            running[pid] = (job, started, deadline, True)


def _reap[K](running: _Running[K], done: Callable[[Done[K]], None]) -> None:
    """Take every child in *running* that has ended, and pass each to *done*.

    Each is waited for by its own pid: a `waitpid(-1)` would also take a child of
    the parent that the scheduler did not start, such as a process a conftest
    started, and leave its owner without its exit status.
    """
    for pid in list(running):
        try:
            ended, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            ended, status = pid, 0
        if ended == 0:
            continue
        job, started, _deadline, killed = running.pop(pid)
        # Anything the child started and left in its group goes with it.
        kill_group(pid)
        done(Done(job.key, status, killed, time.monotonic() - started))


def describe_status(status: int) -> str:
    """Return how a process ended, from its `os.waitpid` *status*, as words.

    The words fit after "the child": `killed by SIGKILL`, `exited 5`.
    """
    if os.WIFSIGNALED(status):
        number = os.WTERMSIG(status)
        try:
            name = signal.Signals(number).name
        except ValueError:
            name = str(number)
        return f"killed by {name}"
    if os.WIFEXITED(status):
        return f"exited {os.WEXITSTATUS(status)}"
    return f"ended with status {status}"


def tail(path: str, lines: int = 20) -> str:
    """Return the last *lines* lines of the file *path*, or "" if it does not exist."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:])
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return ""
        raise

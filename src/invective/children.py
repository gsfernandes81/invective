"""Forking a child from the collected parent, and running many at once.

A child is a function run after `os.fork` in its own process group, with
`/dev/null` for stdin and one file for its stdout and stderr, that ends in
`os._exit`. Nothing a child does reaches the parent but the files it writes.
The scheduler keeps up to N children alive, wakes on each exit through a
pipe `SIGCHLD` writes to, and kills a child's whole process group when its
wall budget runs out. A parent sent SIGTERM or SIGHUP while the scheduler
runs kills every running child's group before it stops.
"""

from __future__ import annotations

import errno
import os
import select
import signal
import sys
import time
import traceback
from collections.abc import Callable
from typing import NamedTuple


class Job(NamedTuple):
    key: object
    run: Callable[[], None]
    out: str                 #: the file the child's stdout and stderr go to
    budget: float | None     #: seconds of wall time, or None for no limit


class Done(NamedTuple):
    key: object
    status: int              #: as `os.waitpid` gives it
    timed_out: bool
    seconds: float


#: The signals that stop the parent, whose children go with it.
STOPPING = (signal.SIGTERM, signal.SIGHUP)


def fork(job: Job, undo: Callable[[], None] = lambda: None) -> int:
    """Start *job* in a child and return its pid; never returns in the child.

    *undo*, called first in the child, takes back what the scheduler set up
    in the parent: its signal handlers and its pipe.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    pid = os.fork()
    if pid:
        # Both sides set the group, so that a kill that comes before the
        # child's own call still finds it.
        try:
            os.setpgid(pid, pid)
        except OSError:
            pass
        return pid
    code = 70
    try:
        undo()
        try:
            os.setpgid(0, 0)
        except OSError:
            pass
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
        try:
            traceback.print_exc()
        except Exception:
            pass
    finally:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        os._exit(code)


def kill_group(pid: int) -> None:
    for send in (lambda: os.killpg(pid, signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
        try:
            send()
        except OSError:
            pass


def run_all(jobs: list[Job], workers: int, done: Callable[[Done], None]) -> None:
    """Run every job, *workers* at a time, calling *done* as each ends.

    SIGTERM or SIGHUP to the parent meanwhile kills and reaps every running
    child, then stops the parent as the handler it had before would. A
    signal the parent ignored stays ignored.
    """
    pending = list(jobs)
    running: dict[int, tuple[Job, float, float | None, bool]] = {}
    read_end, write_end = os.pipe()
    os.set_blocking(read_end, False)
    os.set_blocking(write_end, False)
    before_fd = signal.set_wakeup_fd(write_end, warn_on_full_buffer=False)
    before_chld = signal.signal(signal.SIGCHLD, lambda *_args: None)
    before = {signum: signal.getsignal(signum) for signum in STOPPING}
    # None is a handler not set from Python, which cannot be put back; the
    # default is the nearest.
    before = {signum: signal.SIG_DFL if handler is None else handler
              for signum, handler in before.items() if handler != signal.SIG_IGN}

    def stop(signum, _frame):
        for pid in list(running):
            kill_group(pid)
        for pid in list(running):
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        # The signal is sent again under the handler the parent had, rather
        # than raising SystemExit here: with the default handler the parent
        # dies of the signal, the status a supervisor expects, and pytest's
        # session does not go on to report an interrupted run as an internal
        # error. A handler a plugin set runs as it would have.
        signal.signal(signum, before[signum])
        os.kill(os.getpid(), signum)

    def undo():
        signal.set_wakeup_fd(-1)
        # A Python handler left in a child interrupts its blocking calls
        # with EINTR, which a C extension may not retry.
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
            while pending and len(running) < workers:
                job = pending.pop(0)
                started = time.monotonic()
                deadline = started + job.budget if job.budget is not None else None
                # Held until the child is in `running`, so that `stop` finds it.
                signal.pthread_sigmask(signal.SIG_BLOCK, STOPPING)
                try:
                    running[fork(job, undo)] = (job, started, deadline, False)
                finally:
                    signal.pthread_sigmask(signal.SIG_UNBLOCK, STOPPING)
            now = time.monotonic()
            for pid, (job, started, deadline, killed) in list(running.items()):
                if deadline is not None and now >= deadline and not killed:
                    kill_group(pid)
                    running[pid] = (job, started, deadline, True)
            _reap(running, done)
            if not running:
                continue
            deadlines = [d for (_j, _s, d, k) in running.values() if d is not None and not k]
            timeout = max(0.0, min(deadlines) - time.monotonic()) if deadlines else None
            select.select([read_end], [], [], timeout)
            try:
                os.read(read_end, 4096)
            except BlockingIOError:
                pass
    finally:
        for pid in running:
            kill_group(pid)
        for pid in list(running):
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        for signum, handler in before.items():
            signal.signal(signum, handler)
        signal.signal(signal.SIGCHLD, before_chld)
        signal.set_wakeup_fd(before_fd)
        os.close(read_end)
        os.close(write_end)


def _reap(running, done) -> None:
    """Every child of the scheduler's that has ended is taken and reported.

    Each is waited for by its own pid: a `waitpid(-1)` would take any child
    of the parent, a process a conftest started among them, and leave its
    owner without a status.
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
    if os.WIFSIGNALED(status):
        number = os.WTERMSIG(status)
        try:
            name = signal.Signals(number).name
        except ValueError:
            name = str(number)
        return "killed by %s" % name
    if os.WIFEXITED(status):
        return "exited %d" % os.WEXITSTATUS(status)
    return "ended with status %d" % status


def tail(path: str, lines: int = 20) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:])
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return ""
        raise

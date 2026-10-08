"""The processes invective starts: their groups, how they are stopped, and
SIGTERM.

A run of the tests and a git command are each started in a process group of
their own (`OWN_GROUP`) and stopped with everything they started (`stop`). A
SIGTERM to invective unwinds as a ^C does (`stopping_on_sigterm`), so the
copy is removed, and the process then ends by the signal (`exit_by`).
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from typing import NoReturn


#: Each run is started in a process group of its own, so that stopping it
#: stops everything it started. A test suite can start processes of its own
#: (this one starts pytest), and killing the run alone would leave those
#: running, and theirs, for as long as they like.
if os.name == "nt":
    OWN_GROUP = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
else:
    OWN_GROUP = {"start_new_session": True}


#: How long a stopped run's output is waited for once its group is killed.
STOP_GRACE = 10.0


def stop(proc: subprocess.Popen) -> None:
    """Kill *proc* and every process it started, and collect what it said."""
    if os.name == "nt":
        # invective: accept[untestable: True -> False] Windows only, and the sweep runs on Linux
        quietly = {"capture_output": True}
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], **quietly)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.communicate(timeout=STOP_GRACE)
    except subprocess.TimeoutExpired:
        # Something the run started left its group and still holds the
        # output open: waiting for the end of it would be waiting for ever.
        proc.kill()
        proc.stdout.close()
        proc.stderr.close()
        proc.wait()


class Terminated(KeyboardInterrupt):
    """A SIGTERM, raised where it landed. A `KeyboardInterrupt`, so that
    every path that unwinds on a ^C -- the live run stopped with its group,
    the copy removed, pytest's own session ended as interrupted -- unwinds on
    it unchanged. *signum* is the signal, for the exit by it at the end."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


@contextlib.contextmanager
def stopping_on_sigterm():
    """A SIGTERM during the body raises `Terminated` in it, once.

    Without a handler the process dies where it stands and no `finally`
    runs, so a copy of the project stays in the temporary directory. With
    one that raises, the signal unwinds exactly as a ^C does: it interrupts
    the `communicate` that waits on a run (the syscall is retried only when
    the handler returns), the run's group is stopped on the way out, and
    the copy is removed. **Once**: a second SIGTERM while the copy is being
    removed would raise inside that removal and cut it short, so later ones
    are ignored until the previous handler is back. SIGKILL remains the way
    to stop a cleanup that hangs, and the copy's marker covers what that
    leaves. A SIGTERM ignored on entry stays ignored, and no handler is
    installed. Windows never delivers SIGTERM (`TerminateProcess` ends a
    process outright), so there the handler is installed and never runs.
    """
    # The shell's `trap '' TERM` asked for that; the marker covers the copy
    # of a run then killed outright.
    if signal.getsignal(signal.SIGTERM) is signal.SIG_IGN:
        yield
        return
    fired = False

    def handler(signum, frame):
        nonlocal fired
        if fired:
            return
        fired = True
        raise Terminated(signum)

    try:
        previous = signal.signal(signal.SIGTERM, handler)
    except ValueError:
        # Not the main thread, the only one a handler can be set from: the
        # signal keeps its default, and the marker is what covers the copy.
        yield
        return
    try:
        yield
    finally:
        # `None` is a handler Python did not install (an embedding host's, a
        # C extension's), which it cannot put back: `signal.signal` refuses
        # it, and the error would replace whatever is unwinding.
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def exit_by(exc: Terminated) -> NoReturn:
    """End this process by the signal *exc* carries, as it would have ended
    without a handler. A shell, `timeout(1)` or CI's cancel then sees the
    status it expects of a process it terminated (143 in a shell), and not an
    exit code that reads as a verdict; as a pid namespace's init, which the
    signal cannot end, it exits with 128 + the signal's number. What was
    printed is written out first: the signal ends the process without
    flushing a buffer, and a run's output sent to a file or a pipe is
    buffered."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            # A standard stream closed when the process started: nothing
            # was written to it.
            continue
        try:
            stream.flush()
        except (OSError, ValueError):
            # A closed or broken stream must not replace the exit.
            pass
    signal.signal(exc.signum, signal.SIG_DFL)
    os.kill(os.getpid(), exc.signum)
    # Reached only where the kernel ignores a signal a process sends itself
    # at its default action: a pid namespace's init, such as a container's
    # entry point. 128 + N is the status a shell reports for a process the
    # signal ended, and CPython's own exit on an uncaught KeyboardInterrupt
    # falls back to it the same way.
    raise SystemExit(128 + exc.signum)

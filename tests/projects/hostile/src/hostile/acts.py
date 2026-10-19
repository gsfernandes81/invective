"""Flags a test reads, each `True` as written, whose one mutant, `False`, is hostile.

Each test does one hostile thing to the process it runs in when its flag is `False`,
and then fails, if it is still running. The two that end the process are last in
their file, so that plain pytest runs every other test first.
"""

from __future__ import annotations


def quiet() -> bool:
    """`False` is killed: the test prints 64 MB first."""
    return True


def keeps_stdout() -> bool:
    """`False` is killed: the test closes `sys.stdout` first."""
    return True


def keeps_descriptors() -> bool:
    """`False` is killed: the test closes file descriptors 1 and 2 first."""
    return True


def returns() -> bool:
    """`False` is killed: the test raises `SystemExit(0)`."""
    return True


def lives() -> bool:
    """Unmeasured: `False` ends the child with `os._exit(0)` before it writes a verdict.

    Plain pytest exits 0 there, with no report for that test or any after it.
    """
    return True


def uninterrupted() -> bool:
    """Unmeasured: `False` raises `KeyboardInterrupt`, which ends the child unanswered.

    Plain pytest stops the session there and exits 2, with no report for the test.
    """
    return True

"""The tests of `acts`, each hostile to its process when its flag is `False`."""

import os
import sys

from hostile import acts


def test_large_output():
    """Prints 64 MB when the flag is `False`."""
    if not acts.quiet():
        print("x" * (64 * 1024 * 1024))
    assert acts.quiet()


def test_closed_stdout():
    """Closes `sys.stdout` when the flag is `False`."""
    if not acts.keeps_stdout():
        sys.stdout.close()
    assert acts.keeps_stdout()


def test_closed_descriptors():
    """Closes file descriptors 1 and 2 when the flag is `False`."""
    if not acts.keeps_descriptors():
        os.close(1)
        os.close(2)
    assert acts.keeps_descriptors()


def test_system_exit():
    """Raises `SystemExit(0)` when the flag is `False`."""
    if not acts.returns():
        raise SystemExit(0)


def test_hard_exit():
    """Ends the process with `os._exit(0)` when the flag is `False`."""
    if not acts.lives():
        os._exit(0)


def test_keyboard_interrupt():
    """Raises `KeyboardInterrupt` when the flag is `False`."""
    if not acts.uninterrupted():
        raise KeyboardInterrupt

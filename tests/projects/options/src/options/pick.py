"""Functions whose tests the project's `addopts` stop early on or leave out.

Under `-x` a run stops at its first failure, which changes no verdict: one failing
test is a kill. A test `-k` or `--deselect` leaves out runs in no arm, so a function
only it calls is unreached.
"""

from __future__ import annotations


def first(items: list[int]) -> int:
    """Killed by every value: two tests fail, and `-x` stops the run at the first."""
    return items[0]


def unselected() -> bool:
    """Unreached: only the test `-k 'not unselected'` leaves out calls it."""
    return True


def deselected() -> bool:
    """Unreached: only the test `--deselect` names calls it."""
    return True

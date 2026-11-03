"""Functions behind a strict `xfail` and behind a fixture that skips.

A strict `xfail` test that passes fails the run, so a mutant that makes it pass is
killed. A skip is not a failure, so a mutant that only makes a test skip survives.
"""

from __future__ import annotations


def is_weekend(day: str) -> bool:
    """Killed by both values; `True` only by the strict `xfail` test, which it passes."""
    return day in {"sat", "sun"}


def in_stock(count: int) -> bool:
    """Both values survive: `False` makes the fixture skip the one test that reads it."""
    return count > 0

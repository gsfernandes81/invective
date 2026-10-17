"""A second module, whose mutant ordinals overlap `gate`'s: each ordinal
is live in its own module only, and the verdicts here show it."""

from __future__ import annotations


def twice(n: int) -> int:
    """Killed by every value: the test checks `twice(2)`."""
    return n * 2


def sign(n: int) -> bool:
    """`True` survives: the one test that reaches it checks `sign(1)` only.
    Its ordinal is `count -> 1`'s in `gate`, which the same test would
    notice, so a switch that set every module at once would kill it."""
    return n > 0

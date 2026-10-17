"""One-line functions, whose `def` line is also their body's line.

The module runs that line at import, but the functions run only in a test, so every
mutant is measured and killed.
"""

from __future__ import annotations


def one() -> int: """Killed by every value."""; return 3  # fmt: skip


class Box:
    """A class with a one-line method."""

    def get(self) -> int: """Killed by every value."""; return 4  # fmt: skip

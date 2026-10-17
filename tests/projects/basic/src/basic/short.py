"""One-line functions, whose `def` line is also their body's line: the
module runs that line at import, but the functions run only in a test,
so every mutant is measured and killed."""

from __future__ import annotations


def one() -> int: return 3


class Box:
    def get(self) -> int: return 4

"""One-line functions, whose `def` line is also their body's line.

Only the test imports the module, inside its body. The functions it calls are
measured and killed. `never`, and the `inner` that `wrapped` defines, are never
called, so they are unreached; but the test runs their `def` lines, at the import
and in `wrapped`, which the engine takes for lines of their bodies, and reports them
survived, a disagreement `expected.toml` lists.
"""

from __future__ import annotations


def one() -> int: """Killed by every value."""; return 3  # fmt: skip


class Box:
    """A class with a one-line method."""

    def get(self) -> int: """Killed by every value."""; return 4  # fmt: skip


def never() -> bool: """Unreached: no test calls it."""; return True  # fmt: skip


def wrapped() -> str:
    """Killed by both values; the one-line function it defines is never called."""

    def inner() -> bool: """Unreached: nothing calls it."""; return True  # fmt: skip

    return "wrapped"

"""Functions whose mutants are known by hand; the suite asserts each verdict."""

from __future__ import annotations

from collections.abc import Iterator

#: The first open hour, and the first closed hour after it.
OPENS = 9
CLOSES = 17


def is_open(hour: int) -> bool:
    """Killed by both values: tests check an open hour and closed ones."""
    return OPENS <= hour < CLOSES


def count(items: list[str]) -> int:
    """`0` and `-1` survive: the one test asserts only that the count is not 1."""
    return len(items)


def label(name: str) -> str:
    """Unreached: no test calls it."""
    return "<" + name + ">"


def evens(limit: int) -> Iterator[int]:
    """`iter([])` and `iter([0])` survive; the other yielded values are killed.

    The test checks the first value only, with a default of 0 for an empty iterator.
    """
    for n in range(limit):
        if n % 2 == 0:
            yield n


class Counter:
    """A count that `bump` raises by one."""

    def __init__(self) -> None:
        """Start the count at 0; a constructor has no mutant."""
        self.n = 0

    def bump(self) -> None:
        """Replaced by `return None`: killed, since the test reads `n`."""
        self.n += 1

    def value(self) -> int:
        """Every value survives.

        The test bumps once and asserts `value() < 2`, which 0, 1 and -1 all satisfy.
        """
        return self.n


def through_fixture(seed: int) -> int:
    """Killed through a fixture: only a session fixture calls it.

    The tests use the fixture's result, so its mutants are covered through the
    fixture.
    """
    return seed * 2


def helper() -> str:
    """Unmeasured: the module calls it at import, and the engine has no slow path yet."""
    return "x"


GREETING = helper()

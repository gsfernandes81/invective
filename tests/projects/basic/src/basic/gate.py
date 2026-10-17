"""Functions whose mutants are known by hand; the suite asserts each verdict."""

from __future__ import annotations

from collections.abc import Iterator


def is_open(hour: int) -> bool:
    """Killed by both values: tests check an open hour and a closed one."""
    return 9 <= hour < 17


def count(items: list[str]) -> int:
    """`0` and `-1` survive: the one test asserts only that the count is
    not 1."""
    return len(items)


def label(name: str) -> str:
    """Unreached: no test calls it."""
    return "<" + name + ">"


def evens(limit: int) -> Iterator[int]:
    """`iter([])` and `iter([0])` survive: the test checks the first value
    only, with a default of 0 for an empty iterator; the other yielded
    values are killed."""
    for n in range(limit):
        if n % 2 == 0:
            yield n


class Counter:
    def __init__(self) -> None:
        self.n = 0

    def bump(self) -> None:
        """Replaced by `return None`: killed, since the test reads `n`."""
        self.n += 1

    def value(self) -> int:
        """Every value survives: the test bumps once and asserts
        `value() < 2`, which 0, 1 and -1 all satisfy."""
        return self.n


def through_fixture(seed: int) -> int:
    """Only a session fixture calls it; the tests use the fixture's result,
    so its mutants are covered through the fixture and killed."""
    return seed * 2


def helper() -> str:
    """Called at import below: unmeasured until the slow path exists."""
    return "x"


GREETING = helper()

"""Functions only the set-up and tear-down of a test class call.

A class-scoped fixture, and a `unittest` class's `setUpClass` and `tearDownClass`,
run once for the class: in the set-up of its first test and the tear-down of its
last.
"""

from __future__ import annotations


def stock(shelf: list[str]) -> None:
    """Killed: a class-scoped fixture stocks the shelf, and both tests read it."""
    shelf.append("bread")


def sold_out(shelf: list[str]) -> bool:
    """`True` survives; `False` is killed by the fixture's tear-down, which asserts it."""
    return not shelf


def fill(jars: list[str]) -> None:
    """Killed: `setUpClass` fills the jars, and both tests of the class read them."""
    jars.append("jam")


def eaten(jars: list[str]) -> bool:
    """`True` survives; `False` is killed by `tearDownClass`, which asserts it."""
    return not jars

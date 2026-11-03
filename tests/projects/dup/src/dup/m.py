"""Functions that share a qualname, so their mutants share an id.

The `singledispatch` overloads are both named `_`, and the property's getter and
setter are both `Temp.c`. Each docstring gives the verdict plain pytest gives.
"""

from __future__ import annotations

import functools


@functools.singledispatch
def show(_x: object) -> str:
    """Unreached: the one call dispatches to the `int` overload, not here."""
    return "obj"


@show.register
def _(_x: int) -> str:
    """Killed by both values: the test checks `show(3)`."""
    return "int"


@show.register
def _(_x: float) -> str:
    """Unreached: no test passes a float."""
    return "float"


class Temp:
    """A value behind a property with a getter and a setter."""

    def __init__(self) -> None:
        """Start at 0; a constructor has no mutant."""
        self._c = 0

    @property
    def c(self) -> int:
        """Unreached: the test reads `_c` directly, never through the getter."""
        return self._c

    # The setter returns an int so that its mutants' ids are the getter's.
    @c.setter  # ty: ignore[invalid-argument-type]
    def c(self, v: int) -> int:
        """Killed by every value: each replacement skips the store the test reads."""
        self._c = v
        return 0

"""Functions reached through the other shapes Python gives a function.

A decorator applied at import, a dataclass's `default_factory`, a static method, a
class method, a property, and a factory whose closure is a lambda.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Self

#: The name of every function `registered` was applied to, in the module's order.
REGISTRY: list[str] = []


def registered[F: Callable[..., object]](function: F) -> F:
    """Unmeasured: it runs at import, before any switch, to register `default_tags`.

    With it replaced by `None`, `default_tags` is None and making a tile raises.
    """
    REGISTRY.append(getattr(function, "__name__", "?"))
    return function


@registered
def default_tags() -> list[str]:
    """Killed by every value: a new tile's tags are `["new"]`, and the test reads them."""
    return ["new"]


@dataclass
class Tile:
    """A square tile, whose tags `default_tags` makes."""

    side: int
    tags: list[str] = field(default_factory=default_tags)

    @staticmethod
    def fits(side: int) -> bool:
        """Killed by both values: the test checks a fitting side and one too long."""
        return 0 < side <= 10

    @classmethod
    def unit(cls) -> Self:
        """Killed: the test reads the side of the unit tile."""
        return cls(1)

    @property
    def is_unit(self) -> bool:
        """Killed by both values: the test reads it on the unit tile and a larger one."""
        return self.side == 1


def scaler(factor: int) -> Callable[[int], int]:
    """Killed: the test calls the lambda it returns."""
    return lambda n: n * factor

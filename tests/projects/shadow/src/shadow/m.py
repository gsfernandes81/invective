"""Functions with a local named like a builtin that a replacement value calls.

The engine puts each replacement at the top of the body, where the local is
assigned further down, so a replacement that calls the builtin by that name raises
`UnboundLocalError` and is killed. Written in place of the body, as plain pytest
runs it, the replacement calls the builtin and the weak tests pass.
"""

import builtins
from collections.abc import Iterator


def chunks(data: list[int]) -> Iterator[list[int]]:
    """Every value survives in plain pytest; the engine kills every one.

    The test asserts only that each chunk is a list, which holds for an empty
    iterator and for each one-chunk iterator. Every replacement calls `iter`, which
    the engine's guard reads before the local `iter` is assigned.
    """
    iter = 2  # a local named like the builtin the replacement calls
    return (data[i : i + iter] for i in range(0, len(data), iter))


def initials(names: list[str]) -> set[str]:
    """Every value survives in plain pytest; the engine kills `set()` only.

    The test asserts only that the result is a set. `set()` calls the builtin, which
    the engine's guard reads before the local `set` is assigned; the other values
    are set literals, which call nothing.
    """
    set = {name[:1] for name in names}  # a local named like the builtin
    set.discard("")
    return set


def distinct(names: list[str]) -> frozenset[str]:
    """Every value survives in plain pytest; the engine kills every one.

    The test asserts only that the result is a frozenset. Every replacement calls
    `frozenset`, which the engine's guard reads before the local is assigned.
    """
    frozenset = builtins.frozenset  # a local named like the builtin
    return frozenset(names)

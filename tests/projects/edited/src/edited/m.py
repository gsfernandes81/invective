"""A target a baseline-only test edits, prepending a function, as a save mid-run would.

No test module imports it at collection, so each of the engine's children compiles
the file from disk after the baseline's edit. The prepended function's mutant takes
ordinal 1 and every other mutant's ordinal rises by one, so the child sent ordinal N
runs the mutant the parent's table numbers N - 1. Plain pytest runs each mutant as
the table first compiled it.
"""


def total(xs: list[int]) -> int:
    """Killed by every value: `test_total` checks `total([1, 2]) == 3`.

    The engine reports `0` survived: its child runs the prepended function's mutant,
    which no test calls, and `test_total` passes. `1` and `-1` are killed, by the
    mutants `0` and `1` the children run in their place.
    """
    return sum(xs)


def greeting() -> str:
    """Killed by both values: `test_greeting` checks `greeting() == "hi"`.

    The engine reports `''` survived: its child runs `total`'s `-1`, which
    `test_greeting`, the only covering test, does not call. `'xyzzy'` is killed by
    `''` run in its place.
    """
    return "hi"

"""The target of the gccycle project."""


def size(items: list[int]) -> int:
    """Killed by every value: `test_cycle_is_freed` checks `size([1, 2]) == 2`.

    The engine refuses the run as red before any mutant: the parent freezes the
    garbage collector before it forks, so the reference cycle the test module made
    at collection is never collected in the baseline child, and the test fails.
    """
    return len(items)

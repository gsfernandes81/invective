"""A function whose docstring example is the only test that runs it."""


def double(n: int) -> int:
    """Return twice *n*; every value is killed by this example, run as a doctest.

    >>> double(2)
    4

    The engine refuses the run before any mutant: the canary raised in the example
    fails the doctest item with the doctest's own failure, so the canary child's
    first failure is not the canary's.
    """
    return n * 2

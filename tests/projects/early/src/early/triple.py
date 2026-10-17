"""A function three tests call, each of which kills every mutant."""


def triple(n: int) -> int:
    """Every value is killed by each of the three tests that call it.

    A child that stops at the first failure runs one of them only.
    """
    return 3 * n

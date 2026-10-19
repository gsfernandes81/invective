"""Functions that fixtures and class set-ups call, most of them at teardown.

The engine gives each the verdict plain pytest gives; each docstring says which.
"""


def closed() -> int:
    """Killed by every value: a module fixture asserts it is 4 after its yield."""
    return 4


def cleared() -> int:
    """Killed by every value: a class-scoped fixture asserts it is 2 after its yield."""
    return 2


def opened() -> bool:
    """`False` is killed: `setUpClass` stores it, and a test asserts it."""
    return True


def released() -> int:
    """Killed by every value: `tearDownClass` asserts it is 3."""
    return 3
